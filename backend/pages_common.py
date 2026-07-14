"""Shared page infrastructure: the Jinja environment and its filters, plus the
context/visual helpers the domain page modules build their responses from.

Split out of `pages.py` so each domain module (library, import, match review,
completions, account) can import the shared pieces without importing every other
domain's routes. Moved verbatim — no behaviour changes.
"""

import datetime
import html as _html
import logging
import os
import re

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session, joinedload

from . import importer, match_review, models, users
from .models import get_db

logger = logging.getLogger(__name__)

TEMPLATES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _platform_color_class(val) -> str:
    """Jinja filter — accepts a Platform model instance or a raw platform name string.

    For linked Platform rows, returns tag-platform-{color} using the stored accent.
    Falls back to models._platform_heuristic_css() for unlinked string values.
    """
    if hasattr(val, "css_class"):
        return val.css_class
    return models._platform_heuristic_css(str(val))


templates.env.filters["platform_color"] = _platform_color_class


def _grid_cover_url(entry, orientation: str) -> str | None:
    """Pick the right cover URL for the grid view's orientation. Returns None
    when there is no art for the required orientation — no cross-orientation
    borrowing (stretched/squished art looks worse than a clean placeholder).

    Resolution order:
      1. UserArtwork for this entry — explicit pick (SGDB picker, user upload).
      2. Valid GameArtwork for this release — native sources (steam/psn) before
         sgdb to prefer the authoritative art when it exists.

    Vertical mode wants cover_v (library_600x900.jpg / portrait art).
    Horizontal mode wants cover_h (header.jpg / landscape header).
    IGDB/manual entries without a horizontal cover should use the SGDB picker
    to find one; that UserArtwork is then found in step 1."""
    wanted = "cover_v" if orientation == "grid_v" else "cover_h"
    # 1. Explicit user pick
    for ua in entry.user_artwork:
        if ua.artwork_type == wanted and ua.url:
            return ua.url
    # 2. Valid GameArtwork — native sources preferred
    native_url = None
    sgdb_url = None
    for art in entry.release.artwork:
        if art.artwork_type == wanted and art.is_valid and art.url:
            if art.source in ("steam", "psn"):
                if native_url is None:
                    native_url = art.url
            elif sgdb_url is None:
                sgdb_url = art.url
    return native_url or sgdb_url


templates.env.filters["grid_cover_url"] = _grid_cover_url


def _playtime_human(minutes) -> str:
    """Convert an integer playtime in minutes to a Steam-style readable string.

    Format rules match Steam's library display convention:
      < 60 min:   "N minutes"     ("45 minutes")
      >= 60 min:  "X.Y hours"     ("4,507.2 hours") — one decimal, thousands sep
      0 / None:   ""
    """
    if not minutes:
        return ""
    total = int(minutes)
    if total < 60:
        return f"{total} minutes"
    hours = total / 60
    return f"{hours:,.1f} hours"


templates.env.filters["playtime_human"] = _playtime_human


def _html_unescape(s: str) -> str:
    """Unescape HTML entities in a string (e.g. ``&amp;`` → ``&``).
    Used in templates to clean Steam API text that sometimes contains
    encoded entities before Jinja2 re-escapes for safe HTML output."""
    return _html.unescape(s or "")


templates.env.filters["html_unescape"] = _html_unescape


def _completion_date(obj) -> str:
    """Format a Completion/ImportRow's completed_at according to its
    completed_at_precision ('day' | 'month' | 'year' | None). completed_at
    itself always holds a full date (fabricated day/month for coarser
    precision, for sorting) — this controls what's actually shown so a
    historical import that only knew "2012" doesn't render as a fake
    "January 1, 2012"."""
    d = getattr(obj, "completed_at", None)
    if not d:
        return "Unknown"
    precision = getattr(obj, "completed_at_precision", None) or "day"
    if precision == "year":
        return str(d.year)
    if precision == "month":
        return d.strftime("%B %Y")
    return d.strftime("%B %-d, %Y")


templates.env.filters["completion_date"] = _completion_date


def _completion_month(obj) -> str:
    """Ballpark variant of completion_date: month + year (or bare year for
    year-precision rows). Used where a list only needs 'a sense of the
    years', e.g. the import review completion slats."""
    d = getattr(obj, "completed_at", None)
    if not d:
        return "Unknown"
    if (getattr(obj, "completed_at_precision", None) or "day") == "year":
        return str(d.year)
    return d.strftime("%B %Y")


templates.env.filters["completion_month"] = _completion_month


def _titles_differ(a, b) -> bool:
    """Whether two titles are meaningfully different — judged by the same
    normalization the import matcher uses, so typographic noise (curly vs
    straight apostrophes, ™, dash styles) never flags an 'uncertain match'
    the matcher itself considered exact."""
    na = importer._normalize_title(str(a or ""))
    nb = importer._normalize_title(str(b or ""))
    # Spaceless comparison mirrors the matcher's exact tier (BLADECHIMERA
    # vs Blade Chimera) — if the matcher calls it exact, don't flag it.
    return na != nb and na.replace(" ", "") != nb.replace(" ", "")


templates.env.filters["titles_differ"] = _titles_differ


def _release_year(release) -> str | None:
    """Best-effort release year for disambiguating identically-titled
    entries (the two Preys, both 'Prey (Steam)'). Steam appdetails first,
    IGDB metadata as fallback; None renders as nothing."""
    raw = getattr(release, "raw_data", None) or {}
    date_str = (((raw.get("appdetails") or {}).get("release_date")) or {}).get("date") or ""
    m = re.search(r"(?:19|20)\d{2}", date_str)
    if m:
        return m.group(0)
    year = (raw.get("igdb") or {}).get("year")
    return str(year) if year else None


templates.env.filters["release_year"] = _release_year

_URL_RE = re.compile(r"https?://[^\s<]+")


def _linkify(text: str | None) -> Markup:
    """Escape text, then turn bare http(s):// URLs into clickable links.
    Escaping happens first so the filter is safe to use directly in place
    of Jinja's normal auto-escaping — nothing in the input can inject HTML,
    only recognized URL substrings become anchor tags."""
    if not text:
        return Markup("")
    escaped = str(escape(text))

    def _replace(m: re.Match) -> str:
        url = m.group(0)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'

    return Markup(_URL_RE.sub(_replace, escaped))


templates.env.filters["linkify"] = _linkify


def _base_ctx(db: Session, user: models.User) -> dict:
    """Common context vars injected into every full-page response."""
    return {
        "pending_matches": match_review.pending_count(db, user),
    }


# How long Steam appdetails can sit before we consider it stale enough to
# auto-refresh on next detail-pane open. 7 days balances "user sees current
# data when they actually look" against burning API calls on every click.
_METADATA_STALENESS_DAYS = 7


def _needs_metadata_refresh(release) -> bool:
    """True when a Steam release's cached appdetails is missing or older than
    the staleness threshold. Used by the detail-pane endpoints to decide
    whether to fire a background refresh. Only Steam — other sources don't
    have an appdetails endpoint to refresh against."""
    if release.source != "steam" or not release.external_id:
        return False
    fetched_at = release.metadata_fetched_at
    if fetched_at is None:
        return True
    # SQLite stores DateTime columns as offset-naive even though our model
    # declares tz-aware. Treat any naive value as UTC so the subtraction
    # below doesn't blow up the entire detail pane (which it did, returning
    # a 500 and rendering as a blank offcanvas).
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=datetime.UTC)
    age = datetime.datetime.now(datetime.UTC) - fetched_at
    return age.days >= _METADATA_STALENESS_DAYS


# Steam category IDs worth surfacing in the detail pane. Filters out store
# housekeeping entries (Steam Cloud, Trading Cards, Family Sharing) and the
# accessibility sub-tags Steam added in their 2024 revamp.
_GAMEPLAY_CATEGORY_IDS = {
    1,  # Multi-player
    2,  # Single-player
    9,  # Co-op
    18,  # Partial Controller Support
    24,  # Shared/Split Screen
    27,  # Cross-Platform Multiplayer
    28,  # Full controller support
    31,  # VR Support
    36,  # Online PvP
    38,  # Online Co-op
    39,  # Shared/Split Screen Co-op
    44,  # Remote Play Together
    49,  # PvP
    53,  # VR Supported
    54,  # VR Only
}


_STEAM_DATE_FORMATS = [
    "%b %d, %Y",  # "Aug 12, 2016"   ← most common US format
    "%B %d, %Y",  # "August 12, 2016"
    "%d %b, %Y",  # "28 May, 2026"   ← day-first with comma (UK/EU locale)
    "%d %B, %Y",  # "28 May, 2026"   full month name
    "%d %b %Y",  # "28 May 2026"    no comma
    "%d %B %Y",  # "28 May 2026"    full month, no comma
    "%b %Y",  # "Aug 2016"       month + year only
    "%B %Y",  # "August 2016"
]


def _normalize_steam_date(raw: str) -> str:
    """Normalize Steam's free-form release date strings to 'Mon D, YYYY'.

    Steam has no enforced format — titles come back as 'Aug 12, 2016',
    '28 May, 2026', '28 May 2026', 'Q2 2024', 'Coming soon', etc. We try
    the common parse patterns and fall back to the raw string for anything
    we can't handle (quarter strings, 'Coming soon', bare years).
    """
    if not raw:
        return raw
    for fmt in _STEAM_DATE_FORMATS:
        try:
            dt = datetime.datetime.strptime(raw.strip(), fmt)
            # Month+year-only: omit the day so we don't invent one.
            if "%d" not in fmt:
                return dt.strftime("%B %Y")
            return dt.strftime("%B %-d, %Y")
        except ValueError:
            continue
    return raw  # Q1 2024, Coming soon, bare year, etc. — pass through as-is


def _extract_igdb_meta(release: "models.GameRelease") -> dict:
    """Pull IGDB metadata from release.description and release.raw_data['igdb'].

    Returns a dict with keys: summary, genres, year.
    Callers should guard with `if igdb_meta.summary` etc.
    """
    igdb = (release.raw_data or {}).get("igdb") or {}
    return {
        "summary": release.description or "",
        "genres": igdb.get("genres") or [],
        "year": igdb.get("year"),
    }


def _extract_steam_meta(appdetails: dict) -> dict:
    """Pull display-ready fields from a cached appdetails payload.

    Returns a dict with only the keys that have usable values — callers
    (templates) should guard with `if steam_meta.x` rather than assume
    presence. Publisher is omitted when it matches the developer exactly
    (very common for indie studios).
    """
    genres = [g["description"] for g in (appdetails.get("genres") or [])]
    features = [c["description"] for c in (appdetails.get("categories") or []) if c.get("id") in _GAMEPLAY_CATEGORY_IDS]
    devs = appdetails.get("developers") or []
    pubs = appdetails.get("publishers") or []

    metacritic = appdetails.get("metacritic") or {}
    release_date = _normalize_steam_date((appdetails.get("release_date") or {}).get("date") or "")

    return {
        "released": release_date,
        "developers": devs,
        "publishers": pubs,
        "genres": genres,
        "features": features,
        "metacritic_score": metacritic.get("score"),
        "metacritic_url": metacritic.get("url"),
        "website": (appdetails.get("website") or "").strip() or None,
    }


_STEAM_CDN_BASE = "https://cdn.akamai.steamstatic.com/steam/apps"


def _build_detail_pane_visuals(db: Session, entry, game, release) -> dict:
    """Compute the visual chrome (hero, logo, header, parent info) for a
    library detail pane render. Centralized so both library and completion
    detail endpoints use the same logic.

    Resolution order (per art type):
      1. UserArtwork for this entry  — explicit user pick
      2. Valid GameArtwork for this release — native sources (steam/psn) first

    For DLC, the parent's hero/logo are the right fallback default since Steam
    rarely issues distinct art for DLC appids.
    """
    parent_release = None
    parent_game = None
    parent_entry_id = None
    if game.parent_id:
        parent_release = (
            db.query(models.GameRelease)
            .options(joinedload(models.GameRelease.artwork), joinedload(models.GameRelease.game))
            .filter(models.GameRelease.game_id == game.parent_id, models.GameRelease.source == "steam")
            .first()
        )
        if parent_release:
            parent_game = parent_release.game
            # Find the user's library entry for the parent (if they own it) so
            # the breadcrumb can swap the pane to the parent's detail.
            parent_entry = db.query(models.UserLibraryEntry).filter_by(user_id=entry.user_id, release_id=parent_release.id).first()
            if parent_entry:
                parent_entry_id = parent_entry.id

    def _user_art_url(art_type: str) -> str | None:
        """First UserArtwork URL for this entry matching art_type."""
        for ua in entry.user_artwork:
            if ua.artwork_type == art_type and ua.url:
                return ua.url
        return None

    def _art_url(rel, art_type: str) -> str | None:
        """Best valid GameArtwork URL for a release.

        Priority: steam/psn (first-party) > igdb (official) > sgdb (community).
        """
        if not rel:
            return None
        native = None  # steam / psn
        igdb_url = None  # igdb official art
        sgdb_url = None  # sgdb / other
        for art in rel.artwork:
            if art.artwork_type == art_type and art.is_valid and art.url:
                if art.source in ("steam", "psn"):
                    if native is None:
                        native = art.url
                elif art.source == "igdb":
                    if igdb_url is None:
                        igdb_url = art.url
                elif sgdb_url is None:
                    sgdb_url = art.url
        return native or igdb_url or sgdb_url

    def _steam_logo_url(rel) -> str | None:
        # Logo isn't captured as GameArtwork — construct it from the appid.
        # May 404; handled client-side by onerror auto-fetch from SGDB.
        if not rel or rel.source != "steam" or not rel.external_id:
            return None
        return f"{_STEAM_CDN_BASE}/{rel.external_id}/logo.png"

    # Header (460x215): UserArtwork pick > valid GameArtwork(cover_h)
    header_url = _user_art_url("cover_h") or _art_url(release, "cover_h")
    fallback_header_url = _art_url(parent_release, "cover_h")

    # Hero (~1920x620): UserArtwork pick > valid GameArtwork(hero)
    hero_url = _user_art_url("hero") or _art_url(release, "hero")
    fallback_hero_url = _art_url(parent_release, "hero")

    # Logo (transparent PNG): UserArtwork pick > Steam CDN constructed URL
    logo_url = _user_art_url("logo") or _steam_logo_url(release)
    fallback_logo_url = _steam_logo_url(parent_release) if parent_release else None

    # Compute a "subtitle" for DLC display_title — what's left after stripping
    # Parent appid — shown alongside the parent name in the parent row of
    # the metadata block. Just the Steam external_id; safe to be None.
    parent_appid = parent_release.external_id if parent_release else None

    # Contextual label + chip class for the parent relationship.
    # The template uses these to render the label as a colored chip
    # ([DLC FOR] peach / [IN COLLECTION] teal) instead of plain muted text,
    # so the relationship has the same visual punch as the entry-type chips
    # in the badges row above. parent_label_class maps to the existing
    # .tag-badge variant for that color.
    parent_label = None
    parent_label_class = None
    if parent_game:
        if game.is_dlc:
            parent_label = "Base Game"
            parent_label_class = "tag-dlc"
        else:
            parent_label = "Collection"
            parent_label_class = "tag-in-collection"

    # parent_release_id + parent_edit_label are used by the Edit button in the
    # detail pane to pre-fill the edit modal's parent search input.
    parent_release_id = parent_release.id if parent_release else None
    parent_edit_label = f"{parent_game.display_title} ({parent_release.platform})" if parent_game and parent_release else ""

    return {
        "header_url": header_url,
        "fallback_header_url": fallback_header_url,
        "hero_url": hero_url,
        "fallback_hero_url": fallback_hero_url,
        "logo_url": logo_url,
        "fallback_logo_url": fallback_logo_url,
        "parent_game": parent_game,
        "parent_entry_id": parent_entry_id,
        "parent_appid": parent_appid,
        "parent_label": parent_label,
        "parent_label_class": parent_label_class,
        "parent_release_id": parent_release_id,
        "parent_edit_label": parent_edit_label,
    }


def _attach_parent_fallbacks(db: Session, entries, current_user=None) -> None:
    """For each entry whose game has a parent (DLC -> base game), look up the
    parent's Steam artwork and stamp `_fallback_v` / `_fallback_h` URLs onto
    the entry as transient attributes. Templates read these and emit them as
    `data-fallback` so cgtCoverFallback() can degrade to parent art when the
    DLC's own cover/header 404s.

    Also stamps `_parent_label` and `_parent_release_id` for the edit modal:
    the parent's display title + platform, and its release ID in the user's
    library. Requires current_user to look up the library entry — omit (or
    pass None) for callers where the user isn't available (completions pane).

    One batched query per data type — avoids N+1 across long lists."""
    parent_ids = {e.release.game.parent_id for e in entries if e.release.game.parent_id}
    parent_art: dict[int, dict[str, str | None]] = {}
    parent_meta: dict[int, dict] = {}  # game_id → {label, release_id}

    if parent_ids:
        rows = (
            db.query(models.GameRelease)
            .options(joinedload(models.GameRelease.artwork))
            .filter(
                models.GameRelease.game_id.in_(parent_ids),
                models.GameRelease.source == "steam",
            )
            .all()
        )
        for r in rows:
            d: dict[str, str | None] = {"cover_v": None, "cover_h": None}
            for art in r.artwork:
                if art.artwork_type in d and not d[art.artwork_type] and art.is_valid:
                    d[art.artwork_type] = art.url
            parent_art[r.game_id] = d

        if current_user is not None:
            # One query: find the user's library entry for each parent game so
            # the edit modal can pre-select the right release.
            parent_entries = (
                db.query(models.UserLibraryEntry)
                .options(joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game))
                .join(models.GameRelease)
                .filter(
                    models.UserLibraryEntry.user_id == current_user.id,
                    models.GameRelease.game_id.in_(parent_ids),
                )
                .all()
            )
            for pe in parent_entries:
                game = pe.release.game
                parent_meta[game.id] = {
                    "label": f"{game.display_title} ({pe.release.platform})",
                    "release_id": pe.release.id,
                }

    for e in entries:
        parent_id = e.release.game.parent_id
        p = parent_art.get(parent_id) if parent_id else None
        e._fallback_v = (p or {}).get("cover_v")
        e._fallback_h = (p or {}).get("cover_h")
        meta = parent_meta.get(parent_id) if parent_id else None
        e._parent_label = meta["label"] if meta else ""
        e._parent_release_id = meta["release_id"] if meta else ""


def _get_all_platforms(db: Session) -> list[models.Platform]:
    """Return all Platform rows ordered by name for dropdown/datalist use."""
    return (
        db.query(models.Platform)
        .options(joinedload(models.Platform.aliases), joinedload(models.Platform.family))
        .order_by(models.Platform.name)
        .all()
    )


def get_web_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Dependency for page routes — raises RequiresLoginException instead of 401."""
    from .main import RequiresLoginException

    token = request.cookies.get("session")
    user = users.get_user_by_token(db, token) if token else None
    if not user:
        raise RequiresLoginException()
    return user
