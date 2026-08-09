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
- **a tier appears or disappears** — "General Admission — no longer listed" is
  the signal that matters most;
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
python ticket-watch/ticket_watch.py --history   # summarise the snapshots, offline

cd ticket-watch && python -m unittest test_ticket_watch   # offline tests
```

The first run only records a baseline — you get the first email on the run
*after* that, when there is something to compare against.

## State and history

`ticket_prices.json` holds the last seen price per tier, when that price was
first seen, the cheapest price, the last check time, and the current failure
streak. `price_history.jsonl` gets one appended snapshot per successful run. The
workflow commits both, so the record survives between runs.

Delete `ticket_prices.json` to reset the alert baseline; delete
`price_history.jsonl` to reset the history.

## Reading the history: can you tell how fast it's selling?

Not directly. **No public source publishes sales velocity** — not AXS, not the
venue, not the resale sites. There is no ticket-count endpoint to read and no
honest way to derive one from a price. Anything claiming a live "only 4 left!"
number on a page like this is marketing copy, not inventory.

What the snapshot log gives you is the *shape of the inventory over time*, which
is the closest available proxy:

```
$ python ticket-watch/ticket_watch.py --history
36 snapshot(s), 2026-08-09T20:00:00+00:00 -> 2026-08-10T14:00:00+00:00
cheapest ever $62.00, dearest cheapest $68.00, now $62.00

General Admission: $62.00 -> $62.00  [2 move(s), 1 up / 1 down; still listed]
The Patio: $256.00 -> $256.00        [steady; still listed]
```

Read it like this:

- **Tiers stepping up, repeatedly, with short holds** → inventory is selling
  through. Buy.
- **Prices flat for a day or more, nothing disappearing** → it is not selling
  out. Waiting costs you nothing, and day-of resale is where a real discount
  would come from.
- **A tier vanishing** → that price is gone for good; the primary floor just
  moved up.

Each alert email also carries an **"Old price held"** column: how long the price
being replaced had stood. A tier that holds for eight hours and a tier that
holds for twenty minutes are telling you very different things about demand.

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

The watcher tells you *that* a price moved; it can't make a price move.

On AXS, **primary** inventory is tiered: as cheap tiers sell, the listed price
steps up and the cheapest tier disappears rather than getting cheaper. So
waiting rarely lowers the *primary* price. Meaningful downward movement comes
from AXS Official Resale, where other fans reprice, and that concentrates in the
last hours before doors.

Whether that's a good bet depends entirely on whether the show is actually
scarce, and KC Live! is an ~8,000-capacity outdoor block, not a small club — the
Hot Country Nights series has historically run free-for-21+ nights with a cover
for under-21s. Let the history log answer it rather than guessing: if tiers sit
flat for a day, the scarcity story is wrong and waiting is cheap.
