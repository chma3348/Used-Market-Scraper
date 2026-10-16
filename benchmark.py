#!/usr/bin/env python3
"""Screening-accuracy benchmark. Run after any prompt/model change:

    .venv/bin/python3 benchmark.py            # uses the configured backend
    .venv/bin/python3 benchmark.py MODEL_TAG  # test a specific Ollama model

Cases are (query, title, expected) where expected is:
  "in"  - a working-condition buyer wants to see this listing
  "out" - they don't (accessory / unrelated / parts / damaged)
  None  - gray area, shown but not scored

Add a case every time real-world screening gets one wrong."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_filter            # noqa: E402
import dealfinder           # noqa: E402
import filters              # noqa: E402
from sources import Listing  # noqa: E402

CASES = [
    # --- camera lens: query words appear in tons of accessory titles
    ("nikon 28mm 2.8 ai-s", "Vivitar Ai Rear Lens Cap for Nikon E series Ai-s 50mm 28mm", "out"),
    ("nikon 28mm 2.8 ai-s", "Nikon 28mm f/2.8 AF Nikkor AI-s instruction manual", "out"),
    ("nikon 28mm 2.8 ai-s", "HN-1 Metal Lens Hood for Nikon AI-S 24mm f/2.8 AiS 28mm f/2", "out"),
    ("nikon 28mm 2.8 ai-s", "Nikon Nikkor 28mm f/2.8 AI-S Wide Angle Lens - Clean Glass", "in"),
    ("nikon 28mm 2.8 ai-s", "Nikkor 28mm 1:2.8 AI-S w/ caps EXC+", "in"),
    ("nikon 28mm 2.8 ai-s", "Nikon Zoom-NIKKOR 35-70mm f/3.3-4.5 AIS lens", "out"),
    ("nikon 28mm 2.8 ai-s", "Nikon Nikkor 28mm f/2.8 AI-S - FOR PARTS fungus and haze", "out"),
    ("nikon 28mm 2.8 ai-s", "Sigma 28mm f2.8 Mini-Wide II for Nikon AI-S mount", "out"),
    # --- the flip side: the buyer wants the accessory itself
    ("nikon ai-s rear lens cap", "Genuine Nikon LF-1 Rear Lens Cap for AI AI-S lenses", "in"),
    ("nikon ai-s rear lens cap", "Nikon Nikkor 28mm f/2.8 AI-S Wide Angle Lens", "out"),
    # --- camera body
    ("nikon f3", "Nikon F3 HP 35mm SLR Film Camera Body, meter works", "in"),
    ("nikon f3", "Nikon D300 DSLR body and many lenses", "out"),
    ("nikon f3", "Nikon F3 F3HP NEW Main FPC with LCD OEM RARE", "out"),
    ("nikon f3", "Nikon MD-4 Motor Drive for F3", "out"),
    # --- game console: successors, games, accessories, bundles
    ("nintendo switch", "Nintendo Switch 2 Console 512GB brand new sealed", "out"),
    ("nintendo switch", "Nintendo Switch console with dock and joycons, works great", "in"),
    ("nintendo switch", "Nintendo Switch bundle - console, 5 games, charger, case", "in"),
    ("nintendo switch", "Zelda Tears of the Kingdom - Nintendo Switch game", "out"),
    ("nintendo switch", "Nintendo Switch AC adapter charger OEM", "out"),
    ("nintendo switch", "Nintendo Switch Pro Controller neon", "out"),
    ("nintendo switch", "Nintendo Switch dock only, no console", "out"),
    ("nintendo switch", "Nintendo swich console w games (works)", "in"),
    ("nintendo switch", "Nintendo Switch, cracked screen but boots fine", "out"),
    ("nintendo switch", "Nintendo Switch OLED white console complete", None),
    ("nintendo switch", "Nintendo Switch Lite turquoise handheld", None),
    # --- point-and-shoot: zoom successors carry the same name; sellers
    #     title them with "VF" / bare focal numbers (the mju II disaster)
    ("olympus mju ii", "Olympus mju II Stylus Epic 35mm f/2.8 point and shoot, tested", "in"),
    ("olympus mju ii", "Olympus Stylus Epic QD quartz date 35mm compact camera", "in"),
    ("olympus mju ii", "Olympus mju II 80 VF 35mm point and shoot camera", "out"),
    ("olympus mju ii", "Olympus mju: II 115 VF Zoom all-weather compact", "out"),
    ("olympus mju ii", "Olympus mju II 170 VF zoom camera w/ case", "out"),
    ("olympus mju ii", "Olympus mju Zoom 105 Deluxe 35mm film camera", "out"),
    # --- power tools
    ("dewalt 20v drill", "DeWalt DCD791 20V MAX XR Brushless Drill/Driver w/ battery", "in"),
    ("dewalt 20v drill", "DeWalt 20V MAX battery 5Ah DCB205 (battery only)", "out"),
    ("dewalt 20v drill", "DeWalt drill carrying case / tool bag", "out"),
    ("dewalt 20v drill", "Milwaukee M18 drill driver kit", "out"),
]


def run(backend, label):
    print(f"--- {label}")
    by_query = {}
    for query, title, expected in CASES:
        by_query.setdefault(query, []).append((title, expected))
    right = wrong = missing = 0
    for query, items in by_query.items():
        listings, expects = [], []
        for i, (title, expected) in enumerate(items):
            # realistic id shape - regression-guards the pipe-in-id bug
            listings.append(Listing(id=f"ebay:v1|90000000{i:04d}|0",
                                    source="ebay", title=title,
                                    price=40.0, url=""))
            expects.append(expected)
        # keyword stage first, exactly like the real pipeline
        undecided, keyword_out = [], {}
        for l in listings:
            hit = filters.keyword_category(l, query=query)
            if hit:
                keyword_out[l.id] = hit[0]
            else:
                undecided.append(l)
        verdicts = ai_filter.classify(undecided, query, "", backend)
        for l, expected in zip(listings, expects):
            if l.id in keyword_out:
                got = keyword_out[l.id]
            elif l.id in verdicts:
                got = verdicts[l.id][0]
            else:
                got = None
            kept = got == "ok"
            if expected is None:
                mark = " ~"
            elif got is None:
                missing += 1
                mark = " ?"
            elif kept == (expected == "in"):
                right += 1
                mark = "OK"
            else:
                wrong += 1
                mark = " X"
            print(f"  [{mark}] want={expected or 'gray':<4} got={got or 'MISSING':<9} "
                  f"[{query[:22]:<22}] {l.title[:48]}")
    scored = right + wrong + missing
    print(f"  == {label}: {right}/{scored} correct, {wrong} wrong, "
          f"{missing} missing\n")
    return right, scored


VARIANT_TESTS = [
    ("nintendo switch", lambda p: p and any(
        "oled" in v["name"].lower() for v in p["same"]),
     "Switch OLED auto-included as essentially the same"),
    ("nintendo switch", lambda p: p and not any(
        "oled" in v["name"].lower() for v in p["ambiguous"] + p["different"]),
     "Switch OLED not wrongly excluded"),
    ("nikon 28mm 2.8 ai-s", lambda p: p and bool(p["ambiguous"]),
     "AI-S lens flags ambiguity (Series E / AI / third-party)"),
]


def run_variants(backend, label):
    print(f"--- variant analysis: {label}")
    plans = {}
    for query, check, desc in VARIANT_TESTS:
        if query not in plans:
            plans[query] = ai_filter.analyze_variants(query, "", backend)
        ok = check(plans[query])
        print(("  [OK] " if ok else "  [ X] ") + desc)
    for query, p in plans.items():
        if p:
            print(f"  {query}: same={[v['name'] for v in p['same']]} "
                  f"ambiguous={[v['name'] for v in p['ambiguous']]} "
                  f"different={[v['name'] for v in p['different']]}")


# --- search-profile draft: markers must speak seller-title vocabulary.
#     compile_profile(draft) rejects must kill every wrong-variant title
#     below and none of the correct ones.
PROFILE_QUERY = "olympus mju ii"
PROFILE_KILL = [
    "Olympus mju II 80 VF 35mm point and shoot camera",
    "Olympus mju: II 115 VF Zoom all-weather compact",
    "Olympus mju II 170 VF zoom camera w/ case",
    "Olympus mju Zoom 105 Deluxe 35mm film camera",
]
PROFILE_KEEP = [
    "Olympus mju II Stylus Epic 35mm f/2.8 point and shoot, tested",
    "Olympus Stylus Epic QD quartz date 35mm compact camera",
    "Olympus mju-II f2.8 compact camera black",
]


def run_profile(backend, label):
    print(f"--- profile draft: {label}")
    p = ai_filter.draft_profile(PROFILE_QUERY, "", backend)
    if not p:
        print("  [ X] draft_profile returned nothing")
        return
    print(f"  comment: {p.get('comment', '')[:110]}")
    print(f"  include: {[i['name'] for i in p.get('include', [])]}")
    for e in p.get("exclude", []):
        print(f"  exclude: {e['name']} -> {e.get('markers', [])}")
    rejects, guidance, _ = dealfinder.compile_profile(p, PROFILE_QUERY)
    print(f"  guidance: {guidance[:140]}")
    ok = bad = 0
    for t in PROFILE_KILL:
        hit = filters.reject_hit(t, rejects)
        if hit:
            ok += 1
            print(f'  [OK] killed by "{hit}": {t}')
        else:
            bad += 1
            print(f"  [ X] SURVIVED (marker gap): {t}")
    for t in PROFILE_KEEP:
        hit = filters.reject_hit(t, rejects)
        if hit:
            bad += 1
            print(f'  [ X] WRONGLY killed by "{hit}": {t}')
        else:
            ok += 1
            print(f"  [OK] kept: {t}")
    total = len(PROFILE_KILL) + len(PROFILE_KEEP)
    print(f"  == profile: {ok}/{total} correct\n")
    return ok == total


if __name__ == "__main__":
    if "--profile" in sys.argv:
        config = dealfinder.load_config(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "config.json"))
        strong = dealfinder.ai_backend(config, strong=True)
        run_profile(strong, f"{strong['provider']}:{strong['model']}")
        sys.exit(0)
    if "--variants" in sys.argv:
        config = dealfinder.load_config(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "config.json"))
        strong = dealfinder.ai_backend(config, strong=True)
        run_variants(strong, f"{strong['provider']}:{strong['model']}")
        sys.exit(0)
    if len(sys.argv) > 1:
        backend = {"provider": "local", "url": "http://127.0.0.1:11434",
                   "model": sys.argv[1]}
        run(backend, sys.argv[1])
    else:
        config = dealfinder.load_config(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "config.json"))
        backend = dealfinder.ai_backend(config)
        run(backend, f"{backend['provider']}:{backend['model']}")
