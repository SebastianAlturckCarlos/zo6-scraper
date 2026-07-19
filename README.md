# Corvette Z06 deal scraper

Checks automotive marketplaces for one exact spec — a **Chevrolet Corvette Z06
in 2LZ or 3LZ trim, coupe body, model year 2025–2027, any colour** — and emails
an alert listing **any new ones** since the last run. Runs on GitHub Actions
every six hours — four times a day (and on demand via **Actions → Corvette Z06
deal scraper → Run workflow**).

## How it works

`scraper.py` fetches each search page and extracts listings (VIN, price,
mileage, link), primarily from the JSON-LD structured data the sites embed. It
keeps only listings matching the exact spec, decoded from the VIN (which every
listing carries) and cross-checked against the sites' own labels:

- **position 10** — model year: `S`/`T`/`V` = 2025/2026/2027
- **position 5** — trim: `E` = 2LZ, `F` = 3LZ (`D` = 1LZ and Stingrays A/B/C are excluded)
- **position 6** — body: `2` = coupe (`3` = convertible, excluded)

E-Ray and ZR1 (which reuse the LZ trim names) are excluded by name. Matching
VINs are compared against `seen_vins.json`; any not seen before are emailed and
then added to it. The workflow commits `seen_vins.json` back to the repo so the
record persists between runs. **No email is sent when there is nothing new.**

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
