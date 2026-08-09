# AXS ticket price watch

Emails **sebastianrhoton@gmail.com** whenever the ticket prices move on one AXS
event page. Set up for:

> **Coors Light Hot Country Nights: Tucker Wetmore** — Kansas City Live! at the
> Power & Light District, Thursday **Aug 13, 2026**, doors 6pm / music 7pm, 18+.

This is completely separate from the Corvette Z06 scraper in the repository
root: its own script, its own `requirements.txt`, its own state file, its own
workflow. Nothing in `scraper.py`, `seen_vins.json`, or
`.github/workflows/scraper.yml` was changed.

## What it does

Once every ~30 minutes it makes **one** request to the public event page, reads
every advertised price tier out of it, and compares them to the previous run.
It emails you when:

- **a tier's price changes** by at least `$1` (drops are listed first, in the
  subject line, and coloured green);
- **a tier appears or disappears** — on a near-sold-out show, "General
  Admission — no longer listed" is the important signal;
- **the cheapest ticket hits a target** you set (optional, off by default);
- **it can't read the page** three runs in a row, so the watcher never goes
  quiet on you while prices are actually moving.

It reads the same public page a browser loads before you sign in. It does not
log in, hold a cart, defeat a CAPTCHA, or buy anything — the buying is yours.

## Setup

Add these repository **secrets** (Settings → Secrets and variables → Actions):

| Secret | Required | Purpose |
| --- | --- | --- |
| `GMAIL_ADDRESS` | yes | Gmail account the alert is sent *from* |
| `GMAIL_APP_PASSWORD` | yes | [Google app password](https://myaccount.google.com/apppasswords), not your login password |
| `TICKET_ALERT_EMAIL` | no | Where alerts go; defaults to `sebastianrhoton@gmail.com` |
| `TICKET_PROXY` | no | Residential proxy URL, if AXS blocks the runner (see below) |

The Z06 scraper already uses `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`, so if
that one is working these are set already and there is nothing to add.

Optional repository **variables** tune the behaviour without touching code:

| Variable | Default | Purpose |
| --- | --- | --- |
| `EVENT_URL` | the Tucker Wetmore page | Watch a different AXS event |
| `EVENT_NAME` | Tucker Wetmore … | Text used in the email subject |
| `PRICE_CHANGE_THRESHOLD` | `1.00` | Minimum move, in dollars, worth an email |
| `PRICE_TARGET` | unset | Also email when the cheapest ticket is at or below this |

The workflow runs on a 30-minute schedule and can be started by hand from the
Actions tab (**AXS ticket price watch** → Run workflow).

## Running it locally

```bash
pip install -r ticket-watch/requirements.txt

python ticket-watch/ticket_watch.py --show      # print prices, change nothing
python ticket-watch/ticket_watch.py --dry-run   # compare, print, send no email
python ticket-watch/ticket_watch.py             # normal run: email + save state

cd ticket-watch && python -m unittest test_ticket_watch   # offline tests
```

The first run only records a baseline — you get the first email on the run
*after* that, when there is something to compare against.

## State

`ticket_prices.json` holds the last seen price per tier, the cheapest price, the
last check time, and the current failure streak. The workflow commits it after
each run, so the price history is visible in the file's git log. Delete it to
reset the baseline.

## When AXS blocks the runner

AXS sits behind an anti-bot layer that scores both TLS fingerprint and IP
reputation. The fingerprint half is handled — requests go out through
`curl_cffi` impersonating Safari/Chrome, falling through four profiles. The IP
half cannot be: GitHub Actions runners use datacenter ranges that are sometimes
rejected outright, and no amount of header tweaking changes that.

If that happens you'll get the "cannot read the AXS page" email rather than
silence. The fix is to set the `TICKET_PROXY` secret to a residential proxy URL;
without one, run the script from your own machine on a schedule instead:

```bash
# every 30 minutes, from a laptop that stays awake
*/30 * * * * cd /path/to/zo6-scraper && GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... \
  python ticket-watch/ticket_watch.py >> /tmp/ticket-watch.log 2>&1
```

## A caveat worth knowing before you wait

The watcher tells you *that* a price moved; it can't make a price move. On AXS,
**primary** inventory for a near-sold-out show is tiered — as the cheap tiers
sell out the listed price steps **up**, and the cheapest tier disappears rather
than getting cheaper. Meaningful downward movement generally comes from AXS
Official Resale, where other fans reprice, and that mostly happens in the last
hours before doors — by which point the cheap primary tier is usually gone.
Treat a drop alert as a short window, not a trend.
