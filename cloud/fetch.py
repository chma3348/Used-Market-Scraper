#!/usr/bin/env python3
"""Phase 1 of the scheduled scan: fetch candidate listings. NO AI, NO COST.

Runs on the GitHub Actions runner with only the eBay/Craigslist credentials.
Everything expensive is deliberately left to phase 2, where Claude Code does
the screening against the Claude subscription instead of API billing.

Writes cloud/work/candidates.json for Claude to read.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import dealfinder          # noqa: E402  (compile_profile only - no AI calls)
import filters             # noqa: E402
import sources             # noqa: E402

WORK = os.path.join(HERE, "work")
SEEN_PATH = os.path.join(HERE, "seen.json")
# Each search sends at most this many candidates to the screening step. The
# cheapest survive: a scan hunts for underpriced listings, so the expensive
# tail is noise that would only inflate the prompt.
PER_SEARCH_CAP = 60


def load_json(path, fallback):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def main():
    searches_cfg = load_json(os.path.join(HERE, "searches.json"), {})
    searches = [s for s in searches_cfg.get("searches", [])
                if s.get("enabled", True)]
    if searches_cfg.get("paused"):
        print("[fetch] config is paused - nothing to do")
        searches = []
    seen = set(load_json(SEEN_PATH, {}).get("ids", []))

    client_id = os.environ.get("EBAY_CLIENT_ID", "")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "")
    token = None
    if client_id and client_secret:
        try:
            token = sources.ebay_get_token(client_id, client_secret)
        except Exception as e:
            print(f"[fetch] eBay auth failed: {e}")
    else:
        print("[fetch] no eBay credentials in env - eBay skipped")

    cl_site = (searches_cfg.get("craigslist") or {}).get("site", "")
    if cl_site in ("", "CHANGE_ME"):
        cl_site = ""

    out = []
    for search in searches:
        name = search.get("name") or search.get("query", "")
        query = search.get("query", name)
        profile = search.get("profile") or {}
        rejects = (profile.get("reject_keywords")
                   or dealfinder.compile_profile(profile, query)[0])

        listings = []
        if token:
            queries = search.get("queries") or [query]
            for q in queries[:6]:
                try:
                    listings += sources.fetch_ebay(
                        token, q, search.get("min_price"),
                        search.get("max_price"),
                        marketplace=searches_cfg.get("ebay", {}).get(
                            "marketplace", "EBAY_US"),
                        include_auctions=bool(search.get("include_auctions")),
                        limit=200, pages=2, sort="newlyListed")
                except Exception as e:
                    print(f"[fetch] {name}: eBay '{q}' failed: {e}")
        if cl_site:
            try:
                listings += sources.fetch_craigslist(
                    cl_site, query, search.get("min_price"),
                    search.get("max_price"))
            except Exception as e:
                print(f"[fetch] {name}: craigslist failed: {e}")

        # dedupe, drop anything already notified on, then the free layers
        uniq, ids = [], set()
        for l in listings:
            if l.id in ids or l.id in seen:
                continue
            ids.add(l.id)
            uniq.append(l)

        kept, killed = [], 0
        for l in uniq:
            if filters.reject_hit(l.title, rejects):
                killed += 1
                continue
            mode = search.get("condition", "working")
            hit = filters.keyword_category(l, query=query)
            if hit and ((mode == "working" and hit[0] != "ok")
                        or (mode == "parts" and hit[0] in ("accessory",))):
                killed += 1
                continue
            kept.append(l)

        kept.sort(key=sources.total_cost)
        kept = kept[:PER_SEARCH_CAP]
        out.append({
            "name": name,
            "query": query,
            "condition": search.get("condition", "working"),
            "notes": search.get("notes", ""),
            "alert_under": search.get("alert_under"),
            "auction_max_hours": search.get(
                "auction_max_hours", searches_cfg.get("auction_max_hours", 24)),
            "fetched": len(uniq),
            "free_killed": killed,
            "listings": [{
                "i": i,
                "id": l.id,
                "price": round(sources.total_cost(l), 2),
                "ship": (l.extra or {}).get("shipping"),
                "title": l.title,
                "url": l.url,
                "source": l.source,
                "condition": l.condition or "",
                "auction": bool((l.extra or {}).get("auction")),
                "ends": (l.extra or {}).get("end_date") or "",
            } for i, l in enumerate(kept)],
        })
        print(f"[fetch] {name}: {len(uniq)} new, {killed} free-killed, "
              f"{len(kept)} to screen")

    os.makedirs(WORK, exist_ok=True)
    with open(os.path.join(WORK, "candidates.json"), "w") as f:
        json.dump({"searches": out}, f, indent=1)
    total = sum(len(s["listings"]) for s in out)
    print(f"[fetch] wrote {total} candidates across {len(out)} searches")
    # Phase 2 is skipped entirely when there is nothing to look at.
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"candidates={total}\n")


if __name__ == "__main__":
    main()
