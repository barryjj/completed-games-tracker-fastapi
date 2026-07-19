"""Platform-neutral title heuristics.

Lifted out of steam.py for the PSN sync (#135): collection detection and
title cleaning are pure title-keyword logic, not Steam-specific, and both
sync paths must share ONE implementation covered by the same
test_integrations cases (an earlier substring-scan copy produced false
positives like "Recollection" — those tests guard against reintroducing it).
steam.py re-exports these names so existing imports and tests are untouched.
"""

import html
import re

# Symbols platforms append to titles but are meaningless for display
_JUNK_RE = re.compile(r"[™®©]+")

# Collection-detection regex.  Only word-boundary matches to avoid false
# positives from words like "Recollection" or "Legacy".  "collection" is
# anchored to end-of-title (optionally followed by a volume indicator like
# "Vol.1") so "Master Collection Vol.1 Bonus Content" is NOT flagged — the
# trailing words push it past the anchor.
_COLLECTION_RE = re.compile(
    r"""
    \btrilogy\b |
    \bcompilation\b |
    \bcomplete\s+pack\b |
    # "collection" only qualifies at/near end of title — e.g. "Mega Man
    # Legacy Collection" yes, "Post Modern Collection" (DLC) handled by
    # the is_dlc guard in _infer_is_collection.
    \bcollection\b ( \s* (vol\.?\s*\d+ | volume\s+\d+) )? \s* $
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Explicit allowlist of acronyms and Roman numerals to preserve as uppercase
# during title-case normalization. Length-based "looks like an acronym" rules
# produced false positives (OF, OPS, etc. — short English words that happen to
# render in all caps when shouting). Better to leave new acronyms title-cased
# and let the user fix them manually (display_name_user_set protects the edit).
_PRESERVE_UPPER = {
    # Roman numerals I–XX
    "I",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "VII",
    "VIII",
    "IX",
    "X",
    "XI",
    "XII",
    "XIII",
    "XIV",
    "XV",
    "XVI",
    "XVII",
    "XVIII",
    "XIX",
    "XX",
    # Common gaming acronyms / franchise IDs
    "GTA",
    "FTL",
    "RE",
    "MGS",
    "COD",
    "BFG",
    "FPS",
    "RPG",
    "MMO",
    "MMORPG",
    "JRPG",
    "ARPG",
    "VR",
    "AR",
    "AI",
    "HD",
    "UHD",
    "DLC",
    "OST",
    "GOTY",
    "NPC",
    "HUD",
    "UI",
    "PVE",
    "PVP",
    "PUBG",
    "TES",
    "GTAV",
}


def _is_loud_caps(s: str) -> bool:
    """True when a string looks like SHOUTING that should be title-cased.
    Single-word and short titles (DOOM, FTL) are left alone."""
    if len(s) < 8 or " " not in s:
        return False
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) > 0.95


def _smart_title_case(s: str) -> str:
    """Title-case a string while preserving listed acronyms / Roman numerals
    and apostrophe contractions ("Assassin's" not "Assassin'S"). Idempotent."""
    out = []
    for word in s.split(" "):
        alpha = "".join(c for c in word if c.isalpha())
        if word.isupper() and alpha and alpha in _PRESERVE_UPPER:
            out.append(word)
        else:
            tc = word.title()
            # str.title() does "Don'T" — lowercase the letter after apostrophe
            tc = re.sub(r"'(\w)", lambda m: "'" + m.group(1).lower(), tc)
            out.append(tc)
    return " ".join(out)


def _infer_is_collection(title: str, is_dlc: bool = False) -> bool:
    """DLC can never be a collection regardless of title keywords."""
    if is_dlc:
        return False
    return bool(_COLLECTION_RE.search(title or ""))


def _clean_title(title: str) -> str:
    """Return title with HTML entities unescaped, trademark/copyright symbols
    stripped, and whitespace normalised. Idempotent.

    Platform name catalogs sometimes include HTML-encoded characters
    (e.g. ``&amp;`` for ``&``, ``&quot;`` for ``"``).  We unescape those
    before stripping junk so display_name shows clean text.

    We used to also title-case loud ALL-CAPS titles ("ELDEN RING NIGHTREIGN"
    → "Elden Ring Nightreign") but that produced inconsistent results when
    titles mixed cases (only whole-string ALL CAPS triggered, so DLC names
    like "ELDEN RING NIGHTREIGN The Forsaken Hollows" passed through
    unchanged). Decision: leave the platform's casing alone. If a user
    dislikes a SHOUTING title, the edit modal lets them override
    display_name."""
    return _JUNK_RE.sub("", html.unescape(title)).strip()
