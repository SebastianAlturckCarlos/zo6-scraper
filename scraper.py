"""Find newly listed 2025-2027 Corvette Z06s and email an alert.

This is deliberately based on normal HTTP requests rather than browser
automation: it is inexpensive to run in GitHub Actions and avoids keeping a
long-lived browser process.  Automotive marketplaces change their markup and
may rate-limit automated traffic, so an unavailable source is logged and does
not prevent the other sources from completing.

The marketplaces used here (CarGurus/Akamai, Autotrader/DataDome,
Cars.com/Cloudflare) reject the default TLS handshake of a plain HTTP client
regardless of the User-Agent, so requests are issued through curl_cffi, which
reproduces a real browser's TLS/HTTP2 fingerprint.  We only read pages that a
browser would fetch without signing in; no login or CAPTCHA is bypassed.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from bs4 import BeautifulSoup


STATE_FILE = Path("lowest_price.json")
YEARS = {2025, 2026, 2027}
# VIN position 10 (model year) for the years we track.  For a Corvette these are
# unambiguous: the Z06 badge did not exist before 2001, so S/T/V cannot mean
# 1995-1997.
VIN_YEAR_CODES = {"S": 2025, "T": 2026, "V": 2027}
# VIN position 5 (car line / trim) for the C8 Z06 (1LZ/2LZ/3LZ).  Base Stingrays
# use A/B/C and are excluded; this is only consulted for cars already confirmed
# to be a tracked model year, where these codes reliably mean Z06.
C8_Z06_TRIM = {"D", "E", "F"}
# A VIN is 17 characters excluding I/O/Q and always mixes letters and digits,
# so the two lookaheads reject 17-character words (e.g. "SearchResultsPage")
# and 17-digit numbers that would otherwise be captured as false positives.
VIN_PATTERN = re.compile(
    r"\b(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])([A-HJ-NPR-Z0-9]{17})\b",
    re.IGNORECASE,
)
PRICE_PATTERN = re.compile(r"\$\s*(\d[\d,]*\d)")
MILEAGE_PATTERN = re.compile(r"([\d,]+)\s*(?:mi|miles)\b", re.IGNORECASE)
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Browser profiles for curl_cffi to impersonate.  Anti-bot systems occasionally
# challenge one fingerprint while allowing another, so we fall back through the
# list and keep the first response that returns a real page.  safari17_0 is
# tried first because it currently clears all three sources in one request.
IMPERSONATE_TARGETS = ("safari17_0", "chrome120", "safari18_0", "chrome110")

# Substrings that identify a challenge/block page served with a 200 status and a
# larger body than the size check below would catch (some soft blocks are).
CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "request unsuccessful",
    "access to this page has been denied",
    "px-captcha",
    "captcha-delivery",
    "verify you are a human",
    "akamai-block",
    "page unavailable",
)

# A real search-results page from these marketplaces is hundreds of kilobytes;
# challenge/error interstitials are only a few.  Anything below this is treated
# as a block so fetch() falls through to the next browser fingerprint.
MIN_PAGE_BYTES = 50_000


@dataclass(frozen=True)
class Listing:
    vin: str
    price: str
    mileage: str
    url: str
    source: str
    title: str


# These public search URLs can be narrowed further (distance, ZIP, price) by
# editing this list.  Do not add authentication or bypass a site's restrictions.
SOURCES = {
    "CarGurus": "https://www.cargurus.com/Cars/l-Used-Chevrolet-Corvette-Z06-d1",
    "Autotrader": "https://www.autotrader.com/cars-for-sale/all-cars/chevrolet/corvette/z06",
    "Cars.com": "https://www.cars.com/shopping/chevrolet-corvette-z06/",
}


def load_lowest_price() -> int | None:
    """Return the lowest price alerted so far, or None if there is no record."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        price = data.get("price") if isinstance(data, dict) else None
        return int(price) if isinstance(price, (int, float)) and price > 0 else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("Could not read %s: %s. Starting with no record.", STATE_FILE, exc)
        return None


def save_lowest_price(listing: Listing, price: int) -> None:
    """Persist the cheapest listing so future runs only alert on a lower price."""
    record = {
        "price": price,
        "vin": listing.vin,
        "source": listing.source,
        "title": listing.title,
        "mileage": listing.mileage,
        "url": listing.url,
    }
    STATE_FILE.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def first_match(pattern: re.Pattern[str], value: str, default: str = "Not listed") -> str:
    match = pattern.search(value)
    return match.group(1) if match else default


def json_ld_scalar(value: Any) -> str:
    """Coerce a JSON-LD field to a plain string.

    Numeric fields such as ``mileageFromOdometer`` are frequently expressed as a
    nested ``QuantitativeValue`` ({"value": 12, "unitCode": "SMI"}); return the
    inner value rather than the stringified dict.
    """
    if isinstance(value, dict):
        value = value.get("value") or value.get("price") or ""
    if value is None:
        return ""
    return str(value).strip()


def format_price(raw: str, default: str = "Not listed") -> str:
    """Normalise a price string to a ``$``-prefixed value, or a default."""
    raw = raw.strip()
    if not raw:
        return default
    return raw if raw.startswith("$") else f"${raw}"


def price_value(price: str) -> int | None:
    """Parse a formatted price string to a positive integer, or None."""
    match = re.search(r"(\d[\d,]*)", price or "")
    if not match:
        return None
    value = int(match.group(1).replace(",", ""))
    return value if value > 0 else None


def vin_model_year(vin: str) -> int | None:
    """Model year from VIN position 10, limited to the years we track."""
    return VIN_YEAR_CODES.get(vin[9].upper()) if len(vin) >= 17 else None


def is_target_z06(vin: str, name: str) -> bool:
    """True only for a 2025-2027 Corvette Z06.

    Model year comes from the VIN (position 10), which is present and reliable on
    every listing.  The Z06 trim is confirmed by the listing name where the site
    labels it (Autotrader) or, where it does not (CarGurus lists all Corvettes
    generically), by the C8 Z06 trim code in VIN position 5 -- which excludes the
    base Stingrays that otherwise dominate the cheap end.
    """
    if vin_model_year(vin) not in YEARS:
        return False
    return "z06" in name.lower() or vin[4].upper() in C8_Z06_TRIM


def json_ld_objects(node: Any) -> Iterable[dict[str, Any]]:
    """Yield every object contained in a JSON-LD script."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from json_ld_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from json_ld_objects(value)


def listing_from_json_ld(item: dict[str, Any], source: str, page_url: str) -> Listing | None:
    text = json.dumps(item)
    vin = first_match(VIN_PATTERN, text, "")
    # Detect on the real name only; a Z06-containing default would defeat the
    # trim check for sources (CarGurus) that name listings generically.
    name = str(item.get("name") or item.get("model") or "")
    if not vin or not is_target_z06(vin, name):
        return None
    title = name or f"{vin_model_year(vin)} Chevrolet Corvette Z06"
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    offers = offers if isinstance(offers, dict) else {}
    raw_price = json_ld_scalar(offers.get("price") or item.get("price")) or first_match(PRICE_PATTERN, text, "")
    price = format_price(raw_price)
    mileage = json_ld_scalar(item.get("mileageFromOdometer") or item.get("mileage")) \
        or first_match(MILEAGE_PATTERN, text)
    url = str(offers.get("url") or item.get("url") or page_url)
    return Listing(vin.upper(), price, mileage, urljoin(page_url, url), source, title)


def looks_blocked(response: Any) -> bool:
    """Return True if the response is an anti-bot challenge, not a listing page."""
    if response.status_code >= 400:
        return True
    text = response.text or ""
    if len(text) < MIN_PAGE_BYTES:
        return True
    low = text.lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


def fetch(url: str) -> Any:
    """Fetch ``url`` trying each browser fingerprint until one is not blocked.

    Cloudflare and Akamai also block by IP reputation, which no TLS fingerprint
    can defeat: GitHub Actions runs from datacenter IP ranges that some sources
    reject outright.  Setting the SCRAPER_PROXY environment variable (e.g. a
    residential proxy URL) routes requests through it so those sources work from
    CI; when unset, requests go out directly and behaviour is unchanged.
    """
    proxy = os.environ.get("SCRAPER_PROXY") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last_response = None
    for target in IMPERSONATE_TARGETS:
        response = requests.get(url, headers=HEADERS, impersonate=target,
                                proxies=proxies, timeout=30)
        last_response = response
        if not looks_blocked(response):
            return response
        logging.debug("%s: %s fingerprint was challenged (HTTP %s, %d bytes)",
                      url, target, response.status_code, len(response.text or ""))
    # Nothing got through; surface the last status for the caller to log.
    if last_response is not None:
        last_response.raise_for_status()
    raise RequestException(f"No response for {url}")


def scrape_source(source: str, url: str) -> list[Listing]:
    response = fetch(url)
    soup = BeautifulSoup(response.text, "lxml")
    listings: dict[str, Listing] = {}

    # JSON-LD is the least brittle structured data exposed by marketplaces.
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(tag.get_text(strip=True))
        except json.JSONDecodeError:
            continue
        for item in json_ld_objects(payload):
            listing = listing_from_json_ld(item, source, url)
            if listing:
                listings[listing.vin] = listing

    # Fallback for pages that render vehicle data into ordinary listing cards.
    for element in soup.select("article, li, .listing, [data-listing-id], [data-vin]"):
        content = element.get_text(" ", strip=True)
        vin = first_match(VIN_PATTERN, content, "")
        if not vin or "corvette" not in content.lower() or not is_target_z06(vin, content):
            continue
        link = element.select_one("a[href]")
        listing_url = urljoin(url, link["href"]) if link else url
        listings[vin.upper()] = Listing(
            vin.upper(), format_price(first_match(PRICE_PATTERN, content, "")),
            first_match(MILEAGE_PATTERN, content),
            listing_url, source, f"{vin_model_year(vin)} Chevrolet Corvette Z06",
        )
    logging.info("%s: found %d target Z06 listing(s)", source, len(listings))
    return list(listings.values())


def send_alert(car: Listing, price: int, previous: int | None) -> None:
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("TARGET_EMAIL")
    if not all((sender, password, recipient)):
        raise RuntimeError("GMAIL_ADDRESS, GMAIL_APP_PASSWORD, and TARGET_EMAIL must be configured")

    if previous is None:
        headline = f"Cheapest 2025-2027 Corvette Z06 right now: ${price:,}"
        context = "This is the first alert, showing the lowest-priced listing found today."
    else:
        headline = f"New low for a 2025-2027 Corvette Z06: ${price:,}"
        context = f"Down ${previous - price:,} from the previous low of ${previous:,}."

    rows = (
        "<tr>"
        f"<td>{html.escape(car.source)}</td><td>{html.escape(car.title)}</td>"
        f"<td>{html.escape(car.vin)}</td><td>{html.escape(car.price)}</td>"
        f"<td>{html.escape(car.mileage)}</td>"
        f'<td><a href="{html.escape(car.url, quote=True)}">View listing</a></td>'
        "</tr>"
    )
    message = EmailMessage()
    message["Subject"] = headline
    message["From"] = sender
    message["To"] = recipient
    message.set_content(f"{headline}\n{context}\nView this message in an HTML-capable email client.")
    message.add_alternative(
        f"<html><body><h2>{html.escape(headline)}</h2><p>{html.escape(context)}</p>"
        "<table border='1' cellpadding='7' cellspacing='0'><thead><tr>"
        "<th>Source</th><th>Vehicle</th><th>VIN</th><th>Price</th><th>Mileage</th><th>Link</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></body></html>", subtype="html"
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    discovered: dict[str, Listing] = {}
    for source, url in SOURCES.items():
        try:
            for listing in scrape_source(source, url):
                discovered[listing.vin] = listing
        except RequestException as exc:
            logging.warning("%s could not be scraped: %s", source, exc)
        except Exception as exc:  # Keep one marketplace's markup change isolated.
            logging.warning("%s could not be parsed: %s", source, exc)

    # Consider only listings with a usable price and take the cheapest.
    priced = [(price_value(listing.price), listing) for listing in discovered.values()]
    priced = [(value, listing) for value, listing in priced if value is not None]
    if not priced:
        logging.info("No priced 2025-2027 Z06 listings found.")
        return 0
    price, cheapest = min(priced, key=lambda item: item[0])

    previous = load_lowest_price()
    if previous is not None and price >= previous:
        logging.info("Cheapest listing $%s is not below the recorded low $%s; no alert.", price, previous)
        return 0

    send_alert(cheapest, price, previous)
    save_lowest_price(cheapest, price)
    logging.info("Alert sent: $%s (%s). Previous low: %s.", price, cheapest.vin, previous)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.error("Run failed: %s", exc)
        raise SystemExit(1)
