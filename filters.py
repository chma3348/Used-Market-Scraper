"""Keyword-based first-pass categorizer for listings (free, catches the obvious).

Instead of a plain drop-list, phrases are categorized so searches can run in
different condition modes: "working" keeps clean listings, "parts" keeps only
broken/for-parts listings (repair donors), "any" keeps both.
"""

import functools
import re

# Phrases that almost always mean parts / damage. Matched case-insensitively
# against the listing title and eBay's condition field.
DAMAGE_PHRASES = [
    "for parts", "parts only", "for part", "spares or repair", "spares/repair",
    "not working", "doesn't work", "does not work", "doesnt work", "non working",
    "no power", "won't turn on", "wont turn on", "won't power", "wont power",
    "broken", "cracked", "shattered", "damaged", "water damage", "liquid damage",
    "faulty", "defective", "bad screen", "bad battery", "no display",
    "as-is", "sold as is", "sold as-is", "for repair", "needs repair",
    "icloud locked", "activation lock", "bad esn", "blacklisted",
    "read description", "read desc",
]

# Phrases that mean the listing is an accessory/part-of-package, not the item.
ACCESSORY_PHRASES = [
    "box only", "empty box", "case only", "manual only", "cable only",
    "charger only", "stand only", "remote only",
    "lens cap", "body cap", "rear cap", "front cap", "lens hood",
    "instruction manual", "owner's manual", "owners manual", "user manual",
    "instruction book", "warranty card",
]

# eBay condition strings that mean parts/not-working.
DAMAGE_CONDITIONS = [
    "for parts or not working",
]


@functools.lru_cache(maxsize=8192)
def _norm(s):
    """Lowercase, punctuation-collapsed, space-padded for whole-word matching.

    A decimal point BETWEEN DIGITS survives, so "f2.8" stays one token. Without
    that, "f2.8" collapsed to "f2 8" and the marker "f2" (meaning the f/2 lens)
    word-matched every f/2.8 listing - silently killing the exact lenses the
    search was for."""
    s = s.lower()
    s = re.sub(r"(?<=\d)[.](?=\d)", "\u0000", s)      # protect real decimals
    s = re.sub(r"[^a-z0-9%\u0000]+", " ", s)
    return " " + " ".join(s.replace("\u0000", ".").split()) + " "


def reject_hit(title, keywords):
    """First reject keyword present in the title as WHOLE WORDS, else None.
    Plain substring matching is a trap here: the marker "mju I" is a
    substring of every "mju II" title. Both sides are lowercased and
    punctuation-collapsed, so "mju-I" / "mju: I" still match "mju I" but
    never "mju II"."""
    t = _norm(title)
    for k in keywords or []:
        nk = _norm(k)
        if nk.strip() and nk in t:
            return k
    return None


def keyword_category(listing, query=""):
    """Return (category, reason) if a keyword rule decides this listing:
    category is "parts" or "accessory". Return None if undecided (clean so far -
    the AI screen makes the final call).

    Query-aware: an accessory phrase that appears in the buyer's own search
    ("nikon 28mm lens cap") is skipped - the accessory IS the item then."""
    title = listing.title.lower()
    query = (query or "").lower()
    for phrase in DAMAGE_PHRASES:
        if phrase in title:
            return ("parts", f'title contains "{phrase}"')
    for phrase in ACCESSORY_PHRASES:
        if phrase in title and phrase not in query:
            return ("accessory", f'title contains "{phrase}"')
    condition = (listing.condition or "").lower()
    for bad in DAMAGE_CONDITIONS:
        if bad in condition:
            return ("parts", f'condition is "{listing.condition}"')
    return None
