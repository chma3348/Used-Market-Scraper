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

    sent, lines = 0, [f"# Scheduled scan - {stamp}", ""]
    for entry in results.get("searches", []):
        name = entry.get("name", "")
        src = by_name.get(name)
        if not src:
            print(f"[report] skipping unknown search '{name}'")
            continue
        listings = {l["i"]: l for l in src["listings"]}
        deals = [d for d in entry.get("deals", []) if d.get("i") in listings]
        lines.append(f"## {name} - {len(deals)} deal(s) of "
                     f"{len(src['listings'])} screened")
        if entry.get("note"):
            lines.append(f"_{entry['note']}_")
        for d in deals:
            l = listings[d["i"]]
            reason = d.get("reason", "")
            lines.append(f"- **${l['price']:,.0f}** {l['title']}")
            lines.append(f"  {l['url']}")
            lines.append(f"  {reason}")
            if topic:
                body = f"${l['price']:,.0f} on {l['source']}"
                if l.get("auction") and l.get("ends"):
                    body += f" - auction ends {l['ends'][:16]}"
                body += f"\n{reason}"
                try:
                    notify.send(topic, l["title"][:80], body,
                                click_url=l["url"])
                    sent += 1
                except Exception as e:
                    print(f"[report] notify failed: {e}")
        # everything screened this run is 'seen', deal or not - otherwise the
        # same rejects would be re-screened (and re-paid for) every scan
        seen.extend(l["id"] for l in src["listings"])
        lines.append("")

    seen_doc["ids"] = list(dict.fromkeys(seen))[-SEEN_CAP:]
    seen_doc["updated"] = stamp
    with open(SEEN_PATH, "w") as f:
        json.dump(seen_doc, f, indent=1)

    os.makedirs(HISTORY, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", stamp.lower())[:16].strip("-")
    with open(os.path.join(HISTORY, f"{slug}_cloud-scan.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[report] {sent} notification(s) sent, "
          f"{len(seen_doc['ids'])} ids remembered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
