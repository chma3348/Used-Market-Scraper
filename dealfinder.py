#!/usr/bin/env python3
"""DealFinder - scans eBay + Craigslist for your saved searches, filters out
junk listings (parts/damage/accessories), computes typical prices, and pushes
a phone notification when a listing is priced well below the market.

Commands:
  run [--dry-run]      scan all enabled searches (default command)
  test-notify          send a test push to your ntfy topic
  recent               show deals found in the last 7 days
  install-schedule     install a launchd job to run automatically each day
  uninstall-schedule   remove the launchd job
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone

import ai_filter
import db
import filters
import notify
import sources

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LAUNCHD_LABEL = "com.dealfinder.check"
MAX_NOTIFICATIONS_PER_RUN = 8
MIN_PRICES_FOR_STATS = 6


def load_config(path):
    with open(path) as f:
        config = json.load(f)
    # Environment variables fill in credentials when the config has none -
    # this is how the GitHub cloud runner receives its secrets.
    env = os.environ
    ebay = config.setdefault("ebay", {})
    if not ebay.get("client_id") and env.get("EBAY_CLIENT_ID"):
        ebay["client_id"] = env["EBAY_CLIENT_ID"]
    if not ebay.get("client_secret") and env.get("EBAY_CLIENT_SECRET"):
        ebay["client_secret"] = env["EBAY_CLIENT_SECRET"]
    anth = config.setdefault("anthropic", {})
    if not anth.get("api_key") and env.get("ANTHROPIC_API_KEY"):
        anth["api_key"] = env["ANTHROPIC_API_KEY"]
    notif = config.setdefault("notifications", {})
    if not notif.get("ntfy_topic") and env.get("NTFY_TOPIC"):
        notif["ntfy_topic"] = env["NTFY_TOPIC"]
    return config


def save_config(path, config):
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def find_search(config, name):
    """Find a search by exact or unambiguous partial name (case-insensitive)."""
    searches = config.get("searches", [])
    exact = [s for s in searches if s["name"].lower() == name.lower()]
    if exact:
        return exact[0]
    partial = [s for s in searches if name.lower() in s["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        print(f"'{name}' matches several searches: "
              + ", ".join(s["name"] for s in partial))
    else:
        print(f"No search matching '{name}'. Run: dealfinder.py list")
    sys.exit(1)


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def hours_left(end_iso):
    """Hours until an ISO-8601 end time (eBay auctions), or None."""
    if not end_iso:
        return None
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - datetime.now(timezone.utc)).total_seconds() / 3600


# ------------------------------------------------------------------ run

def ai_key_for(config):
    """The API key to pass to AI calls. None when prefer_oauth is set."""
    anth = config.get("anthropic", {})
    if anth.get("prefer_oauth"):
        return None
    return anth.get("api_key") or None


def ai_backend(config, strong=False):
    """Backend descriptor for AI calls. strong=True picks the heavyweight
    model for verdicts and Deep Hunt (same local model in local mode)."""
    local = config.get("local_ai", {})
    if local.get("enabled"):
        model = local.get("model", "qwen3.6:27b")
        if not strong:
            # Bulk screening runs on the small/fast model when one is set;
            # the big model is saved for verdicts and Deep Hunt.
            model = local.get("screen_model") or model
        return {"provider": "local",
                "url": local.get("url", "http://127.0.0.1:11434"),
                "model": model}
    anth = config.get("anthropic", {})
    return {"provider": "anthropic",
            "api_key": ai_key_for(config),
            "model": (anth.get("hunt_model", "claude-opus-4-8") if strong
                      else anth.get("model", "claude-haiku-4-5"))}


def variant_analysis(config, query, notes=""):
    """Strong-model look at near-variants of the searched item. Returns
    {"same": [...], "ambiguous": [...], "different": [...], "comment"} or
    None when unavailable/unknown."""
    try:
        plan = ai_filter.analyze_variants(query, notes,
                                          ai_backend(config, strong=True))
    except Exception:
        return None
    if plan and not (plan["same"] or plan["ambiguous"] or plan["different"]):
        return None
    return plan


def variant_profile_draft(config, query, notes="", feedback="", prior=None):
    """Strong-model search-profile draft (seller-vocabulary exclusions)."""
    try:
        return ai_filter.draft_profile(query, notes,
                                       ai_backend(config, strong=True),
                                       feedback=feedback, prior=prior)
    except Exception:
        return None


def _expand_family(markers):
    """Cross-product numeric marker families: from ["Zoom 80", "80 VF",
    "Zoom 105"] derive every form x every number ("105 VF", ...). The AI
    drafts each form and each number at least once; this makes coverage
    deterministic instead of hoping it enumerated the full grid."""
    templates, numbers = set(), set()
    for m in markers:
        nums = [t for t in m.split() if t.isdigit()]
        if len(nums) == 1 and 2 <= len(nums[0]) <= 3:
            numbers.add(int(nums[0]))
            templates.add(m.replace(nums[0], "{n}", 1))
    out = list(markers)
    have = {x.lower() for x in out}
    filled = set()
    if len(numbers) >= 2:
        fill = set(numbers)
        # a real numbered series: also fill the numeric range (and a bit
        # beyond), since draws under-enumerate. A marker for a model that
        # doesn't exist ("Zoom 95") matches nothing, so this is free.
        if len(numbers) >= 3 and max(numbers) <= 3 * min(numbers):
            hi = max(numbers) * 2
            fill |= {n for n in range(min(numbers), hi + 1) if n % 5 == 0}
            filled = fill
        for t in templates:
            for n in sorted(fill):
                v = t.replace("{n}", str(n))
                if v.lower() not in have:
                    have.add(v.lower())
                    out.append(v)
    return out, filled


def compile_profile(profile, query=""):
    """(reject_keywords, guidance_note, include_names) from an approved
    profile. Rejects are enforced free at the keyword layer; guidance arms
    the AI stages' affirmative-evidence rule via ONLY-phrasing."""
    profile = profile or {}
    include = [i["name"] for i in profile.get("include", [])
               if i.get("enabled", True) and i.get("name")]
    # every word sellers use for the CORRECT item - a marker built solely
    # from these words would kill wanted listings ("Olympus Infinity" when
    # "Infinity Stylus Epic" is the US name), so such markers are dropped
    pool = set()
    for name in include + [query]:
        pool.update(filters._norm(name).split())

    def marker_ok(m):
        toks = filters._norm(m).split()
        return bool(toks) and not all(t in pool for t in toks)
    rejects = []
    exclude_bits = []
    family_nums = set()
    for e in profile.get("exclude", []):
        if not e.get("enabled", True):
            continue
        markers, filled = _expand_family(
            [m for m in e.get("markers", []) if m])
        family_nums |= filled
        markers = [m for m in markers if marker_ok(m)]
        rejects.extend(markers)
        shown = markers[:10] + (["..."] if len(markers) > 10 else [])
        exclude_bits.append(e["name"] + (
            " (titled: " + ", ".join(shown) + ")" if markers else ""))
    # bare-number wrong variants: sellers title zooms as just "mju II 110",
    # so pair every correct name with every family number. _norm keeps "%",
    # so "mju II 100% working" is safe from the "100" marker.
    for name in ([query] if query else []) + include:
        base = filters._norm(name).strip()
        if not base or base.split()[-1].isdigit():
            continue
        for n in sorted(family_nums):
            v = f"{base} {n}"
            if marker_ok(v):
                rejects.append(v)
    rejects.extend(m for m in profile.get("manual_rejects", []) if m)
    # dedupe, keep order
    seen = set()
    rejects = [r for r in rejects
               if not (r.lower() in seen or seen.add(r.lower()))]
    parts = []
    if include:
        parts.append("ONLY include these variants: " + ", ".join(include))
    if exclude_bits:
        parts.append("exclude: " + "; ".join(exclude_bits))
    if profile.get("guidance", "").strip():
        parts.append(profile["guidance"].strip())
    guidance = ("Search profile - " + ". ".join(parts) + ".") if parts else ""
    return rejects, guidance, include


def compile_variant_resolution(resolution):
    """Turn a clarification answer {"include": [names], "exclude": [names],
    "custom": text} into (extra_queries, guidance_note)."""
    include = [str(n).strip() for n in (resolution or {}).get("include", [])
               if str(n).strip()]
    exclude = [str(n).strip() for n in (resolution or {}).get("exclude", [])
               if str(n).strip()]
    custom = str((resolution or {}).get("custom", "")).strip()
    parts = []
    if include:
        parts.append("also include: " + ", ".join(include))
    if exclude:
        parts.append("do NOT include: " + ", ".join(exclude))
    if custom:
        parts.append(custom)
    guidance = ("Variant guidance - " + "; ".join(parts) + ".") if parts else ""
    return include, guidance


def resolve_variants_cli(config, query, notes):
    """CLI variant clarification. Returns (extra_queries, guidance, info)."""
    plan = variant_analysis(config, query, notes)
    if not plan:
        return [], "", ""
    if not plan["ambiguous"]:
        extra, guidance = compile_variant_resolution({
            "include": [v["name"] for v in plan["same"]],
            "exclude": [v["name"] for v in plan["different"]]})
        return extra, guidance, plan.get("comment", "")
    print(f"  Variant check: {plan.get('comment', '')}")
    auto = [v["name"] for v in plan["same"]]
    if auto:
        print("  auto-including: " + ", ".join(auto))
    options = plan["ambiguous"]
    for i, v in enumerate(options, 1):
        print(f"    {i}. {v['name']} - {v['note']}")
    include, exclude = list(auto), [v["name"] for v in plan["different"]]
    if sys.stdin.isatty():
        raw = input("  Include which numbers (comma-separated, blank=none)? ")
        picked = set()
        for tok in raw.replace(",", " ").split():
            if tok.isdigit() and 1 <= int(tok) <= len(options):
                picked.add(int(tok))
        for i, v in enumerate(options, 1):
            (include if i in picked else exclude).append(v["name"])
        custom = input("  Extra guidance (blank for none)? ").strip()
    else:
        print("  (non-interactive: excluding all ambiguous variants)")
        exclude += [v["name"] for v in options]
        custom = ""
    extra, guidance = compile_variant_resolution(
        {"include": include, "exclude": exclude, "custom": custom})
    return extra, guidance, ""


def expand_for_search(config, query, notes=""):
    """AI query variants (original first). [query] if no backend / error."""
    try:
        return ai_filter.expand_query(query, notes, ai_backend(config))
    except Exception:
        return [query]


def gather_listings(search, config, ebay_token, pages=1, sort=None):
    """Fetch listings for one search from all its enabled sources, running
    every query variant and de-duplicating by listing id."""
    wanted = search.get("sources", ["ebay", "craigslist"])
    queries = search.get("queries") or [search["query"]]
    min_p, max_p = search.get("min_price"), search.get("max_price")
    listings, seen_ids = [], set()

    def merge(found):
        for l in found:
            if l.id not in seen_ids:
                seen_ids.add(l.id)
                listings.append(l)

    if len(queries) > 1:
        log("  queries: " + " | ".join(queries))

    if "ebay" in wanted:
        if config.get("_skip_ebay"):
            log("  ebay: handled by the cloud watcher")
        elif ebay_token:
            before = len(listings)
            for q in queries:
                try:
                    merge(sources.fetch_ebay(
                        ebay_token, q, min_p, max_p,
                        marketplace=config.get("ebay", {}).get("marketplace",
                                                               "EBAY_US"),
                        include_auctions=search.get("include_auctions", False),
                        pages=pages, sort=sort,
                    ))
                except Exception as e:
                    log(f"  ebay '{q}': fetch failed - {e}")
            log(f"  ebay: {len(listings) - before} listings")
        else:
            log("  ebay: skipped (no API keys - see Settings)")

    if "craigslist" in wanted:
        site = config.get("craigslist", {}).get("site", "")
        if site and site != "CHANGE_ME":
            before = len(listings)
            for q in queries:
                try:
                    merge(sources.fetch_craigslist(site, q, min_p, max_p))
                except Exception as e:
                    log(f"  craigslist '{q}': fetch failed - {e}")
            log(f"  craigslist ({site}): {len(listings) - before} listings")
        else:
            log("  craigslist: skipped (set your city in Settings)")

    return listings


# Which listing categories each condition mode keeps.
# Categories: ok / damaged / parts / accessory / unrelated
KEEP_BY_MODE = {
    "working": {"ok"},
    "parts": {"damaged", "parts"},
    "any": {"ok", "damaged", "parts"},
}


DESC_SCREEN_LIMIT = 60  # cheapest N survivors get the description pass


def screen_listings(listings, name, notes, mode, config, ebay_token=None,
                    read_descriptions=False, reject_keywords=None):
    """Triage screen. Stage 1: keyword rules (free). Stage 2: AI title review.
    Stage 3 (read_descriptions=True): fetch and AI-review the full listing
    descriptions of the cheapest survivors - where sellers bury problems.
    Returns (kept, dropped) as (listing, category, reason) tuples."""
    decided = []
    undecided = []
    for listing in listings:
        red = filters.reject_hit(listing.title, reject_keywords)
        if red:
            decided.append((listing, "unrelated", f'red flag "{red}"'))
            continue
        hit = filters.keyword_category(listing, query=name)
        if hit:
            decided.append((listing, hit[0], hit[1]))
        else:
            undecided.append(listing)

    verdicts = ai_filter.classify(undecided, name, notes, ai_backend(config))
    for listing in undecided:
        verdict, why = verdicts.get(listing.id, ("ok", "not screened"))
        decided.append((listing, verdict, why))

    keep = KEEP_BY_MODE[mode]
    kept = [d for d in decided if d[1] in keep]
    dropped = [d for d in decided if d[1] not in keep]

    if read_descriptions and len(kept) >= 15:
        # Early sweep: wrong-variant markers from kept titles, applied BEFORE
        # the expensive description stage so it only reads true candidates.
        strong_early = ai_backend(config, strong=True)
        sample = [l.title for l, _, _ in
                  sorted(kept, key=lambda t: t[0].price)[:50]]
        markers = ai_filter.wrong_variant_markers(name, notes, sample,
                                                  strong_early)
        safe = [m for m in markers
                if 0 < sum(1 for l, _, _ in kept
                           if m.lower() in l.title.lower())
                <= max(1, int(len(kept) * 0.8))]
        if safe:
            lowered = [m.lower() for m in safe]
            new_kept = []
            swept = 0
            for listing, category, why in kept:
                hit = next((safe[i] for i, m in enumerate(lowered)
                            if m in listing.title.lower()), None)
                if hit:
                    dropped.append((listing, "unrelated",
                                    f"wrong variant: matches '{hit}'"))
                    swept += 1
                else:
                    new_kept.append((listing, category, why))
            kept = new_kept
            if swept:
                log(f"  early sweep removed {swept} wrong-variant listing(s) "
                    f"({', '.join(safe)})")

    if read_descriptions and kept:
        # Stage 3: the STRONG model reads full descriptions of the cheapest
        # survivors (the ones that could become deals - expensive listings
        # are rarely hidden junk).
        marketplace = config.get("ebay", {}).get("marketplace", "EBAY_US")
        strong = ai_backend(config, strong=True)
        kept.sort(key=lambda t: t[0].price)
        checked, passthrough = kept[:DESC_SCREEN_LIMIT], kept[DESC_SCREEN_LIMIT:]
        survivors = [l for l, _, _ in checked]
        log(f"  reading {len(survivors)} listing descriptions...")
        sources.fetch_descriptions(survivors, ebay_token, marketplace)
        desc_verdicts = ai_filter.classify_descriptions(
            survivors, name, notes, strong)
        kept = []
        desc_dropped = 0
        for listing, category, why in checked:
            dv = desc_verdicts.get(listing.id)
            if dv is None or dv[0] in keep:
                kept.append((listing, category, why))
            else:
                dropped.append((listing, dv[0], f"description: {dv[1]}"))
                desc_dropped += 1
        if desc_dropped:
            log(f"  descriptions killed {desc_dropped} more")

        # Second chance: the strong model also re-reads the cheapest listings
        # the fast title pass rejected - rescuing bargains a small model
        # wrongly dropped. Keyword rejections are deterministic and skipped.
        second_look = sorted(
            (d for d in dropped
             if not d[2].startswith("title contains")
             and not d[2].startswith("description:")),
            key=lambda t: t[0].price)[:15]
        if second_look:
            rescue_candidates = [l for l, _, _ in second_look]
            sources.fetch_descriptions(rescue_candidates, ebay_token,
                                       marketplace)
            rescue_verdicts = ai_filter.classify_descriptions(
                rescue_candidates, name, notes, strong)
            rescued = 0
            for listing, category, why in second_look:
                rv = rescue_verdicts.get(listing.id)
                if rv and rv[0] in keep:
                    dropped.remove((listing, category, why))
                    kept.append((listing, rv[0], f"rescued on review: {rv[1]}"))
                    rescued += 1
            if rescued:
                log(f"  strong model rescued {rescued} wrongly-rejected "
                    "listing(s)")
        kept.extend(passthrough)
    return kept, dropped


def process_search(search, config, store, ebay_token, dry_run):
    name = search["name"]
    mode = search.get("condition", "working")
    log(f"Search: {name}" + (f" [{mode} condition]" if mode != "working" else ""))
    # Newest-first + two pages: fresh listings get caught the scan after
    # they're posted, and coverage goes deep enough to catch older sleepers.
    listings = gather_listings(search, config, ebay_token,
                               pages=2, sort="newlyListed")
    if not listings:
        return []

    new, watched = [], []
    for l in listings:
        if not store.seen(l.id):
            new.append(l)
        elif store.get_status(l.id) == "watch":
            # auctions we're waiting on - re-check with fresh bid and end time
            watched.append(l)
    log(f"  {len(new)} new, {len(watched)} watched auctions "
        f"(of {len(listings)} total)")

    kept, dropped_items = screen_listings(
        new, name, search.get("notes", ""), mode, config,
        ebay_token=ebay_token, read_descriptions=True,
        reject_keywords=(search.get("profile") or {}).get("reject_keywords")
        or compile_profile(search.get("profile"), search.get("query", ""))[0])
    for listing, category, why in dropped_items:
        store.add(listing, name, "filtered", f"{category}: {why}")
    survivors = []
    for listing, category, why in kept:
        reason = why if category == "ok" else f"{category}: {why}"
        # Auction-only listings never enter price history: a current bid is
        # not a price. They sit in 'watch' until near their end time.
        status = "watch" if listing.extra.get("auction") else "ok"
        store.add(listing, name, status, reason)
        survivors.append(listing)
    log(f"  filtered out {len(dropped_items)}, kept {len(survivors)}")

    # Trend snapshot: the scan already screened these listings, so recording
    # the stats costs nothing extra and turns every scan into a data point.
    scan_prices = sorted(sources.total_cost(l) for l in survivors
                         if not l.extra.get("auction"))
    if len(scan_prices) >= MIN_PRICES_FOR_STATS and not dry_run:
        snap = {
            "query": name, "mode": mode, "fetched": len(listings),
            "kept": len(scan_prices),
            "median": statistics.median(scan_prices),
            "low": scan_prices[0], "high": scan_prices[-1],
            "spreads": {"ebay_listed": {"stats": _stats(scan_prices),
                                        "currency": "$"}},
            "listings": [{"price": sources.total_cost(l), "title": l.title,
                          "url": l.url, "source": l.source}
                         for l in sorted(survivors, key=sources.total_cost)],
        }
        try:
            store.save_price_check(name, mode, snap, method={"source": "scan"})
        except Exception as e:
            log(f"  couldn't record trend snapshot: {e}")

    # Typical price from clean fixed-price listings (this run + history)
    prices = store.recent_prices(name, days=config.get("history_days", 14))
    median = (statistics.median(prices)
              if len(prices) >= MIN_PRICES_FOR_STATS else None)

    alert_under = search.get("alert_under")
    if alert_under is not None:
        threshold = alert_under
        log(f"  price alert: notify at <= ${threshold:.0f}"
            + (f" (typical price ${median:.0f})" if median else ""))
    elif median is not None:
        discount = search.get("deal_discount", config.get("deal_discount", 0.25))
        threshold = median * (1 - discount)
        log(f"  typical price ${median:.0f} (n={len(prices)}), "
            f"deal threshold <= ${threshold:.0f}")
    else:
        log(f"  only {len(prices)} price points - need {MIN_PRICES_FOR_STATS} "
            "before auto-flagging deals (or set a fixed alert: "
            f"set \"{name}\" --alert-under N)")
        return []

    window = search.get("auction_max_hours", config.get("auction_max_hours", 24))
    deals = []
    for listing in survivors + watched:
        total = sources.total_cost(listing)
        if total > threshold:
            continue
        note = ""
        if listing.extra.get("auction"):
            left = hours_left(listing.extra.get("end_date"))
            if left is not None and left > window:
                continue  # too far out - stays watched, re-checked each run
            if left is not None:
                note = (f" - auction ends in {left:.0f}h" if left >= 1
                        else " - auction ends in <1h")
        ship = (listing.extra or {}).get("shipping")
        body = f"${total:.0f}" + (f" (incl ${ship:.0f} ship)" if ship else "")
        if alert_under is not None:
            body += f" - under your ${alert_under:.0f} alert"
            if median:
                body += f" (typical ${median:.0f})"
        else:
            pct = round(100 * (1 - total / median))
            body += f" vs typical ${median:.0f} ({pct}% below)"
        body += f" on {listing.source}{note}"
        if median and total < median * 0.25:
            body += "\nUnusually low - verify it's not a scam."
        store.set_status(listing.id, "deal", f"notified at ${listing.price:.0f}")
        deals.append((listing, f"{listing.title}\n{body}"))
        log(f"  DEAL: ${listing.price:.0f} - {listing.title[:70]}{note}")
    return deals


def cmd_run(config, args):
    if config.get("paused"):
        log("DealFinder is paused - skipping run. Resume with: dealfinder.py on")
        return
    store = db.Store(config.get("db_path") or os.path.join(PROJECT_DIR, "dealfinder.db"))
    searches = [s for s in config.get("searches", []) if s.get("enabled", True)]
    if not searches:
        log("No enabled searches in config.json - add some under \"searches\".")
        return

    if config.get("cloud", {}).get("enabled"):
        # The GitHub watcher owns eBay; local scans cover Craigslist so the
        # two never double-notify the same listing.
        config["_skip_ebay"] = True
        log("eBay is scanned by the cloud watcher - this run covers Craigslist")

    ebay_cfg = config.get("ebay", {})
    ebay_token = None
    if not config.get("_skip_ebay") and ebay_cfg.get("client_id") \
            and ebay_cfg.get("client_secret"):
        try:
            ebay_token = sources.ebay_get_token(
                ebay_cfg["client_id"], ebay_cfg["client_secret"])
        except sources.EbayAuthError as e:
            log(str(e))

    all_deals = []
    for search in searches:
        try:
            all_deals.extend(process_search(search, config, store, ebay_token,
                                            args.dry_run))
        except Exception as e:
            log(f"  search failed: {e}")

    if not all_deals:
        log("No new deals this run.")
        return

    topic = config.get("notifications", {}).get("ntfy_topic", "")
    sent = 0
    for listing, message in all_deals:
        if args.dry_run:
            log(f"[dry-run] would notify: {listing.title[:60]} ${listing.price:.0f}")
            continue
        if sent >= MAX_NOTIFICATIONS_PER_RUN:
            log(f"Notification cap reached ({MAX_NOTIFICATIONS_PER_RUN}); "
                f"{len(all_deals) - sent} more deals logged but not pushed.")
            break
        try:
            notify.send(topic, "Deal found", message, click_url=listing.url)
            store.mark_notified(listing.id)
            sent += 1
        except Exception as e:
            log(f"notification failed: {e}")
    if sent:
        log(f"Sent {sent} notification(s) to ntfy topic '{topic}'.")


# ---------------------------------------------------------- price check

def _stats(prices):
    """Spread stats for a price list, or None if too few points."""
    prices = sorted(p for p in prices if p is not None)
    if len(prices) < 4:
        return None
    q = statistics.quantiles(prices, n=4)
    return {"n": len(prices), "median": statistics.median(prices),
            "p25": q[0], "p75": q[2], "low": prices[0], "high": prices[-1]}


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "good_price": {"type": "number"},
        "great_price": {"type": "number"},
        "note": {"type": "string"},
        "wrong_variant_markers": {
            "type": "array", "items": {"type": "string"}
        },
    },
    "required": ["good_price", "great_price", "note",
                 "wrong_variant_markers"],
    "additionalProperties": False,
}

VERDICT_PROMPT = """A buyer wants: {query}{notes_line}
Condition sought: {mode_label}.

Market data gathered just now:
{market_lines}

Cheapest current clean listings:
{sample}

Advise the buyer:
- good_price: a realistic "that's a good deal, be happy" threshold for a \
clean example - grounded in SOLD prices when available (asking prices run \
high), in the buyer's listed currency
- great_price: a "stop thinking and buy it now" threshold
- note: ONE practical sentence - what drives value for this item, or what to \
double-check before paying (versions/variants, common flaws, what inflates \
or deflates price).
- wrong_variant_markers: short, distinctive title substrings identifying \
WRONG variants/models visible in the sample - these auto-remove matching \
listings, so cover EVERY wrong variant you can see or warned about in your \
note, especially suffix models (zoom versions, different model numbers - \
e.g. "170 VF", "80 VF", "II 110"). Use the shortest substring that is \
unmistakable. NEVER mark cosmetic/date/color editions of the CORRECT model \
(a date-back "QD" of the right camera is still the right camera). Each \
marker must never appear in a correct listing's title. Empty list only if \
the sample is genuinely clean."""


def price_check(config, query, min_price=None, max_price=None, notes="",
                mode="working", source_list=None, auctions=False,
                extra_queries=None, reject_keywords=None):
    """One-off market price check. Touches no DB, sends no notifications.
    Returns a dict used by both the CLI and the web dashboard."""
    search = {"name": query, "query": query,
              "min_price": min_price, "max_price": max_price}
    search["queries"] = expand_for_search(config, query, notes)
    for q in (extra_queries or []):
        if q.lower() not in [x.lower() for x in search["queries"]]:
            search["queries"].append(q)
    search["queries"] = search["queries"][:7]
    if source_list:
        search["sources"] = source_list
    if auctions:
        search["include_auctions"] = True

    ebay_cfg = config.get("ebay", {})
    ebay_token = None
    if ebay_cfg.get("client_id") and ebay_cfg.get("client_secret") \
            and "ebay" in search.get("sources", ["ebay"]):
        try:
            ebay_token = sources.ebay_get_token(
                ebay_cfg["client_id"], ebay_cfg["client_secret"])
        except sources.EbayAuthError as e:
            log(str(e))

    listings = gather_listings(search, config, ebay_token, pages=3)
    kept, dropped = screen_listings(listings, query, notes or "", mode, config,
                                    ebay_token=ebay_token,
                                    read_descriptions=True,
                                    reject_keywords=reject_keywords)
    breakdown = {}
    for _, category, _ in dropped:
        breakdown[category] = breakdown.get(category, 0) + 1

    kept_sorted = sorted(kept, key=lambda t: sources.total_cost(t[0]))
    prices = [sources.total_cost(l) for l, _, _ in kept_sorted]
    ai_on = ai_filter.backend_ready(ai_backend(config))
    result = {
        "query": query, "mode": mode, "queries": search["queries"],
        "ai": "on" if ai_on else "off",
        "unscreened": sum(1 for _, _, why in kept if why == "not screened"),
        "fetched": len(listings), "kept": len(prices), "dropped": breakdown,
        "median": None, "p25": None, "p75": None,
        "low": prices[0] if prices else None,
        "high": prices[-1] if prices else None,
        "listings": [{"price": sources.total_cost(l),
                      "ship": (l.extra or {}).get("shipping"),
                      "title": l.title, "url": l.url,
                      "source": l.source} for l, _, _ in kept_sorted],
        "rejected": [{"price": sources.total_cost(l), "title": l.title,
                      "url": l.url, "source": l.source,
                      "category": category, "reason": why}
                     for l, category, why in
                     sorted(dropped,
                            key=lambda t: sources.total_cost(t[0]))[:300]],
    }
    if len(prices) >= 4:
        result["median"] = statistics.median(prices)
        q = statistics.quantiles(prices, n=4)
        result["p25"], result["p75"] = q[0], q[2]

    # Per-source spreads: eBay listed vs eBay sold vs Craigslist listed
    by_source = {}
    for l, _, _ in kept:
        by_source.setdefault(l.source, []).append(sources.total_cost(l))
    wanted = search.get("sources", ["ebay", "craigslist"])
    spreads = {}
    spreads["ebay_listed"] = {
        "stats": _stats(by_source.get("ebay", [])), "currency": "$",
        "note": None if (ebay_token or "ebay" not in wanted)
                else "needs eBay API keys (Settings)",
    }
    cl_ok = config.get("craigslist", {}).get("site", "") not in ("", "CHANGE_ME")
    spreads["craigslist_listed"] = {
        "stats": _stats(by_source.get("craigslist", [])), "currency": "$",
        "note": None if (cl_ok or "craigslist" not in wanted)
                else "set your Craigslist city (Settings)",
    }
    sold_stats, sold_currency, sold_note = None, "$", None
    cache_path = os.path.join(PROJECT_DIR, "sold_cache.json")
    cache_key = f"{query.lower()}|{mode}"
    if "ebay" in wanted:
        try:
            sold_raw, sold_currency = sources.fetch_ebay_sold(
                query, config.get("ebay", {}).get("marketplace", "EBAY_US"))
            sold_kept, _ = screen_listings(sold_raw, query, notes or "",
                                           mode, config)
            sold_stats = _stats([l.price for l, _, _ in sold_kept])
            if sold_stats is None:
                sold_note = "not enough clean sold listings found"
            else:
                try:
                    cache = json.load(open(cache_path))
                except (OSError, json.JSONDecodeError):
                    cache = {}
                cache[cache_key] = {
                    "stats": sold_stats, "currency": sold_currency,
                    "when": datetime.now().isoformat(timespec="seconds")}
                with open(cache_path, "w") as f:
                    json.dump(cache, f, indent=1)
        except Exception as e:
            sold_note = str(e)
        if sold_stats is None:
            # Fall back to the last successful sold lookup for this item
            try:
                entry = json.load(open(cache_path)).get(cache_key)
            except (OSError, json.JSONDecodeError):
                entry = None
            if entry:
                sold_stats = entry["stats"]
                sold_currency = entry["currency"]
                age = (datetime.now()
                       - datetime.fromisoformat(entry["when"])).total_seconds()
                sold_note = (f"cached comps from "
                             f"{max(1, round(age / 3600))}h ago (eBay "
                             "throttled the live lookup)")
    spreads["ebay_sold"] = {"stats": sold_stats, "currency": sold_currency,
                            "note": sold_note}
    result["spreads"] = spreads

    # AI verdict: a concrete "good deal" number, grounded in the data above
    result["verdict"] = None
    if ai_on and prices:
        market_lines = []
        for spread_key, label in (("ebay_listed", "eBay asking prices"),
                                  ("ebay_sold", "eBay SOLD prices"),
                                  ("craigslist_listed", "Craigslist asking prices")):
            sp = spreads[spread_key]
            st = sp["stats"]
            if st:
                market_lines.append(
                    f"- {label}: usual {sp['currency']}{st['p25']:,.0f}-"
                    f"{sp['currency']}{st['p75']:,.0f}, typical "
                    f"{sp['currency']}{st['median']:,.0f}, full "
                    f"{sp['currency']}{st['low']:,.0f}-"
                    f"{sp['currency']}{st['high']:,.0f} ({st['n']} listings)")
        sample = "\n".join(f"${l['price']:,.0f} - {l['title']}"
                           for l in result["listings"][:15])
        mode_label = {"working": "working condition",
                      "parts": "broken/for-parts (repair donor)",
                      "any": "any condition"}[mode]
        verdict = ai_filter.call_json(
            VERDICT_PROMPT.format(
                query=query,
                notes_line=f"\nBuyer notes: {notes}" if notes else "",
                mode_label=mode_label,
                market_lines="\n".join(market_lines) or "- (only the listing sample below)",
                sample=sample),
            VERDICT_SCHEMA, ai_backend(config, strong=True), max_tokens=2000)
        if verdict:
            result["verdict"] = verdict
            # Enforcement sweep: the verdict names wrong-variant title
            # markers; anything still in the kept list matching one gets
            # culled and stats recomputed. Safety: a marker matching more
            # than half the kept list is treated as a bad marker and ignored.
            markers = [str(m).strip() for m in
                       verdict.get("wrong_variant_markers", [])
                       if len(str(m).strip()) >= 2][:6]
            kept_now = result["listings"]
            safe = []
            for m in markers:
                hits = sum(1 for l in kept_now
                           if m.lower() in l["title"].lower())
                if 0 < hits <= max(1, int(len(kept_now) * 0.8)):
                    safe.append(m)
            if safe:
                lowered = [m.lower() for m in safe]
                still, culled = [], []
                for l in kept_now:
                    hit = next((safe[i] for i, m in enumerate(lowered)
                                if m in l["title"].lower()), None)
                    (culled if hit else still).append((l, hit))
                culled = [(l, h) for l, h in culled]
                if culled:
                    log(f"  verdict swept {len(culled)} wrong-variant "
                        f"listing(s) via markers: {', '.join(safe)}")
                    result["listings"] = [l for l, _ in still]
                    result["kept"] = len(still)
                    result["dropped"]["wrong variant (verdict sweep)"] =                         len(culled)
                    for l, hit in culled:
                        result["rejected"].append({
                            "price": l["price"], "title": l["title"],
                            "url": l["url"], "source": l["source"],
                            "category": "unrelated",
                            "reason": f"verdict sweep: title matches '{hit}'",
                        })
                    result["rejected"].sort(key=lambda x: x["price"])
                    prices2 = sorted(l["price"] for l in result["listings"])
                    result["low"] = prices2[0] if prices2 else None
                    result["high"] = prices2[-1] if prices2 else None
                    result["median"] = result["p25"] = result["p75"] = None
                    if len(prices2) >= 4:
                        result["median"] = statistics.median(prices2)
                        q2 = statistics.quantiles(prices2, n=4)
                        result["p25"], result["p75"] = q2[0], q2[2]
                    for spread_key, src_name in (
                            ("ebay_listed", "ebay"),
                            ("craigslist_listed", "craigslist")):
                        result["spreads"][spread_key]["stats"] = _stats(
                            [l["price"] for l in result["listings"]
                             if l["source"] == src_name])
    if result["fetched"]:
        try:
            store = db.Store(config.get("db_path")
                             or os.path.join(PROJECT_DIR, "dealfinder.db"))
            store.save_price_check(query, mode, result)
        except Exception as e:
            log(f"  could not save price check: {e}")
    return result


def cmd_price(config, args):
    mode = "parts" if args.parts else ("any" if args.any_condition else "working")
    source_list = None
    if args.craigslist_only:
        source_list = ["craigslist"]
    elif args.ebay_only:
        source_list = ["ebay"]

    label = {"working": "working condition",
             "parts": "PARTS/BROKEN only",
             "any": "any condition"}[mode]
    print(f"\nPrice check: {args.query}  ({label})")
    notes = args.notes or ""
    extra, guidance, vinfo = resolve_variants_cli(config, args.query, notes)
    if guidance:
        notes = (notes + " " if notes else "") + guidance
    if vinfo:
        print(f"  i {vinfo}")
    r = price_check(config, args.query, args.min, args.max, notes,
                    mode, source_list, args.auctions, extra_queries=extra)
    if not r["fetched"]:
        print("No listings found.")
        return
    if r["ai"] == "off":
        print("  !! AI screening is OFF (no working AI backend - check "
              "Settings). Results are keyword-filtered only.")
    dropped_desc = ", ".join(f"{n} {c}" for c, n in sorted(r["dropped"].items()))
    print(f"  fetched {r['fetched']}, kept {r['kept']} after screening"
          + (f"  (dropped: {dropped_desc})" if r["dropped"] else ""))
    labels = {"ebay_listed": "eBay listed", "ebay_sold": "eBay sold",
              "craigslist_listed": "Craigslist listed"}
    for key in ("ebay_listed", "ebay_sold", "craigslist_listed"):
        spread = r["spreads"][key]
        stats, cur = spread["stats"], spread["currency"]
        if stats:
            note = f"  ({spread['note']})" if spread["note"] else ""
            print(f"  {labels[key]:<18} {cur}{stats['p25']:,.0f} - "
                  f"{cur}{stats['p75']:,.0f} usual · typical "
                  f"{cur}{stats['median']:,.0f} · {stats['n']} listings{note}")
        else:
            print(f"  {labels[key]:<18} {spread['note'] or 'not enough listings'}")
    if r.get("verdict"):
        v = r["verdict"]
        print(f"  AI verdict: under ${v['good_price']:,.0f} is a good deal - "
              f"under ${v['great_price']:,.0f} jump on it")
        print(f"              {v['note']}")
    if not r["kept"]:
        print("  No active listings matched that condition mode.")
    elif r["median"] is None:
        print("  Too few active listings for overall stats - cheapest below:")
    count = 300 if getattr(args, "all", False) else 10
    if r["kept"]:
        print("  cheapest listings that passed screening"
              + (f" (top {count}):" if count > 10 else ":"))
    for l in r["listings"][:count]:
        print(f"    ${l['price']:>7.0f}  [{l['source']}] {l['title'][:65]}\n"
              f"             {l['url']}")
    if getattr(args, "rejects", False) and r.get("rejected"):
        print(f"  rejected by screening ({len(r['rejected'])}):")
        for l in r["rejected"]:
            print(f"    ${l['price']:>7.0f}  [{l['category']}] "
                  f"{l['title'][:60]}\n             {l['reason']} - {l['url']}")
    saved = any(s["query"].lower() == args.query.lower()
                for s in config.get("searches", []))
    if not saved:
        print(f'\n  Happy with a target price? Start watching with:\n'
              f'    dealfinder.py add "{args.query}" --alert-under <price>')


# ------------------------------------------------------------ deep hunt

HUNT_PLAN_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"], "additionalProperties": False,
}

HUNT_PLAN_PROMPT = """You are planning an aggressive second-hand-marketplace \
hunt for: {query}{notes}

Generate up to 12 search strings that cast the widest net for bargains other \
buyers miss:
- common misspellings and run-together spellings sellers actually type
- alternate, older, or colloquial names, and model numbers
- bundle/lot phrasings ("lot", "bundle", "untested", "estate", "moving sale")
{parts_hint}
Each string must work as-is on eBay/Craigslist search and still plausibly \
surface this item. Do not include the original query."""

HUNT_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "finds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "interest": {"type": "string",
                                 "enum": ["maybe", "strong"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "interest", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["finds"], "additionalProperties": False,
}

HUNT_SCORE_PROMPT = """A buyer wants: {query}{notes}
Typical clean market price: {typical}.

Below are second-hand listings (title AND a description excerpt) pulled from \
deliberately broad searches - most are irrelevant, that is expected. Your job \
is finding hidden value, reading descriptions closely:
- the item itself priced clearly below typical, including mistitled or \
misspelled listings other buyers won't find
- lots/bundles whose contents likely include the item and look underpriced \
for what's in them
- "untested"/vague listings where the price makes the gamble attractive
- auctions still cheap near their end{parts_hint}

Prices shown INCLUDE shipping - judge the all-in cost. Mark "strong" only \
for finds a sharp reseller would click immediately; "maybe" for worth-a-look. \
The buyer wants a SATURATED view - the best 25-30 candidates across the \
whole pool - so 4-6 finds per batch is normal when the pool is rich. Skip \
only the clearly overpriced and the junk; when in doubt at a fair-or-better \
price, include it as "maybe" with the honest value logic. Each reason: one concrete sentence with the value logic. \
Listings are numbered like #7 - set "id" to that number as a string.

Listings (id | price | source | condition | title):
{rows}"""


def _override_provider(config, choice):
    """Per-run AI provider override ("local" / "api") without touching the
    saved setting - used by Deep Hunt and Deep Dive toggles."""
    if choice not in ("local", "api"):
        return config
    cfg2 = dict(config)
    local = dict(cfg2.get("local_ai", {}))
    local["enabled"] = (choice == "local")
    cfg2["local_ai"] = local
    return cfg2


DIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "grade": {"type": "string",
                              "enum": ["strong", "maybe", "drop"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "grade", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"], "additionalProperties": False,
}

DIVE_PROMPT = """A buyer wants: {query}
Typical market: {typical}

These candidates were graded from TITLES ONLY by an earlier hunt pass. Now \
each has its full seller description. Re-judge every one with the new \
evidence - sellers bury problems (fungus, haze, "for parts", missing bits) \
and hide gems (tested film, CLA'd, extras included) in descriptions:
- "strong": description CONFIRMS the value case, no hidden problems
- "maybe": still plausible but the description leaves real questions open
- "drop": description reveals a dealbreaker or the value case collapses
Reason: one concrete sentence citing what the description showed. Prices \
include shipping. Items are numbered like #3 - set "id" to that number.

Candidates (id | price | prior grade | title):
{rows}"""


def deep_dive(config, check_id, ai_provider=None):
    """Second-pass audit of a saved hunt: fetch full descriptions for every
    find (plus the cheapest pool entries the title pass skipped) and have
    the strong model re-grade each with the description evidence."""
    store = db.Store(config.get("db_path")
                     or os.path.join(PROJECT_DIR, "dealfinder.db"))
    row = store.get_price_check(check_id)
    if not row:
        return {"error": "No saved check with that id."}
    ts, query, mode, data = row[0], row[1], row[2], json.loads(row[3])
    if mode != "hunt":
        return {"error": "Deep dive works on saved hunts."}
    config = _override_provider(config, ai_provider)
    backend = ai_backend(config, strong=True)
    if not ai_filter.backend_ready(backend):
        return {"error": "Deep dive needs a working AI backend."}

    ebay_cfg = config.get("ebay", {})
    try:
        token = sources.ebay_get_token(
            ebay_cfg["client_id"], ebay_cfg["client_secret"])
    except Exception as e:
        return {"error": f"eBay auth failed: {e}"}

    finds = data.get("finds", [])
    have = {f.get("url", "").split("?")[0] for f in finds}
    extras = [l for l in (data.get("listings") or [])
              if l.get("url", "").split("?")[0] not in have][:30]
    candidates = ([dict(f, _kind="find") for f in finds]
                  + [dict(l, _kind="pool", interest="unrated",
                          reason="cheap pool entry the title pass skipped")
                     for l in extras])
    log(f"Deep dive on check {check_id}: {len(finds)} finds + "
        f"{len(extras)} pool entries")
    for c in candidates:
        m = re.search(r"/itm/(\d+)", c.get("url", ""))
        c["_desc"] = ""
        if m:
            try:
                c["_desc"] = (sources.fetch_ebay_item_description(
                    token, f"v1|{m.group(1)}|0") or "")[:400]
            except Exception:
                pass

    graded = {}
    for start in range(0, len(candidates), 15):
        batch = candidates[start:start + 15]
        rows = "\n".join(
            f"#{n} | ${c['price']:,.0f} | {c.get('interest', '?')} | "
            f"{c['title']}\n    desc: {c['_desc'] or '(none available)'}"
            for n, c in enumerate(batch, 1))
        out = ai_filter.call_json(
            DIVE_PROMPT.format(query=query,
                               typical=data.get("typical", "unknown"),
                               rows=rows),
            DIVE_SCHEMA, backend, max_tokens=4000)
        for item in (out or {}).get("items", []):
            raw = str(item.get("id", "")).strip().lstrip("#")
            if raw.isdigit() and 1 <= int(raw) <= len(batch):
                graded[id(batch[int(raw) - 1])] = (item["grade"],
                                                   item["reason"])

    new_finds, dropped = [], []
    promoted = 0
    for c in candidates:
        grade_reason = graded.get(id(c))
        kind = c.pop("_kind"); c.pop("_desc", None)
        if grade_reason is None:
            if kind == "find":
                new_finds.append(c)   # ungraded finds keep their spot
            continue
        grade, reason = grade_reason
        if grade == "drop":
            if kind == "find":
                dropped.append({"title": c["title"], "price": c["price"],
                                "reason": reason})
            continue
        c["interest"] = grade
        c["reason"] = reason + " [deep dive]"
        if kind == "pool":
            promoted += 1
            c.setdefault("first_seen", None)
            c.setdefault("auction_hours_left", None)
        new_finds.append(c)
    new_finds.sort(key=lambda r: (r.get("first_seen") is not None,
                                  r.get("interest") != "strong",
                                  r["price"]))
    data["finds"] = new_finds
    data["kept"] = len(new_finds)
    data["dive_dropped"] = dropped
    data["dived"] = True
    store.conn.execute(
        "UPDATE price_checks SET data = ?, kept = ? WHERE id = ?",
        (json.dumps(data), data["kept"], check_id))
    store.conn.commit()
    try:
        db.export_check_file(store.path, ts, query, "hunt", data)
    except Exception:
        pass
    log(f"  dive done: {len(new_finds)} finds "
        f"({promoted} promoted, {len(dropped)} dropped)")
    return {"ok": True, "result": data, "query": query, "ts": ts}


def deep_hunt(config, query, notes="", mode="working",
              auction_mode="all", auction_max_hours=None, ai_provider=None):
    """Aggressive AI hunt: broad query net (auctions included), then a strong
    model reads every title looking for mispriced/mislabeled/bundled value."""
    config = _override_provider(config, ai_provider)
    backend = ai_backend(config, strong=True)
    if not ai_filter.backend_ready(backend):
        return {"error": "Deep Hunt needs a working AI backend - check the "
                         "AI provider in Settings."}
    model = backend["model"]
    parts_plan = ('- broken/for-parts phrasings (the buyer WANTS repairable units)'
                  if mode == "parts" else
                  '- broken/for-parts phrasings ONLY if a cheap fixable unit '
                  'would tempt this buyer')
    parts_score = ('\n- broken units priced like junk that are good repair '
                   'candidates' if mode != "working" else "")

    log(f"Deep hunt: {query} (model {model})")
    plan = ai_filter.call_json(
        HUNT_PLAN_PROMPT.format(
            query=query, parts_hint=parts_plan,
            notes=f"\nBuyer notes: {notes}" if notes else ""),
        HUNT_PLAN_SCHEMA, backend, max_tokens=1500) or {}
    queries, seen_q = [query], {query.lower()}
    for q in plan.get("queries", []):
        q = str(q).strip()
        if q and q.lower() not in seen_q:
            seen_q.add(q.lower())
            queries.append(q)
    queries = queries[:12]

    search = {"name": query, "query": query, "queries": queries,
              "include_auctions": True}
    ebay_cfg = config.get("ebay", {})
    ebay_token = None
    if ebay_cfg.get("client_id") and ebay_cfg.get("client_secret"):
        try:
            ebay_token = sources.ebay_get_token(
                ebay_cfg["client_id"], ebay_cfg["client_secret"])
        except sources.EbayAuthError as e:
            log(str(e))
    listings = gather_listings(search, config, ebay_token)
    if auction_mode == "none":
        listings = [l for l in listings if not l.extra.get("auction")]
    elif auction_mode == "ending":
        window = float(auction_max_hours or 24)

        def _close_enough(l):
            if not l.extra.get("auction"):
                return True
            left = hours_left(l.extra.get("end_date"))
            return left is not None and left <= window
        listings = [l for l in listings if _close_enough(l)]
    listings = listings[:350]
    if not listings:
        return {"error": "No listings fetched - add eBay keys or a Craigslist "
                         "city in Settings first."}

    # Baseline typical price from a normal title-only screen (cheap model)
    kept, _ = screen_listings(listings, query, notes, "working", config)
    base = _stats([l.price for l, _, _ in kept])
    typical = f"${base['median']:,.0f}" if base else "unknown"

    # Full-text: every listing's description gets read, including ones a
    # normal screen would discard - that's where the sneaky finds hide.
    log(f"  fetching {len(listings)} full descriptions...")
    sources.fetch_descriptions(
        listings, ebay_token,
        config.get("ebay", {}).get("marketplace", "EBAY_US"))

    finds = {}
    hunt_batch = 25 if backend["provider"] == "local" else 50
    for start in range(0, len(listings), hunt_batch):
        batch = listings[start:start + hunt_batch]
        rows = "\n".join(
            f"#{n} | ${sources.total_cost(l):,.0f} | {l.source} | "
            f"{l.condition or '-'} | "
            f"{l.title}\n    desc: {(l.extra.get('description') or '-')[:250]}"
            for n, l in enumerate(batch, 1))
        log(f"  hunt-scoring listings {start + 1}-{start + len(batch)} "
            f"of {len(listings)}")
        out = ai_filter.call_json(
            HUNT_SCORE_PROMPT.format(
                query=query, typical=typical, rows=rows,
                parts_hint=parts_score,
                notes=f"\nBuyer notes: {notes}" if notes else ""),
            HUNT_SCORE_SCHEMA, backend, max_tokens=8000)
        for f in (out or {}).get("finds", []):
            raw = str(f.get("id", "")).strip().lstrip("#")
            try:
                idx = int(float(raw)) - 1
            except ValueError:
                continue
            if 0 <= idx < len(batch):
                finds[batch[idx].id] = (f["interest"], f["reason"])

    by_id = {l.id: l for l in listings}
    results = []
    for lid, (interest, reason) in finds.items():
        listing = by_id.get(lid)
        if not listing:
            continue
        left = (hours_left(listing.extra.get("end_date"))
                if listing.extra.get("auction") else None)
        results.append({
            "id": listing.id, "interest": interest, "reason": reason,
            "price": sources.total_cost(listing),
            "ship": (listing.extra or {}).get("shipping"),
            "title": listing.title,
            "url": listing.url, "source": listing.source,
            "auction_hours_left": round(left, 1) if left is not None else None,
        })
    store = None
    try:
        store = db.Store(config.get("db_path")
                         or os.path.join(PROJECT_DIR, "dealfinder.db"))
        seen = store.hunt_seen_map([r["id"] for r in results])
        for r in results:
            r["first_seen"] = seen.get(r["id"])   # None = new this hunt
        store.hunt_mark_seen([r["id"] for r in results], query)
    except Exception as e:
        log(f"  hunt-seen memory unavailable: {e}")
        for r in results:
            r["first_seen"] = None
    # new finds first, then strongest, then cheapest
    results.sort(key=lambda r: (r["first_seen"] is not None,
                                r["interest"] != "strong", r["price"]))
    log(f"  hunt done: {len(results)} finds "
        f"({sum(1 for r in results if not r['first_seen'])} new)")
    pool = sorted(listings, key=sources.total_cost)[:400]
    result = {"queries": queries, "fetched": len(listings),
              "typical": typical, "finds": results,
              "condition": mode, "kept": len(results),
              "listings": [{"price": sources.total_cost(l),
                            "ship": (l.extra or {}).get("shipping"),
                            "title": l.title, "url": l.url,
                            "source": l.source} for l in pool]}
    try:
        if store:
            store.save_price_check(query, "hunt", result)
    except Exception as e:
        log(f"  couldn't save hunt to history: {e}")
    return result


def cmd_hunt(config, args):
    mode = "parts" if args.parts else ("any" if args.any_condition else "working")
    r = deep_hunt(config, args.query, args.notes or "", mode)
    if r.get("error"):
        print(r["error"])
        sys.exit(1)
    print(f"\nDeep hunt: {args.query}  (typical {r['typical']}, "
          f"{r['fetched']} listings examined)")
    print("  queries used: " + " | ".join(r["queries"]))
    if not r["finds"]:
        print("  Nothing exciting out there right now - try again tomorrow.")
        return
    for f in r["finds"]:
        tag = "STRONG" if f["interest"] == "strong" else "maybe "
        auction = (f" - auction ends in {f['auction_hours_left']:.0f}h"
                   if f.get("auction_hours_left") is not None else "")
        print(f"  [{tag}] ${f['price']:,.0f} [{f['source']}] "
              f"{f['title'][:70]}{auction}\n"
              f"           {f['reason']}\n"
              f"           {f['url']}")


# --------------------------------------------------------- other commands

def cmd_test_notify(config, args):
    topic = config.get("notifications", {}).get("ntfy_topic", "")
    if not topic:
        print("No notifications.ntfy_topic set in config.json")
        sys.exit(1)
    notify.send(
        topic, "DealFinder test",
        "Notifications are working. You'll get a push like this when a deal is found.",
        priority="default", tags="white_check_mark",
    )
    print(f"Test notification sent to https://ntfy.sh/{topic}")
    print("If you didn't get a push: install the ntfy app and subscribe to "
          f"the topic '{topic}'.")


def cmd_recent(config, args):
    store = db.Store(config.get("db_path") or os.path.join(PROJECT_DIR, "dealfinder.db"))
    rows = store.recent_deals(days=7)
    if not rows:
        print("No deals recorded in the last 7 days.")
        return
    for first_seen, search, price, title, url in rows:
        print(f"{first_seen}  [{search}] ${price:.0f}  {title}\n    {url}")


def _plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")


def parse_run_times(times):
    """['8:00', '13:30'] -> sorted [(8, 0), (13, 30)]. Raises ValueError."""
    parsed = set()
    for t in times:
        m = re.match(r"^(\d{1,2}):(\d{2})$", str(t).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(
                f"Bad time '{t}' - use 24h HH:MM, e.g. 8:00 or 21:30")
        parsed.add((int(m.group(1)), int(m.group(2))))
    return sorted(parsed)


def cmd_install_schedule(config, args):
    if config.get("run_times"):
        try:
            entries = parse_run_times(config["run_times"])
        except ValueError as e:
            print(e)
            sys.exit(1)
    else:
        runs = max(1, min(12, int(config.get("runs_per_day", 4))))
        if runs == 1:
            hours = [9]
        else:
            hours = sorted({round(9 + i * (12 / (runs - 1)))
                            for i in range(runs)})
        entries = [(h, 0) for h in hours]
    python = os.path.join(PROJECT_DIR, ".venv", "bin", "python3")
    if not os.path.exists(python):
        python = sys.executable
    script = os.path.join(PROJECT_DIR, "dealfinder.py")
    logfile = os.path.join(PROJECT_DIR, "dealfinder.log")

    intervals = "\n".join(
        f"    <dict><key>Hour</key><integer>{h}</integer>"
        f"<key>Minute</key><integer>{m}</integer></dict>"
        for h, m in entries
    )
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{PROJECT_DIR}</string>
  <key>StartCalendarInterval</key>
  <array>
{intervals}
  </array>
  <key>StandardOutPath</key><string>{logfile}</string>
  <key>StandardErrorPath</key><string>{logfile}</string>
</dict>
</plist>
"""
    path = _plist_path()
    with open(path, "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    result = subprocess.run(["launchctl", "load", path], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr.strip()}")
        sys.exit(1)
    print(f"Scheduled {len(entries)} run(s)/day at "
          + ", ".join(f"{h}:{m:02d}" for h, m in entries)
          + f" (only while your Mac is awake).\nLogs: {logfile}")


def cmd_uninstall_schedule(config, args):
    path = _plist_path()
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    if os.path.exists(path):
        os.remove(path)
        print("Schedule removed.")
    else:
        print("No schedule was installed.")


def cmd_add(config, args):
    name = args.name or args.query
    if any(s["name"].lower() == name.lower() for s in config.get("searches", [])):
        print(f"A search named '{name}' already exists.")
        sys.exit(1)
    search = {"name": name, "query": args.query, "enabled": True}
    if args.min is not None:
        search["min_price"] = args.min
    if args.max is not None:
        search["max_price"] = args.max
    if args.notes:
        search["notes"] = args.notes
    if args.craigslist_only:
        search["sources"] = ["craigslist"]
    elif args.ebay_only:
        search["sources"] = ["ebay"]
    if args.auctions:
        search["include_auctions"] = True
    if args.parts:
        search["condition"] = "parts"
    elif args.any_condition:
        search["condition"] = "any"
    if args.alert_under is not None:
        search["alert_under"] = args.alert_under
    if args.auction_window is not None:
        search["auction_max_hours"] = args.auction_window
    extra, guidance, vinfo = resolve_variants_cli(
        config, search["query"], search.get("notes", ""))
    if guidance:
        search["notes"] = ((search.get("notes", "") + " ")
                           if search.get("notes") else "") + guidance
    if vinfo:
        print(f"  i {vinfo}")
    variants = expand_for_search(config, search["query"], search.get("notes", ""))
    for q in extra:
        if q.lower() not in [x.lower() for x in variants]:
            variants.append(q)
    variants = variants[:7]
    if len(variants) > 1:
        search["queries"] = variants
        print("AI will also search: " + " | ".join(variants[1:]))
    config.setdefault("searches", []).append(search)
    save_config(args.config, config)
    mode = search.get("condition", "working")
    extra = "" if mode == "working" else f" ({mode} condition)"
    print(f"Added search '{name}'{extra}. It will be included in the next run.")


def cmd_set(config, args):
    """Change settings on an existing search without re-adding it."""
    search = find_search(config, args.name)
    changed = []
    if args.clear_alert:
        search.pop("alert_under", None)
        changed.append("alert removed (back to auto 25%-below-typical rule)")
    elif args.alert_under is not None:
        search["alert_under"] = args.alert_under
        changed.append(f"notify at <= ${args.alert_under:g}")
    if args.min is not None:
        search["min_price"] = args.min
        changed.append(f"min ${args.min:g}")
    if args.max is not None:
        search["max_price"] = args.max
        changed.append(f"max ${args.max:g}")
    if args.notes is not None:
        search["notes"] = args.notes
        changed.append("notes updated")
    if args.discount is not None:
        search["deal_discount"] = args.discount
        changed.append(f"deal threshold {args.discount:.0%} below typical")
    if args.auction_window is not None:
        search["auction_max_hours"] = args.auction_window
        changed.append(f"auction window {args.auction_window:g}h")
    if not changed:
        print("Nothing to change. Options: --alert-under N, --clear-alert, "
              "--min N, --max N, --notes \"...\", --discount 0.3, "
              "--auction-window HOURS")
        return
    save_config(args.config, config)
    print(f"Updated '{search['name']}': " + "; ".join(changed))


def cmd_remove(config, args):
    search = find_search(config, args.name)
    config["searches"].remove(search)
    save_config(args.config, config)
    print(f"Removed search '{search['name']}'.")


def cmd_list(config, args):
    searches = config.get("searches", [])
    if not searches:
        print("No searches configured. Add one with: dealfinder.py add \"<query>\"")
        return
    state = "PAUSED (run 'on' to resume)" if config.get("paused") else "active"
    print(f"DealFinder is {state}. Searches:")
    for s in searches:
        flag = "on " if s.get("enabled", True) else "OFF"
        lo, hi = s.get("min_price"), s.get("max_price")
        price = ""
        if lo is not None or hi is not None:
            price = f"  ${lo or 0:g}-{('%g' % hi) if hi is not None else 'any'}"
        srcs = "+".join(s.get("sources", ["ebay", "craigslist"]))
        mode = s.get("condition", "working")
        cond = "" if mode == "working" else f", {mode.upper()}"
        extra_q = (f", +{len(s['queries']) - 1} AI queries"
                   if len(s.get("queries", [])) > 1 else "")
        alert = (f"  ALERT <= ${s['alert_under']:g}"
                 if s.get("alert_under") is not None else "")
        print(f"  [{flag}] {s['name']}{price}  ({srcs}{cond}{extra_q}){alert}")


def cmd_on(config, args):
    if args.name:
        search = find_search(config, args.name)
        search["enabled"] = True
        print(f"Search '{search['name']}' enabled.")
    else:
        config["paused"] = False
        print("DealFinder resumed - scheduled runs are active again.")
    save_config(args.config, config)


def cmd_off(config, args):
    if args.name:
        search = find_search(config, args.name)
        search["enabled"] = False
        print(f"Search '{search['name']}' disabled.")
    else:
        config["paused"] = True
        print("DealFinder paused - scheduled runs will do nothing until you run: on")
    save_config(args.config, config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",
                        default=os.path.join(PROJECT_DIR, "config.json"))
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="scan all enabled searches")
    p_run.add_argument("--dry-run", action="store_true",
                       help="scan and report, but send no notifications")

    p_add = sub.add_parser("add", help="add a search")
    p_add.add_argument("query", help='search terms, e.g. "nintendo switch oled"')
    p_add.add_argument("--name", help="label (defaults to the query)")
    p_add.add_argument("--min", type=float, help="minimum price")
    p_add.add_argument("--max", type=float, help="maximum price")
    p_add.add_argument("--notes", help='guidance for the AI screen, e.g. "must include dock"')
    p_add.add_argument("--craigslist-only", action="store_true")
    p_add.add_argument("--ebay-only", action="store_true")
    p_add.add_argument("--auctions", action="store_true",
                       help="include eBay auctions (off by default)")
    p_add.add_argument("--parts", action="store_true",
                       help="hunt broken/for-parts listings only (repair donors)")
    p_add.add_argument("--any-condition", action="store_true",
                       help="keep both working and broken listings")
    p_add.add_argument("--alert-under", type=float, metavar="PRICE",
                       help="notify at or under this price (instead of the "
                            "auto 25%%-below-typical rule)")
    p_add.add_argument("--auction-window", type=float, metavar="HOURS",
                       help="only alert on auctions ending within this many "
                            "hours (default 24)")

    p_set = sub.add_parser("set", help="change settings on an existing search")
    p_set.add_argument("name")
    p_set.add_argument("--alert-under", type=float, metavar="PRICE")
    p_set.add_argument("--clear-alert", action="store_true",
                       help="remove the fixed alert, back to the auto rule")
    p_set.add_argument("--min", type=float)
    p_set.add_argument("--max", type=float)
    p_set.add_argument("--notes")
    p_set.add_argument("--discount", type=float,
                       help="auto-rule threshold, e.g. 0.3 = 30%% below typical")
    p_set.add_argument("--auction-window", type=float, metavar="HOURS")

    p_price = sub.add_parser(
        "price", help="one-off market price check (no saving, no notifications)")
    p_price.add_argument("query")
    p_price.add_argument("--min", type=float)
    p_price.add_argument("--max", type=float)
    p_price.add_argument("--notes", help="guidance for the AI screen")
    p_price.add_argument("--craigslist-only", action="store_true")
    p_price.add_argument("--ebay-only", action="store_true")
    p_price.add_argument("--auctions", action="store_true")
    p_price.add_argument("--parts", action="store_true",
                         help="price broken/for-parts listings instead")
    p_price.add_argument("--any-condition", action="store_true")
    p_price.add_argument("--all", action="store_true",
                         help="print up to 300 listings instead of the top 10")
    p_price.add_argument("--rejects", action="store_true",
                         help="also print what screening rejected, and why")

    p_hunt = sub.add_parser(
        "hunt", help="deep AI hunt for hidden value (misspellings, lots, "
                     "auctions) - takes minutes, uses ~$0.25 of API credit")
    p_hunt.add_argument("query")
    p_hunt.add_argument("--notes", help="guidance, e.g. \"want the 512GB model\"")
    p_hunt.add_argument("--parts", action="store_true")
    p_hunt.add_argument("--any-condition", action="store_true")

    p_remove = sub.add_parser("remove", help="delete a search")
    p_remove.add_argument("name")

    sub.add_parser("list", help="show all searches and on/off state")

    p_on = sub.add_parser("on", help="resume DealFinder (or enable one search)")
    p_on.add_argument("name", nargs="?")
    p_off = sub.add_parser("off", help="pause DealFinder (or disable one search)")
    p_off.add_argument("name", nargs="?")

    sub.add_parser("test-notify", help="send a test push")
    sub.add_parser("recent", help="deals found in the last 7 days")
    sub.add_parser("install-schedule", help="run automatically each day")
    sub.add_parser("uninstall-schedule", help="remove the automatic schedule")

    args = parser.parse_args()
    if args.command is None:
        args.command = "run"
        args.dry_run = False

    config = load_config(args.config)
    {
        "run": cmd_run,
        "price": cmd_price,
        "hunt": cmd_hunt,
        "add": cmd_add,
        "set": cmd_set,
        "remove": cmd_remove,
        "list": cmd_list,
        "on": cmd_on,
        "off": cmd_off,
        "test-notify": cmd_test_notify,
        "recent": cmd_recent,
        "install-schedule": cmd_install_schedule,
        "uninstall-schedule": cmd_uninstall_schedule,
    }[args.command](config, args)


if __name__ == "__main__":
    main()
