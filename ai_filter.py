"""AI analysis layer: listing screening, query expansion, and structured
calls. Two interchangeable backends:

- "anthropic": the Claude API (API key or `ant auth login` OAuth profile)
- "local": an Ollama model on this machine (free, private, slower)

Every call goes through call_json() with a JSON schema, so both backends
return validated structures. All failures fail open (callers keep listings
rather than dropping them)."""

import json
import os
import urllib.error
import urllib.request

OLLAMA_TIMEOUT = 600  # 27B models chew on big batches for a while


# ------------------------------------------------------------ credentials

def has_credentials(api_key=None):
    """True if any Anthropic credential source exists: an explicit key, env
    vars, or an `ant auth login` OAuth profile on disk (which the SDK's
    bare client resolves and auto-refreshes)."""
    if api_key or os.environ.get("ANTHROPIC_API_KEY") \
            or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    import glob
    return bool(glob.glob(os.path.expanduser(
        "~/.config/anthropic/credentials/*.json")))


def oauth_status():
    """(ok, label) for the `ant auth login` OAuth profile path."""
    import glob
    if glob.glob(os.path.expanduser("~/.config/anthropic/credentials/*.json")):
        return True, "AI: Claude login (OAuth)"
    return False, "AI: OAuth selected but not logged in - run 'ant auth login'"


def backend_ready(backend):
    """Can this backend actually serve a call right now?"""
    if backend.get("provider") == "local":
        try:
            url = backend["url"].rstrip("/") + "/api/tags"
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False
    return has_credentials(backend.get("api_key"))


# ------------------------------------------------------------- the bridge

def call_json(prompt, schema, backend, max_tokens=4000):
    """One structured-output call, returning the parsed dict or None."""
    if backend.get("provider") == "local":
        try:
            return _ollama_json(prompt, schema, backend, max_tokens)
        except Exception as e:
            print(f"  [ai] local model call failed ({e})")
            return None
    return _anthropic_json(prompt, schema, backend, max_tokens)


def _ollama_json(prompt, schema, backend, max_tokens):
    # Thinking models (qwen3 family) burn the output budget on reasoning and
    # return empty content - disable thinking first, fall back for models
    # that don't support the toggle.
    last_error = None
    for think in (False, None):
        body = {
            "model": backend["model"],
            "messages": [{"role": "user", "content": prompt}],
            "format": schema,      # Ollama constrains output to this schema
            "stream": False,
            "options": {"num_predict": max_tokens, "num_ctx": 16384},
        }
        if think is not None:
            body["think"] = think
        req = urllib.request.Request(
            backend["url"].rstrip("/") + "/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_error = RuntimeError(f"Ollama HTTP {e.code}")
            continue  # e.g. model rejects the think toggle - retry without
        content = ((data.get("message") or {}).get("content") or "").strip()
        if not content:
            last_error = RuntimeError(
                "model returned empty output (thinking budget exhausted?)")
            continue
        return json.loads(content)
    raise last_error or RuntimeError("Ollama call failed")


def _anthropic_json(prompt, schema, backend, max_tokens):
    if not has_credentials(backend.get("api_key")):
        print("  [ai] no Anthropic credentials (key or Claude login)")
        return None
    try:
        import anthropic
    except ImportError as e:
        print(f"  [ai] FATAL: anthropic SDK not importable in this process "
              f"({e}) - all API calls will fail. Restart the app.")
        return None
    api_key = backend.get("api_key")
    client = anthropic.Anthropic(api_key=api_key) if api_key \
        else anthropic.Anthropic()
    try:
        # thinking models spend part of max_tokens on reasoning, so a JSON
        # answer can come back truncated - retry once with double budget
        for budget in (max_tokens, max_tokens * 2):
            response = client.messages.create(
                model=backend["model"],
                max_tokens=budget,
                output_config={"format": {"type": "json_schema",
                                          "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
            if response.stop_reason == "refusal":
                return None
            if response.stop_reason == "max_tokens":
                print(f"  [ai] answer truncated at {budget} tokens - "
                      "retrying with more room")
                continue
            # answers can arrive split across several text blocks - join
            text = "".join(b.text for b in response.content
                           if b.type == "text")
            return json.loads(text)
        print("  [ai] answer still truncated after retry - giving up")
        return None
    except anthropic.AuthenticationError:
        print("  [ai] Anthropic auth failed - check your key / login")
        return None
    except anthropic.RateLimitError:
        print("  [ai] Anthropic rate limited - skipping")
        return None
    except Exception as e:
        print(f"  [ai] Anthropic call failed ({e})")
        return None


# --------------------------------------------------------- query expansion

EXPAND_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["queries"],
    "additionalProperties": False,
}

EXPAND_PROMPT = """You are helping a buyer search second-hand marketplaces \
(eBay, Craigslist) for: {query}{notes}

Sellers title their listings inconsistently. Give up to 4 ADDITIONAL search \
strings that would catch listings the original search misses - think shorter \
or alternate names, common misspellings or run-together spellings, model \
numbers, or how casual sellers phrase it. Each must still clearly target this \
item (marketplace search matches broadly - avoid words so generic they would \
return a different product). Do not repeat the original search string. Fewer \
than 3 is fine if the original already covers it well."""


def expand_query(query, notes, backend):
    """Return search query variants, original first. Fails open to [query]."""
    if not backend_ready(backend):
        return [query]
    prompt = EXPAND_PROMPT.format(
        query=query, notes=f"\nBuyer notes: {notes}" if notes else "")
    out = call_json(prompt, EXPAND_SCHEMA, backend, 1500)
    if not out:
        return [query]
    result = [query]
    seen = {query.lower()}
    for v in out.get("queries", []):
        v = str(v).strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            result.append(v)
    return result[:5]


# -------------------------------------------------------- variant analysis

VARIANT_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "relation": {"type": "string",
                                 "enum": ["essentially_same", "ambiguous",
                                          "different"]},
                    "note": {"type": "string"},
                },
                "required": ["name", "relation", "note"],
                "additionalProperties": False,
            },
        },
        "comment": {"type": "string"},
    },
    "required": ["variants", "comment"],
    "additionalProperties": False,
}

VARIANT_PROMPT = """A buyer is about to search second-hand marketplaces for: \
{query}{notes}

List closely-related things that sellers commonly list under similar titles, \
and classify each:
- "essentially_same": functionally the same item for a typical buyer \
(cosmetic or minor-upgrade variants of the same product) - safe to include
- "ambiguous": related but different in ways many buyers care about - a \
different product line, version, or tier sharing the name, or THIRD-PARTY / \
lookalike products commonly (sometimes deliberately misleadingly) titled \
with these same words
- "different": a different product that a buyer who typed this search does \
not want (successors, other models)

Also write "comment": one short sentence saying what you'd include and exclude.

Watch especially for same-name sub-variants: suffix models (zoom versions, \
number/letter suffixes like 170/80/110/VF/DLX/QD, Mark versions) that share \
the base name but are genuinely different products - never assume a suffixed \
model is the base model.

Rules: max 5 variants. If the buyer's notes already settle a question, do not \
raise it. If you don't know this product category well, return an empty \
variants list - never guess. Each note: a few words on how it differs."""


def analyze_variants(query, notes, backend):
    """Returns {"same": [...], "ambiguous": [...], "different": [...],
    "comment": str} or None. Each entry: {"name", "note"}."""
    if not backend_ready(backend):
        return None
    out = call_json(
        VARIANT_PROMPT.format(
            query=query, notes=f"\nBuyer notes: {notes}" if notes else ""),
        VARIANT_SCHEMA, backend, 2000)
    if out is None:
        return None
    grouped = {"same": [], "ambiguous": [], "different": [],
               "comment": out.get("comment", "")}
    seen = {query.lower()}
    for v in out.get("variants", []):
        name = str(v.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        bucket = {"essentially_same": "same", "ambiguous": "ambiguous",
                  "different": "different"}[v["relation"]]
        grouped[bucket].append({"name": name, "note": v.get("note", "")})
    return grouped


# --------------------------------------------------------- listing screen

SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["ok", "damaged", "parts", "accessory", "unrelated"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["id", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


# Title screening returns verdicts only - no prose, minimum output tokens.
TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["ok", "damaged", "parts", "accessory",
                                 "unrelated"],
                    },
                },
                "required": ["id", "verdict"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

PROMPT_TEMPLATE = """You are screening second-hand marketplace listings for a buyer.
The buyer wants exactly this item: {search_name}.{notes}

For every listing, decide whether its MAIN ARTICLE is that item itself:
- "ok": the listing sells the item the buyer wants, nothing suggesting problems
- "damaged": it is the item, but with significant damage (cracked, water \
damage, faulty, fungus/haze, missing key parts)
- "parts": it is the item but sold for parts / not working / untested repair project
- "accessory": the main article is an add-on FOR the item or its system, not \
the item itself: caps, hoods, cases, bags, boxes, manuals, warranty cards, \
chargers, cables, straps, remotes, filters, spare components (screens, \
boards, batteries)
- "unrelated": a different product, brand, model, size/focal length, or \
version/variant

Critical rules:
- The buyer's search words appearing in a title does NOT make it "ok". \
"Rear Lens Cap for Nikon 28mm f/2.8" contains the search words but is an \
accessory, not the lens.
- Titles shaped like "X for [the item]" or "fits [the item]" are almost \
always accessories.
- Judge relative to what the buyer wants: if they want a camera body, a lens \
is not it; if they want a specific lens, caps/hoods/manuals for that lens \
are accessories and other lenses are unrelated.
- If the item the buyer wants IS an accessory-type product (a cap, hood, \
charger, manual, controller), then listings selling that product are "ok" - \
"accessory" only means add-ons to what the buyer actually wants.
- Version/variant mismatches are "unrelated" (AI vs AI-S, Mark I vs II, a \
different focal length or aperture) unless the buyer's notes say otherwise.
- The item sold WITH extras included ("with caps", "bundle with games and \
charger", "w/ box") is still "ok" - a bundle CONTAINING the item counts as \
the item; a listing of only the extras does not.
- Successor or next-generation products that share the name ("<item> 2", \
"Mark II", "Pro", "new model") are "unrelated" - the buyer named the one \
they want.
- Seller grading shorthand (EXC+, MINT-, 9/10, "1:2.8" aperture notation) \
describes condition or specs, not damage. Do not invent problems the title \
doesn't state - a plain normal sale listing of the item is "ok".
- Use "parts" or "damaged" ONLY when the text actually indicates a problem. \
A listing that says it works is never "parts".
- Spelling variations of the same designation are identical (AIS = AI-S = \
Ai-s, MK2 = Mark II = mkii). Do not exclude over formatting.
- If the buyer's notes restrict to ONE specific variant ("ONLY ..."), keep a \
listing only when its text affirmatively shows it IS that variant. If the \
text doesn't establish which variant it is, classify "unrelated" with reason \
"variant unverified" - sellers of the premium variant nearly always name it.

Judge only from the text given. Return ONLY the number and verdict for each listing - no explanations.
{id_rule}

Listings (#number | price | condition | title):
{rows}"""


def _batch_rows(batch, with_desc=False):
    """Number listings 1..N - models reliably echo small numbers, unlike
    marketplace ids full of pipes and hashes."""
    rows = []
    for n, l in enumerate(batch, 1):
        row = f"#{n} | ${l.price:.0f} | {l.condition or '-'} | {l.title}"
        if with_desc:
            row += (f"\n    description: "
                    f"{l.extra.get('description') or '(none provided)'}")
        rows.append(row)
    return "\n".join(rows)


def _apply_verdicts(out, batch, results):
    """Map numbered verdicts back to listings. Returns listings that got no
    usable verdict (for retry)."""
    matched = set()
    for v in (out or {}).get("verdicts", []):
        raw = str(v.get("id", "")).strip().lstrip("#")
        try:
            idx = int(float(raw)) - 1
        except ValueError:
            continue
        if 0 <= idx < len(batch) and idx not in matched:
            results[batch[idx].id] = (v.get("verdict", "ok"),
                                      v.get("reason", ""))
            matched.add(idx)
    return [l for i, l in enumerate(batch) if i not in matched]


ID_RULE = """
Each listing starts with its number, like #7. In your verdicts, set "id" to \
that number as a string (e.g. "7"). You MUST return exactly one verdict for \
every listing number - never skip or invent numbers."""


DESC_TEMPLATE = """You are the second screening stage for a buyer shopping \
for: {search_name}.{notes}

These listings already passed a title-only screen. Now verify each against \
its full description - sellers bury problems there. Reclassify each id:
- "ok": description is consistent with a normal working example of the item
- "damaged": description reveals damage, faults, missing parts, heavy wear
- "parts": description says for parts / not working / untested / no returns \
on a suspicious cheap unit
- "accessory": description reveals it's actually an accessory, empty box, or \
component rather than the item
- "unrelated": description reveals it's a different product or model

Two extra rules: spelling variations of the same designation are identical \
(AIS = AI-S = Ai-s). And if the buyer's notes restrict to ONE specific \
variant, keep a listing only when the text affirmatively shows it IS that \
variant - unclear means "unrelated" with reason "variant unverified".
Stay strict but fair - normal used-item wear is still "ok". Reasons: 2-6 words; empty reason for "ok".
{id_rule}

Listings:
{rows}"""


def _screen_batches(listings, backend, prompt_for_batch, batch_size,
                    schema=SCREEN_SCHEMA):
    """Shared batching loop with one retry pass for unmatched listings."""
    results = {}
    unmatched = []
    for start in range(0, len(listings), batch_size):
        batch = listings[start:start + batch_size]
        out = call_json(prompt_for_batch(batch), schema, backend, 8000)
        unmatched.extend(_apply_verdicts(out, batch, results))
    if unmatched:
        for start in range(0, len(unmatched), batch_size):
            batch = unmatched[start:start + batch_size]
            out = call_json(prompt_for_batch(batch), schema,
                            backend, 8000)
            still = _apply_verdicts(out, batch, results)
            if still:
                print(f"  [ai] {len(still)} listings got no verdict even "
                      "after retry - kept unscreened")
    return results


def classify_descriptions(listings, search_name, notes, backend):
    """Second-stage screen over full descriptions. Same verdict shape as
    classify(). Listings with empty descriptions are judged on title alone."""
    if not listings or not backend_ready(backend):
        return {}

    def prompt_for(batch):
        return DESC_TEMPLATE.format(
            search_name=search_name,
            notes=f" Buyer notes: {notes}" if notes else "",
            id_rule=ID_RULE,
            rows=_batch_rows(batch, with_desc=True),
        )

    batch_size = 10 if backend.get("provider") == "local" else 20
    return _screen_batches(listings, backend, prompt_for, batch_size)


def classify(listings, search_name, notes, backend):
    """Return {listing_id: (verdict, reason)}. Missing ids mean 'not screened'."""
    if not listings:
        return {}
    if not backend_ready(backend):
        if backend.get("provider") == "local":
            print("  [ai] Ollama isn't reachable - skipping AI screen "
                  "(keyword filter still applied)")
        else:
            print("  [ai] no Anthropic credentials (key or Claude login) - "
                  "skipping AI screen (keyword filter still applied)")
        return {}
    def prompt_for(batch):
        return PROMPT_TEMPLATE.format(
            search_name=search_name,
            notes=f" Buyer notes: {notes}" if notes else "",
            id_rule=ID_RULE,
            rows=_batch_rows(batch),
        )

    batch_size = 25 if backend.get("provider") == "local" else 40
    return _screen_batches(listings, backend, prompt_for, batch_size,
                           schema=TITLE_SCHEMA)


# ----------------------------------------------------- early marker sweep

MARKER_SCHEMA = {
    "type": "object",
    "properties": {"markers": {"type": "array", "items": {"type": "string"}}},
    "required": ["markers"],
    "additionalProperties": False,
}

MARKER_PROMPT = """A buyer wants: {query}{notes}

Below are titles that passed a first screening. Some may still be WRONG \
variants/models (suffix models, zoom versions, different series) that slipped \
through. Return "markers": short, distinctive title substrings that identify \
wrong variants visible below - matching listings will be auto-removed. Use \
the shortest unmistakable substring (e.g. "170 VF", "f3.5"). NEVER mark \
cosmetic/date/color editions of the CORRECT model. Each marker must never \
appear in a correct listing's title. Empty list if the titles look clean.

Titles:
{rows}"""


def wrong_variant_markers(query, notes, titles, backend):
    """Wrong-variant title markers from a sample of kept titles, or []."""
    if not backend_ready(backend):
        return []
    out = call_json(
        MARKER_PROMPT.format(
            query=query, notes=f"\nBuyer notes: {notes}" if notes else "",
            rows="\n".join(t[:110] for t in titles[:50])),
        MARKER_SCHEMA, backend, 1000)
    if not out:
        return []
    return [str(m).strip() for m in out.get("markers", [])
            if len(str(m).strip()) >= 2][:6]


# ---------------------------------------------------------- search profile

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "comment": {"type": "string"},
        "include": {"type": "array", "items": {"type": "string"}},
        "exclude": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "markers": {"type": "array",
                                "items": {"type": "string"}},
                },
                "required": ["name", "markers"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["comment", "include", "exclude"],
    "additionalProperties": False,
}

PROFILE_PROMPT = """A buyer is setting up a second-hand marketplace search \
for: {query}{notes}

Draft their search profile:

- "include": the item itself plus variants that are essentially the same \
thing (cosmetic/date/color editions). Use the names sellers actually use.
- "exclude": related products the buyer does NOT want: different product \
lines/versions/tiers sharing the name, successors, zoom/budget editions, \
third-party lookalikes. For EACH exclusion, "markers" is the critical part: \
the exact short substrings that appear in REAL LISTING TITLES for that \
product - not catalog names. Sellers worldwide title things differently \
(e.g. the camera catalogs call "mju II Zoom 80" is titled "mju II 80 VF" by \
Japanese sellers) - enumerate the title vocabulary: model-number suffixes, \
regional names, abbreviations. These markers auto-reject listings, matched as \
whole words (a marker like "mju I" is safe - it cannot hit "mju II"). Rules:
  1. Shortest distinguishing form. Sellers rarely type full catalog names: \
prefer "80 VF" and "Zoom 80" over "mju II Zoom 80" (a title like "mju: II \
115 VF Zoom" contains "115 VF" but not the long phrase).
  2. Cover the ENTIRE numeric family, and over-enumerate: a marker for a \
model that doesn't exist rejects nothing (harmless), but one missing \
suffix lets that junk flood the results. If a zoom line has members \
around 80-170, list EVERY plausible number in that range in each seller \
form ("80 VF", "Zoom 80", ..., "170 VF", "Zoom 170").
  3. A marker must never appear in a correct listing's title.
  3b. If one WORD alone disqualifies a listing (e.g. "Zoom" when the \
wanted camera is fixed-lens, "Digital" for a film item), use that single \
word as its own marker - sellers often drop the model number entirely \
("mju II Zoom", "Stylus II ZOOM"), and only the single word catches all \
of those.
  4. Emit EVERY title form you know for a family, each at least once \
("Zoom 80" AND "80 VF" are different forms). Numeric families are \
auto-expanded later (every form x every number), so what matters most is \
that no FORM is missing.
- "comment": one or two sentences summarizing the plan and anything to \
verify before buying.

If the buyer's notes already give instructions, honor them. If you don't \
know this product category well, keep include/exclude minimal rather than \
guessing.{feedback}"""


def draft_profile(query, notes, backend, feedback="", prior=None):
    """Draft (or refine) a search profile. Returns the profile dict or None."""
    if not backend_ready(backend):
        return None
    fb = ""
    if feedback:
        fb = (f"\n\nThe buyer reviewed a previous draft and says: {feedback}"
              f"\nPrevious draft: {json.dumps(prior) if prior else '(none)'}"
              "\nProduce the corrected profile.")
    out = call_json(
        PROFILE_PROMPT.format(
            query=query,
            notes=f"\nBuyer notes: {notes}" if notes else "",
            feedback=fb),
        PROFILE_SCHEMA, backend, 5000)
    if not out:
        return None
    return {
        "comment": out.get("comment", ""),
        "include": [{"name": str(n).strip(), "enabled": True}
                    for n in out.get("include", []) if str(n).strip()][:8],
        "exclude": [{"name": str(e.get("name", "")).strip(),
                     "markers": [str(m).strip() for m in e.get("markers", [])
                                 if len(str(m).strip()) >= 2][:60],
                     "enabled": True}
                    for e in out.get("exclude", [])
                    if str(e.get("name", "")).strip()][:8],
        "manual_rejects": [],
        "guidance": "",
    }
