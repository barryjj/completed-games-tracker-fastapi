"""Platform-neutral title heuristics.

Lifted out of steam.py for the PSN sync (#135): collection detection and
title cleaning are pure title-keyword logic, not Steam-specific, and both
sync paths must share ONE implementation covered by the same
test_integrations cases (an earlier substring-scan copy produced false
positives like "Recollection" — those tests guard against reintroducing it).
steam.py re-exports these names so existing imports and tests are untouched.
"""

import functools
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
    return re.sub(r"\s+", " ", _JUNK_RE.sub("", html.unescape(title))).strip()


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
# Sony sometimes appends the platform to the title itself ("Rocketbirds PS
# Vita", "Grounded PS4 & PS5"). Meaningless for identity.
_PLATFORM_SUFFIX_RE = re.compile(
    r"\s+(?:for\s+)?(?:ps\s?vita|playstation\s?vita|psvita|ps\s?portable|psp|"
    r"ps\s?[345](?:\s*(?:&|and)\s*ps\s?[345])*|playstation\s?[345]?)\s*$",
    re.IGNORECASE,
)

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


# Pure and hammered: one pass over the PSN review queue called this 467,858
# times for 567 rows, because cross_buy_exception re-normalizes both sides
# of every comparison against a long exception list. Titles repeat heavily,
# so caching turns that into a few hundred real computations and was worth
# ~10s of the queue's 12s build.
@functools.lru_cache(maxsize=8192)
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
        s = _PLATFORM_SUFFIX_RE.sub("", s)
    s = s.lower()
    # Punctuation (and newlines — platforms really do ship those inside titles)
    # becomes space, so hyphenation and curly quotes stop mattering.
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    out = []
    for word in s.split():
        out.append(_WORD_NUMBERS.get(word) or _roman_to_arabic(word) or word)
    return " ".join(out)


def _numeric_tokens(words: list[str]) -> list[str]:
    return [w for w in words if w.isdigit()]


def titles_match(a: str | None, b: str | None) -> str | None:
    """How confidently two titles refer to the same game.

    Returns:
      "exact"     — same after folding, including spaceless ("SOULCALIBUR" vs
                    "Soul Calibur"). Safe to act on.
      "contained" — one is a word-boundary prefix or suffix of the other. A
                    strong hint, NOT a fact: platforms and people disagree about
                    subtitles ("Uncharted" vs "Uncharted: Drake's Fortune") and
                    about franchise prefixes, where Sony's title is the shorter
                    one ("The Elder Scrolls V: Skyrim" is just "Skyrim"). But
                    the same shape also covers genuinely different products —
                    "Peggle Nights" is an expansion, not "Peggle". Callers
                    should surface these for confirmation rather than merge them.
      None        — no match.

    Guards that keep "contained" usable: the dropped words must not begin with a
    number, which is what separates a subtitle from a sequel ("Uncharted" never
    reaches "Uncharted 2"); a lone short extra word is rejected ("Hitman GO" is
    not "HITMAN"); and numeric components must agree throughout, so "Final
    Fantasy XII" can never reach "XVI" (#160).
    """
    na, nb = normalize_for_match(a), normalize_for_match(b)
    if not na or not nb:
        return None
    if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
        return "exact"

    short, long_ = sorted((na.split(), nb.split()), key=len)
    if not short:
        return None
    if short == long_[: len(short)]:
        extra = long_[len(short) :]
    elif short == long_[-len(short) :]:
        extra = long_[: -len(short)]
    else:
        return None

    # A leading number in the dropped words means the longer title is a
    # different entry in the series, not the same game with a subtitle.
    if extra and extra[0].isdigit():
        return None
    # A single SHORT extra word is more often a different game than a dropped
    # subtitle — "Hitman GO" is not "HITMAN". Longer single words are usually a
    # franchise prefix the other side omits ("Oddworld: Stranger's Wrath HD"),
    # so length is the discriminator. Imperfect, which is why this tier is a
    # suggestion rather than a match.
    if len(extra) == 1 and len(extra[0]) <= 3:
        return None
    # Numbers inside the shared portion must agree.
    if _numeric_tokens(short) != _numeric_tokens([w for w in long_ if w not in extra]):
        return None
    return "contained"
