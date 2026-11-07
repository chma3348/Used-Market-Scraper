# Gear

Everything about the gear itself, as opposed to the code that hunts for it.
Two halves, because they answer opposite questions.

| | question | where the truth lives |
|---|---|---|
| [**selling/**](selling/) | *what is my gear worth?* | these files — written by hand, refreshed on demand |
| [**buying/**](buying/) | *what should I buy?* | the app — dashboard, `dealfinder.db`, `history/` |

The asymmetry is deliberate. The buying side is already automated: searches run
on a schedule, results land in `history/`, prices accumulate in the database and
plot on the trends page. Nothing there needs writing down by hand, and anything
copied into a file here would drift out of date within a week.

The selling side has no automation, so it is written down.

## Selling

- [**price-sheet.md**](selling/price-sheet.md) — the master list. What to ask on
  eBay, what it should realistically go for, what you net after fees and
  postage, and what to ask locally. Start here.
- [**sold-log.md**](selling/sold-log.md) — what actually sold, for how much, and
  how long it took. Fill it in as you go; it is what makes the next price sheet
  better than this one.
- [**method.md**](selling/method.md) — how the numbers were derived, and how to
  refresh them when they go stale.

## Buying

- [**buying/README.md**](buying/) — where to find what the scanner has learned:
  the dashboard, the scan reports, the price-history graphs.
