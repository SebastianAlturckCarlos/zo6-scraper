# Corvette Z06 deal scraper

Checks automotive marketplaces for 2025–2027 Chevrolet Corvette Z06s and emails
an alert **only when the cheapest one found beats the lowest price seen so far**.
Runs on GitHub Actions every six hours — four times a day (and on demand via
**Actions → Corvette Z06 deal scraper → Run workflow**).

## How it works

`scraper.py` fetches each search page and extracts listings (VIN, price,
mileage, link), primarily from the JSON-LD structured data the sites embed. It
keeps only genuine **2025–2027 Z06s**: the model year comes from the VIN (a
Corvette with VIN model-year code S/T/V), and the Z06 trim is confirmed either
from the listing name or from the C8 Z06 trim code in the VIN — this excludes
older Corvettes and base Stingrays that the marketplace searches also return.

Among those, it takes the cheapest with a usable price and compares it to
`lowest_price.json`. If it is lower than the recorded low (or there is no record
yet, as on the first run), it emails that one listing and saves the new low. The
workflow commits `lowest_price.json` back to the repo so the record persists
between runs. No email is sent on runs where nothing beats the record.

Requests go through [`curl_cffi`](https://github.com/lexiforest/curl_cffi) with
a real browser's TLS fingerprint, because these marketplaces reject an ordinary
Python HTTP client at the TLS layer regardless of the `User-Agent`. Only pages a
normal browser can load without signing in are read; no login or CAPTCHA is
bypassed. A source that is unavailable is logged and skipped so the others still
complete.

## Configuration

Set these in **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
| --- | --- |
| `GMAIL_ADDRESS` | Gmail account that sends the alert |
| `GMAIL_APP_PASSWORD` | Gmail [app password](https://support.google.com/accounts/answer/185833) (not your login password) |
| `TARGET_EMAIL` | Where alerts are sent |
| `SCRAPER_PROXY` | *Optional.* Proxy URL (see below) |

## Sources and the datacenter-IP limitation

`curl_cffi` defeats TLS-fingerprint blocking, but some sources (notably
**Cars.com**, behind Cloudflare) also block by IP reputation and reject GitHub
Actions' datacenter IP ranges with a `403`. No fingerprint can change the source
IP, so from CI those sources fail while CarGurus and Autotrader work.

To include an IP-blocked source, add a residential/rotating proxy as the
`SCRAPER_PROXY` secret (e.g. `http://user:pass@host:port`). When set, all
requests route through it; when unset, requests go out directly and the scraper
still runs on the sources that do not IP-block CI.

## Running locally

```sh
python -m venv .venv && . .venv/Scripts/activate   # or .venv/bin/activate
pip install -r requirements.txt
GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... TARGET_EMAIL=... python scraper.py
```
