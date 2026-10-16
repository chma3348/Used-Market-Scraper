# Scheduled scans on GitHub Actions

The scan runs in three phases so that only the judgement call costs anything:

| Phase | What runs | Cost |
|---|---|---|
| 1 | `cloud/fetch.py` — eBay/Craigslist fetch + free keyword filtering | free |
| 2 | `anthropics/claude-code-action` runs `.claude/skills/scan-screen` | your Claude **subscription**, via `CLAUDE_CODE_OAUTH_TOKEN` |
| 3 | `cloud/report.py` — ntfy push, `seen.json`, `history/` | free |

Phase 2 uses a subscription OAuth token rather than an API key, so scans do
not bill the Anthropic API account. The DealFinder Python code never calls
the Messages API in this path — `ai_filter.py` is not used at all.

## One-time setup

1. `npm install -g @anthropic-ai/claude-code` (already installed locally)
2. `claude setup-token` → copy the `sk-ant-oat01-…` value
3. Install the Claude GitHub App on the repo: https://github.com/apps/claude
4. Add these repository secrets under **Settings → Secrets and variables → Actions**:
   - `CLAUDE_CODE_OAUTH_TOKEN` — from step 2
   - `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`
   - `NTFY_TOPIC`
5. Push to the default branch. Run it once by hand from the **Actions** tab
   (**DealFinder scan → Run workflow**) before trusting the schedule.

## Changing searches

`cloud/searches.json` holds the searches with **no credentials in it**. Edit
it (or regenerate it from the dashboard's config) and push — the next
scheduled run picks it up. `"paused": true` stops scanning without deleting
anything.

## Schedule

Edit the `cron:` lines in `.github/workflows/scan.yml`. They are **UTC**; the
committed ones are 09:00/13:00/17:00/21:00 Denver. GitHub only runs schedules
from the default branch and can delay a run under load.

## Things that will bite

- **Secrets never go in the repo.** `config.json` is gitignored for this
  reason; the runner reads credentials from GitHub Secrets only.
- **`seen.json` is what stops repeat notifications.** It is committed back by
  the workflow after every run. Delete it and the next scan re-notifies
  everything it finds.
- **Phase 2 is skipped when phase 1 finds nothing**, so a quiet run costs
  nothing at all.
- **Phase 3 fails the run** if phase 2 wrote no parseable `results.json`,
  rather than silently notifying nobody.
