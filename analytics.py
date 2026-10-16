"""Analytics layer for the Trends page.

Every number here is derived from saved price checks - nothing is fetched or
re-screened. Each check stored its full kept-listing array, so distributions,
deal floors and junk breakdowns can all be recomputed after the fact.

Honesty rules baked in:
  - hunts never contribute price points (their pools are unscreened)
  - series spanning a methodology change are flagged, not silently trended
  - anything computed from < MIN_POINTS listings is reported with its n so a
    thin sample can't masquerade as a market
"""

import json
import statistics

import db as dbmod

FLOOR_N = 5          # "deal floor" = mean of the N cheapest kept listings
HIST_BUCKETS = 14


def _checks(store):
    return store.conn.execute(
        "SELECT id, ts, query, mode, listed_median, sold_median, kept, data "
        "FROM price_checks ORDER BY ts").fetchall()


def _blob(data):
    try:
        return json.loads(data) if data else {}
    except Exception:
        return {}


def _point(pid, ts, listed, sold, kept, blob):
    """One price check reduced to a comparable datapoint."""
    listings = blob.get("listings") or []
    prices = sorted(l["price"] for l in listings if l.get("price") is not None)
    method = blob.get("method") or {}
    ships = [l.get("ship") for l in listings if l.get("ship")]
    by_source = {}
    for l in listings:
        by_source[l.get("source", "?")] = by_source.get(l.get("source", "?"), 0) + 1
    verdict = blob.get("verdict") or {}
    return {
        "id": pid,
        "ts": ts,
        "median": listed,
        "sold": sold,
        "kept": kept,
        "fetched": blob.get("fetched"),
        "p25": blob.get("p25"),
        "p75": blob.get("p75"),
        "low": prices[0] if prices else blob.get("low"),
        "high": prices[-1] if prices else blob.get("high"),
        # what a buyer can actually act on: the cheapest end of the market
        "floor": (round(statistics.mean(prices[:FLOOR_N]), 2)
                  if len(prices) >= 2 else (prices[0] if prices else None)),
        "floor_n": min(FLOOR_N, len(prices)),
        "dropped": blob.get("dropped") or {},
        "by_source": by_source,
        "ship_median": round(statistics.median(ships), 2) if ships else None,
        "ship_share": (round(100 * len(ships) / len(listings))
                       if listings else None),
        "good_price": verdict.get("good_price"),
        "great_price": verdict.get("great_price"),
        "source": method.get("source", "manual"),
        "shipping_inclusive": method.get(
            "shipping_inclusive", ts > dbmod.METHOD_CHANGES[1][0]),
        "n_listings": len(prices),
    }


def overview(store):
    """Headline numbers across everything ever searched."""
    checks = _checks(store)
    items, hunts = {}, []
    listings_screened = fetched_total = 0
    junk = {}
    for pid, ts, query, mode, listed, sold, kept, data in checks:
        blob = _blob(data)
        fetched_total += blob.get("fetched") or 0
        if mode == "hunt":
            hunts.append({"id": pid, "ts": ts, "query": query,
                          "finds": kept or 0})
            continue
        listings_screened += kept or 0
        for cat, n in (blob.get("dropped") or {}).items():
            junk[cat] = junk.get(cat, 0) + n
        key = f"{query.lower()}|{mode or 'working'}"
        items.setdefault(key, 0)
        items[key] += 1
    return {
        "items_tracked": len(items),
        "checks": sum(items.values()),
        "hunts": len(hunts),
        "finds": sum(h["finds"] for h in hunts),
        "fetched_total": fetched_total,
        "listings_kept": listings_screened,
        "junk": junk,
        "junk_total": sum(junk.values()),
        "recent_hunts": hunts[-6:],
    }


def item_detail(store, query, mode):
    """Full series + distribution for one tracked item."""
    rows = [r for r in _checks(store)
            if r[2].lower() == query.lower() and (r[3] or "working") == mode
            and r[3] != "hunt"]
    if not rows:
        return None
    points = [_point(r[0], r[1], r[4], r[5], r[6], _blob(r[7])) for r in rows]
    hunts = [{"id": r[0], "ts": r[1], "finds": r[6]} for r in _checks(store)
             if r[2].lower() == query.lower() and r[3] == "hunt"]

    # distribution of the most recent check that actually carried listings
    hist, hist_from = [], None
    for pid, ts, q, m, listed, sold, kept, data in reversed(rows):
        prices = sorted(l["price"] for l in (_blob(data).get("listings") or [])
                        if l.get("price") is not None)
        if len(prices) >= 8:
            lo, hi = prices[0], prices[-1]
            span = (hi - lo) or 1
            width = span / HIST_BUCKETS
            buckets = [0] * HIST_BUCKETS
            for p in prices:
                idx = min(HIST_BUCKETS - 1, int((p - lo) / width))
                buckets[idx] += 1
            hist = [{"from": round(lo + i * width),
                     "to": round(lo + (i + 1) * width),
                     "n": n} for i, n in enumerate(buckets)]
            hist_from = {"id": pid, "ts": ts, "n": len(prices)}
            break

    priced = [p for p in points if p["median"] is not None]
    breaks = []
    for a, b in zip(priced, priced[1:]):
        for label in dbmod.method_changes_between(a["ts"], b["ts"]):
            breaks.append({"after": a["ts"], "before": b["ts"], "label": label})
    return {
        "query": query, "mode": mode,
        "points": points, "hunts": hunts,
        "hist": hist, "hist_from": hist_from,
        "breaks": breaks,
        "latest": priced[-1] if priced else None,
        "first": priced[0] if priced else None,
        "change_pct": (round(100 * (priced[-1]["median"] / priced[0]["median"] - 1))
                       if len(priced) > 1 and priced[0]["median"] else None),
        "floor_change_pct": (
            round(100 * (priced[-1]["floor"] / priced[0]["floor"] - 1))
            if len(priced) > 1 and priced[0].get("floor")
            and priced[-1].get("floor") else None),
    }


# ----------------------------------------------------------- selling side

# eBay's standard final-value fee for cameras/photo, plus the per-order fee.
# Override in config.json: {"selling": {"fee_pct": 13.25, "fixed_fee": 0.40}}
DEFAULT_FEE_PCT = 13.25
DEFAULT_FIXED_FEE = 0.40


def sell_advice(result, fee_pct=DEFAULT_FEE_PCT, fixed_fee=DEFAULT_FIXED_FEE,
                ship_cost=0.0):
    """What to LIST at, derived from a saved price check.

    The buy side asks "is this cheap?"; the sell side has to answer two
    different questions: what will actually sell, and what do I keep after
    fees. Both are computable from the check we already stored - every kept
    listing in it is a competitor.

    Honest limits, surfaced in the output rather than hidden:
      - asking prices are NOT sale prices. When the check captured eBay SOLD
        data we anchor on that; otherwise we anchor low in the ask range and
        say so, because the listings that actually sell skew cheap.
      - we cannot estimate days-to-sell: nothing in the data records how long
        a listing sat. Instead each tier reports how many active competitors
        it undercuts, which is the part we can prove.
    """
    listings = [l for l in (result.get("listings") or [])
                if l.get("price") is not None]
    asks = sorted(l["price"] for l in listings)
    if len(asks) < 4:
        return {"error": "Not enough comparable listings in this check to "
                         "advise a sell price."}
    n = len(asks)

    def pct(p):
        i = max(0, min(n - 1, int(round(p / 100 * (n - 1)))))
        return asks[i]

    sold_stats = ((result.get("spreads") or {}).get("ebay_sold") or {}).get("stats")
    sold_median = (sold_stats or {}).get("median")
    if sold_median:
        anchor, anchor_basis = sold_median, "eBay sold median (what buyers paid)"
    else:
        # no sold data: anchor at the 40th percentile of asks. Listings that
        # sell skew toward the cheap end, so the ask median overstates it.
        anchor, anchor_basis = pct(40), ("40th percentile of asking prices "
                                         "(no sold data - asks run high)")

    def tier(name, price, blurb):
        under = sum(1 for a in asks if a > price)
        fees = price * fee_pct / 100 + fixed_fee
        return {
            "name": name, "price": round(price),
            "undercuts": under, "of": n,
            "undercut_pct": round(100 * under / n),
            "fees": round(fees, 2),
            "ship_cost": round(ship_cost, 2),
            "net": round(price - fees - ship_cost, 2),
            "blurb": blurb,
        }

    tiers = [
        tier("quick sale", min(anchor * 0.9, pct(20)),
             "priced to move - undercuts most of the board"),
        tier("market", anchor,
             "what the evidence says it is worth"),
        tier("patient", max(anchor * 1.25, pct(70)),
             "top of the range; expect it to sit"),
    ]
    cheapest = [{"price": l["price"], "title": l.get("title", ""),
                 "url": l.get("url", "")} for l in
                sorted(listings, key=lambda l: l["price"])[:8]]
    return {
        "query": result.get("query"),
        "n": n, "anchor": round(anchor), "anchor_basis": anchor_basis,
        "have_sold": bool(sold_median),
        "ask_p25": round(pct(25)), "ask_median": round(pct(50)),
        "ask_p75": round(pct(75)),
        "fee_pct": fee_pct, "fixed_fee": fixed_fee, "ship_cost": ship_cost,
        "tiers": tiers,
        "competition": cheapest,
        "caveats": [
            "Prices here include shipping, so list at these numbers with free "
            "shipping - or subtract what you'd charge separately.",
            "Condition matters more than any percentile: a mint copy belongs "
            "at the patient tier, a flawed one below the quick tier.",
            "No days-to-sell estimate is possible from this data - nothing "
            "records how long listings sat. Undercut counts are the real signal.",
        ],
    }
