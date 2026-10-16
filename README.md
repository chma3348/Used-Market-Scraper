# DealFinder

Scans **eBay** (official API) and **Craigslist** for your saved searches several
times a day, filters out junk listings (for-parts, damaged, accessories, wrong
items) with keyword rules plus an AI screen, computes the typical market price
for each search, and **pushes a notification to your phone** when a listing is
priced well below market — so you can message the seller fast.

Facebook Marketplace is handled separately in **semi-auto mode** (see below).

---

## One-time setup (about 15 minutes)

### 1. eBay API keys (free)

1. Go to <https://developer.ebay.com> and sign up (free, instant for personal use).
2. Once logged in: **Your Account → Application Keys**.
3. Under **Production**, create a keyset. Copy the **App ID (Client ID)** and
   **Cert ID (Client Secret)**.
4. Paste them into `config.json` under `ebay.client_id` and `ebay.client_secret`.
   - Outside the US? Change `ebay.marketplace` too (e.g. `EBAY_GB`, `EBAY_CA`, `EBAY_AU`).

### 2. The AI brain — local model or Claude API

All AI features (junk screening, query expansion, price verdicts, Deep Hunt)
run on whichever **AI provider** is selected in the dashboard Settings:

- **Local model via Ollama** — free and private. Install [Ollama](https://ollama.com),
  pull a model (e.g. `ollama pull qwen3.6:27b`), select "Local" in Settings.
  Slower on big price checks (a few minutes when thousands of listings get
  screened), but costs nothing and scheduled scans barely notice.
- **Claude API / OAuth** — best quality and much faster on large batches.
  Needs an Anthropic developer account **with credits purchased**
  (platform.claude.com → Billing), plus either an `sk-ant-` key in Settings
  or a browser login via `.bin/ant auth login`.

The cloud watcher on GitHub can't reach your Mac's Ollama, so it always uses
the Anthropic API (or keyword-only filtering if no key is in GitHub Secrets).

### 2b. Anthropic credentials (for the Claude API provider)

Normal mode uses an Anthropic API key:

1. Go to <https://platform.claude.com>, create an API key.
2. Paste it into `config.json` under `anthropic.api_key`, or paste it in the
   dashboard Settings card.
3. Cost: the screen uses Claude Haiku on listing titles only - typically a few
   cents per day at 4 runs/day. For sharper filtering you can change
   `anthropic.model` to `"claude-opus-4-8"` (costs more per run).

Local development can use your Claude OAuth login instead, so you can test the
AI scorer before adding paid API credits:

1. Run `ant auth login` from the DealFinder folder.
2. Leave `anthropic.api_key` blank.
3. Set `anthropic.prefer_oauth` to `true`, or turn on **Use Claude login/OAuth
   for local AI testing** in the dashboard Settings card.

If neither an API key nor a Claude OAuth profile is available, everything still
works; you just rely on keyword filtering alone. The GitHub cloud watcher still
needs an API key later, because your local OAuth profile stays on your Mac.

### 3. Phone notifications (free, 2 minutes)

1. Install the **ntfy** app ([iPhone](https://apps.apple.com/us/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. In the app, tap **+ / Subscribe to topic** and enter exactly:

   ```
   <your-ntfy-topic>   # see config.json (gitignored)
   ```

   (Your topic name is in `config.json`. It's random so strangers can't guess it —
   treat it like a password and don't share it.)
3. Test it:

   ```
   cd "~/Desktop/untitled folder 2"
   .venv/bin/python3 dealfinder.py test-notify
   ```

### 4. Craigslist city

Set `craigslist.site` in `config.json` to your city's subdomain — the bit
before `.craigslist.org` when you browse it (e.g. `sfbay`, `newyork`,
`losangeles`, `chicago`, `seattle`).

---

## The app (easiest way to use it)

Double-click **DealFinder.app** in this folder. It starts the local dashboard
and opens it in your browser at `http://127.0.0.1:8377` (local-only — nothing
is exposed to the internet). From there you can:

- run price checks and turn them into watched searches with one click
- add/remove searches, set or change alert prices, toggle anything on/off
- pause/resume everything, trigger a scan now, see recent deals and the log
- turn the automatic schedule on/off and send a test notification

Tip: drag DealFinder.app to the Dock. The launcher finds its own folder, so
you can move the DealFinder folder anywhere (avoid the Desktop — macOS blocks
apps from reading files there; if runs are scheduled, re-run
`install-schedule` after moving). API keys and all settings are managed in the
dashboard's **Settings** card. The dashboard reads the same `config.json` as
everything else, so the app and the commands below are interchangeable.

## Sharing DealFinder with someone else

The whole folder is self-contained. To give it to a friend:

1. Copy the DealFinder folder, but first delete `config.json`, `dealfinder.db`,
   and the `.venv` folder from the copy (your keys, history, and Mac-specific
   Python environment).
2. In the copy, save this as `config.json`:

   ```json
   {"notifications": {"ntfy_topic": ""}, "searches": []}
   ```

3. They put the folder anywhere except the Desktop, double-click
   DealFinder.app (first launch takes a minute — it builds its own Python
   environment), then fill in the **Settings** card: their own eBay keys,
   Anthropic credentials, Craigslist city, and a made-up secret ntfy topic
   (e.g. `dealfinder-<their name>-x8k2p`). Everyone needs their own topic —
   sharing one means sharing each other's deal alerts.

## Everyday use (Terminal alternative)

Everything the app does is also available as commands, run from the project
folder (`cd ~/Desktop/"untitled folder 2"`) via
`.venv/bin/python3 dealfinder.py <command>`:

```
price "airpods pro 2"    # one-off market price check (see below)
add "airpods pro 2" --alert-under 120   # then: watch, alert at <= $120
add "dewalt drill" --max 120 --craigslist-only
set "airpods" --alert-under 100         # change the alert any time
set "airpods" --clear-alert             # back to the auto 25%-below-typical rule
list                     # show all searches, alerts, and on/off state
off "dewalt drill"       # pause one search
on "dewalt drill"        # resume it
off                      # pause EVERYTHING (schedule keeps firing but does nothing)
on                       # resume everything
remove "dewalt drill"    # delete a search
recent                   # deals found in the last 7 days
```

`off`/`on`/`remove` accept partial names as long as they're unambiguous.

## One-off price checks

Before committing to a search, gauge whether an item is even in your budget:

```
price "airpods pro 2"
price "iphone 13" --parts        # what do BROKEN ones go for?
```

Screens out junk with the same keyword + AI filtering, then shows three
spreads: **eBay listed** (what sellers are asking), **eBay sold** (what buyers
actually paid — the real number), and **Craigslist listed**, each as a
usual-range + typical price, plus the cheapest clean listings with links.
Nothing is saved and nothing is notified.

Sold prices come from eBay's public sold-listings page (there's no official
API for them), shown in whatever currency eBay serves your location. eBay
occasionally throttles this — if the sold row says it was blocked, wait a
minute and re-check.

## Deep Hunt (the "go hard" button)

For when you want the deals other buyers miss — mistitled items, misspellings,
bulk lots, estate-sale vagueness, cheap auctions:

```
hunt "nintendo switch oled"
hunt "iphone 13" --parts --notes "want screens and boards"
```

(or the **Deep hunt** button next to Price check in the app). AI plans up to
12 aggressive queries, fetches everything including auctions, then a stronger
model (`anthropic.hunt_model`, default Claude Opus) reads every single title
hunting for hidden value and returns a ranked list — STRONG for
click-it-now finds, maybe for worth-a-look — each with one line of value
logic. Takes a few minutes and uses roughly $0.25 of API credit per hunt.

## Condition modes (working vs. broken)

Every search and price check runs in one of three modes:

- **working** (default) — drop parts/damaged listings
- **`--parts`** — keep ONLY broken/for-parts listings; use this when you want a
  cheap repair donor. Deal alerts then track the market for broken units.
- **`--any-condition`** — keep both

Example: `add "iphone 13" --name "iphone 13 screen donor" --parts --max 150`.
Tip: keep a working-mode and a parts-mode search for the same item under
different names — each builds its own price history.

## Search options

`add` flags (you can also edit `searches` in `config.json` directly):

| field | meaning |
|---|---|
| `name` | Label used in notifications and stats (required) |
| `query` | The search terms, as you'd type them on eBay (required) |
| `min_price` / `max_price` | Optional price bounds |
| `notes` | Optional guidance for the AI screen ("must include dock", "no Lite models") — this meaningfully improves filtering |
| `sources` | Optional, e.g. `["craigslist"]`. Default: both |
| `include_auctions` | eBay auctions too (off by default — current bids look like fake deals) |
| `deal_discount` | Per-search override of the deal threshold |
| `enabled` | Set `false` to pause a search |

Two disabled examples are in the file — copy one and edit it.

## Running

```
cd "~/Desktop/untitled folder 2"
.venv/bin/python3 dealfinder.py run --dry-run   # scan + report, no notifications
.venv/bin/python3 dealfinder.py run             # real run
.venv/bin/python3 dealfinder.py recent          # deals found in the last 7 days
```

## Running automatically

```
.venv/bin/python3 dealfinder.py install-schedule
```

Installs a macOS launchd job that runs `runs_per_day` times per day (default 4,
spread between 9:00 and 21:00), while your Mac is awake. Output goes to
`dealfinder.log`. Remove with `uninstall-schedule`.

Scans run only while the Mac is awake (a missed scan runs shortly after it
wakes). For scans that keep running around the clock, turn on **"Keep Mac
awake for scans"** in the dashboard — it prevents system sleep *only while
plugged into power* (display still sleeps; battery is unaffected). A closed
MacBook lid still forces sleep, so leave the lid open when plugged in. For
true off-Mac scanning, set up the **cloud watcher** (below).

## Cloud watcher (scans even when your Mac is off)

A private GitHub repository runs your **eBay** searches on your schedule from
GitHub's servers — notifications still arrive on your phone via ntfy.
Craigslist and sold-price lookups stay on your Mac (marketplaces block cloud
IPs for those). While the cloud watcher is on, your Mac's scheduled scans
cover Craigslist only, so the two never double-notify.

Setup: create a free GitHub account, then a **fine-grained Personal Access
Token** (Settings → Developer settings → Personal access tokens) with these
repository permissions set to Read and write: **Administration, Contents,
Secrets, Actions, Workflows**. Paste the token into the DealFinder Settings
card, Save, then click **Set up cloud watcher**. The app creates the repo,
uploads the scanner, stores your keys as encrypted GitHub Secrets, mirrors
your scan schedule (converted to UTC), and fires a test run.

After that it's automatic: adding/removing/toggling searches, pausing, or
changing keys/schedule in the dashboard syncs to the cloud within seconds.
GitHub's scheduler has ±5–15 min of jitter, and the free tier (2,000
minutes/month) covers this comfortably. Disable any time with the same button.

Exact times are set in the dashboard's Settings card: there's one time picker
per daily scan (pre-filled with the current schedule), and changing "scans per
day" adds or removes pickers. Adjust any picker, hit Save, and if the schedule
is already on it reinstalls automatically. (Config equivalent:
`"run_times": ["8:00", ...]`, then re-run `install-schedule`.)

## Price alerts (the recommended workflow)

1. `price "airpods pro 2"` — see what they actually go for.
2. Decide your number: `add "airpods pro 2" --alert-under 120`
3. You get a push only when a clean listing appears at or under $120.
4. Change your mind any time: `set "airpods" --alert-under 100`, or
   `set "airpods" --clear-alert` to fall back to the automatic
   25%-below-typical rule.

A search with an alert starts notifying immediately (no price history needed).
The auto rule is the default for searches without an alert.

> The AI screen matters most in alert mode: a $120 alert on "airpods" would
> match every $20 case and cable on keyword filtering alone. With the
> Anthropic credentials available, accessories and junk are classified out first.

## eBay auctions

Auctions are off by default (`--auctions` enables them per search) because a
current bid isn't a price. When enabled:

- **Auctions with a Buy It Now option** are treated as fixed-price listings at
  the BIN price — you can act on those immediately.
- **Bid-only auctions** are *watched*, not alerted: their current bid never
  enters price history, and you're only notified when the auction is below
  your threshold **and** ends within the auction window (default 24h, so you
  don't get pinged about a $1 bid with 7 days left). Watched auctions are
  re-checked on every scheduled run with fresh bid and time remaining —
  set `--auction-window` smaller (e.g. 6) if you only want snipe-range alerts,
  and make sure the runs-per-day cadence is at least as frequent as the window.

## How deals are flagged

1. All listings for a search are fetched from eBay + Craigslist.
2. Junk is dropped: keyword rules catch "for parts", "cracked", "as-is", etc.,
   and eBay's own "For parts or not working" condition; the AI screen then
   catches subtle damage, accessories (cases, boxes, chargers) and wrong items.
3. The **median price** of clean listings (this run + last 14 days of history)
   is the "typical price". A listing at least `deal_discount` (default 25%)
   below the median is flagged and pushed to your phone with a tap-through
   link to the listing.
4. Nothing is ever notified twice, and the first run or two mostly builds
   price history — deal flagging starts once a search has 6+ clean price points
   (usually immediately, since one scan returns ~50 listings).
5. Listings under 25% of the typical price get a scam warning in the
   notification — too-good-to-be-true usually is, especially on Craigslist.

## Facebook Marketplace (semi-auto)

FB Marketplace has no public API and blocks automated access, so it isn't part
of the scheduled runs. Instead, ask Claude in this project:

> "Run a Facebook Marketplace sweep for my searches"

Claude will open Marketplace in your browser session, check each search in
`config.json`, apply the same junk-filtering judgment, compare prices against
the typical prices in `dealfinder.db`, and report anything that looks like a
deal. Slower than the automated runs, but it keeps your FB account safe.

## Files

- `DealFinder.app` — double-clickable launcher for the dashboard
- `server.py` / `ui.html` — the local web dashboard
- `dealfinder.py` — main script and commands
- `sources.py` — eBay Browse API + Craigslist fetchers
- `filters.py` — keyword junk filter
- `ai_filter.py` — AI listing screen (Anthropic API, structured output)
- `notify.py` — ntfy push notifications
- `db.py` — SQLite store (seen listings, price history)
- `config.json` — all your settings and searches
- `dealfinder.db` — created on first run
- `dealfinder.log` — created by the scheduler
