"""Watch AXS ticket prices for one event and email when they move.

Self-contained and unrelated to the Corvette scraper in the repository root:
it has its own requirements file, its own state file, and its own workflow.

Only the public event page is read -- the same document a browser loads before
signing in.  Nothing here logs in, holds a cart, defeats a CAPTCHA, or buys a
ticket; it is a read-only price watcher that makes one request per run.  AXS
sits behind an anti-bot layer that rejects a default HTTP client's TLS
handshake regardless of User-Agent, so requests go out through curl_cffi, which
reproduces a real browser's TLS/HTTP2 fingerprint.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from bs4 import BeautifulSoup


# Coors Light Hot Country Nights: Tucker Wetmore, Kansas City Live! at the
# Power & Light District, Thu 13 Aug 2026.  Override with EVENT_URL to watch a
# different event; EVENT_NAME only affects the email subject line.
DEFAULT_EVENT_URL = (
    "https://www.axs.com/events/1378791/"
    "coors-light-hot-country-nights-tucker-wetmore-tickets"
)
DEFAULT_EVENT_NAME = "Tucker Wetmore - Hot Country Nights, KC Live! (Thu Aug 13)"
DEFAULT_RECIPIENT = "sebastianrhoton@gmail.com"

STATE_FILE = Path(__file__).with_name("ticket_prices.json")
# Append-only snapshot log.  Nobody publishes how fast an event is selling, so
# the next best thing is the shape of the inventory over time: a tier stepping
# up, or vanishing, is demand becoming visible after the fact.
HISTORY_FILE = Path(__file__).with_name("price_history.jsonl")

# Ignore sub-dollar noise so rounding differences in AXS's markup do not send
# mail; a real tier change is always at least a dollar.
DEFAULT_THRESHOLD = 1.00
# Consecutive failed reads before mailing a "cannot read the page" warning.  A
# silent watcher is worse than a noisy one when the show is days away, but a
# single blocked request is routine, so warn only once a pattern is clear.
FAILURES_BEFORE_WARNING = 3
# After that first warning the cause is known and unchanging (an IP block does
# not clear itself), so repeat only about once a day at a 30-minute cadence
# rather than every third run.
FAILURE_REMINDER_EVERY = 48

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
IMPERSONATE_TARGETS = ("safari17_0", "chrome120", "safari18_0", "chrome110")
CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "request unsuccessful",
    "access to this page has been denied",
    "px-captcha",
    "captcha-delivery",
    "verify you are a human",
    "are you a robot",
)
# A real AXS event page is well over this; challenge interstitials are a few KB.
MIN_PAGE_BYTES = 20_000

PRICE_PATTERN = re.compile(r"\$\s*(\d[\d,]*(?:\.\d{2})?)")
# Keys whose value is a price, and keys whose value names the thing priced.
PRICE_KEYS = ("price", "lowprice", "highprice", "minprice", "maxprice", "amount")
LABEL_KEYS = ("name", "sectionname", "section", "leveiname", "levelname",
              "title", "description", "pricelevel", "tickettype")
# Prices outside this range are page furniture (a $0 placeholder, a $10,000
# suite) rather than a ticket tier for a country show on a plaza.
MIN_SANE_PRICE = 1.0
MAX_SANE_PRICE = 2_000.0


@dataclass(frozen=True)
class Offer:
    """One priced ticket tier as advertised on the event page."""

    label: str
    price: float
    availability: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"price": self.price, "availability": self.availability}


def money(value: float) -> str:
    return f"${value:,.2f}"


def parse_price(value: Any) -> float | None:
    """Coerce a JSON-LD/embedded-JSON price field to a plausible dollar figure."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        match = PRICE_PATTERN.search(str(value)) or re.search(r"\d[\d,]*(?:\.\d{2})?", str(value))
        if not match:
            return None
        try:
            price = float(match.group(match.lastindex or 0).replace(",", ""))
        except ValueError:
            return None
    return price if MIN_SANE_PRICE <= price <= MAX_SANE_PRICE else None


def parse_setting(raw: str | None) -> float | None:
    """Parse a dollar figure from configuration, without the ticket-range clamp.

    A threshold or target is a knob the operator chose, so values that would be
    implausible for a *ticket* (50 cents, say) are still legitimate here.
    """
    if raw is None or not raw.strip():
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def json_objects(node: Any) -> Iterable[dict[str, Any]]:
    """Yield every object nested anywhere inside a decoded JSON document."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from json_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from json_objects(value)


def offers_from_object(item: dict[str, Any]) -> list[Offer]:
    """Read any priced tiers out of one JSON object.

    AXS exposes prices in more than one shape depending on the page revision
    (JSON-LD ``offers``, hydration state, a ``priceRange`` summary), so this
    matches on key *names* rather than on one known schema.
    """
    lowered = {str(key).lower(): value for key, value in item.items()}
    label = ""
    for key in LABEL_KEYS:
        candidate = lowered.get(key)
        if isinstance(candidate, str) and candidate.strip():
            label = " ".join(candidate.split())[:80]
            break
    availability = str(lowered.get("availability") or lowered.get("status") or "")
    availability = availability.rsplit("/", 1)[-1]

    found: list[Offer] = []
    for key in PRICE_KEYS:
        price = parse_price(lowered.get(key))
        if price is None:
            continue
        # "lowPrice"/"highPrice" describe one range, so keep them distinguishable.
        suffix = {"lowprice": " (from)", "highprice": " (to)"}.get(key, "")
        found.append(Offer(f"{label or 'Ticket'}{suffix}", price, availability))
    return found


def extract_offers(page_html: str) -> dict[str, Offer]:
    """Collect the priced tiers advertised on an event page, keyed by label."""
    soup = BeautifulSoup(page_html, "lxml")
    offers: dict[str, Offer] = {}

    scripts = soup.select('script[type="application/ld+json"]')
    scripts += soup.select('script[type="application/json"]')
    scripts += [tag for tag in soup.select("script#__NEXT_DATA__")]
    for tag in scripts:
        raw = tag.get_text(strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in json_objects(payload):
            for offer in offers_from_object(item):
                # Keep the cheapest reading of any given label.
                existing = offers.get(offer.label)
                if existing is None or offer.price < existing.price:
                    offers[offer.label] = offer

    if not offers:
        # Last resort: the page rendered prices as text only.  Record the range
        # so a move is still detected even without per-section labels.
        prices = sorted(
            {price for price in
             (parse_price(match) for match in PRICE_PATTERN.findall(page_html))
             if price is not None}
        )
        if prices:
            offers["Listed price (from)"] = Offer("Listed price (from)", prices[0])
            if len(prices) > 1:
                offers["Listed price (to)"] = Offer("Listed price (to)", prices[-1])
    return offers


def looks_blocked(response: Any) -> bool:
    if response.status_code >= 400:
        return True
    text = response.text or ""
    if len(text) < MIN_PAGE_BYTES:
        return True
    low = text.lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


def fetch(url: str) -> str:
    """Return the event page, trying each fingerprint until one is not blocked.

    Anti-bot layers also score IP reputation, which no TLS fingerprint changes:
    GitHub Actions runners use datacenter ranges that AXS may reject outright.
    Set TICKET_PROXY (e.g. a residential proxy URL) to route around that; unset,
    requests go out directly.
    """
    proxy = os.environ.get("TICKET_PROXY") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last: Any = None
    for target in IMPERSONATE_TARGETS:
        response = requests.get(url, headers=HEADERS, impersonate=target,
                                proxies=proxies, timeout=30)
        last = response
        if not looks_blocked(response):
            return response.text
        logging.debug("%s fingerprint challenged (HTTP %s, %d bytes)",
                      target, response.status_code, len(response.text or ""))
    status = last.status_code if last is not None else "no response"
    raise RequestException(f"every fingerprint was blocked (last status: {status})")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logging.warning("Could not read %s (%s); starting fresh.", STATE_FILE.name, exc)
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def previous_offers(state: dict[str, Any]) -> dict[str, float]:
    stored = state.get("offers")
    if not isinstance(stored, dict):
        return {}
    prices: dict[str, float] = {}
    for label, value in stored.items():
        price = parse_price(value.get("price") if isinstance(value, dict) else value)
        if price is not None:
            prices[str(label)] = price
    return prices


def should_warn(failures: int) -> bool:
    """Warn once the streak is established, then about daily, not every run.

    A blocked IP stays blocked, so repeating the same warning every 90 minutes
    for three days would be dozens of identical emails.
    """
    if failures < FAILURES_BEFORE_WARNING:
        return False
    return failures == FAILURES_BEFORE_WARNING or failures % FAILURE_REMINDER_EVERY == 0


def stored_since(state: dict[str, Any]) -> dict[str, str]:
    """When each tier's current price was first seen, from the saved state."""
    stored = state.get("offers")
    if not isinstance(stored, dict):
        return {}
    return {
        str(label): str(value["since"])
        for label, value in stored.items()
        if isinstance(value, dict) and value.get("since")
    }


def format_duration(seconds: float) -> str:
    """A short human duration: "3d 4h", "6h 12m", "45m"."""
    minutes = max(0, int(seconds // 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def held_for(since_iso: str | None, now: datetime) -> str:
    """How long a price has stood, for the "Held" column in the alert."""
    if not since_iso:
        return ""
    try:
        since = datetime.fromisoformat(since_iso)
    except ValueError:
        return ""
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return format_duration((now - since).total_seconds())


def append_history(offers: dict[str, Offer], cheapest: float | None, now: datetime) -> None:
    """Append one snapshot; a corrupt or unwritable log must not break the run."""
    record = {
        "at": now.isoformat(timespec="seconds"),
        "cheapest": cheapest,
        "tiers": len(offers),
        "offers": {label: offer.as_json() for label, offer in sorted(offers.items())},
    }
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        logging.warning("Could not append to %s: %s", HISTORY_FILE.name, exc)


def summarise_history() -> int:
    """Print what the snapshot log says about how inventory has moved."""
    if not HISTORY_FILE.exists():
        print("No history yet: the watcher has not completed a successful run.")
        return 0
    records = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not records:
        print("No readable snapshots in the history log.")
        return 0

    first, last = records[0], records[-1]
    print(f"{len(records)} snapshot(s), {first['at']} -> {last['at']}")
    cheapest_seen = [record["cheapest"] for record in records
                     if isinstance(record.get("cheapest"), (int, float))]
    if cheapest_seen:
        print(f"cheapest ever {money(min(cheapest_seen))}, "
              f"dearest cheapest {money(max(cheapest_seen))}, "
              f"now {money(last['cheapest']) if last.get('cheapest') else 'n/a'}")

    # Per tier: how many times the price moved, and in which direction.
    history: dict[str, list[float]] = {}
    for record in records:
        for label, value in (record.get("offers") or {}).items():
            price = parse_price(value.get("price") if isinstance(value, dict) else value)
            if price is not None:
                history.setdefault(label, []).append(price)
    print()
    for label, prices in sorted(history.items()):
        steps = sum(1 for a, b in zip(prices, prices[1:]) if a != b)
        ups = sum(1 for a, b in zip(prices, prices[1:]) if b > a)
        trend = "steady" if not steps else f"{steps} move(s), {ups} up / {steps - ups} down"
        present = "still listed" if label in (last.get("offers") or {}) else "NO LONGER LISTED"
        print(f"{label}: {money(prices[0])} -> {money(prices[-1])}  [{trend}; {present}]")
    return 0


@dataclass(frozen=True)
class Change:
    label: str
    old: float | None
    new: float | None

    @property
    def kind(self) -> str:
        if self.old is None:
            return "new"
        if self.new is None:
            return "gone"
        return "down" if self.new < self.old else "up"

    @property
    def delta(self) -> float | None:
        if self.old is None or self.new is None:
            return None
        return self.new - self.old


def diff_offers(old: dict[str, float], new: dict[str, float], threshold: float) -> list[Change]:
    changes = [
        Change(label, old[label], new[label])
        for label in sorted(old.keys() & new.keys())
        if abs(new[label] - old[label]) >= threshold
    ]
    changes += [Change(label, None, new[label]) for label in sorted(new.keys() - old.keys())]
    changes += [Change(label, old[label], None) for label in sorted(old.keys() - new.keys())]
    # Biggest drops first: that is the reason to open the mail.
    return sorted(changes, key=lambda change: (change.delta if change.delta is not None else 0.0))


def send_email(subject: str, body_html: str, body_text: str) -> None:
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("TICKET_ALERT_EMAIL") or DEFAULT_RECIPIENT
    if not (sender and password):
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body_text)
    message.add_alternative(body_html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)
    logging.info("Emailed %s: %s", recipient, subject)


def change_rows(changes: list[Change], held: dict[str, str] | None = None) -> str:
    arrows = {"down": "&#9660; down", "up": "&#9650; up", "new": "new", "gone": "no longer listed"}
    held = held or {}
    rows = []
    for change in changes:
        old = money(change.old) if change.old is not None else "&mdash;"
        new = money(change.new) if change.new is not None else "&mdash;"
        delta = f"{'+' if change.delta > 0 else ''}{money(change.delta)}" if change.delta else "&mdash;"
        colour = {"down": "#0a7d28", "up": "#b00020"}.get(change.kind, "#444")
        rows.append(
            "<tr>"
            f"<td>{html.escape(change.label)}</td><td>{old}</td><td>{new}</td>"
            f"<td style='color:{colour}'>{delta} ({arrows[change.kind]})</td>"
            f"<td>{html.escape(held[change.label]) if held.get(change.label) else '&mdash;'}</td>"
            "</tr>"
        )
    return "".join(rows)


def price_change_email(event_name: str, event_url: str, changes: list[Change],
                       cheapest: float | None,
                       held: dict[str, str] | None = None) -> tuple[str, str, str]:
    drops = [change for change in changes if change.kind == "down"]
    headline = (
        f"{len(drops)} price drop(s) on {event_name}" if drops
        else f"Ticket prices changed for {event_name}"
    )
    cheapest_line = (
        f"Cheapest listed right now: {money(cheapest)}" if cheapest is not None
        else "No price could be read from the page."
    )
    body_html = (
        f"<html><body><h2>{html.escape(headline)}</h2>"
        f"<p><strong>{html.escape(cheapest_line)}</strong></p>"
        "<table border='1' cellpadding='7' cellspacing='0'><thead><tr>"
        "<th>Ticket</th><th>Was</th><th>Now</th><th>Change</th><th>Old price held</th>"
        f"</tr></thead><tbody>{change_rows(changes, held)}</tbody></table>"
        f"<p><a href=\"{html.escape(event_url, quote=True)}\">Open the AXS event page</a></p>"
        "<p style='color:#666;font-size:12px'>\"Old price held\" is how long the "
        "previous price stood. Short holds and repeated upward steps mean tiers "
        "are selling through; long holds mean they are not.</p>"
        "</body></html>"
    )
    lines = [headline, cheapest_line, ""]
    for change in changes:
        was = money(change.old) if change.old is not None else "-"
        now = money(change.new) if change.new is not None else "-"
        lines.append(f"{change.label}: {was} -> {now} ({change.kind})")
    lines += ["", event_url]
    return headline, body_html, "\n".join(lines)


def target_reached_email(event_name: str, event_url: str, cheapest: float,
                         target: float) -> tuple[str, str, str]:
    headline = f"{money(cheapest)} ticket available - at or below your {money(target)} target"
    body_html = (
        f"<html><body><h2>{html.escape(headline)}</h2>"
        f"<p>{html.escape(event_name)}</p>"
        f"<p><a href=\"{html.escape(event_url, quote=True)}\">Buy on AXS</a></p></body></html>"
    )
    return headline, body_html, f"{headline}\n{event_name}\n{event_url}"


def failure_email(event_name: str, event_url: str, failures: int,
                  reason: str) -> tuple[str, str, str]:
    headline = f"Ticket watcher cannot read the AXS page ({failures} tries in a row)"
    body_html = (
        f"<html><body><h2>{html.escape(headline)}</h2>"
        f"<p>{html.escape(event_name)}</p>"
        f"<p>Last error: {html.escape(reason)}</p>"
        "<p>Prices are <strong>not</strong> being monitored until this clears. "
        "AXS is most likely blocking the CI runner's IP; set the TICKET_PROXY "
        "secret to a residential proxy, or check the page by hand.</p>"
        f"<p><a href=\"{html.escape(event_url, quote=True)}\">Open the AXS event page</a></p>"
        "</body></html>"
    )
    return headline, body_html, f"{headline}\n{event_name}\nLast error: {reason}\n{event_url}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and do not email or write state")
    parser.add_argument("--show", action="store_true",
                        help="print the prices found and exit without comparing")
    parser.add_argument("--history", action="store_true",
                        help="summarise the recorded snapshots and exit (no network)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.history:
        return summarise_history()

    event_url = os.environ.get("EVENT_URL") or DEFAULT_EVENT_URL
    event_name = os.environ.get("EVENT_NAME") or DEFAULT_EVENT_NAME
    threshold = parse_setting(os.environ.get("PRICE_CHANGE_THRESHOLD")) or DEFAULT_THRESHOLD
    target = parse_setting(os.environ.get("PRICE_TARGET"))

    state = load_state()
    try:
        page = fetch(event_url)
    except Exception as exc:  # one bad read must not fail the whole run
        failures = int(state.get("consecutive_failures") or 0) + 1
        logging.warning("Could not read the event page (%d in a row): %s", failures, exc)
        state["consecutive_failures"] = failures
        state["last_error"] = str(exc)
        state["last_checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not args.dry_run:
            if should_warn(failures):
                send_email(*failure_email(event_name, event_url, failures, str(exc)))
            # The counter only survives because the workflow commits this file;
            # without that every run would restart at 1 and never warn at all.
            save_state(state)
        return 0

    offers = extract_offers(page)
    current = {label: offer.price for label, offer in offers.items()}
    cheapest = min(current.values()) if current else None

    if args.show or not current:
        for label, price in sorted(current.items(), key=lambda pair: pair[1]):
            print(f"{money(price):>10}  {label}")
        if not current:
            logging.warning("No prices found on the page; markup may have changed.")
        if args.show:
            return 0

    now = datetime.now(timezone.utc)
    previous = previous_offers(state)
    since = stored_since(state)
    first_run = not previous
    changes = diff_offers(previous, current, threshold)
    # How long the price we are replacing had stood: the closest thing to a
    # sales-rate signal that a public page exposes.
    held = {change.label: held_for(since.get(change.label), now) for change in changes}

    if first_run:
        logging.info("First run: recorded %d price(s), cheapest %s.",
                     len(current), money(cheapest) if cheapest is not None else "n/a")
    elif changes:
        logging.info("%d change(s) detected.", len(changes))
        if not args.dry_run:
            send_email(*price_change_email(event_name, event_url, changes, cheapest, held))
    else:
        logging.info("No change beyond %s (cheapest %s).", money(threshold),
                     money(cheapest) if cheapest is not None else "n/a")

    if target is not None and cheapest is not None and cheapest <= target:
        already = parse_price(state.get("target_notified_at_price"))
        # Re-notify only if it dropped further, not on every run while under.
        if already is None or cheapest < already:
            if not args.dry_run:
                send_email(*target_reached_email(event_name, event_url, cheapest, target))
            state["target_notified_at_price"] = cheapest
    elif target is not None:
        state.pop("target_notified_at_price", None)

    if args.dry_run:
        for change in changes:
            print(f"would report: {change.label}: {change.old} -> {change.new}")
        return 0

    stamp = now.isoformat(timespec="seconds")
    state.update({
        "event_url": event_url,
        "event_name": event_name,
        "last_checked": stamp,
        "consecutive_failures": 0,
        "cheapest": cheapest,
        # "since" carries forward while a price stands, so the next change can
        # report how long the old one lasted.
        "offers": {
            label: dict(offer.as_json(),
                        since=since.get(label, stamp)
                        if previous.get(label) == offer.price else stamp)
            for label, offer in sorted(offers.items())
        },
    })
    state.pop("last_error", None)
    save_state(state)
    append_history(offers, cheapest, now)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.error("Run failed: %s", exc)
        raise SystemExit(1)
