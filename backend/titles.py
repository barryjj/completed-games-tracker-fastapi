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
import unicodedata

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


# ─── Match normalization ────────────────────────────────────────────────────
# ONE folding used by every matcher (spreadsheet import, PSN merge). Display
# always keeps the platform's own styling — this is only ever for comparison.
#
# A coverage audit of 383 spreadsheet rows against real PSN data (#180) found
# most "missing" games were present and simply failed to match, because the
# PSN-side folding *deleted* characters instead of folding them: ABZÛ -> "abz",
# NINJA GAIDEN Σ2 -> "ninjagaiden2", STREET FIGHTER Ⅳ -> "streetfighter".

# NFKD handles accents and unicode Roman numerals (Û->U, Ⅳ->IV, ö->o) but
# leaves Greek alone — and platforms do use it in titles.
_LOOKALIKES = {
    "Σ": "sigma",
    "σ": "sigma",
    "Ω": "omega",
    "ω": "omega",
    "α": "alpha",
    "β": "beta",
    "×": "x",
    "＋": "+",
    "&": " and ",
}

# Sony appends these to trophy-set names ("God of War® II Trophies",
# "TEKKEN 6 Trophy Set", "Slayaway Camp trophies"). _strip_trophy_suffix in
# psn.py handles display; matching needs it too or the suffix defeats every
# comparison.
_MATCH_TROPHY_SUFFIX_RE = re.compile(
    r"\s+(?:trophies|trophy(?:\s+(?:set|pack|collection|list))?)[.!]?\s*$",
    re.IGNORECASE,
)

# Disambiguators and port/edition markers platforms add or drop freely:
# "Hitman (2016)" vs "HITMAN", "Resident Evil 4 HD" vs "resident evil 4",
# "PaRappa The Rapper Remastered" vs the sheet's plain title. Removed for
# comparison only — the display title keeps them.
_PARENTHETICAL_RE = re.compile(r"\s*\((?:\d{4}|remake|remaster(?:ed)?|hd|classic)\)", re.IGNORECASE)
_EDITION_SUFFIX_RE = re.compile(
    r"\s+(?:hd|remastered|remaster|definitive\s+edition|complete\s+edition|"
    r"game\s+of\s+the\s+year(?:\s+edition)?|goty(?:\s+edition)?|"
    r"anniversary\s+edition|special\s+edition|deluxe\s+edition|gold\s+edition)\s*$",
    re.IGNORECASE,
)

_WORD_NUMBERS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}

_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
_ROMAN_RE = re.compile(r"^(?=[ivxlcdm]+$)m*(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")


def _roman_to_arabic(token: str) -> str | None:
    """Convert a Roman-numeral token to its arabic value.

    Single letters are deliberately NOT converted: the "X" in Soldner-X is part
    of the name, not a numeral, and turning it into 10 made that title
    unmatchable. Both sides of a comparison leave single letters alone, so
    "Street Fighter V" still matches "STREET FIGHTER V".
    """
    if len(token) < 2 or not _ROMAN_RE.match(token):
        return None
    total, prev = 0, 0
    for ch in reversed(token):
        val = _ROMAN_VALUES[ch]
        total = total - val if val < prev else total + val
        prev = max(prev, val)
    return str(total)


def normalize_for_match(title: str | None) -> str:
    """Fold a title to its comparable form: lowercase words, no punctuation,
    accents and look-alikes folded, trophy suffixes gone, numbers canonical.

    Returns a space-separated string. Callers should usually compare the
    SPACELESS form too — platforms disagree about word breaks ("SOULCALIBUR" vs
    "Soul Calibur", "BLADECHIMERA" vs "Blade Chimera"), and that difference is
    never meaningful.
    """
    if not title:
        return ""
    s = str(title)
    for glyph, repl in _LOOKALIKES.items():
        s = s.replace(glyph, f" {repl} ")
    # Trademark glyphs go FIRST: NFKD expands ™ into the letters "TM", so
    # stripping afterwards would leave "Stellar Blade™" as "stellarbladetm".
    s = _JUNK_RE.sub("", s)
    # Fold accents and unicode numerals, then drop the combining marks. Keeps
    # non-Latin scripts intact (nothing to decompose), so a Japanese title
    # doesn't normalize away to nothing.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _MATCH_TROPHY_SUFFIX_RE.sub("", s)
    s = _PARENTHETICAL_RE.sub("", s)
    # Loop: a title can carry several ("... HD Remastered").
    prev = None
    while prev != s:
        prev = s
        s = _EDITION_SUFFIX_RE.sub("", s)
    s = s.lower()
    # Punctuation (and newlines — platforms really do ship those inside titles)
    # becomes space, so hyphenation and curly quotes stop mattering.
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    out = []
    for word in s.split():
        out.append(_WORD_NUMBERS.get(word) or _roman_to_arabic(word) or word)
    return " ".join(out)
