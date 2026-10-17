#!/usr/bin/env python3
"""Phase 3 of the scheduled scan: act on Claude's verdicts. NO AI, NO COST.

Reads the candidates from phase 1 and the verdicts Claude wrote in phase 2,
pushes a notification for each deal, records what was seen so the next run
does not re-notify, and leaves a human-readable record in history/.

Fails loudly: if phase 2 produced nothing parseable, this exits non-zero so
the run goes red rather than quietly notifying nobody.
"""

import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import notify             # noqa: E402

WORK = os.path.join(HERE, "work")
SEEN_PATH = os.path.join(HERE, "seen.json")
HISTORY = os.path.join(ROOT, "history")
# Keep the seen-list from growing without bound; ids age out after this many
# runs' worth of entries, by which point the listing is long gone.
SEEN_CAP = 5000


def _first_run_of_day(name, stamp):
    """True once per calendar day per search, so 'daily' really is daily."""
    path = os.path.join(HERE, "heartbeat.json")
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    today = stamp[:10]
    if doc.get(name) == today:
        return False
    doc[name] = today
    with open(path, "w") as f:
        json.dump(doc, f, indent=1)
    return True


def clean_url(url):
    """Strip eBay's tracking suffix (?_skw=...&hash=item...:g:...).

    Two reasons: it triples the length of a link that has to be readable in a
    notification body, and those stale search-context links can land on an
    error page instead of the item. The bare /itm/<id> form always resolves."""
    m = re.match(r"(https?://(?:www\.)?ebay\.[a-z.]+/itm/\d+)", url or "")
    return m.group(1) if m else (url or "")


def find_list(entry, listings):
    """The genuinely-matching listings for one search, cheapest first.

    Phase 2 used to hand back only `cheapest`, so accept that shape too - a
    scan screened by the old skill should still report something."""
    raw = entry.get("finds")
    if raw is None:
        c = entry.get("cheapest") or {}
        raw = [c] if c.get("i") is not None else []
    out, taken = [], set()
    for f in raw or []:
        l = listings.get(f.get("i"))
        if l and l["id"] not in taken:
            taken.add(l["id"])
            out.append((l, (f.get("what") or "").strip()))
    out.sort(key=lambda t: t[0].get("price") or 0)
    return out


def list_body(finds, limit=8):
    """A numbered, tappable list - the thing you actually scroll on a phone."""
    lines = []
    for n, (l, what) in enumerate(finds[:limit], 1):
        head = f"{n}. ${l['price']:,.0f}"
        if what:
            head += f" \u00b7 {what}"
        if l.get("auction") and l.get("ends"):
            head += f" (bid, ends {l['ends'][5:10]})"
        lines.append(head)
        lines.append("   " + clean_url(l["url"]))
    if len(finds) > limit:
        lines.append(f"...and {len(finds) - limit} more in the report.")
    return "\n".join(lines)


def report_url(slug):
    """Where the full write-up for this run lives, when running in Actions."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return None
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    return (f"https://github.com/{repo}/blob/{branch}/"
            f"history/{slug}_cloud-scan.md")


def main():
    with open(os.path.join(WORK, "candidates.json")) as f:
        cand = json.load(f)
    by_name = {s["name"]: s for s in cand["searches"]}

    results_path = os.path.join(WORK, "results.json")
    if not os.path.exists(results_path):
        print("[report] FATAL: phase 2 wrote no results.json")
        return 1
    try:
        with open(results_path) as f:
            results = json.load(f)
    except ValueError as e:
        print(f"[report] FATAL: results.json is not valid JSON ({e})")
        return 1

    topic = os.environ.get("NTFY_TOPIC", "")
    stamp = datetime.now().isoformat(timespec="seconds")
    seen_doc = {"ids": []}
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH) as f:
                seen_doc = json.load(f)
        except ValueError:
            pass
    seen = list(seen_doc.get("ids", []))

    sent, heartbeats = 0, 0
    slug = re.sub(r"[^a-z0-9]+", "-", stamp.lower())[:16].strip("-")
    full_url = report_url(slug)
    lines = [f"# Scheduled scan - {stamp}", ""]
    for entry in results.get("searches", []):
        name = entry.get("name", "")
        src = by_name.get(name)
        if not src:
            print(f"[report] skipping unknown search '{name}'")
            continue
        listings = {l["i"]: l for l in src["listings"]}
        deals = [d for d in entry.get("deals", []) if d.get("i") in listings]
        finds = find_list(entry, listings)
        deal_ids = {listings[d["i"]]["id"] for d in deals}
        lines.append(f"## {name} - {len(deals)} deal(s), {len(finds)} genuine "
                     f"match(es) of {len(src['listings'])} screened")
        if entry.get("note"):
            lines.append(f"_{entry['note']}_")
        lines.append("")

        # The browsable list: every real match, cheapest first, deals marked.
        # This is what the phone notification links back to.
        if finds:
            for n, (l, what) in enumerate(finds, 1):
                mark = " **DEAL**" if l["id"] in deal_ids else ""
                bits = [f"{n}. **${l['price']:,.0f}**{mark} "
                        f"[{l['title']}]({clean_url(l['url'])})"]
                tail = []
                if what:
                    tail.append(what)
                if l.get("auction") and l.get("ends"):
                    tail.append(f"auction ends {l['ends'][:16].replace('T', ' ')}")
                if tail:
                    bits.append("   - " + " \u00b7 ".join(tail))
                lines.extend(bits)
            lines.append("")

        for d in deals:
            l = listings[d["i"]]
            reason = d.get("reason", "")
            lines.append(f"- why #{d['i']} is a deal: {reason}")
            if topic:
                # A phone notification is a glance, not a report. The full
                # reasoning already lives in the history file above.
                short = reason.split(" [screened")[0].strip()
                if len(short) > 100:
                    cut = short[:100].rsplit(" ", 1)[0]
                    short = cut + "\u2026"
                body = f"${l['price']:,.0f}"
                if l.get("auction") and l.get("ends"):
                    body += f" \u00b7 auction ends {l['ends'][11:16]} {l['ends'][5:10]}"
                if short:
                    body += f" \u2014 {short}"
                others = [f for f in finds if f[0]["id"] != l["id"]]
                if others:
                    body += (f"\n\n{len(others)} other real match"
                             f"{'es' if len(others) > 1 else ''} this scan:\n"
                             + list_body(others, limit=5))
                acts = [("Open listing", clean_url(l["url"]))]
                if full_url:
                    acts.append(("All finds" if len(finds) < 2
                                 else f"All {len(finds)} finds", full_url))
                try:
                    notify.send(topic, l["title"][:70], body,
                                click_url=clean_url(l["url"]), actions=acts)
                    sent += 1
                except Exception as e:
                    print(f"[report] notify failed: {e}")
        if deals:
            lines.append("")

        # A patient search can go weeks without a hit. When asked, send a
        # quiet ping listing what IS out there, so the buyer can browse the
        # real market instead of just being told "nothing yet".
        hb = src.get("heartbeat")
        if topic and not deals and hb in ("scan", "daily"):
            if hb != "daily" or _first_run_of_day(name, stamp):
                n = len(src["listings"])
                acts = []
                if finds:
                    cheapest = finds[0][0]
                    title = (f"{name} \u2014 {len(finds)} found, "
                             f"from ${cheapest['price']:,.0f}")
                    body = list_body(finds)
                    if src.get("alert_under"):
                        body += (f"\n\nNone under your ${src['alert_under']:,.0f} "
                                 f"alert yet \u2014 these are what the market "
                                 f"looks like today.")
                    for l, what in finds[:2]:
                        label = f"${l['price']:,.0f} {what}".strip()
                        if len(label) > 25:
                            label = label[:25].rsplit(" ", 1)[0]
                        acts.append((label, clean_url(l["url"])))
                    if full_url:
                        acts.append(("Full report", full_url))
                else:
                    title = f"{name} \u2014 still looking"
                    body = (f"{n} listings checked, none were actually a match"
                            if n else "nothing matched the filters")
                    if full_url:
                        acts.append(("Why nothing matched", full_url))
                try:
                    notify.send(topic, title, body, priority="low",
                                tags="eyes", actions=acts or None,
                                click_url=(clean_url(finds[0][0]["url"])
                                           if finds else full_url))
                    heartbeats += 1
                except Exception as e:
                    print(f"[report] heartbeat failed: {e}")
        # everything screened this run is 'seen', deal or not - otherwise the
        # same rejects would be re-screened (and re-paid for) every scan
        seen.extend(l["id"] for l in src["listings"])
        lines.append("")

    seen_doc["ids"] = list(dict.fromkeys(seen))[-SEEN_CAP:]
    seen_doc["updated"] = stamp
    with open(SEEN_PATH, "w") as f:
        json.dump(seen_doc, f, indent=1)

    os.makedirs(HISTORY, exist_ok=True)
    with open(os.path.join(HISTORY, f"{slug}_cloud-scan.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[report] {sent} deal alert(s), {heartbeats} heartbeat(s), "
          f"{len(seen_doc['ids'])} ids remembered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
