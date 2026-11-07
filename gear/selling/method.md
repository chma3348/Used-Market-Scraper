# How the selling prices were derived

Written down so the numbers can be checked, argued with, and regenerated the
same way later.

## The problem

**eBay now requires a login to view sold listings.** The usual way to price used
gear — filter to "Sold items", read the median — is closed off, and the
scraper in `sources.py:fetch_ebay_sold` returns an empty page as a result.

So the price sheet is built from **live asking prices**, which are not the same
thing and run considerably higher.

## The method

1. **Fetch.** Live listings via the eBay Browse API (`sources.fetch_ebay`),
   several query spellings per item, shipping included in every price via
   `sources.total_cost` — a $60 lens with $40 postage is a $100 lens.
2. **Screen by hand.** The raw pools are mostly noise: caps, brochures, manuals,
   kits, wrong variants, and in one case a modern Sony lens sharing a focal
   length with a 1990s one. Roughly half of every pool gets thrown out.
3. **Take percentiles** of what survives.
4. **Price against the 20th–25th percentile, not the median.**

## Why the bottom quartile

The asking-price boards for vintage gear are dominated by sellers — Japanese
dealers especially — who grade everything "[MINT]" or "[Exc+5]" and sit on
stock for months. Those listings inflate the median without ever being sales.
The listings that actually move cluster near the bottom of the clean-condition
band.

The one external check available: CameraWorth's Nikon F3 **eBay sold** median is
£328 (≈$430) against a live ask median of $550 — so sales land around the 20th
percentile of asks. That ratio is what the sheet is built on.

Its weakness: that is a sample of 8 sales, and the rest of CameraWorth's F3 data
is UK auction-house hammer prices, which run lower than US eBay retail. Treat
every number in the price sheet as **±15%**, and treat the
[sold log](sold-log.md) as the thing that will eventually replace it.

## The net column

`net = list − 13.25% final value fee − $0.40 − postage`

Postage assumes free shipping built into the price, which is what the buyer
expects and what the price sheet's comps all do. Rough weights used: camera
body $14–18, lens $10–12, motor drive $14, flash $11.

## The local column

On eBay you keep about 82% of the list price minus postage. That net is the
floor for a Facebook or Craigslist price: matching it leaves you even while
still reading as a discount to the buyer. The "take" figure is roughly 15%
below the ask, which is where offers land in practice.

## Refreshing

The fetch scripts live in the session scratchpad, not the repo — they were
one-offs. To redo this from scratch:

```python
import sources, filters, json
cfg = json.load(open("config.json"))
tok = sources.ebay_get_token(cfg["ebay"]["client_id"], cfg["ebay"]["client_secret"])
ls = sources.fetch_ebay(tok, "nikon f3 hp body", None, None, limit=100)
for l in sorted(ls, key=lambda x: sources.total_cost(x)):
    print(round(sources.total_cost(l)), l.title[:70])
```

Then screen by eye and read off the bottom quartile. Worth redoing if the sheet
is more than about two months old, or sooner for anything with a thin market —
the Polaroid Copal set had only 11 comparable listings site-wide, so a single
new listing moves that market.

## What is not in here

- **Sold prices.** See above. The [sold log](sold-log.md) is where real numbers
  go as they arrive.
- **Days to sell.** Nothing in listing data records how long something sat.
  Undercut counts are the closest proxy, which is what `analytics.sell_advice`
  reports.
- **Condition grading.** Every condition judgement in the price sheet came from
  your description of the item, not from any data source.
