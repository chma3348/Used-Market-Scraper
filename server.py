#!/usr/bin/env python3
"""DealFinder web dashboard - serves http://127.0.0.1:8377 (local only).

Start with:  .venv/bin/python3 server.py
Or just double-click DealFinder.app, which starts this and opens the browser.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

import analytics            # noqa: E402
import db as dbmod          # noqa: E402
import filters              # noqa: E402
import sources              # noqa: E402
import dealfinder           # noqa: E402
import notify               # noqa: E402

PORT = 8377
CONFIG_PATH = os.environ.get(
    "DEALFINDER_CONFIG", os.path.join(PROJECT_DIR, "config.json"))
LOG_PATH = os.path.join(PROJECT_DIR, "dealfinder.log")
PLIST_PATH = os.path.expanduser(
    "~/Library/LaunchAgents/com.dealfinder.check.plist")
AWAKE_PLIST_PATH = os.path.expanduser(
    "~/Library/LaunchAgents/com.dealfinder.awake.plist")

AWAKE_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.dealfinder.awake</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/caffeinate</string><string>-s</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
"""
UI_PATH = os.path.join(PROJECT_DIR, "ui.html")

scan_proc = None


def cfg():
    return dealfinder.load_config(CONFIG_PATH)


def save(config):
    dealfinder.save_config(CONFIG_PATH, config)


import ai_filter            # noqa: E402


def _ai_status(config):
    """(ok, label) describing how AI calls will run."""
    local = config.get("local_ai", {})
    if local.get("enabled"):
        model = local.get("model", "qwen3.6:27b")
        url = local.get("url", "http://127.0.0.1:11434").rstrip("/")
        try:
            with urllib.request.urlopen(url + "/api/tags", timeout=2) as resp:
                names = [m.get("name", "")
                         for m in json.loads(resp.read()).get("models", [])]
        except Exception:
            return False, ("AI: local model selected but Ollama isn't "
                           "running - start the Ollama app")
        if any(n == model or n.split(":")[0] == model.split(":")[0]
               for n in names):
            return True, f"AI: local {model} (free, on this Mac)"
        return False, (f"AI: Ollama is running but '{model}' isn't pulled - "
                       f"run: ollama pull {model}")
    anth = config.get("anthropic", {})
    if anth.get("prefer_oauth"):
        return ai_filter.oauth_status()
    key = anth.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if key and not key.startswith("sk-ant-"):
        return False, ("AI screening: key looks wrong - Anthropic keys start "
                       "with sk-ant- (an OpenAI key won't work); get one at "
                       "platform.claude.com")
    if key:
        return True, "AI screening"
    ok, label = ai_filter.oauth_status()
    if ok:
        return ok, label
    return False, "AI screening: no Anthropic credentials"


def _next_scan(config):
    """Local clock time of the next scheduled scan, or None when paused.
    Answers 'is this thing actually going to do anything?' without opening
    GitHub."""
    if config.get("paused"):
        return None
    times = config.get("run_times")
    if not times:
        runs = max(1, min(24, int(config.get("runs_per_day", 4))))
        times = [f"{h:02d}:00" for h in
                 ([9] if runs == 1 else
                  sorted({round(9 + i * (12 / (runs - 1))) for i in range(runs)}))]
    try:
        entries = dealfinder.parse_run_times(times)
    except ValueError:
        return None
    now = datetime.now()
    for h, m in entries:
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if when > now:
            return when.isoformat(timespec="minutes")
    h, m = entries[0]
    return (now.replace(hour=h, minute=m, second=0, microsecond=0)
            + timedelta(days=1)).isoformat(timespec="minutes")


def get_state():
    global scan_proc
    config = cfg()
    ai_ok, ai_label = _ai_status(config)
    store = dbmod.Store(config.get("db_path")
                        or os.path.join(PROJECT_DIR, "dealfinder.db"))
    deals = [
        {"when": row[0], "search": row[1], "price": row[2],
         "title": row[3], "url": row[4]}
        for row in store.recent_deals(days=14)
    ]
    ebay = config.get("ebay", {})
    return {
        "paused": bool(config.get("paused")),
        "scan_running": scan_proc is not None and scan_proc.poll() is None,
        "runs_per_day": config.get("runs_per_day", 4),
        "setup": {
            "ebay": bool(ebay.get("client_id") and ebay.get("client_secret")),
            "ai_ok": ai_ok,
            "ai_label": ai_label,
            "craigslist": config.get("craigslist", {}).get("site", ""),
            "craigslist_ok": config.get("craigslist", {}).get("site", "")
                             not in ("", "CHANGE_ME"),
            "ntfy_topic": config.get("notifications", {}).get("ntfy_topic", ""),
            "schedule": os.path.exists(PLIST_PATH),
            "awake": os.path.exists(AWAKE_PLIST_PATH),
        },
        "searches": config.get("searches", []),
        "deals": deals,
        "settings": {
            "ebay_client_id_tail": _tail(ebay.get("client_id", "")),
            "ebay_client_secret_tail": _tail(ebay.get("client_secret", "")),
            "anthropic_key_tail": _tail(
                config.get("anthropic", {}).get("api_key", "")),
            "anthropic_prefer_oauth": bool(
                config.get("anthropic", {}).get("prefer_oauth")),
            "marketplace": ebay.get("marketplace", "EBAY_US"),
            "craigslist_site": config.get("craigslist", {}).get("site", ""),
            "ntfy_topic": config.get("notifications", {}).get("ntfy_topic", ""),
            "runs_per_day": config.get("runs_per_day", 4),
            "run_times": ", ".join(config.get("run_times", [])),
            "next_scan": _next_scan(config),
            "github_token_tail": _tail(
                config.get("github", {}).get("token", "")),
            "ai_provider": ("local" if config.get("local_ai", {}).get("enabled")
                            else "api"),
            "local_model": config.get("local_ai", {}).get("model",
                                                          "qwen3.6:27b"),
            "local_screen_model": config.get("local_ai", {}).get(
                "screen_model", ""),
        },
        "cloud": {
            "enabled": bool(config.get("cloud", {}).get("enabled")),
            "repo": config.get("cloud", {}).get("repo", ""),
            # last run is read from GitHub; cached briefly so the 5s dashboard
            # refresh doesn't spend an API call every tick
            "status": _cloud_status_cached(config),
        },
    }


def find_search(config, name):
    for s in config.get("searches", []):
        if s["name"] == name:
            return s
    return None


# --------------------------------------------------------------- actions

def act_add(body):
    config = cfg()
    query = (body.get("query") or "").strip()
    if not query:
        return {"error": "Search terms are required."}
    name = (body.get("name") or "").strip() or query
    if find_search(config, name):
        return {"error": f"A search named '{name}' already exists."}
    search = {"name": name, "query": query, "enabled": True}
    for key in ("min_price", "max_price", "alert_under", "auction_max_hours"):
        if body.get(key) not in (None, ""):
            search[key] = float(body[key])
    if body.get("notes"):
        search["notes"] = body["notes"]
    if body.get("condition") in ("parts", "any"):
        search["condition"] = body["condition"]
    if body.get("sources") in (["ebay"], ["craigslist"]):
        search["sources"] = body["sources"]
    if body.get("include_auctions"):
        search["include_auctions"] = True
    profile = body.get("profile")
    if profile is None and not body.get("skip_profile"):
        draft = dealfinder.variant_profile_draft(
            config, query, search.get("notes", ""))
        if not draft:
            return {"error": "Couldn't draft the search profile (AI hiccup). "
                             "Try again."}
        return {"profile_draft": draft}
    rejects, guidance, include = dealfinder.compile_profile(profile, query)
    if profile:
        profile["reject_keywords"] = rejects   # snapshot for scans
        search["profile"] = profile
    if guidance:
        search["notes"] = ((search.get("notes", "") + " ")
                           if search.get("notes") else "") + guidance
    variants = dealfinder.expand_for_search(
        config, search["query"], search.get("notes", ""))
    for q in include:
        if q.lower() not in [x.lower() for x in variants]:
            variants.append(q)
    variants = variants[:8]
    if len(variants) > 1:
        search["queries"] = variants
    config.setdefault("searches", []).append(search)
    save(config)
    maybe_sync_cloud(config)
    return {"ok": True, "queries": variants, "variant_info": guidance}
def act_remove(body):
    config = cfg()
    search = find_search(config, body.get("name", ""))
    if not search:
        return {"error": "Search not found."}
    config["searches"].remove(search)
    save(config)
    maybe_sync_cloud(config)
    return {"ok": True}


def act_toggle(body):
    config = cfg()
    if body.get("name"):
        search = find_search(config, body["name"])
        if not search:
            return {"error": "Search not found."}
        search["enabled"] = bool(body.get("enabled", True))
    else:
        config["paused"] = not bool(body.get("enabled", True))
    save(config)
    maybe_sync_cloud(config)
    return {"ok": True}


def act_set(body):
    config = cfg()
    search = find_search(config, body.get("name", ""))
    if not search:
        return {"error": "Search not found."}
    # A key present with null clears it; present with a value sets it.
    for key in ("alert_under", "min_price", "max_price", "auction_max_hours",
                "deal_discount"):
        if key in body:
            if body[key] in (None, ""):
                search.pop(key, None)
            else:
                search[key] = float(body[key])
    if "notes" in body:
        if body["notes"]:
            search["notes"] = body["notes"]
        else:
            search.pop("notes", None)
    if "condition" in body:
        if body["condition"] in ("parts", "any"):
            search["condition"] = body["condition"]
        else:
            search.pop("condition", None)
    if "include_auctions" in body:
        if body["include_auctions"]:
            search["include_auctions"] = True
        else:
            search.pop("include_auctions", None)
    if "sources" in body:
        if body["sources"] in (["ebay"], ["craigslist"]):
            search["sources"] = body["sources"]
        else:
            search.pop("sources", None)   # both marketplaces
    if "heartbeat" in body:
        # "off" | "daily" | "scan" - a quiet ping so a patient search still
        # shows signs of life, with the cheapest seen so you learn whether
        # your target price is realistic
        if body["heartbeat"] in ("daily", "scan"):
            search["heartbeat"] = body["heartbeat"]
        else:
            search.pop("heartbeat", None)
    save(config)
    maybe_sync_cloud(config)
    return {"ok": True}


def act_price(body):
    query = (body.get("query") or "").strip()
    if not query:
        return {"error": "Search terms are required."}
    source_list = None
    if body.get("sources") in (["ebay"], ["craigslist"]):
        source_list = body["sources"]
    notes = body.get("notes", "")
    profile = body.get("profile")
    if profile is None and not body.get("skip_profile"):
        draft = dealfinder.variant_profile_draft(cfg(), query, notes)
        if not draft:
            # never spend money without showing the card first
            return {"error": "Couldn't draft the search profile (AI hiccup). "
                             "Hit Check again to retry."}
        return {"profile_draft": draft}
    rejects, guidance, include = dealfinder.compile_profile(profile, query)
    if guidance:
        notes = (notes + " " if notes else "") + guidance
    result = dealfinder.price_check(
        cfg(), query,
        min_price=float(body["min_price"]) if body.get("min_price") else None,
        max_price=float(body["max_price"]) if body.get("max_price") else None,
        notes=notes,
        mode=body.get("condition") if body.get("condition") in ("parts", "any")
             else "working",
        source_list=source_list,
        auctions=bool(body.get("include_auctions")),
        extra_queries=include,
        reject_keywords=rejects,
    )
    if profile:
        result["profile"] = profile
    if guidance:
        result["variant_info"] = guidance
    return result
DEFAULT_CLOUD_REPO = "chma3348/Used-Market-Scraper"
CLOUD_SEARCHES_PATH = "cloud/searches.json"
CLOUD_WORKFLOW_PATH = ".github/workflows/scan.yml"
# Local copy of that workflow: the dashboard rewrites only its cron
# block, so the scan-time pickers stay the single source of truth.
WORKFLOW_PATH = os.path.join(PROJECT_DIR, CLOUD_WORKFLOW_PATH)


def _gh(config, method, path, body=None):
    """One GitHub REST API call. Returns (status, parsed_json)."""
    token = config.get("github", {}).get("token", "")
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "dealfinder",
            "X-GitHub-Api-Version": "2022-11-28",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            return resp.status, (json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except (ValueError, OSError):
            return e.code, {}


def _gh_put_file(config, repo, path, content, message):
    b64 = base64.b64encode(content).decode()
    status, existing = _gh(config, "GET",
                           f"/repos/{repo}/contents/{urllib.parse.quote(path)}?ref=main")
    body = {"message": message, "content": b64, "branch": "main"}
    if status == 200 and isinstance(existing, dict) and existing.get("sha"):
        if (existing.get("content") or "").replace("\n", "") == b64:
            return  # unchanged
        body["sha"] = existing["sha"]
    status, resp = _gh(config, "PUT", f"/repos/{repo}/contents/{path}", body)
    if status not in (200, 201):
        raise RuntimeError(
            f"GitHub push of {path} failed ({status}): {resp.get('message')}")


def _gh_set_secret(config, repo, name, value):
    from nacl import public
    status, pk = _gh(config, "GET", f"/repos/{repo}/actions/secrets/public-key")
    if status != 200:
        raise RuntimeError(f"couldn't fetch the repo's secrets key ({status})")
    sealed = public.SealedBox(
        public.PublicKey(base64.b64decode(pk["key"]))).encrypt(value.encode())
    status, resp = _gh(config, "PUT", f"/repos/{repo}/actions/secrets/{name}",
                       {"encrypted_value": base64.b64encode(sealed).decode(),
                        "key_id": pk["key_id"]})
    if status not in (201, 204):
        raise RuntimeError(f"storing secret {name} failed ({status})")


def cloud_config(config, paused=None):
    """The file the GitHub runner reads: searches and non-secret settings.
    Credentials never appear here - they live only in GitHub Secrets."""
    return {
        "ebay": {"marketplace": config.get("ebay", {}).get("marketplace",
                                                           "EBAY_US")},
        "craigslist": {"site": config.get("craigslist", {}).get("site", "")},
        "deal_discount": config.get("deal_discount", 0.25),
        "history_days": config.get("history_days", 14),
        "auction_max_hours": config.get("auction_max_hours", 24),
        "paused": config.get("paused", False) if paused is None else paused,
        "searches": config.get("searches", []),
    }


def _workflow_yaml(config):
    """Schedule mirroring the local scan times, converted to UTC cron."""
    if config.get("run_times"):
        entries = dealfinder.parse_run_times(config["run_times"])
    else:
        runs = max(1, min(12, int(config.get("runs_per_day", 4))))
        hours = ([9] if runs == 1 else
                 sorted({round(9 + i * (12 / (runs - 1))) for i in range(runs)}))
        entries = [(h, 0) for h in hours]
    offset_min = int(datetime.now().astimezone().utcoffset().total_seconds() // 60)
    lines = []
    for h, m in entries:
        total = (h * 60 + m - offset_min) % 1440
        lines.append(f'    - cron: "{total % 60} {total // 60} * * *"')
    # Rewrite only the schedule block of the checked-in workflow, so hand
    # edits to the rest of the file survive a sync.
    try:
        with open(WORKFLOW_PATH) as f:
            current = f.read()
    except OSError:
        return None
    return re.sub(r"(on:\n  schedule:\n)(?:    - cron: .*\n)+",
                  lambda m: m.group(1) + "\n".join(lines) + "\n",
                  current, count=1)


def _cloud_secrets(config):
    """Secrets the scan workflow needs. Screening runs on the Claude Code
    OAuth token (subscription billing), so no API key is pushed."""
    return {
        "EBAY_CLIENT_ID": config.get("ebay", {}).get("client_id", ""),
        "EBAY_CLIENT_SECRET": config.get("ebay", {}).get("client_secret", ""),
        "NTFY_TOPIC": config.get("notifications", {}).get("ntfy_topic", ""),
    }


def sync_cloud(config, include_secrets=False, repo=None):
    """Push the current searches (and optionally the secrets and schedule) to
    the scan repo. `repo` is passed explicitly during first-time setup, before
    config["cloud"] exists."""
    repo = repo or config["cloud"]["repo"]
    _gh_put_file(config, repo, CLOUD_SEARCHES_PATH,
                 (json.dumps(cloud_config(config), indent=1) + "\n").encode(),
                 "dashboard: sync searches")
    # The workflow file is only rewritten when the schedule actually changed,
    # because editing it needs the token's Workflows permission and we would
    # rather fail loudly on that than silently on every search edit.
    yaml = _workflow_yaml(config)
    if yaml:
        try:
            _gh_put_file(config, repo, CLOUD_WORKFLOW_PATH, yaml.encode(),
                         "dashboard: sync schedule")
        except RuntimeError as e:
            if "workflow" not in str(e).lower():
                raise
            print(f"[cloud] schedule not synced (token lacks Workflows): {e}")
    if include_secrets:
        # Optional: most tokens are scoped Contents+Actions+Workflows only,
        # and the secrets are usually set once by hand in GitHub's UI. A 403
        # here must not fail an otherwise-good search sync.
        for name, value in _cloud_secrets(config).items():
            if not value:
                continue
            try:
                _gh_set_secret(config, repo, name, value)
            except Exception as e:
                print(f"[cloud] secret {name} not pushed ({e}); "
                      "set it in GitHub's UI instead")


def maybe_sync_cloud(config):
    """Best-effort search sync after a change - never blocks the UI action."""
    if not config.get("cloud", {}).get("enabled"):
        return
    try:
        sync_cloud(config)
    except Exception as e:
        print(f"[cloud] sync failed: {e}")


def act_cloud_setup(body):
    """Connect the dashboard to an existing scan repo. The repo and its
    workflow are checked in; this only verifies access and pushes searches."""
    config = cfg()
    if body.get("disable"):
        if config.get("cloud", {}).get("enabled"):
            try:
                _gh_put_file(
                    config, config["cloud"]["repo"], CLOUD_SEARCHES_PATH,
                    (json.dumps(cloud_config(config, paused=True), indent=1)
                     + "\n").encode(),
                    "dashboard: pause scanning")
            except Exception as e:
                print(f"[cloud] pause push failed: {e}")
            config["cloud"]["enabled"] = False
            save(config)
        return {"ok": True}

    token = config.get("github", {}).get("token", "")
    if not token:
        return {"error": "Paste your GitHub token in Settings first, then Save."}
    repo = (body.get("repo") or config.get("cloud", {}).get("repo")
            or DEFAULT_CLOUD_REPO)
    status, info = _gh(config, "GET", f"/repos/{repo}")
    if status == 404:
        return {"error": f"Can't see {repo}. Check the repo name, and that the "
                         "token is scoped to it with Contents read & write."}
    if status != 200:
        return {"error": f"GitHub rejected the token ({status}): "
                         f"{info.get('message', '')}"}
    status, wf = _gh(config, "GET",
                     f"/repos/{repo}/contents/{CLOUD_WORKFLOW_PATH}?ref=main")
    if status != 200:
        return {"error": f"{repo} has no {CLOUD_WORKFLOW_PATH}. Push the "
                         "DealFinder repo there first."}
    try:
        sync_cloud(config, include_secrets=bool(body.get("push_secrets")),
                   repo=repo)
    except Exception as e:
        return {"error": str(e)}
    config.setdefault("cloud", {})
    config["cloud"].update({"enabled": True, "repo": repo})
    save(config)
    return {"ok": True, "repo": repo,
            "url": f"https://github.com/{repo}/actions"}


def act_cloud_run(body):
    """Fire a scan right now instead of waiting for the schedule."""
    config = cfg()
    cloud = config.get("cloud", {})
    if not cloud.get("enabled"):
        return {"error": "Cloud scanning isn't connected yet."}
    try:
        sync_cloud(config)
    except Exception as e:
        return {"error": f"Couldn't sync before running: {e}"}
    status, resp = _gh(
        config, "POST",
        f"/repos/{cloud['repo']}/actions/workflows/scan.yml/dispatches",
        {"ref": "main"})
    if status not in (201, 204):
        return {"error": f"GitHub refused to start the scan ({status}): "
                         f"{resp.get('message', '')}. The token needs Actions "
                         "read & write."}
    return {"ok": True, "url": f"https://github.com/{cloud['repo']}/actions"}


_CLOUD_STATUS_CACHE = {"at": 0.0, "value": None}


def _cloud_status_cached(config, ttl=60):
    if not config.get("cloud", {}).get("enabled"):
        return None
    now = time.time()
    if now - _CLOUD_STATUS_CACHE["at"] < ttl:
        return _CLOUD_STATUS_CACHE["value"]
    _CLOUD_STATUS_CACHE.update({"at": now, "value": cloud_status(config)})
    return _CLOUD_STATUS_CACHE["value"]


def cloud_status(config):
    """Last scan outcome, read back from GitHub. Never raises - the dashboard
    renders whatever it gets."""
    cloud = config.get("cloud", {})
    if not (cloud.get("enabled") and config.get("github", {}).get("token")):
        return None
    repo = cloud["repo"]
    out = {"repo": repo, "url": f"https://github.com/{repo}/actions"}
    try:
        status, runs = _gh(
            config, "GET",
            f"/repos/{repo}/actions/workflows/scan.yml/runs?per_page=1")
        if status == 200 and runs.get("workflow_runs"):
            r = runs["workflow_runs"][0]
            out.update({
                "last_run": r.get("run_started_at") or r.get("created_at"),
                "state": r.get("status"),
                "result": r.get("conclusion"),
                "run_url": r.get("html_url"),
            })
    except Exception as e:
        out["error"] = str(e)
    return out


def act_cloud_results(body):
    """Pull the newest scan write-ups out of the repo so cloud finds show up
    in the dashboard next to locally-run checks."""
    config = cfg()
    cloud = config.get("cloud", {})
    if not cloud.get("enabled"):
        return {"error": "Cloud scanning isn't connected yet."}
    repo = cloud["repo"]
    status, listing = _gh(config, "GET",
                          f"/repos/{repo}/contents/history?ref=main")
    if status != 200:
        return {"error": f"Couldn't read history/ from {repo} ({status})."}
    scans = sorted((f for f in listing if f["name"].endswith("_cloud-scan.md")),
                   key=lambda f: f["name"], reverse=True)[:10]
    pulled = []
    for f in scans:
        local = os.path.join(PROJECT_DIR, "history", f["name"])
        if os.path.exists(local):
            continue
        status, blob = _gh(config, "GET",
                           f"/repos/{repo}/contents/history/{f['name']}?ref=main")
        if status == 200 and blob.get("content"):
            text = base64.b64decode(blob["content"]).decode("utf-8", "replace")
            with open(local, "w") as fh:
                fh.write(text)
            pulled.append(f["name"])
    return {"ok": True, "pulled": pulled, "available": len(scans)}


def act_cloud_sync(body):
    config = cfg()
    if not config.get("cloud", {}).get("enabled"):
        return {"error": "Cloud watcher isn't set up yet."}
    try:
        sync_cloud(config)          # searches + schedule; secrets stay manual
    except Exception as e:
        return {"error": f"Sync failed: {e}"}
    return {"ok": True}


def act_ai_selftest(body):
    """End-to-end AI check through this exact server process."""
    info = {"python": sys.executable}
    try:
        import anthropic
        info["anthropic_sdk"] = anthropic.__version__
    except Exception as e:
        info["anthropic_sdk"] = "IMPORT FAILED: " + repr(e)
    from sources import Listing
    listing = Listing(id="selftest", source="ebay",
                      title="Nikon F3 35mm film camera body", price=100.0,
                      url="")
    backend = dealfinder.ai_backend(cfg())
    info["backend"] = f"{backend['provider']}:{backend['model']}"
    verdict = ai_filter.classify([listing], "nikon f3", "", backend)
    info["verdict"] = verdict.get("selftest") or "NO VERDICT - AI path broken"
    return info


def act_hunt_dive(body):
    try:
        check_id = int(body.get("id"))
    except (TypeError, ValueError):
        return {"error": "Missing saved-hunt id."}
    return dealfinder.deep_dive(cfg(), check_id,
                                ai_provider=body.get("ai"))


def act_profile_refine(body):
    query = (body.get("query") or "").strip()
    if not query:
        return {"error": "Search terms are required."}
    draft = dealfinder.variant_profile_draft(
        cfg(), query, body.get("notes", ""),
        feedback=body.get("feedback", ""), prior=body.get("prior"))
    if not draft:
        return {"error": "Couldn't draft a profile - check the AI backend."}
    return {"profile_draft": draft}


def act_dry_preview(body):
    """Free reality check: fetch live titles, apply ONLY the free keyword
    layer (profile rejects + built-in phrases). No AI, no DB writes."""
    query = (body.get("query") or "").strip()
    if not query:
        return {"error": "Search terms are required."}
    config = cfg()
    rejects, _, include = dealfinder.compile_profile(body.get("profile"),
                                                     query)
    search = {"name": query, "query": query,
              "queries": ([query] + include)[:6]}
    if body.get("sources") in (["ebay"], ["craigslist"]):
        search["sources"] = body["sources"]
    ebay_cfg = config.get("ebay", {})
    ebay_token = None
    if ebay_cfg.get("client_id") and ebay_cfg.get("client_secret"):
        try:
            ebay_token = sources.ebay_get_token(
                ebay_cfg["client_id"], ebay_cfg["client_secret"])
        except sources.EbayAuthError as e:
            return {"error": str(e)}
    listings = dealfinder.gather_listings(search, config, ebay_token, pages=2)
    killed, kept = [], []
    for l in listings:
        red = filters.reject_hit(l.title, rejects)
        if red:
            killed.append({"price": sources.total_cost(l), "title": l.title,
                           "url": l.url, "why": red})
            continue
        hit = filters.keyword_category(l, query=query)
        if hit:
            killed.append({"price": sources.total_cost(l), "title": l.title,
                           "url": l.url, "why": hit[1]})
        else:
            kept.append({"price": sources.total_cost(l), "title": l.title,
                         "url": l.url})
    killed.sort(key=lambda x: x["price"])
    kept.sort(key=lambda x: x["price"])
    return {"fetched": len(listings), "killed": len(killed),
            "kept": len(kept), "killed_sample": killed[:12],
            "kept_sample": kept[:12]}


def act_hunt(body):
    query = (body.get("query") or "").strip()
    if not query:
        return {"error": "Search terms are required."}
    mode = (body.get("condition")
            if body.get("condition") in ("parts", "any") else "working")
    return dealfinder.deep_hunt(
        cfg(), query, body.get("notes", ""), mode,
        auction_mode=body.get("auction_mode", "all"),
        auction_max_hours=body.get("auction_max_hours"),
        ai_provider=body.get("ai"))


def _tail(value):
    """Last 4 characters of a secret, for 'configured (…1234)' display."""
    return value[-4:] if value and len(value) >= 8 else ("set" if value else "")


def act_settings(body):
    """Update API keys and app settings from the dashboard. Blank/missing
    fields are left unchanged."""
    config = cfg()
    config.setdefault("ebay", {})
    config.setdefault("anthropic", {})
    config.setdefault("craigslist", {})
    config.setdefault("notifications", {})

    def put(section, key, value):
        if value is not None and str(value).strip():
            section[key] = str(value).strip()

    put(config["ebay"], "client_id", body.get("ebay_client_id"))
    put(config["ebay"], "client_secret", body.get("ebay_client_secret"))
    put(config["ebay"], "marketplace", body.get("marketplace"))
    put(config["anthropic"], "api_key", body.get("anthropic_api_key"))
    if "anthropic_prefer_oauth" in body:
        config["anthropic"]["prefer_oauth"] = bool(
            body.get("anthropic_prefer_oauth"))
    put(config["craigslist"], "site", body.get("craigslist_site"))
    put(config["notifications"], "ntfy_topic", body.get("ntfy_topic"))
    config.setdefault("github", {})
    put(config["github"], "token", body.get("github_token"))
    if body.get("ai_provider") in ("local", "api"):
        config.setdefault("local_ai", {})["enabled"] = \
            body["ai_provider"] == "local"
    if body.get("local_model"):
        config.setdefault("local_ai", {})["model"] = body["local_model"].strip()
    if "local_screen_model" in body:
        value = str(body["local_screen_model"] or "").strip()
        if value:
            config.setdefault("local_ai", {})["screen_model"] = value
        else:
            config.get("local_ai", {}).pop("screen_model", None)
    reinstall = False
    if body.get("runs_per_day"):
        runs = max(1, min(12, int(body["runs_per_day"])))
        reinstall = runs != config.get("runs_per_day", 4)
        config["runs_per_day"] = runs
    raw_times = str(body.get("run_times") or "").strip()
    if raw_times:
        if raw_times.lower() == "auto":
            reinstall = reinstall or config.pop("run_times", None) is not None
        else:
            times = [t for t in re.split(r"[,\s]+", raw_times) if t]
            try:
                dealfinder.parse_run_times(times)
            except ValueError as e:
                return {"error": str(e)}
            reinstall = reinstall or times != config.get("run_times")
            config["run_times"] = times
    save(config)
    if reinstall and os.path.exists(PLIST_PATH):
        act_schedule({"install": True})
    if config.get("cloud", {}).get("enabled"):
        try:
            sync_cloud(config, include_secrets=bool(
                body.get("ebay_client_id") or body.get("ebay_client_secret")
                or body.get("anthropic_api_key") or body.get("ntfy_topic")))
        except Exception as e:
            print(f"[cloud] settings sync failed: {e}")
    return {"ok": True}


def act_scan(body):
    global scan_proc
    if scan_proc is not None and scan_proc.poll() is None:
        return {"error": "A scan is already running."}
    python = os.path.join(PROJECT_DIR, ".venv", "bin", "python3")
    if not os.path.exists(python):
        python = sys.executable
    logf = open(LOG_PATH, "a")
    scan_proc = subprocess.Popen(
        [python, os.path.join(PROJECT_DIR, "dealfinder.py"),
         "--config", CONFIG_PATH, "run"],
        stdout=logf, stderr=logf, cwd=PROJECT_DIR)
    return {"ok": True}


def act_test_notify(body):
    topic = cfg().get("notifications", {}).get("ntfy_topic", "")
    if not topic:
        return {"error": "No ntfy topic configured."}
    notify.send(topic, "DealFinder test",
                "Notifications are working.",
                priority="default", tags="white_check_mark")
    return {"ok": True, "topic": topic}


def act_schedule(body):
    python = os.path.join(PROJECT_DIR, ".venv", "bin", "python3")
    if not os.path.exists(python):
        python = sys.executable
    command = "install-schedule" if body.get("install") else "uninstall-schedule"
    result = subprocess.run(
        [python, os.path.join(PROJECT_DIR, "dealfinder.py"), command],
        capture_output=True, text=True, cwd=PROJECT_DIR)
    out = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return {"error": out or "schedule command failed"}
    return {"ok": True, "message": out}


def act_awake(body):
    """Install/remove a login agent that prevents system sleep on AC power,
    so scheduled scans keep running while the Mac is plugged in."""
    if body.get("enable"):
        with open(AWAKE_PLIST_PATH, "w") as f:
            f.write(AWAKE_PLIST)
        subprocess.run(["launchctl", "unload", AWAKE_PLIST_PATH],
                       capture_output=True)
        result = subprocess.run(["launchctl", "load", AWAKE_PLIST_PATH],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return {"error": "launchctl load failed: " + result.stderr.strip()}
    else:
        subprocess.run(["launchctl", "unload", AWAKE_PLIST_PATH],
                       capture_output=True)
        if os.path.exists(AWAKE_PLIST_PATH):
            os.remove(AWAKE_PLIST_PATH)
    return {"ok": True}


ACTIONS = {
    "/api/add": act_add,
    "/api/remove": act_remove,
    "/api/toggle": act_toggle,
    "/api/set": act_set,
    "/api/price": act_price,
    "/api/profile-refine": act_profile_refine,
    "/api/dry-preview": act_dry_preview,
    "/api/hunt-dive": act_hunt_dive,
    "/api/hunt": act_hunt,
    "/api/ai-selftest": act_ai_selftest,
    "/api/settings": act_settings,
    "/api/scan": act_scan,
    "/api/test-notify": act_test_notify,
    "/api/schedule": act_schedule,
    "/api/awake": act_awake,
    "/api/cloud-setup": act_cloud_setup,
    "/api/cloud-sync": act_cloud_sync,
    "/api/cloud-run": act_cloud_run,
    "/api/cloud-results": act_cloud_results,
}


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, content, content_type="application/json"):
        data = content if isinstance(content, bytes) else \
            json.dumps(content).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            with open(UI_PATH, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/ping":
            self._send(200, {"ok": True})
        elif self.path == "/api/state":
            self._send(200, get_state())
        elif self.path.startswith("/api/price-history"):
            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            config = cfg()
            store = dbmod.Store(config.get("db_path")
                                or os.path.join(PROJECT_DIR, "dealfinder.db"))
            if params.get("id"):
                row = store.get_price_check(int(params["id"][0]))
                if not row:
                    self._send(404, {"error": "not found"})
                    return
                self._send(200, {"ts": row[0], "query": row[1],
                                 "mode": row[2],
                                 "result": json.loads(row[3])})
            else:
                checks = []
                for (pid, ts, query, mode, listed, sold, kept) \
                        in store.list_price_checks():
                    prev = store.prev_price_check(pid, query)
                    checks.append({
                        "id": pid, "ts": ts, "query": query, "mode": mode,
                        "listed": listed, "sold": sold, "kept": kept,
                        "prev_listed": prev[0] if prev else None,
                        "prev_sold": prev[1] if prev else None,
                    })
                self._send(200, {"checks": checks})
        elif self.path == "/trends" or self.path.startswith("/trends?"):
            with open(os.path.join(PROJECT_DIR, "trends.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/sell-advice"):
            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            config = cfg()
            store = dbmod.Store(config.get("db_path")
                                or os.path.join(PROJECT_DIR, "dealfinder.db"))
            row = store.get_price_check(int((params.get("id") or ["0"])[0]))
            if not row:
                self._send(404, {"error": "no such saved check"})
                return
            if row[2] == "hunt":
                self._send(400, {"error": "Sell advice needs a price check, "
                                          "not a hunt - hunt pools are unscreened."})
                return
            selling = config.get("selling", {})
            def num(key, fallback):
                try:
                    return float((params.get(key) or [""])[0])
                except ValueError:
                    return fallback
            advice = analytics.sell_advice(
                json.loads(row[3]),
                fee_pct=num("fee", selling.get("fee_pct",
                                               analytics.DEFAULT_FEE_PCT)),
                fixed_fee=selling.get("fixed_fee", analytics.DEFAULT_FIXED_FEE),
                ship_cost=num("ship", selling.get("ship_cost", 0.0)))
            advice["ts"] = row[0]
            advice["query"] = row[1]
            self._send(200, advice)
        elif self.path == "/api/trends-overview":
            config = cfg()
            store = dbmod.Store(config.get("db_path")
                                or os.path.join(PROJECT_DIR, "dealfinder.db"))
            self._send(200, analytics.overview(store))
        elif self.path.startswith("/api/trends-detail"):
            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            config = cfg()
            store = dbmod.Store(config.get("db_path")
                                or os.path.join(PROJECT_DIR, "dealfinder.db"))
            detail = analytics.item_detail(store,
                                           (params.get("query") or [""])[0],
                                           (params.get("mode") or ["working"])[0])
            if detail is None:
                self._send(404, {"error": "no such tracked item"})
            else:
                self._send(200, detail)
        elif self.path == "/api/trends":
            config = cfg()
            store = dbmod.Store(config.get("db_path")
                                or os.path.join(PROJECT_DIR, "dealfinder.db"))
            self._send(200, {"items": store.trend_items()})
        elif self.path == "/api/log":
            lines = []
            if os.path.exists(LOG_PATH):
                with open(LOG_PATH, errors="replace") as f:
                    lines = f.readlines()[-150:]
            self._send(200, {"log": "".join(lines)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        action = ACTIONS.get(self.path)
        if not action:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "bad JSON"})
            return
        try:
            result = action(body)
        except Exception as e:
            result = {"error": str(e)}
        self._send(200 if "error" not in result else 400, result)

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"DealFinder dashboard: http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
