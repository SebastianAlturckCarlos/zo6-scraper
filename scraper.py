"""Find newly listed 2025-2027 Corvette Z06s and email an alert.

This is deliberately based on normal HTTP requests rather than browser
automation: it is inexpensive to run in GitHub Actions and avoids keeping a
long-lived browser process.  Automotive marketplaces change their markup and
may rate-limit automated traffic, so an unavailable source is logged and does
not prevent the other sources from completing.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


STATE_FILE = Path("seen_vins.json")
YEARS = {"2025", "2026", "2027"}
VIN_PATTERN = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"\$\s*([\d,]+)")
MILEAGE_PATTERN = re.compile(r"([\d,]+)\s*(?:mi|miles)\b", re.IGNORECASE)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CorvetteZ06DealNotifier/1.0; +https://github.com/)",
    "Accept-Language": "en-US,en;q=0.9",
}


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


def load_seen_vins() -> set[str]:
    """Load both the current list format and a legacy {"vins": [...]} format."""
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("vins", [])
        if not isinstance(data, list):
            raise ValueError("state is not a JSON list")
        return {str(vin).upper() for vin in data if VIN_PATTERN.fullmatch(str(vin).upper())}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("Could not read %s: %s. Starting with an empty state.", STATE_FILE, exc)
        return set()


def save_seen_vins(vins: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(vins), indent=2) + "\n", encoding="utf-8")


def first_match(pattern: re.Pattern[str], value: str, default: str = "Not listed") -> str:
    match = pattern.search(value)
    return match.group(1) if match else default


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
    title = str(item.get("name") or item.get("model") or "Chevrolet Corvette Z06")
    if not vin or "corvette" not in (title + text).lower() or "z06" not in (title + text).lower():
        return None
    year = str(item.get("vehicleModelDate") or item.get("productionDate") or "")
    if year and not any(y in year for y in YEARS):
        return None
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    offers = offers if isinstance(offers, dict) else {}
    raw_price = str(offers.get("price") or item.get("price") or "")
    price = f"${raw_price}" if raw_price and not raw_price.startswith("$") else (raw_price or first_match(PRICE_PATTERN, text))
    mileage = str(item.get("mileageFromOdometer") or item.get("mileage") or first_match(MILEAGE_PATTERN, text))
    url = str(offers.get("url") or item.get("url") or page_url)
    return Listing(vin.upper(), price, mileage, urljoin(page_url, url), source, title)


def scrape_source(source: str, url: str) -> list[Listing]:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
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
        if not vin or "corvette" not in content.lower() or "z06" not in content.lower() or not any(y in content for y in YEARS):
            continue
        link = element.select_one("a[href]")
        listing_url = urljoin(url, link["href"]) if link else url
        listings[vin.upper()] = Listing(
            vin.upper(), first_match(PRICE_PATTERN, content), first_match(MILEAGE_PATTERN, content),
            listing_url, source, "2025-2027 Chevrolet Corvette Z06",
        )
    logging.info("%s: found %d VIN-bearing listing(s)", source, len(listings))
    return list(listings.values())


def send_alert(listings: list[Listing]) -> None:
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("TARGET_EMAIL")
    if not all((sender, password, recipient)):
        raise RuntimeError("GMAIL_ADDRESS, GMAIL_APP_PASSWORD, and TARGET_EMAIL must be configured")

    rows = "".join(
        "<tr>"
        f"<td>{html.escape(car.source)}</td><td>{html.escape(car.title)}</td>"
        f"<td>{html.escape(car.vin)}</td><td>{html.escape(car.price)}</td>"
        f"<td>{html.escape(car.mileage)}</td>"
        f'<td><a href="{html.escape(car.url, quote=True)}">View listing</a></td>'
        "</tr>"
        for car in listings
    )
    message = EmailMessage()
    message["Subject"] = f"{len(listings)} new Corvette Z06 deal(s) found"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("New Corvette Z06 listings found. View this message in an HTML-capable email client.")
    message.add_alternative(
        "<html><body><h2>New Corvette Z06 listings</h2>"
        "<table border='1' cellpadding='7' cellspacing='0'><thead><tr>"
        "<th>Source</th><th>Vehicle</th><th>VIN</th><th>Price</th><th>Mileage</th><th>Link</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></body></html>", subtype="html"
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    seen = load_seen_vins()
    discovered: dict[str, Listing] = {}
    for source, url in SOURCES.items():
        try:
            for listing in scrape_source(source, url):
                discovered[listing.vin] = listing
        except requests.RequestException as exc:
            logging.warning("%s could not be scraped: %s", source, exc)
        except Exception as exc:  # Keep one marketplace's markup change isolated.
            logging.warning("%s could not be parsed: %s", source, exc)

    new_listings = [listing for vin, listing in discovered.items() if vin not in seen]
    if not new_listings:
        logging.info("No new VINs found.")
        return 0
    send_alert(new_listings)
    save_seen_vins(seen | {listing.vin for listing in new_listings})
    logging.info("Alert sent and %d VIN(s) saved.", len(new_listings))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.error("Run failed: %s", exc)
        raise SystemExit(1)
