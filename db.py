"""SQLite store: remembers every listing seen so we never notify twice,
and keeps price history for stable deal-threshold math."""

import json
import sqlite3
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id         TEXT PRIMARY KEY,
    search     TEXT NOT NULL,
    source     TEXT NOT NULL,
    title      TEXT,
    price      REAL,
    url        TEXT,
    condition  TEXT,
    status     TEXT,
    reason     TEXT,
    first_seen TEXT,
    notified   INTEGER DEFAULT 0
)
"""


PRICE_CHECKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    query         TEXT NOT NULL,
    mode          TEXT,
    listed_median REAL,
    sold_median   REAL,
    kept          INTEGER,
    data          TEXT
)
"""


HUNT_SEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS hunt_seen (
    listing_id TEXT PRIMARY KEY,
    query      TEXT,
    first_seen TEXT NOT NULL
)
"""


# Known methodology changes. Two data points that straddle one of these are
# NOT directly comparable - the trends view draws those segments dashed and
# says why, instead of implying a market move that never happened.
METHOD_CHANGES = [
    ("2026-07-22T19:30", "screening rebuilt mid-evening (profile system) - medians moved because the filter changed, not the market"),
    ("2026-07-23T16:30", "prices became shipping-inclusive"),
]


def method_changes_between(ts_a, ts_b):
    """Labels of methodology changes falling between two ISO timestamps."""
    lo, hi = sorted([ts_a, ts_b])
    return [label for when, label in METHOD_CHANGES if lo < when <= hi]


class Store:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute(SCHEMA)
        self.conn.execute(PRICE_CHECKS_SCHEMA)
        self.conn.execute(HUNT_SEEN_SCHEMA)
        self.conn.commit()

    def hunt_seen_map(self, listing_ids):
        """{listing_id: first_seen} for the ids a prior hunt already flagged."""
        out = {}
        for lid in listing_ids:
            row = self.conn.execute(
                "SELECT first_seen FROM hunt_seen WHERE listing_id = ?",
                (lid,)).fetchone()
            if row:
                out[lid] = row[0]
        return out

    def hunt_mark_seen(self, listing_ids, query):
        now = datetime.now().isoformat(timespec="seconds")
        for lid in listing_ids:
            self.conn.execute(
                "INSERT OR IGNORE INTO hunt_seen (listing_id, query, "
                "first_seen) VALUES (?, ?, ?)", (lid, query, now))
        self.conn.commit()

    def save_price_check(self, query, mode, result, method=None):
        import json as _json
        spreads = result.get("spreads", {})
        listed = (spreads.get("ebay_listed", {}).get("stats") or {}).get("median")
        if listed is None:
            listed = result.get("median")
        sold = (spreads.get("ebay_sold", {}).get("stats") or {}).get("median")
        ts = datetime.now().isoformat(timespec="seconds")
        # provenance travels with the result so trends can be honest later
        result["method"] = dict({
            "shipping_inclusive": True,
            "profile": bool(result.get("profile") or result.get("variant_info")),
            "source": "manual",
        }, **(method or {}))
        self.conn.execute(
            "INSERT INTO price_checks (ts, query, mode, listed_median, "
            "sold_median, kept, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, query, mode, listed, sold, result.get("kept", 0),
             _json.dumps(result)))
        self.conn.commit()
        try:
            export_check_file(self.path, ts, query, mode, result)
        except Exception:
            pass   # a failed hard copy must never sink the search itself

    

    def list_price_checks(self, limit=40):
        return self.conn.execute(
            "SELECT id, ts, query, mode, listed_median, sold_median, kept "
            "FROM price_checks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def prev_price_check(self, before_id, query):
        return self.conn.execute(
            "SELECT listed_median, sold_median FROM price_checks "
            "WHERE id < ? AND query = ? AND (mode IS NULL OR mode != 'hunt') "
            "ORDER BY id DESC LIMIT 1",
            (before_id, query)).fetchone()

    def get_price_check(self, pid):
        row = self.conn.execute(
            "SELECT ts, query, mode, data FROM price_checks WHERE id = ?",
            (pid,)).fetchone()
        return row

    def trend_items(self):
        """One series per (query, mode) from real price checks, plus hunt
        dates as timeline markers. Hunts never contribute price points -
        their pools are unscreened, so a median would be noise."""
        rows = self.conn.execute(
            "SELECT id, ts, query, mode, listed_median, sold_median, kept, data "
            "FROM price_checks ORDER BY ts").fetchall()
        items, hunts = {}, {}
        for pid, ts, query, mode, listed, sold, kept, data in rows:
            if mode == "hunt":
                hunts.setdefault(query.lower(), []).append(
                    {"id": pid, "ts": ts, "finds": kept})
                continue
            key = f"{query.lower()}|{mode or 'working'}"
            item = items.setdefault(key, {
                "key": key, "query": query, "mode": mode or "working",
                "points": [],
            })
            try:
                blob = json.loads(data) if data else {}
            except Exception:
                blob = {}
            method = blob.get("method") or {}
            item["points"].append({
                "id": pid, "ts": ts,
                "listed": listed, "sold": sold, "kept": kept,
                "p25": blob.get("p25"), "p75": blob.get("p75"),
                "source": method.get("source", "manual"),
                # pre-dates the flag: infer from the shipping epoch
                "shipping_inclusive": method.get(
                    "shipping_inclusive", ts > METHOD_CHANGES[1][0]),
            })
        out = []
        for item in items.values():
            pts = [p for p in item["points"] if p["listed"] is not None]
            item["n"] = len(pts)
            item["latest"] = pts[-1]["listed"] if pts else None
            item["first"] = pts[0]["listed"] if pts else None
            item["change_pct"] = (
                round(100 * (item["latest"] / item["first"] - 1))
                if pts and len(pts) > 1 and item["first"] else None)
            # comparability: any methodology change inside the series span
            item["breaks"] = (method_changes_between(pts[0]["ts"], pts[-1]["ts"])
                              if len(pts) > 1 else [])
            item["hunts"] = hunts.get(item["query"].lower(), [])
            item["last_ts"] = item["points"][-1]["ts"]
            out.append(item)
        out.sort(key=lambda i: i["last_ts"], reverse=True)
        # hunt-only queries still deserve a row so nothing searched vanishes
        seen_q = {i["query"].lower() for i in out}
        for q, hs in hunts.items():
            if q in seen_q:
                continue
            out.append({"key": f"{q}|hunt", "query": hs[0].get("query", q),
                        "mode": "hunt-only", "points": [], "n": 0,
                        "latest": None, "first": None, "change_pct": None,
                        "breaks": [], "hunts": hs, "last_ts": hs[-1]["ts"]})
        return out

    def get_status(self, listing_id):
        row = self.conn.execute(
            "SELECT status FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        return row[0] if row else None

    def seen(self, listing_id):
        row = self.conn.execute(
            "SELECT 1 FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        return row is not None

    def add(self, listing, search, status, reason=""):
        self.conn.execute(
            "INSERT OR REPLACE INTO listings "
            "(id, search, source, title, price, url, condition, status, reason, first_seen, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "COALESCE((SELECT notified FROM listings WHERE id = ?), 0))",
            (listing.id, search, listing.source, listing.title, listing.price,
             listing.url, listing.condition, status, reason,
             datetime.now().isoformat(timespec="seconds"), listing.id),
        )
        self.conn.commit()

    def set_status(self, listing_id, status, reason=""):
        self.conn.execute(
            "UPDATE listings SET status = ?, reason = ? WHERE id = ?",
            (status, reason, listing_id),
        )
        self.conn.commit()

    def mark_notified(self, listing_id):
        self.conn.execute(
            "UPDATE listings SET notified = 1 WHERE id = ?", (listing_id,)
        )
        self.conn.commit()

    def recent_prices(self, search, days=14):
        """Prices of clean listings for this search seen in the last N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT price FROM listings WHERE search = ? AND first_seen >= ? "
            "AND status IN ('ok', 'deal') AND price IS NOT NULL",
            (search, cutoff),
        ).fetchall()
        return [r[0] for r in rows]

    def recent_deals(self, days=7):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        return self.conn.execute(
            "SELECT first_seen, search, price, title, url FROM listings "
            "WHERE status = 'deal' AND first_seen >= ? ORDER BY first_seen DESC",
            (cutoff,),
        ).fetchall()


def export_check_file(db_path, ts, query, mode, result):
    """Write a human-readable hard copy of one search next to the database:
    ~/DealFinder/history/<date>_<mode>_<query>.md. The dashboard reads the
    database; these files exist so the user always has plain-text copies."""
    import os
    import re
    folder = os.path.join(os.path.dirname(os.path.abspath(db_path)), "history")
    os.makedirs(folder, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:50]
    stamp = ts.replace(":", "").replace("T", "_")[:15]   # date_HHMM
    path = os.path.join(folder, f"{stamp}_{mode or 'working'}_{slug}.md")

    L = []
    money = lambda v: f"${v:,.0f}" if v is not None else "-"
    if mode == "hunt":
        L.append(f"# Deep hunt - {query}")
        L.append(f"{ts.replace('T', ' ')} | {result.get('fetched', '?')} "
                 f"listings examined")
        L.append(f"queries: {' | '.join(result.get('queries', []))}")
        if result.get("typical"):
            L.append(f"typical clean price: {result['typical']}")
        L.append("")
        L.append(f"## Finds ({len(result.get('finds', []))})")
        for f in result.get("finds", []):
            new = "" if f.get("first_seen") else "NEW "
            au = (f" | auction ends in {round(f['auction_hours_left'])}h"
                  if f.get("auction_hours_left") is not None else "")
            L.append(f"- [{new}{f.get('interest', '?').upper()}] "
                     f"{money(f.get('price'))} {f.get('title', '')}{au}")
            L.append(f"  {f.get('url', '')}")
            L.append(f"  {f.get('reason', '')}")
        L.append("")
    else:
        L.append(f"# Price check - {query}")
        L.append(f"{ts.replace('T', ' ')} | mode {mode or 'working'} | "
                 f"fetched {result.get('fetched', '?')}, "
                 f"kept {result.get('kept', '?')}")
        drop = result.get("dropped") or {}
        if drop:
            L.append("dropped: " + ", ".join(f"{v} {k}"
                                             for k, v in drop.items()))
        for key, label in (("ebay_listed", "eBay listed"),
                           ("ebay_sold", "eBay SOLD"),
                           ("craigslist_listed", "Craigslist")):
            sp = (result.get("spreads") or {}).get(key) or {}
            st = sp.get("stats")
            if st:
                cur = sp.get("currency", "$")
                L.append(f"{label}: usual {cur}{st['p25']:,.0f}-"
                         f"{cur}{st['p75']:,.0f} | median "
                         f"{cur}{st['median']:,.0f} | n={st['n']}")
            elif sp.get("note"):
                L.append(f"{label}: {sp['note']}")
        v = result.get("verdict")
        if v:
            L.append(f"AI verdict: good under {money(v.get('good_price'))}, "
                     f"jump under {money(v.get('great_price'))}")
            if v.get("note"):
                L.append(f"  {v['note']}")
        if result.get("variant_info"):
            L.append(f"profile: {result['variant_info']}")
        L.append("")
    lst = result.get("listings") or []
    if lst:
        title = ("## Everything the hunt saw" if mode == "hunt"
                 else "## Kept listings") + f" ({len(lst)}, cheapest first)"
        L.append(title)
        for l in lst:
            L.append(f"- {money(l.get('price'))} {l.get('title', '')} "
                     f"({l.get('source', '?')})")
            L.append(f"  {l.get('url', '')}")
        L.append("")
    rej = result.get("rejected") or []
    if rej:
        L.append(f"## Rejected by screening ({len(rej)})")
        for l in rej:
            L.append(f"- [{l.get('category', '?')}] {money(l.get('price'))} "
                     f"{l.get('title', '')} ({l.get('reason', '')})")
        L.append("")
    with open(path, "w") as f:
        f.write("\n".join(L))
    return path
