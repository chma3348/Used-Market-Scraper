# Buying

Nothing is written down here on purpose. The buying side is automated, and a
file that restated the searches or the prices would be out of date within a
week. This is a map of where the real data lives.

## The dashboard

```bash
python server.py
```

Four tabs: **Check** a price on demand, **Watching** for the active searches
and their alert prices, **History** for past checks, **Settings** for keys,
scan frequency and the cloud link. Every control is editable in place.

This is the source of truth for what is being watched. Change searches here,
never by editing `config.json` directly — the dashboard pushes changes to the
GitHub scan, and editing the file by hand bypasses that and leaves the cloud
running searches you think you deleted.

## Scan reports

`history/` — one Markdown file per scan and per manual check, named
`<date>_<mode>_<query>.md`. Each cloud scan report lists every genuine match
it found, cheapest first, with deals marked. These are committed, so they are
readable on GitHub from a phone.

## Price history and trends

`trends.html`, served by the dashboard. Median and deal-floor over time per
item, the p25–p75 band, sold-price overlay where it exists, a distribution
histogram, and a junk breakdown. Methodology changes are drawn as breaks in the
line rather than silently trended through — the gear being priced changed
partway through, and the graph says so.

The underlying data is `dealfinder.db`, table `price_checks`; `analytics.py`
does the derivation.

## Scheduled scans

`.github/workflows/scan.yml` runs on cron. Three phases: a free fetch, a
Claude Code screening pass billed to the subscription rather than the API, then
a free notify-and-record step. Results are committed back to the repo and
pushed to your phone via ntfy.

## The other direction

What your gear is worth: [`../selling/`](../selling/).
