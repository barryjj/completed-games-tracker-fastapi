import asyncio
import datetime
import html as _html
import logging
import os
import re
from urllib.parse import unquote, urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload, selectinload

from . import importer, jobs, match_review, models, users
from . import steamgriddb as sgdb
from .models import get_db

logger = logging.getLogger(__name__)

_import_upload_lock: asyncio.Lock | None = None


def _get_import_lock() -> asyncio.Lock:
    global _import_upload_lock
    if _import_upload_lock is None:
        _import_upload_lock = asyncio.Lock()
    return _import_upload_lock


router = APIRouter()

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


COLLECTION_KEYWORDS = [
    "collection",
    "anthology",
    "trilogy",
    "compilation",
    "complete edition",
    "complete pack",
    "bundle",
    "chronicles",
    "archives",
    "legacy",
    "origins",
]


def infer_is_collection(title: str) -> bool:
    """Auto-detect collections by title keyword — for import-time use only."""
    t = title.lower()
    return any(kw in t for kw in COLLECTION_KEYWORDS)


def get_web_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Dependency for page routes — raises RequiresLoginException instead of 401."""
    from .main import RequiresLoginException

    token = request.cookies.get("session")
    user = users.get_user_by_token(db, token) if token else None
    if not user:
        raise RequiresLoginException()
    return user


# --- Auth ---


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@router.get("/signup")
def signup_page(request: Request):
    return templates.TemplateResponse(request=request, name="signup.html")


@router.post("/signup")
def signup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if password != password_confirm:
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={"error": "Passwords do not match", "username": username},
            status_code=422,
        )
    if not users.username_available(db, username.strip()):
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={"error": "That username is already taken", "username": username},
            status_code=422,
        )
    u = users.signup_user(db, username.strip(), password)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session", u.api_token, httponly=True, samesite="lax")
    return response


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = users.authenticate(db, username, password)
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid username or password"},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session", user.api_token, httponly=True, samesite="lax")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# --- Account ---


def _annotate_platforms_in_library(
    db: Session,
    user: "models.User",
    platforms: list["models.Platform"],
) -> tuple[list["models.Platform"], bool]:
    """Set .in_library on each Platform and return (platforms, has_any).

    .in_library is True when the user has at least one GameRelease linked to
    that platform via platform_id.  has_any is True when any platform matched.
    """
    used_ids: set[int] = {
        row[0]
        for row in db.query(models.GameRelease.platform_id)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.platform_id.isnot(None),
        )
        .distinct()
        .all()
    }
    for p in platforms:
        p.in_library = p.id in used_ids
    return platforms, bool(used_ids)


def _steam_counts(db: Session, user: models.User) -> dict | None:
    """Return {'games': N, 'dlc': N, 'total': N} for the user's Steam library, or None
    if Steam isn't connected. Used by the Tools page's Steam sync card."""
    if not user.steam_id64:
        return None
    rows = (
        db.query(models.Game.is_dlc, func.count(models.UserLibraryEntry.id))
        .join(models.GameRelease, models.GameRelease.game_id == models.Game.id)
        .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.source == "steam",
        )
        .group_by(models.Game.is_dlc)
        .all()
    )
    games = sum(count for is_dlc, count in rows if not is_dlc)
    dlc = sum(count for is_dlc, count in rows if is_dlc)
    return {"games": games, "dlc": dlc, "total": games + dlc}


# TODO(phase 3): user-configurable yearly goal — hardcoded until widget
# customization lands (see ROADMAP "Home / Tools / Settings restructure").
_YEARLY_GOAL = 52


@router.get("/")
def home_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Home v1 (restructure phase 2): landing page with a static set of stat
    widgets — completions this year vs. goal, library totals, recent
    completions, needs-attention counts. Pin/customize is phase 3."""
    year = datetime.date.today().year
    comp_base = db.query(models.Completion).filter(models.Completion.user_id == current_user.id)
    completions_this_year = comp_base.filter(func.strftime("%Y", models.Completion.completed_at) == str(year)).count()
    # Per-month counts for the current year (mini bar strip on the This-year
    # widget). Always 12 slots; future months render as stubs client-side.
    month_rows = (
        comp_base.with_entities(func.strftime("%m", models.Completion.completed_at), func.count())
        .filter(func.strftime("%Y", models.Completion.completed_at) == str(year))
        .group_by(func.strftime("%m", models.Completion.completed_at))
        .all()
    )
    completions_by_month = [0] * 12
    for m, n in month_rows:
        completions_by_month[int(m) - 1] = n
    recent_completions = (
        comp_base.join(models.Completion.library_entry)
        .join(models.UserLibraryEntry.release)
        .join(models.GameRelease.game)
        .options(
            contains_eager(models.Completion.library_entry)
            .contains_eager(models.UserLibraryEntry.release)
            .contains_eager(models.GameRelease.game),
            contains_eager(models.Completion.library_entry)
            .contains_eager(models.UserLibraryEntry.release)
            .joinedload(models.GameRelease.platform_obj),
            contains_eager(models.Completion.library_entry)
            .contains_eager(models.UserLibraryEntry.release)
            .selectinload(models.GameRelease.artwork),
            contains_eager(models.Completion.library_entry).selectinload(models.UserLibraryEntry.user_artwork),
        )
        # Same ordering contract as the completions page's date sort: date
        # first, spreadsheet row order as the same-date tiebreaker.
        .order_by(
            models.Completion.completed_at.desc(),
            models.Completion.sort_order.asc().nulls_last(),
            models.Completion.id.desc(),
        )
        .limit(5)
        .all()
    )
    library_total = _build_lib_query(db, current_user, "", "", "default", "name", False, False, "list")[0].count()
    # Platform breakdown of the same default view, so the rows sum to the
    # headline total. Grouped by platform_id (multiple raw strings can map to
    # one linked Platform); unlinked entries group by their raw string.
    breakdown_rows = (
        _build_lib_query(db, current_user, "", "", "default", "name", False, False, "list")[0]
        .with_entities(
            models.GameRelease.platform_id,
            models.GameRelease.platform,
            func.count(models.UserLibraryEntry.id),
        )
        .group_by(models.GameRelease.platform_id, models.GameRelease.platform)
        .all()
    )
    counts: dict = {}
    raw_labels: dict = {}
    for pid, raw, n in breakdown_rows:
        key = ("pid", pid) if pid else ("raw", raw)
        counts[key] = counts.get(key, 0) + n
        raw_labels[key] = raw
    pids = [k[1] for k in counts if k[0] == "pid"]
    pmap = {p.id: p for p in db.query(models.Platform).filter(models.Platform.id.in_(pids)).all()} if pids else {}
    platform_breakdown = []
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        kind, val = key
        if kind == "pid" and val in pmap:
            p = pmap[val]
            platform_breakdown.append({"label": p.display_title, "css": p.css_class, "value": f"pid:{val}", "count": n})
        else:
            label = raw_labels[key] or "Unknown"
            platform_breakdown.append({"label": label, "css": models._platform_heuristic_css(label), "value": label, "count": n})
    import_counts = _import_tab_counts(db, current_user.id)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "current_user": current_user,
            "year": year,
            "yearly_goal": _YEARLY_GOAL,
            "completions_this_year": completions_this_year,
            "completions_by_month": completions_by_month,
            "current_month": datetime.date.today().month,
            "month_names": [
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            ],
            "recent_completions": recent_completions,
            "library_total": library_total,
            "platform_breakdown": platform_breakdown,
            "import_counts": import_counts,
            "import_pending": sum(import_counts.values()),
            **_base_ctx(db, current_user),
        },
    )


@router.get("/tools")
def tools_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Operations hub: recurring actions on the library (sync, match review,
    import, artwork) as cards. Replaces the action half of the old
    /integrations hub; configuration lives under /settings."""
    import_counts = _import_tab_counts(db, current_user.id)
    # Entries with no vertical cover (the canonical orientation) — same filter
    # the library's "Missing artwork" checkbox applies in grid_v.
    missing_q, _, _ = _build_lib_query(db, current_user, "", "", "default", "name", False, True, "grid_v")
    return templates.TemplateResponse(
        request=request,
        name="tools.html",
        context={
            "current_user": current_user,
            "steam_counts": _steam_counts(db, current_user),
            "import_counts": import_counts,
            "import_pending": sum(import_counts.values()),
            "missing_covers": missing_q.count(),
            **_base_ctx(db, current_user),
        },
    )


# Preset anchors for the detail-pane hero logo. Empty/absent = default
# (bottom-left); values outside this set are rejected.
_LOGO_POSITIONS = {"top-left", "top-center", "top-right", "center", "bottom-center", "bottom-right", "hidden"}


@router.post("/library/entries/{entry_id}/logo-position")
def set_logo_position(
    entry_id: int,
    position: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Set (or clear) the hero-logo anchor for one library entry."""
    entry = (
        db.query(models.UserLibraryEntry)
        .filter(models.UserLibraryEntry.id == entry_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    if position and position not in _LOGO_POSITIONS:
        return Response(status_code=422)
    entry.logo_position = position or None
    db.commit()
    return Response(status_code=204)


# Size presets for the detail-pane hero logo. Empty/absent = default size.
_LOGO_SCALES = {"small", "large", "xlarge"}


@router.post("/library/entries/{entry_id}/logo-scale")
def set_logo_scale(
    entry_id: int,
    scale: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Set (or clear) the hero-logo size preset for one library entry."""
    entry = (
        db.query(models.UserLibraryEntry)
        .filter(models.UserLibraryEntry.id == entry_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    if scale and scale not in _LOGO_SCALES:
        return Response(status_code=422)
    entry.logo_scale = scale or None
    db.commit()
    return Response(status_code=204)


@router.get("/settings")
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Settings area with grouped left nav: Account (profile/security/appearance)
    + Configuration (platforms, integrations). Section switching is client-side
    via ?section=, same pattern the old account tabs used."""
    platforms = _get_all_platforms(db)
    platforms, has_library_platforms = _annotate_platforms_in_library(db, current_user, platforms)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "current_user": current_user,
            "platforms": platforms,
            "ctp_accents": models.CTP_ACCENTS,
            "has_library_platforms": has_library_platforms,
            **_base_ctx(db, current_user),
        },
    )


# Section names of the settings page — also the complete set of legal
# ?section= values for the /account -> /settings redirect below.
_SETTINGS_SECTIONS = {"profile", "security", "appearance", "platforms", "integrations"}


@router.get("/account")
def account_page(request: Request, tab: str = ""):
    """Old account page — content moved to /settings. Tab names map 1:1 to
    settings sections, so deep links keep working. The tab value is checked
    against the known section names so request data never flows into the
    redirect target (CodeQL: untrusted URL redirection)."""
    target = f"/settings?section={tab}" if tab in _SETTINGS_SECTIONS else "/settings"
    return RedirectResponse(target, status_code=302)


@router.get("/account/platforms/{platform_id}/cancel")
def cancel_platform_edit(
    request: Request,
    platform_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Return the compact row, discarding any unsaved edits."""
    platform = (
        db.query(models.Platform)
        .options(joinedload(models.Platform.aliases), joinedload(models.Platform.family))
        .filter(models.Platform.id == platform_id)
        .first()
    )
    if not platform:
        return Response(status_code=404)
    in_library = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.platform_id == platform.id,
        )
        .limit(1)
        .first()
        is not None
    )
    platform.in_library = in_library
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_row.html",
        context={"platform": platform, "ctp_accents": models.CTP_ACCENTS},
    )


@router.get("/account/platforms/{platform_id}/edit")
def edit_platform_row(
    request: Request,
    platform_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Return the expanded editing row for a platform."""
    platform = (
        db.query(models.Platform)
        .options(joinedload(models.Platform.aliases), joinedload(models.Platform.family))
        .filter(models.Platform.id == platform_id)
        .first()
    )
    if not platform:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_row_edit.html",
        context={"platform": platform, "ctp_accents": models.CTP_ACCENTS},
    )


@router.post("/account/platforms/{platform_id}")
def update_platform(
    request: Request,
    platform_id: int,
    display_name: str = Form(""),
    color: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Update a platform's display_name and/or color. Returns the updated row partial."""
    platform = (
        db.query(models.Platform)
        .options(joinedload(models.Platform.aliases), joinedload(models.Platform.family))
        .filter(models.Platform.id == platform_id)
        .first()
    )
    if not platform:
        return Response(status_code=404)
    display_name = display_name.strip()
    color = color.strip()
    platform.display_name = display_name if display_name else None
    if color in models.CTP_ACCENTS:
        platform.color = color
    elif color == "":
        platform.color = None
    db.commit()
    db.refresh(platform)
    in_library = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.platform_id == platform.id,
        )
        .limit(1)
        .first()
        is not None
    )
    platform.in_library = in_library
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_row.html",
        context={
            "platform": platform,
            "ctp_accents": models.CTP_ACCENTS,
        },
    )


@router.post("/account/platforms/{platform_id}/aliases")
def add_platform_alias(
    request: Request,
    platform_id: int,
    alias: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Add an alias to a platform. Returns the updated aliases partial."""
    platform = db.query(models.Platform).options(joinedload(models.Platform.aliases)).filter(models.Platform.id == platform_id).first()
    if not platform:
        return Response(status_code=404)
    alias = alias.strip()
    if alias:
        db.add(models.PlatformAlias(platform_id=platform_id, alias=alias))
        db.commit()
        db.expire(platform)
        db.refresh(platform)
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_aliases.html",
        context={"platform": platform},
    )


@router.delete("/account/platforms/aliases/{alias_id}")
def delete_platform_alias(
    request: Request,
    alias_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Delete a platform alias. Returns the updated aliases partial."""
    alias = db.query(models.PlatformAlias).filter(models.PlatformAlias.id == alias_id).first()
    if not alias:
        return Response(status_code=404)
    platform_id = alias.platform_id
    db.delete(alias)
    db.commit()
    platform = db.query(models.Platform).options(joinedload(models.Platform.aliases)).filter(models.Platform.id == platform_id).first()
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_aliases.html",
        context={"platform": platform},
    )


@router.post("/account/display-name")
def update_display_name(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    users.update_display_name(db, current_user, name)
    return templates.TemplateResponse(
        request=request,
        name="partials/account_flash.html",
        context={"message": "Display name updated."},
    )


@router.post("/account/username")
def update_username(
    request: Request,
    new_username: str = Form(...),
    current_password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    result = users.update_username(db, current_user, new_username, current_password)
    if result == "incorrect_password":
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "Current password is incorrect."},
            status_code=422,
        )
    if result == "username_taken":
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "That username is already taken."},
            status_code=422,
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/account_flash.html",
        context={"message": "Username updated."},
    )


@router.post("/account/password")
def update_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    if new_password != new_password_confirm:
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "New passwords do not match."},
            status_code=422,
        )
    result = users.update_password(db, current_user, current_password, new_password)
    if result == "incorrect_password":
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "Current password is incorrect."},
            status_code=422,
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/account_flash.html",
        context={"message": "Password updated."},
    )


@router.post("/account/delete")
def delete_account(
    request: Request,
    confirm_username: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    if confirm_username.strip() != current_user.username:
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "Username did not match. Account not deleted.", "target": "flash-delete"},
            status_code=422,
        )
    db.delete(current_user)
    db.commit()
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


PAGE_SIZE = 200  # rows per infinite-scroll page

# --- Library ---

VIEW_OPTIONS = ["default", "dlc", "collections", "in_collection", "manual", "all"]
SORT_OPTIONS = ["name", "recently_played"]


VIEW_MODES = {"list", "grid_v", "grid_h"}


def _build_lib_query(
    db: Session,
    user: "models.User",
    q: str,
    platform: str,
    view: str,
    sort: str,
    show_hidden: bool,
    missing_art: bool,
    view_mode: str,
):
    """Build and filter the library query.

    Returns (base_q, view, sort) with view/sort normalisation applied.
    Shared by library_page (full render) and library_more (scroll continuation).
    """
    base_q = (
        db.query(models.UserLibraryEntry)
        .join(models.UserLibraryEntry.release)
        .join(models.GameRelease.game)
        .options(
            contains_eager(models.UserLibraryEntry.release).contains_eager(models.GameRelease.game),
            contains_eager(models.UserLibraryEntry.release).selectinload(models.GameRelease.artwork),
            contains_eager(models.UserLibraryEntry.release).joinedload(models.GameRelease.platform_obj),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter(models.UserLibraryEntry.user_id == user.id)
        .order_by(func.coalesce(models.Game.display_name, models.Game.title).collate("NOCASE"))
    )
    if sort not in SORT_OPTIONS:
        sort = "name"
    if sort == "recently_played":
        base_q = base_q.order_by(None).order_by(models.UserLibraryEntry.last_played_at.desc().nulls_last())
    if not show_hidden:
        base_q = base_q.filter(models.UserLibraryEntry.is_hidden == False)
    q = q.strip()
    if q:
        base_q = base_q.filter(
            or_(
                models.Game.title.ilike(f"%{q}%"),
                models.Game.display_name.ilike(f"%{q}%"),
            )
        )
    if platform:
        # platform param is either "pid:<id>" (linked) or a raw string (unlinked)
        if platform.startswith("pid:"):
            try:
                pid = int(platform[4:])
                base_q = base_q.filter(models.GameRelease.platform_id == pid)
            except ValueError:
                pass
        else:
            base_q = base_q.filter(models.GameRelease.platform == platform)
    if view not in VIEW_OPTIONS:
        view = "default"
    if view == "default":
        base_q = base_q.filter(
            or_(
                models.Game.is_dlc == False,
                models.UserLibraryEntry.import_source == "manual",
            )
        )
    elif view == "dlc":
        base_q = base_q.filter(models.Game.is_dlc == True)
    elif view == "collections":
        base_q = base_q.filter(models.Game.is_collection == True)
    elif view == "in_collection":
        base_q = base_q.filter(
            models.Game.parent_id != None,
            models.Game.is_dlc == False,
        )
    elif view == "manual":
        base_q = base_q.filter(models.UserLibraryEntry.import_source == "manual")
    if missing_art:
        art_type = "cover_v" if view_mode == "grid_v" else "cover_h"
        has_user_art_ids = (
            db.query(models.UserArtwork.entry_id)
            .filter(
                models.UserArtwork.artwork_type == art_type,
                models.UserArtwork.entry_id.isnot(None),
                models.UserArtwork.url.isnot(None),
            )
            .scalar_subquery()
        )
        has_art_release_ids = (
            db.query(models.GameArtwork.release_id)
            .filter(
                models.GameArtwork.artwork_type == art_type,
                models.GameArtwork.is_valid.is_(True),
            )
            .scalar_subquery()
        )
        base_q = base_q.filter(
            models.UserLibraryEntry.id.not_in(has_user_art_ids),
            models.GameRelease.id.not_in(has_art_release_ids),
        )
    return base_q, view, sort


def _library_next_url(
    page: int,
    total_pages: int,
    q: str,
    platform: str,
    view: str,
    sort: str,
    show_hidden: bool,
    missing_art: bool,
    view_mode: str,
) -> str | None:
    """Return the /library/more URL for the next scroll page, or None if done."""
    if page >= total_pages:
        return None
    params: dict[str, str] = {"page": str(page + 1)}
    if q:
        params["q"] = q
    if platform:
        params["platform"] = platform
    if view != "default":
        params["view"] = view
    if sort != "name":
        params["sort"] = sort
    if show_hidden:
        params["show_hidden"] = "true"
    if missing_art:
        params["missing_art"] = "true"
    params["view_mode"] = view_mode
    return "/library/more?" + urlencode(params)


def _resolve_view_mode(request: Request, query_value: str | None, cookie_name: str) -> str:
    """Resolve the effective view_mode for a list page.

    Order of precedence:
      1. Explicit `?view_mode=X` in the URL (user just clicked the toggle).
      2. The persisted cookie set by the toolbar JS — this is the fix for
         the "list flashes briefly before flipping to grid" lag, since the
         server now renders the right view on first paint.
      3. Default "list".

    Falls back to "list" on junk values from any source.
    """
    if query_value:
        return query_value if query_value in VIEW_MODES else "list"
    cookie_value = request.cookies.get(cookie_name)
    if cookie_value in VIEW_MODES:
        return cookie_value
    return "list"


@router.get("/library")
def library_page(
    request: Request,
    page: int = Query(1, ge=1),
    q: str = Query(""),
    platform: str = Query(""),
    view: str = Query("default"),
    sort: str = Query("name"),
    view_mode: str | None = Query(None),
    show_hidden: bool = Query(False),
    missing_art: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    # view_mode resolved from query → cookie → default. Cookie is the
    # server-side mirror of the JS localStorage preference, set by the
    # view-mode toggle JS. Lets the first paint render in the right mode
    # without the visible flicker the old JS-only redirect caused.
    view_mode = _resolve_view_mode(request, view_mode, "cgt-library-view-mode")

    base_q, view, sort = _build_lib_query(db, current_user, q, platform, view, sort, show_hidden, missing_art, view_mode)

    total = base_q.count()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    entries = base_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    # Attach parent-game artwork fallbacks for DLC entries (single batched
    # query to avoid N+1 across large pages).
    _attach_parent_fallbacks(db, entries, current_user=current_user)

    # base_game_options and collections are only needed to populate the add-form
    # and edit-modal dropdowns, which live outside #library-content and are never
    # re-rendered by HTMX filter/search requests. Skip them on HTMX calls.
    is_htmx = request.headers.get("HX-Request") == "true"

    if is_htmx:
        base_game_options = []
        collections = []
    else:
        # Both dropdown lists exclude hidden entries — a hidden entry isn't
        # something you'd pick as a parent for new DLC or a containing collection.
        base_game_options = (
            db.query(models.UserLibraryEntry)
            .options(joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game))
            .join(models.GameRelease)
            .join(models.Game)
            .filter(
                models.UserLibraryEntry.user_id == current_user.id,
                models.UserLibraryEntry.is_hidden == False,
                models.Game.is_dlc == False,
            )
            .order_by(models.Game.title)
            .all()
        )
        collections = (
            db.query(models.UserLibraryEntry)
            .options(joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game))
            .join(models.GameRelease)
            .join(models.Game)
            .filter(
                models.UserLibraryEntry.user_id == current_user.id,
                models.UserLibraryEntry.is_hidden == False,
                models.Game.is_collection == True,
            )
            .order_by(models.Game.title)
            .all()
        )
    # lib_platforms populates the platform dropdown outside #library-content —
    # skip it on HTMX requests just like base_game_options/collections.
    if is_htmx:
        lib_platform_list = []
    else:
        # Distinct (platform_string, platform_id) pairs for the dropdown.
        # GROUP BY is SQLite-safe; DISTINCT ON is PostgreSQL-only.
        lib_platforms_raw = (
            db.query(models.GameRelease.platform, models.GameRelease.platform_id)
            .join(models.UserLibraryEntry)
            .filter(models.UserLibraryEntry.user_id == current_user.id)
            .group_by(models.GameRelease.platform)
            .order_by(models.GameRelease.platform)
            .all()
        )
        # Bulk-load Platform rows for linked entries.
        _pids = {pid for _, pid in lib_platforms_raw if pid}
        _pmap = {p.id: p for p in db.query(models.Platform).filter(models.Platform.id.in_(_pids)).all()} if _pids else {}
        # Deduplicate by platform_id — multiple raw strings mapping to the same
        # platform (e.g. "PC", "Win", "PC (Microsoft Windows)" all → platform_id=1)
        # should appear as a single dropdown entry. Use "pid:<id>" as the value so
        # the filter can match by platform_id rather than a specific raw string.
        # Unlinked entries (no platform_id) fall back to raw string as before.
        seen_pids: set[int] = set()
        lib_platform_list = []
        for raw, pid in lib_platforms_raw:
            if pid and pid in _pmap:
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)
                lib_platform_list.append({"value": f"pid:{pid}", "label": _pmap[pid].display_title})
            else:
                lib_platform_list.append({"value": raw, "label": raw})
        lib_platform_list.sort(key=lambda p: p["label"])

    next_page_url = _library_next_url(page, total_pages, q, platform, view, sort, show_hidden, missing_art, view_mode)

    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "current_user": current_user,
            **_base_ctx(db, current_user),
            "entries": entries,
            "collections": collections,
            "base_game_options": base_game_options,
            "platforms": _get_all_platforms(db),
            "total": total,
            "q": q,
            "platform": platform,
            "view": view,
            "sort": sort,
            "view_mode": view_mode,
            "show_hidden": show_hidden,
            "missing_art": missing_art,
            "next_page_url": next_page_url,
            "lib_platforms": lib_platform_list,
        },
    )


@router.get("/library/more")
def library_more(
    request: Request,
    page: int = Query(1, ge=1),
    q: str = Query(""),
    platform: str = Query(""),
    view: str = Query("default"),
    sort: str = Query("name"),
    view_mode: str = Query("list"),
    show_hidden: bool = Query(False),
    missing_art: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Infinite-scroll continuation: returns the next batch of rows/cards + sentinel.

    Called by the sentinel element at the bottom of #library-tbody when it
    scrolls into view.  Response is plain rows/cards (no wrapper) so HTMX
    outerHTML-swaps them in place of the sentinel inside the container.
    """
    view_mode = view_mode if view_mode in VIEW_MODES else "list"
    base_q, view, sort = _build_lib_query(db, current_user, q, platform, view, sort, show_hidden, missing_art, view_mode)

    total = base_q.count()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    entries = base_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    _attach_parent_fallbacks(db, entries, current_user=current_user)

    next_page_url = _library_next_url(page, total_pages, q, platform, view, sort, show_hidden, missing_art, view_mode)

    return templates.TemplateResponse(
        request=request,
        name="partials/library_more.html",
        context={
            "entries": entries,
            "next_page_url": next_page_url,
            "view_mode": view_mode,
        },
    )


@router.post("/library/games")
def add_game(
    request: Request,
    title: str = Form(...),
    platform: str = Form(...),
    display_name: str = Form(""),
    is_dlc: bool = Form(False),
    is_collection: bool = Form(False),
    parent_game_id: int | None = Form(None),
    igdb_game_id: int | None = Form(None),
    import_candidate_id: int | None = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    # Resolve parent release_id → game_id
    parent_id: int | None = None
    if parent_game_id:
        parent_release = db.query(models.GameRelease).filter(models.GameRelease.id == parent_game_id).first()
        if parent_release:
            parent_id = parent_release.game_id

    title_clean = title.strip()
    # Manual entries: display_name defaults to title if user didn't specify a
    # different one. Either way, the user typed this — mark display_name_user_set
    # so no heuristic ever touches it.
    display_clean = display_name.strip() or title_clean

    platform_id = models.resolve_platform_id(db, platform)

    # --- Find existing game in this user's library ---
    # Prefer igdb_id match (strongest signal), fall back to exact title match.
    # Only match against manual releases — never merge a new manual entry onto
    # a synced game record (e.g. a Steam entry that happens to share the same
    # igdb_id or title would pull in its DLC/children, which is wrong).
    existing_game: models.Game | None = None
    if igdb_game_id:
        existing_game = (
            db.query(models.Game)
            .join(models.GameRelease)
            .join(models.UserLibraryEntry)
            .filter(
                models.UserLibraryEntry.user_id == current_user.id,
                models.Game.igdb_id == igdb_game_id,
                models.GameRelease.source == "manual",
            )
            .first()
        )
    if existing_game is None:
        existing_game = (
            db.query(models.Game)
            .join(models.GameRelease)
            .join(models.UserLibraryEntry)
            .filter(
                models.UserLibraryEntry.user_id == current_user.id,
                models.Game.title == title_clean,
                models.GameRelease.source == "manual",
            )
            .first()
        )

    if existing_game is not None:
        # Check whether a release on this platform already exists for this user.
        conflict_release = (
            db.query(models.GameRelease)
            .join(models.UserLibraryEntry)
            .filter(
                models.UserLibraryEntry.user_id == current_user.id,
                models.GameRelease.game_id == existing_game.id,
                models.GameRelease.platform_id == platform_id,
            )
            .first()
        )
        if conflict_release is not None:
            existing_display = existing_game.display_name or existing_game.title
            existing_platform = conflict_release.display_platform
            return templates.TemplateResponse(
                request=request,
                name="partials/_toast.html",
                context={
                    "kind": "danger",
                    "body": (
                        f"This game is already in your library.\n"
                        f"Title: {existing_display}\n"
                        f"Platform: {existing_platform}\n\n"
                        f"Title and platform must be unique."
                    ),
                },
                headers={"HX-Reswap": "none"},
            )
        # Same game, different platform — attach a new release to the existing game.
        game = existing_game
        # Update igdb_id if the existing row doesn't have one yet.
        if igdb_game_id and not game.igdb_id:
            game.igdb_id = igdb_game_id
    else:
        # New game — create the Game row.
        game = models.Game(
            title=title_clean,
            display_name=display_clean,
            is_dlc=is_dlc,
            is_collection=is_collection,
            parent_id=parent_id,
            igdb_id=igdb_game_id,
            display_name_user_set=True,
            is_dlc_user_set=True,
            is_collection_user_set=True,
            parent_id_user_set=True,
        )
        db.add(game)
        db.flush()

    release = models.GameRelease(
        game_id=game.id,
        platform=platform,
        platform_id=platform_id,
        source="manual",
    )
    db.add(release)
    db.flush()

    entry = models.UserLibraryEntry(
        user_id=current_user.id,
        release_id=release.id,
        import_source="manual",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    # Fetch IGDB cover art and metadata if we have an igdb_game_id and credentials.
    # Each fetch is wrapped independently so one failure can't suppress the other.
    if igdb_game_id and current_user.twitch_client_id and current_user.twitch_client_secret:
        from . import igdb as _igdb

        try:
            _igdb.save_igdb_cover(
                db,
                entry,
                igdb_game_id,
                current_user.twitch_client_id,
                current_user.twitch_client_secret,
            )
        except Exception:
            pass  # cover failure never blocks the add

        try:
            _igdb.save_igdb_metadata(
                db,
                entry.release,
                igdb_game_id,
                current_user.twitch_client_id,
                current_user.twitch_client_secret,
            )
        except Exception:
            pass  # metadata failure never blocks the add

    # Import candidate confirmed via this add: log completions from its
    # parsed spreadsheet rows against the entry we just created, instead of
    # requiring the user to re-type dates/playthroughs/notes we already have.
    if import_candidate_id:
        candidate = (
            db.query(models.ImportCandidate)
            .filter(models.ImportCandidate.id == import_candidate_id, models.ImportCandidate.user_id == current_user.id)
            .options(joinedload(models.ImportCandidate.rows))
            .first()
        )
        if candidate and candidate.status == "pending":
            created: list = []
            for row in candidate.rows:
                if not row.completed_at:
                    continue
                comp = models.Completion(
                    user_id=current_user.id,
                    library_entry_id=entry.id,
                    completed_at=row.completed_at,
                    completed_at_precision=row.completed_at_precision or "day",
                    playthroughs=row.playthroughs,
                    notes=row.raw_notes,
                    sort_order=row.row_number,
                )
                db.add(comp)
                created.append((row, comp))
            db.flush()
            # Stamp the linkage so Reopen can delete exactly these.
            for row, comp in created:
                row.created_completion_id = comp.id
            candidate.library_entry_id = entry.id
            candidate.status = "confirmed"
            candidate.reviewed_at = datetime.datetime.now(datetime.UTC)
            db.commit()

        # Confirmed from the import review page's in-place add modal: return the
        # OOB count refresh (same partial the Confirm button uses) so the tab
        # badges + pending update without a reload. The page ignores
        # library_row.html and drops the confirmed row client-side.
        tab_counts = _import_tab_counts(db, current_user.id)
        pending = sum(tab_counts.values())
        tab_counts["confirmed"] = _import_confirmed_count(db, current_user.id)
        return templates.TemplateResponse(
            request=request,
            name="partials/_import_counts_oob.html",
            context={"tab_counts": tab_counts, "pending": pending},
            headers={"HX-Reswap": "none"},
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/library_row.html",
        context={"entry": entry},
    )


@router.patch("/library/entries/{entry_id}")
def edit_library_entry(
    request: Request,
    entry_id: int,
    title: str = Form(""),
    display_name: str = Form(""),
    is_dlc: bool = Form(False),
    is_collection: bool = Form(False),
    parent_game_id: int | None = Form(None),
    igdb_game_id: int | None = Form(None),
    platform: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if not entry:
        return Response(status_code=404)

    game = entry.release.game

    # Saving the edit modal is a user override on every field it touches.
    # All user_set flags flip to True so no heuristic ever undoes these edits.
    title_clean = title.strip()
    # Title is editable ONLY for fully-manual games. If any release on this game
    # came from a platform sync (Steam/PSN/etc.), the title is the platform's
    # canonical name and changing it could break future re-sync matching.
    # display_name remains freely editable either way.
    is_fully_manual = all(r.source == "manual" for r in game.releases)
    if title_clean and is_fully_manual:
        game.title = title_clean

    # Platform is editable for fully-manual entries (no sync to break).
    platform_clean = platform.strip()
    if platform_clean and is_fully_manual:
        new_platform_id = models.resolve_platform_id(db, platform_clean)
        # Block if another release on this game already uses this platform_id
        # (excluding the current release being edited).
        conflict = (
            db.query(models.GameRelease)
            .join(models.UserLibraryEntry)
            .filter(
                models.UserLibraryEntry.user_id == current_user.id,
                models.GameRelease.game_id == game.id,
                models.GameRelease.platform_id == new_platform_id,
                models.GameRelease.id != entry.release.id,
            )
            .first()
        )
        if conflict:
            return templates.TemplateResponse(
                request=request,
                name="partials/_toast.html",
                context={
                    "kind": "danger",
                    "body": f"This game already has a release on {conflict.display_platform}. Title and platform must be unique.",
                },
                headers={"HX-Reswap": "none"},
            )
        entry.release.platform = platform_clean
        entry.release.platform_id = new_platform_id

    # display_name: empty string means "use raw title" (display_name stored as NULL)
    game.display_name = display_name.strip() or None
    game.display_name_user_set = True

    game.is_dlc = is_dlc
    game.is_dlc_user_set = True

    game.is_collection = is_collection
    game.is_collection_user_set = True

    # Resolve parent_game_id (release id) → game.parent_id
    if parent_game_id:
        parent_release = db.query(models.GameRelease).filter_by(id=parent_game_id).first()
        game.parent_id = parent_release.game_id if parent_release else None
    else:
        game.parent_id = None
    game.parent_id_user_set = True

    # IGDB link — only for fully-manual games (never overwrite a sync'd game's ID).
    if igdb_game_id and is_fully_manual:
        game.igdb_id = igdb_game_id

    db.commit()
    db.refresh(entry)

    # Fetch IGDB art + metadata if a new IGDB ID was supplied.
    if igdb_game_id and is_fully_manual and current_user.twitch_client_id and current_user.twitch_client_secret:
        from . import igdb as _igdb

        try:
            _igdb.save_igdb_cover(
                db,
                entry,
                igdb_game_id,
                current_user.twitch_client_id,
                current_user.twitch_client_secret,
            )
        except Exception:
            pass

        try:
            _igdb.save_igdb_metadata(
                db,
                entry.release,
                igdb_game_id,
                current_user.twitch_client_id,
                current_user.twitch_client_secret,
            )
        except Exception:
            pass

    return templates.TemplateResponse(
        request=request,
        name="partials/library_row.html",
        context={"entry": entry},
    )


@router.post("/library/entries/{entry_id}/fetch-igdb-metadata")
def fetch_igdb_metadata_for_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Re-fetch IGDB cover art and metadata for a game that has an IGDB ID linked.

    Called from the detail pane's "Refresh IGDB metadata" dropdown item.
    Returns 200 on success; the HTMX caller reloads the pane.
    """
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if not entry:
        return Response(status_code=404)
    game = entry.release.game
    if not game.igdb_id:
        return Response(status_code=400)
    cid = current_user.twitch_client_id
    secret = current_user.twitch_client_secret
    if not cid or not secret:
        return Response(status_code=400)

    from . import igdb as _igdb

    try:
        _igdb.save_igdb_cover(db, entry, game.igdb_id, cid, secret)
    except Exception as exc:
        logger.warning("IGDB cover refresh failed for entry %d: %s", entry_id, exc)
    try:
        _igdb.save_igdb_metadata(db, entry.release, game.igdb_id, cid, secret)
    except Exception as exc:
        logger.warning("IGDB metadata refresh failed for entry %d: %s", entry_id, exc)

    return Response(status_code=200)


@router.post("/library/entries/{entry_id}/unlink-igdb")
def unlink_igdb_for_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Clear the IGDB link and all IGDB-sourced metadata on a game.

    Clears:
      - game.igdb_id
      - release.raw_data['igdb'] (genres / year)
      - release.description if it came from IGDB (no separate flag, so always cleared)
      - GameArtwork rows where source='igdb' (marked invalid, not deleted)
    """
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if not entry:
        return Response(status_code=404)

    game = entry.release.game
    release = entry.release

    # Clear the IGDB game link.
    game.igdb_id = None

    # Clear IGDB metadata block from raw_data.
    raw = dict(release.raw_data or {})
    raw.pop("igdb", None)
    release.raw_data = raw

    # Clear description (IGDB is the only non-Steam source for this field on manual games).
    release.description = None

    # Mark IGDB artwork rows invalid so they stop showing in the detail pane.
    for art in release.artwork:
        if art.source == "igdb":
            art.is_valid = False

    db.commit()
    return Response(status_code=200)


@router.delete("/library/entries/{entry_id}")
def delete_library_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if entry:
        release = entry.release
        game = release.game
        db.delete(entry)
        db.flush()
        # Clean up the release if no other entries reference it.
        remaining_entries = db.query(models.UserLibraryEntry).filter_by(release_id=release.id).count()
        if remaining_entries == 0:
            db.delete(release)
            db.flush()
            # Clean up the game if no other releases reference it.
            remaining_releases = db.query(models.GameRelease).filter_by(game_id=game.id).count()
            if remaining_releases == 0:
                db.delete(game)
        db.commit()
    return Response(status_code=200)


@router.post("/library/entries/{entry_id}/hide")
def hide_library_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Hide a library entry from the default view. Sets is_hidden_user_set so
    the auto-hide heuristic won't override the user's decision either way."""
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if not entry:
        return Response(status_code=404)
    entry.is_hidden = True
    entry.is_hidden_user_set = True
    db.commit()
    return Response(status_code=200)


@router.post("/library/entries/{entry_id}/unhide")
def unhide_library_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Unhide a library entry. Sets is_hidden_user_set so the heuristic can't
    re-hide it on the next enrichment pass."""
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if not entry:
        return Response(status_code=404)
    entry.is_hidden = False
    entry.is_hidden_user_set = True
    db.commit()
    return Response(status_code=200)


@router.post("/library/backfill-hidden")
def backfill_hidden(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """One-shot: run the auto-hide heuristic across the user's existing library.
    Skips entries where is_hidden_user_set = True. Uses appdetails from raw_data
    when present (most entries should have it after the enrichment worker has
    chewed through them); falls back to title-only matching otherwise."""
    from . import steam

    rows = (
        db.query(models.UserLibraryEntry)
        .join(models.GameRelease)
        .join(models.Game)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.UserLibraryEntry.is_hidden_user_set == False,
            models.UserLibraryEntry.is_hidden == False,
        )
        .all()
    )
    hidden = 0
    for entry in rows:
        appdetails = (entry.release.raw_data or {}).get("appdetails")
        if steam._should_auto_hide(entry.release.game.title, appdetails, entry.release.game.is_dlc):
            entry.is_hidden = True
            hidden += 1
    db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={
            "message": (f"Auto-hide complete\n{hidden:,} entries hidden\nUse 'Show hidden' on the library to review"),
        },
    )


@router.get("/library/games/search")
def search_library_games(
    request: Request,
    q: str = Query(""),
    is_dlc: bool | None = Query(None),
    is_collection: bool | None = Query(None),
    callback: str = Query("selectLibraryParent"),
    id_field: str = Query("release"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Search user's library games by title, optionally filtered by type.

    id_field controls which id the callback receives: 'release' (default,
    used by the DLC/collection parent pickers) or 'entry' (UserLibraryEntry.id,
    used by the import candidate manual-link picker)."""
    query = (
        db.query(models.UserLibraryEntry)
        .join(models.GameRelease)
        .join(models.Game)
        .filter(models.UserLibraryEntry.user_id == current_user.id)
    )

    qn = q.strip()
    if qn:
        query = query.filter(models.Game.title.ilike(f"%{qn}%"))

    if is_dlc is not None:
        query = query.filter(models.Game.is_dlc == is_dlc)

    if is_collection is not None:
        query = query.filter(models.Game.is_collection == is_collection)

    # Rank by relevance, not just alphabetically: exact title, then titles that
    # start with the query, then plain substring matches — alphabetical within
    # each tier. A pure-alphabetical order + LIMIT silently dropped the exact
    # match whenever enough longer titles contained the query (searching
    # "Marvel Super Heroes" buried it under 15+ "LEGO Marvel Super Heroes …"
    # rows). The cap is a safety valve, not the primary filter.
    if qn:
        relevance = case(
            (func.lower(models.Game.title) == qn.lower(), 0),
            (models.Game.title.ilike(f"{qn}%"), 1),
            else_=2,
        )
        query = query.order_by(relevance, models.Game.title)
    else:
        query = query.order_by(models.Game.title)

    entries = query.limit(25).all()

    return templates.TemplateResponse(
        request=request,
        name="partials/library_game_results.html",
        context={"entries": entries, "q": q, "callback": callback, "id_field": id_field},
    )


@router.get("/library/entries/{entry_id}/detail")
def library_entry_detail(
    request: Request,
    entry_id: int,
    fresh_open: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Render the slide-out detail pane for a library entry. Loaded via HTMX
    when a row is clicked. Returns just the inner content of an offcanvas body
    so it can be swapped without re-rendering the wrapper."""
    entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.platform_obj),
            joinedload(models.UserLibraryEntry.completions),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)

    game = entry.release.game

    # Child DLC / collection members: other Game rows where parent_id == this
    # game.id AND the current user owns at least one of their releases.
    child_entries = []
    if not game.is_dlc:  # only base games / collections show children
        child_entries = (
            db.query(models.UserLibraryEntry)
            .options(
                joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
                joinedload(models.UserLibraryEntry.release).selectinload(models.GameRelease.artwork),
                joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.platform_obj),
                selectinload(models.UserLibraryEntry.user_artwork),
            )
            .join(models.GameRelease)
            .join(models.Game)
            .filter(
                models.UserLibraryEntry.user_id == current_user.id,
                models.Game.parent_id == game.id,
            )
            .order_by(models.Game.title)
            .all()
        )

    visuals = _build_detail_pane_visuals(db, entry, game, entry.release)
    appdetails = (entry.release.raw_data or {}).get("appdetails") or {}

    # Stale-only auto-refresh: if this is a Steam entry and its metadata is
    # either never-fetched or older than the staleness threshold, the template
    # will emit a hidden HTMX trigger that fires the refresh in the background
    # when the pane opens. Current data shows immediately; the next open picks
    # up the refresh. Avoids hammering Steam on every pane open.
    needs_refresh = _needs_metadata_refresh(entry.release)

    return templates.TemplateResponse(
        request=request,
        name="partials/library_detail.html",
        context={
            "entry": entry,
            "game": game,
            "release": entry.release,
            "appdetails": appdetails,
            "steam_meta": _extract_steam_meta(appdetails),
            "igdb_meta": _extract_igdb_meta(entry.release),
            "child_entries": child_entries,
            "completions": sorted(entry.completions, key=lambda c: c.completed_at, reverse=True),
            "current_user": current_user,
            "needs_refresh": needs_refresh,
            "fresh_open": fresh_open,
            **visuals,
        },
    )


@router.post("/library/entries/{entry_id}/refresh-metadata")
def refresh_entry_metadata(
    request: Request,
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """One-off appdetails refresh for a single library entry — lets the user
    bypass the background worker's queue for a specific entry they noticed
    needs fixing. Re-runs the same post-fetch logic as enrich_next_batch:
    promote/demote is_dlc, link parent_id, apply auto-hide. All gated on
    *_user_set flags so manual overrides stick."""
    from . import steam as _steam

    entry = (
        db.query(models.UserLibraryEntry)
        .options(joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game))
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    release = entry.release
    if release.source != "steam" or not release.external_id:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": "Refresh metadata is only available for Steam entries."},
            status_code=400,
        )

    # Single sync fetch. Wrapped to handle 429 / transient errors gracefully.
    try:
        details = _steam._fetch_appdetails(int(release.external_id))
    except Exception as e:
        # Look for 429 specifically so the toast can tell the user it's a rate
        # limit (which is recoverable) vs an unknown failure.
        msg = str(e)
        if "429" in msg:
            return templates.TemplateResponse(
                request=request,
                name="partials/integrations_flash.html",
                context={
                    "error": (
                        "Steam is rate-limiting right now.\nTry again in a minute, or wait for the background worker to catch this entry."
                    ),
                },
                status_code=429,
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": f"Refresh failed: {e}"},
            status_code=502,
        )

    # Apply the same post-fetch logic the worker uses.
    game = release.game
    if details is not None:
        raw = dict(release.raw_data or {})
        raw["appdetails"] = details
        raw["appdetails_type"] = details.get("type", "game")
        release.raw_data = raw

        app_type = details.get("type", "game")

        # Title backfill — same as enrich_next_batch. Replaces the
        # "App {appid}" placeholder when the sync couldn't find the appid in
        # the catalog cache. Respects manual display_name edits.
        real_name = (details.get("name") or "").strip()
        if real_name and game.title.startswith("App ") and game.title[4:].strip().isdigit():
            game.title = real_name
            if not game.display_name_user_set:
                cleaned = _steam._clean_title(real_name)
                game.display_name = cleaned if cleaned != real_name else None

        if not game.is_dlc_user_set:
            if app_type == "dlc" and not game.is_dlc:
                game.is_dlc = True
            elif app_type == "game" and game.is_dlc:
                # Don't demote if any of these signals say it's attached content:
                #   1. game.parent_id → already resolved DB link, strongest signal
                #   2. fullgame in appdetails → Steam links it to a parent game
                #   3. Title matches auto-hide patterns → purchase wrapper
                has_parent = game.parent_id is not None
                has_fullgame = bool((details.get("fullgame") or {}).get("appid"))
                looks_like_dlc = _steam._should_auto_hide(game.title, details, is_dlc=True)
                if not has_parent and not has_fullgame and not looks_like_dlc:
                    game.is_dlc = False
            elif app_type == "game" and not game.is_dlc:
                # Re-promote entries previously demoted before the guard above
                # existed — catches season passes already sitting at is_dlc=False.
                has_fullgame = bool((details.get("fullgame") or {}).get("appid"))
                looks_like_dlc = _steam._should_auto_hide(game.title, details, is_dlc=True)
                if has_fullgame or looks_like_dlc:
                    game.is_dlc = True

        if app_type == "dlc" and game.parent_id is None and not game.parent_id_user_set:
            fullgame = details.get("fullgame", {})
            parent_appid = str(fullgame.get("appid", "")).strip()
            if parent_appid:
                parent_release = db.query(models.GameRelease).filter_by(source="steam", external_id=parent_appid).first()
                if parent_release:
                    game.parent_id = parent_release.game_id

        # Push DLC status to children listed in the parent's dlc[] array.
        # Covers standalone expansions Steam tags as type=game on the child
        # but correctly lists under the parent (e.g. DOOM Eternal: The Ancient Gods).
        if app_type == "game":
            dlc_appids = details.get("dlc") or []
            if dlc_appids:
                _steam._promote_dlc_children(db, game.id, dlc_appids)

        if _steam._should_auto_hide(game.title, details, game.is_dlc):
            if not entry.is_hidden and not entry.is_hidden_user_set:
                entry.is_hidden = True

        # Pull the canonical header image URL from appdetails — covers the
        # case where Steam serves the asset from a hashed path our legacy
        # constructed CDN URL doesn't match (common on newer DLC).
        _steam._sync_header_artwork_from_appdetails(db, release, details)

    release.metadata_fetched_at = datetime.datetime.now(datetime.UTC)
    db.commit()

    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": f"Refreshed metadata for {game.display_title}."},
    )


_IMAGE_TYPE_LABELS = {
    "v": "vertical cover",
    "h": "horizontal cover",
    "hero": "hero image",
    "logo": "logo",
}


@router.post("/library/entries/{entry_id}/cover-override")
def set_cover_override(
    request: Request,
    entry_id: int,
    image_type: str = Form(...),
    url: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Apply a custom art override URL (typically a SteamGridDB pick) to a
    library entry. image_type: 'v' | 'h' | 'hero' | 'logo'."""
    from . import steamgriddb as sgdb

    if image_type not in sgdb.IMAGE_TYPES:
        return Response(status_code=400)
    url = url.strip()
    if not url:
        return Response(status_code=400)
    entry = (
        db.query(models.UserLibraryEntry)
        .options(selectinload(models.UserLibraryEntry.user_artwork))
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    sgdb._upsert_user_artwork(db, entry, image_type, url)
    db.commit()
    label = _IMAGE_TYPE_LABELS.get(image_type, image_type)
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": f"Custom {label} applied."},
    )


@router.post("/library/entries/{entry_id}/clear-cover-override")
def clear_cover_override(
    request: Request,
    entry_id: int,
    image_type: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Clear a custom art override on a library entry.
    image_type: 'v' | 'h' | 'hero' | 'logo'."""
    from . import steamgriddb as sgdb

    if image_type not in sgdb.IMAGE_TYPES:
        return Response(status_code=400)
    if image_type not in sgdb._IMAGE_TYPE_TO_ARTWORK_TYPE:
        return Response(status_code=400)
    art_type = sgdb._IMAGE_TYPE_TO_ARTWORK_TYPE[image_type]
    ua = (
        db.query(models.UserArtwork)
        .filter_by(user_id=current_user.id, artwork_type=art_type)
        .filter(models.UserArtwork.entry_id == entry_id)
        .first()
    )
    if ua:
        db.delete(ua)
    db.commit()
    label = _IMAGE_TYPE_LABELS.get(image_type, image_type)
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": f"Custom {label} cleared."},
    )


@router.get("/library/entries/{entry_id}/card")
def library_entry_card(
    entry_id: int,
    view_mode: str = Query("list"),
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Return the rendered card or list row for a single library entry.

    Used by the SGDB cover picker to update the background grid/list in-place
    after a v/h cover is applied — avoids a full page reload while the detail
    pane is still open."""
    entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    _attach_parent_fallbacks(db, [entry])
    tmpl = "partials/library_card.html" if view_mode in ("grid_v", "grid_h") else "partials/library_row.html"
    return templates.TemplateResponse(
        tmpl,
        {"request": request, "entry": entry, "view_mode": view_mode},
    )


@router.post("/library/entries/{entry_id}/auto-fetch-hero")
def auto_fetch_hero(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Auto-fetch a hero image from SGDB when the entry has no hero URL.
    Mirrors auto-fetch-logo. Returns 200 + {"url": ...} on success, 204 if
    nothing found. The caller refreshes the detail pane to show the result."""
    from . import steamgriddb as sgdb

    entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    url = sgdb.auto_fetch_hero(db, current_user, entry)
    if url:
        return JSONResponse({"url": url})
    return Response(status_code=204)


@router.post("/library/entries/{entry_id}/auto-fetch-logo")
def auto_fetch_logo(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Auto-fetch a logo from SGDB when the Steam CDN logo 404s or when the
    entry has no logo URL at all. Takes the top SGDB result and stores it as
    a UserArtwork row so future detail pane opens resolve it from there.

    Returns 200 + {"url": ...} on success, 204 when nothing found or no key.
    The JS onerror handler uses the returned URL to update the img src
    in-place so the logo appears without a page reload."""
    from . import steamgriddb as sgdb

    entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    url = sgdb.auto_fetch_logo(db, current_user, entry)
    if url:
        return JSONResponse({"url": url})
    return Response(status_code=204)


@router.get("/library/entries/{entry_id}/hero-block")
def library_entry_hero_block(
    entry_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Return just the hero+logo block for a library entry.
    Used to update only the hero area of a detail pane after a logo or hero
    is auto-fetched, without reloading the full pane."""
    entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    game = entry.release.game
    release = entry.release
    visuals = _build_detail_pane_visuals(db, entry, game, release)
    return templates.TemplateResponse(
        request=request,
        name="partials/detail_hero_block.html",
        context={"entry": entry, **visuals},
    )


@router.post("/library/entries/{entry_id}/auto-fetch-grid")
def auto_fetch_grid(
    entry_id: int,
    orientation: str = Query("h"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Auto-fetch a grid cover (h or v) from SGDB on detail pane open.
    Returns 200 + {"url": ...} on success, 204 if nothing found or already exists."""
    from . import steamgriddb as sgdb

    entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    url = sgdb.auto_fetch_grid(db, current_user, entry, orientation=orientation)
    if url:
        return JSONResponse({"url": url})
    return Response(status_code=204)


@router.get("/completions/{completion_id}/card")
def completion_card_fragment(
    completion_id: int,
    view_mode: str = Query("list"),
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Return the rendered card or list row for a single completion.

    Used to update the background grid/list in-place after a cover is
    auto-fetched when the completion detail pane is opened."""
    completion = (
        db.query(models.Completion)
        .options(
            joinedload(models.Completion.library_entry).joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.Completion.library_entry).joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            joinedload(models.Completion.library_entry).selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=completion_id, user_id=current_user.id)
        .first()
    )
    if not completion:
        return Response(status_code=404)
    tmpl = "partials/completion_card.html" if view_mode in ("grid_h", "grid_v") else "partials/completion_row.html"
    return templates.TemplateResponse(
        tmpl,
        {"request": request, "completion": completion, "view_mode": view_mode},
    )


# --- Match Review ---


@router.get("/library/match-review")
def match_review_page(
    request: Request,
    show_skipped: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidates = match_review.get_candidates(db, current_user, include_skipped=show_skipped)
    # Attach the synced release object to each candidate for the template
    # (look up by platform_source + external_id)
    enriched = []
    for c in candidates:
        synced_release = db.query(models.GameRelease).filter_by(source=c.platform_source, external_id=c.external_id).first()
        synced_entry = None
        if synced_release:
            synced_entry = db.query(models.UserLibraryEntry).filter_by(user_id=current_user.id, release_id=synced_release.id).first()
        enriched.append(
            {
                "candidate": c,
                "manual_entry": c.manual_entry,
                "synced_release": synced_release,
                "synced_entry": synced_entry,
                "label": match_review.confidence_label(c.match_score),
                "color": match_review.confidence_css(c.match_score),
            }
        )

    # Group by manual_entry_id so multi-candidate entries can be rendered
    # as a single "pick one" card rather than separate cards.
    groups: list[dict] = []
    _seen: dict[int, dict] = {}
    for row in enriched:
        mid = row["candidate"].manual_entry_id
        if mid not in _seen:
            g = {
                "manual_entry": row["manual_entry"],
                "candidates": [],
                "multi": False,
            }
            _seen[mid] = g
            groups.append(g)
        _seen[mid]["candidates"].append(row)
    for g in groups:
        g["multi"] = len(g["candidates"]) > 1
    groups.sort(
        key=lambda g: (
            not g["multi"],
            g["manual_entry"].title.lower(),
        )
    )

    pending = match_review.pending_count(db, current_user)
    return templates.TemplateResponse(
        request=request,
        name="match_review.html",
        context={
            "current_user": current_user,
            "enriched": enriched,
            "groups": groups,
            "pending": pending,
            "show_skipped": show_skipped,
            **_base_ctx(db, current_user),
        },
    )


@router.post("/library/match-review/{candidate_id}/merge")
def match_review_merge(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidate = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(models.SyncMatchCandidate.id == candidate_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not candidate:
        return Response(status_code=404)
    ok = match_review.merge_candidate(db, candidate, current_user)
    kind = "success" if ok else "danger"
    body = "Entries merged." if ok else "Merge failed — synced entry not found."
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": kind, "body": body},
        headers={"HX-Reswap": "none"},
    )


@router.post("/library/match-review/{candidate_id}/skip")
def match_review_skip(
    candidate_id: int,
    request: Request,
    note: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidate = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(models.SyncMatchCandidate.id == candidate_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not candidate:
        return Response(status_code=404)
    match_review.dismiss_candidate(db, candidate, note=note or None)
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": "success", "body": "Kept separate."},
        headers={"HX-Reswap": "none"},
    )


@router.get("/library/match-review/{candidate_id}/preview")
def match_review_preview(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Render a preview pane showing what the synced entry looks like, with
    Confirm / Dismiss / Close actions pinned in the footer."""
    candidate = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(models.SyncMatchCandidate.id == candidate_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not candidate:
        return Response(status_code=404)

    # Load the synced (surviving) entry
    synced_entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.platform_obj),
            joinedload(models.UserLibraryEntry.completions),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .join(models.GameRelease)
        .filter(
            models.GameRelease.external_id == candidate.external_id,
            models.GameRelease.source == candidate.platform_source,
            models.UserLibraryEntry.user_id == current_user.id,
        )
        .first()
    )

    # Load the manual entry (the one that will be removed)
    manual_entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.completions),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=candidate.manual_entry_id)
        .first()
    )

    visuals = _build_detail_pane_visuals(db, synced_entry, synced_entry.release.game, synced_entry.release) if synced_entry else {}
    appdetails = (synced_entry.release.raw_data or {}).get("appdetails") or {} if synced_entry else {}
    # Completions come from the manual entry — they migrate to the synced entry on confirm
    completions = sorted(manual_entry.completions, key=lambda c: c.completed_at, reverse=True) if manual_entry else []

    return templates.TemplateResponse(
        request=request,
        name="partials/match_review_preview.html",
        context={
            "candidate": candidate,
            "entry": synced_entry,
            "game": synced_entry.release.game if synced_entry else None,
            "release": synced_entry.release if synced_entry else None,
            "appdetails": appdetails,
            "steam_meta": _extract_steam_meta(appdetails),
            "igdb_meta": _extract_igdb_meta(synced_entry.release) if synced_entry else {},
            "completions": completions,
            "manual_entry": manual_entry,
            "current_user": current_user,
            "needs_refresh": False,
            "fresh_open": False,
            **visuals,
        },
    )


@router.post("/library/match-review/merge-bulk")
def match_review_merge_bulk(
    request: Request,
    candidate_ids: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    ids = [int(x) for x in candidate_ids.split(",") if x.strip().isdigit()]
    merged = 0
    failed = 0
    for cid in ids:
        candidate = (
            db.query(models.SyncMatchCandidate)
            .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
            .filter(models.SyncMatchCandidate.id == cid, models.UserLibraryEntry.user_id == current_user.id)
            .first()
        )
        if candidate and match_review.merge_candidate(db, candidate, current_user):
            merged += 1
        else:
            failed += 1
    parts = [f"{merged} merged"]
    if failed:
        parts.append(f"{failed} failed")
    msg = "Bulk merge complete — " + ", ".join(parts) + "."
    kind = "danger" if failed and not merged else "success"
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": kind, "body": msg},
        headers={"HX-Refresh": "true"},
    )


@router.post("/library/match-review/clear-dismissed")
def match_review_clear_dismissed(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Delete all dismissed candidates for this user so they can be re-detected on next scan."""
    deleted = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.SyncMatchCandidate.status == "dismissed",
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    body = f"{deleted} dismissed match{'es' if deleted != 1 else ''} cleared — they'll resurface on next scan."
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": "success", "body": body},
        headers={"HX-Reswap": "none"},
    )


# --- Historical import ---


@router.get("/library/import")
def import_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    pending = (
        db.query(models.ImportCandidate)
        .filter(
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .count()
    )
    return templates.TemplateResponse(
        request=request,
        name="import.html",
        context={"current_user": current_user, "pending": pending, **_base_ctx(db, current_user)},
    )


@router.post("/library/import/upload")
async def import_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        return templates.TemplateResponse(
            request=request,
            name="partials/_toast.html",
            context={"kind": "error", "body": "Please upload an xlsx file."},
            headers={"HX-Reswap": "none"},
        )
    contents = await file.read()
    jobs.enqueue_import(current_user.id, file.filename, contents)
    # Start draining the queue only if nothing is currently running
    active = [j for j in jobs.active_jobs_for(current_user.id) if j.kind == "import_xlsx" and j.status == jobs.JobStatus.RUNNING]
    if not active:
        asyncio.create_task(_drain_import_queue(current_user.id))
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": "success", "body": f"Queued: {file.filename}"},
    )


async def _drain_import_queue(user_id: int) -> None:
    """Process queued import jobs one at a time until the queue is empty."""
    while True:
        item = jobs.next_queued_import(user_id)
        if not item:
            break
        job_id, _filename, file_bytes = item
        await _run_import_job(job_id, user_id, file_bytes)


async def _run_import_job(job_id: str, user_id: int, file_bytes: bytes) -> None:
    jobs.update(job_id, status=jobs.JobStatus.RUNNING, progress={"phase": "Parsing", "done": 0, "total": 0})
    db = models.SessionLocal()
    try:
        result = await asyncio.to_thread(importer.parse_xlsx, file_bytes, db, user_id)
        total = len(result.candidates)
        jobs.update(job_id, progress={"phase": "Writing", "done": 0, "total": total})

        def on_progress(done: int) -> None:
            jobs.update(job_id, progress={"phase": "Writing", "done": done, "total": total})
            j = jobs.get(job_id)
            if j and j.cancel_requested:
                raise RuntimeError("cancelled")

        count = await asyncio.to_thread(importer.write_candidates, result, db, user_id, on_progress)
        skipped_msg = f" ({result.skipped_rows} blank rows skipped)" if result.skipped_rows else ""
        jobs.mark_done(
            job_id,
            f"Import complete — {count} candidate{'s' if count != 1 else ''} ready to review.{skipped_msg}",
        )
    except RuntimeError as e:
        if str(e) == "cancelled":
            jobs.mark_failed(job_id, "Import cancelled.")
        else:
            logger.exception("Import job %s failed", job_id)
            jobs.mark_failed(job_id, f"Import failed: {e}")
    except Exception as e:
        logger.exception("Import job %s failed", job_id)
        jobs.mark_failed(job_id, f"Import failed: {e}")
    finally:
        db.close()

    user = models.SessionLocal()
    try:
        current_user = user.query(models.User).filter(models.User.id == user_id).first()
        if current_user and current_user.steamgriddb_api_key:
            asyncio.create_task(_run_import_thumbnails_job(user_id))
    finally:
        user.close()


async def _run_import_thumbnails_job(user_id: int) -> None:
    """Fire-and-forget follow-up after an import finishes: fetch a SGDB
    placeholder thumbnail for every pending create_new/needs_review
    candidate, keyed by raw title. No job-tracker entry — this is a quiet
    cosmetic fill, not something the user needs a toast for; the thumbnails
    just appear in the review list as they're fetched (each commit is
    visible to new requests immediately)."""
    db = models.SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user is None:
            return
        await asyncio.to_thread(sgdb.fill_import_candidate_thumbnails, db, user)
    except Exception:
        logger.exception("Import candidate thumbnail fetch failed for user %s", user_id)
    finally:
        db.close()


_IMPORT_PAGE_SIZE = 50


@router.post("/library/import/cancel")
def import_cancel_job(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    active = [j for j in jobs.active_jobs_for(current_user.id) if j.kind == "import_xlsx"]
    for j in active:
        jobs.request_cancel(j.id)
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": "success" if active else "error", "body": "Import cancelled." if active else "No active import."},
    )


@router.post("/library/import/cancel/{job_id}")
def import_cancel_queued(
    request: Request,
    job_id: str,
    current_user: models.User = Depends(get_web_user),
):
    removed = jobs.cancel_queued_import(job_id, current_user.id)
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": "success" if removed else "error", "body": "Queued upload removed." if removed else "Not found."},
    )


@router.get("/library/import/progress")
def import_progress(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    active = [j for j in jobs.active_jobs_for(current_user.id) if j.kind == "import_xlsx"]
    job = active[0] if active else None
    queue = jobs.queued_imports_for(current_user.id)
    pending = (
        db.query(func.count(models.ImportCandidate.id))
        .filter(
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .scalar()
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/_import_progress.html",
        context={"job": job, "queue": queue, "pending": pending},
    )


@router.get("/library/import/status")
def import_status(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Replaces #import-status after an import job completes."""
    pending = (
        db.query(func.count(models.ImportCandidate.id))
        .filter(
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .scalar()
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/_import_status.html",
        context={"pending": pending},
    )


_IMPORT_TABS = ("add_to_existing", "create_new", "needs_review")


def _import_confirmed_count(db: Session, user_id: int) -> int:
    return (
        db.query(models.ImportCandidate)
        .filter(models.ImportCandidate.user_id == user_id, models.ImportCandidate.status == "confirmed")
        .count()
    )


def _import_tab_counts(db: Session, user_id: int) -> dict[str, int]:
    counts = dict.fromkeys(_IMPORT_TABS, 0)
    rows = (
        db.query(models.ImportCandidate.proposed_action, func.count())
        .filter(models.ImportCandidate.user_id == user_id, models.ImportCandidate.status == "pending")
        .group_by(models.ImportCandidate.proposed_action)
        .all()
    )
    for action, count in rows:
        if action in counts:
            counts[action] = count
    return counts


def _import_platform_options(db: Session, user_id: int, tab: str) -> list[dict]:
    """Distinct platforms across ALL pending candidates in this tab (not just
    the currently loaded page) for the filter dropdown."""
    rows = (
        db.query(models.ImportCandidate.platform_id, models.ImportCandidate.raw_platform)
        .filter(
            models.ImportCandidate.user_id == user_id,
            models.ImportCandidate.status == "pending",
            models.ImportCandidate.proposed_action == tab,
        )
        .distinct()
        .all()
    )
    pids = {pid for pid, _ in rows if pid}
    pmap = {p.id: p for p in db.query(models.Platform).filter(models.Platform.id.in_(pids)).all()} if pids else {}
    seen_pids: set[int] = set()
    options = []
    for pid, raw in rows:
        if pid and pid in pmap:
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            options.append({"value": f"pid:{pid}", "label": pmap[pid].display_name or pmap[pid].name})
        elif not pid and raw:
            options.append({"value": f"raw:{raw}", "label": f"{raw} (unresolved)"})
    options.sort(key=lambda p: p["label"])
    return options


def _import_year_options(db: Session, user_id: int, tab: str) -> list[str]:
    """Distinct completion years across ALL pending candidates in this tab."""
    rows = (
        db.query(func.strftime("%Y", models.ImportRow.completed_at))
        .join(models.ImportCandidate, models.ImportRow.candidate_id == models.ImportCandidate.id)
        .filter(
            models.ImportCandidate.user_id == user_id,
            models.ImportCandidate.status == "pending",
            models.ImportCandidate.proposed_action == tab,
            models.ImportRow.completed_at.isnot(None),
        )
        .distinct()
        .all()
    )
    return sorted({y for (y,) in rows if y}, reverse=True)


def _import_candidate_visuals(db: Session, candidate: models.ImportCandidate) -> dict | None:
    """Hero/logo visuals + condensed library metadata for an add_to_existing
    candidate's matched entry, for card view. None if the candidate has no
    matched entry."""
    entry = candidate.library_entry
    if not entry or not entry.release or not entry.release.game:
        return None
    visuals = _build_detail_pane_visuals(db, entry, entry.release.game, entry.release)
    appdetails = (entry.release.raw_data or {}).get("appdetails") or {}
    return {
        **visuals,
        "entry": entry,
        "release": entry.release,
        "steam_meta": _extract_steam_meta(appdetails),
        "igdb_meta": _extract_igdb_meta(entry.release),
    }


@router.get("/library/import/review")
def import_review_page(
    request: Request,
    tab: str = "add_to_existing",
    offset: int = 0,
    q: str = "",
    platform: str = "",
    year: str = "",
    sort: str = "id",
    view: str = "list",
    rows_only: bool = Query(False),
    refresh_filters: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    # Sticky filters are remembered PER TAB as cookies — one key per tab so a
    # platform chosen on "create new" never leaks onto "add to existing", and
    # cookies (not localStorage) specifically so the server can bind them into
    # this initial query and render the list already filtered, with no client
    # re-fetch. The tab is resolved first, then that tab's own filter cookies
    # fill in whatever the request didn't pass explicitly (explicit params —
    # a live filter change — always win).
    qp = request.query_params
    if "tab" not in qp:
        tab = request.cookies.get("cgt-import-tab", tab)
    if tab not in _IMPORT_TABS and tab != "confirmed":
        tab = "add_to_existing"
    # unquote: the client writes these cookies with encodeURIComponent (raw
    # platform names can contain spaces), so a value like "pid:5" is stored as
    # "pid%3A5" — decode it back before the startswith("pid:")/option matching.
    if "platform" not in qp:
        platform = unquote(request.cookies.get(f"cgt-import-{tab}-platform", platform))
    if "year" not in qp:
        year = unquote(request.cookies.get(f"cgt-import-{tab}-year", year))
    if "sort" not in qp:
        sort = unquote(request.cookies.get(f"cgt-import-{tab}-sort", sort))
    if sort not in ("id", "date_desc", "date_asc"):
        sort = "id"
    if view not in ("list", "card") or tab != "add_to_existing":
        view = "list"

    tab_counts = _import_tab_counts(db, current_user.id)
    pending = sum(tab_counts.values())

    if tab == "confirmed":
        # Already-imported candidates, any proposed_action — surfaced so a
        # wrong confirm can be reopened instead of fixed by DB surgery.
        filtered_q = db.query(models.ImportCandidate).filter(
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "confirmed",
        )
    else:
        filtered_q = db.query(models.ImportCandidate).filter(
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
            models.ImportCandidate.proposed_action == tab,
        )

    if q:
        filtered_q = filtered_q.filter(models.ImportCandidate.raw_title.ilike(f"%{q}%"))

    if platform.startswith("pid:"):
        filtered_q = filtered_q.filter(models.ImportCandidate.platform_id == int(platform[4:]))
    elif platform.startswith("raw:"):
        filtered_q = filtered_q.filter(
            models.ImportCandidate.platform_id.is_(None),
            models.ImportCandidate.raw_platform == platform[4:],
        )

    if year:
        filtered_q = filtered_q.filter(
            models.ImportCandidate.id.in_(
                db.query(models.ImportRow.candidate_id).filter(func.strftime("%Y", models.ImportRow.completed_at) == year)
            )
        )

    tab_total = filtered_q.count()

    ordered_q = filtered_q
    if sort in ("date_desc", "date_asc"):
        row_agg = (
            db.query(
                models.ImportRow.candidate_id.label("candidate_id"),
                func.min(models.ImportRow.completed_at).label("min_date"),
            )
            .group_by(models.ImportRow.candidate_id)
            .subquery()
        )
        order_col = row_agg.c.min_date
        ordered_q = ordered_q.outerjoin(row_agg, row_agg.c.candidate_id == models.ImportCandidate.id).order_by(
            order_col.desc() if sort == "date_desc" else order_col.asc(), models.ImportCandidate.id
        )
    else:
        ordered_q = ordered_q.order_by(models.ImportCandidate.id)

    candidate_opts = [
        joinedload(models.ImportCandidate.rows),
        joinedload(models.ImportCandidate.platform),
        joinedload(models.ImportCandidate.library_entry)
        .joinedload(models.UserLibraryEntry.release)
        .joinedload(models.GameRelease.platform_obj),
        # game is needed in list view too — rows display and diff-check
        # against game.display_title (the entry itself has no display name).
        joinedload(models.ImportCandidate.library_entry).joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
    ]
    if view == "card":
        candidate_opts += [
            joinedload(models.ImportCandidate.library_entry)
            .joinedload(models.UserLibraryEntry.release)
            .joinedload(models.GameRelease.game),
            joinedload(models.ImportCandidate.library_entry)
            .joinedload(models.UserLibraryEntry.release)
            .joinedload(models.GameRelease.artwork),
            joinedload(models.ImportCandidate.library_entry).selectinload(models.UserLibraryEntry.user_artwork),
        ]
    candidates = ordered_q.options(*candidate_opts).offset(offset).limit(_IMPORT_PAGE_SIZE).all()
    next_offset = offset + _IMPORT_PAGE_SIZE
    has_more = next_offset < tab_total

    candidate_visuals = {c.id: _import_candidate_visuals(db, c) for c in candidates} if view == "card" else {}

    filter_ctx = {"q": q, "platform": platform, "year": year, "sort": sort, "view": view}
    confirmed_count = _import_confirmed_count(db, current_user.id)

    # rows_only is the explicit ask from the infinite-scroll sentinel — its
    # client-corrected offset can legitimately be 0 (a fully-confirmed first
    # page), so "offset > 0" alone would fall through and nest a whole tab
    # (header row and all) inside the table.
    if request.headers.get("HX-Request") and (rows_only or offset > 0):
        return templates.TemplateResponse(
            request=request,
            name="partials/_import_card_load_more.html" if view == "card" else "partials/_import_rows.html",
            context={
                "candidates": candidates,
                "next_offset": next_offset,
                "has_more": has_more,
                "tab": tab,
                "tab_total": tab_total,
                "candidate_visuals": candidate_visuals,
                **filter_ctx,
            },
        )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="partials/_import_tab_content.html",
            context={
                "candidates": candidates,
                "next_offset": next_offset,
                "has_more": has_more,
                "tab": tab,
                "tab_total": tab_total,
                "tab_counts": tab_counts,
                "candidate_visuals": candidate_visuals,
                "platform_options": _import_platform_options(db, current_user.id, tab),
                "year_options": _import_year_options(db, current_user.id, tab),
                # Tab switches set this so the selects repaint OOB for the new
                # tab; filter changes / load-more leave it False (no repaint).
                "refresh_filters": refresh_filters,
                **filter_ctx,
            },
        )

    return templates.TemplateResponse(
        request=request,
        name="import_review.html",
        context={
            "current_user": current_user,
            "candidates": candidates,
            "pending": pending,
            "confirmed_count": confirmed_count,
            "tab": tab,
            "tab_total": tab_total,
            "tab_counts": tab_counts,
            "next_offset": next_offset,
            "has_more": has_more,
            "candidate_visuals": candidate_visuals,
            "platform_options": _import_platform_options(db, current_user.id, tab),
            "year_options": _import_year_options(db, current_user.id, tab),
            # for the shared add-game modal's platform datalist (in-place "Add new")
            "platforms": _get_all_platforms(db),
            **filter_ctx,
            **_base_ctx(db, current_user),
        },
    )


@router.get("/library/import/{candidate_id}/preview")
def import_candidate_preview(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Detail pane content for an add_to_existing candidate — shows the matched
    library entry with an import-specific Confirm footer."""
    candidate = (
        db.query(models.ImportCandidate)
        .filter(models.ImportCandidate.id == candidate_id, models.ImportCandidate.user_id == current_user.id)
        .options(
            joinedload(models.ImportCandidate.rows),
            joinedload(models.ImportCandidate.platform),
            joinedload(models.ImportCandidate.library_entry),
        )
        .first()
    )
    if not candidate or not candidate.library_entry_id:
        return Response(status_code=404)

    entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.platform_obj),
            joinedload(models.UserLibraryEntry.completions),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter(models.UserLibraryEntry.id == candidate.library_entry_id)
        .first()
    )
    if not entry:
        return Response(status_code=404)

    game = entry.release.game
    child_entries = []
    if not game.is_dlc:
        child_entries = (
            db.query(models.UserLibraryEntry)
            .options(
                joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
                joinedload(models.UserLibraryEntry.release).selectinload(models.GameRelease.artwork),
                joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.platform_obj),
                selectinload(models.UserLibraryEntry.user_artwork),
            )
            .join(models.GameRelease)
            .join(models.Game)
            .filter(
                models.UserLibraryEntry.user_id == current_user.id,
                models.Game.parent_id == game.id,
            )
            .order_by(models.Game.title)
            .all()
        )

    visuals = _build_detail_pane_visuals(db, entry, game, entry.release)
    appdetails = (entry.release.raw_data or {}).get("appdetails") or {}

    return templates.TemplateResponse(
        request=request,
        name="partials/library_detail.html",
        context={
            "entry": entry,
            "game": game,
            "release": entry.release,
            "appdetails": appdetails,
            "steam_meta": _extract_steam_meta(appdetails),
            "igdb_meta": _extract_igdb_meta(entry.release),
            "child_entries": child_entries,
            "completions": sorted(entry.completions, key=lambda c: c.completed_at, reverse=True),
            "current_user": current_user,
            "needs_refresh": False,
            "fresh_open": False,
            "candidate": candidate,
            "import_rows": sorted(candidate.rows, key=lambda r: r.completed_at or datetime.date.min, reverse=True),
            **visuals,
        },
    )


@router.get("/library/import/{candidate_id}/edit")
def import_candidate_edit_form(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Modal content for editing a pending candidate's title/platform, or
    manually linking it to a specific existing library entry."""
    candidate = (
        db.query(models.ImportCandidate)
        .filter(
            models.ImportCandidate.id == candidate_id,
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .options(
            joinedload(models.ImportCandidate.platform),
            joinedload(models.ImportCandidate.library_entry),
            joinedload(models.ImportCandidate.rows),
        )
        .first()
    )
    if not candidate:
        return Response(status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="partials/_import_edit_modal.html",
        context={"candidate": candidate, "platforms": _get_all_platforms(db)},
    )


@router.get("/library/import/{candidate_id}/link")
def import_candidate_link_form(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Modal content for manually linking a pending candidate to a specific
    library entry (search-only; saving confirms immediately)."""
    candidate = (
        db.query(models.ImportCandidate)
        .filter(
            models.ImportCandidate.id == candidate_id,
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .first()
    )
    if not candidate:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="partials/_import_link_modal.html",
        context={"candidate": candidate},
    )


@router.post("/library/import/{candidate_id}/link")
def import_candidate_link(
    candidate_id: int,
    request: Request,
    library_entry_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Manual pick IS the decision: link the candidate to the chosen entry
    and confirm it in the same step. Reopen (Confirmed tab) is the undo."""
    candidate = (
        db.query(models.ImportCandidate)
        .filter(
            models.ImportCandidate.id == candidate_id,
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .options(joinedload(models.ImportCandidate.rows))
        .first()
    )
    if not candidate:
        return Response(status_code=404)
    entry = (
        db.query(models.UserLibraryEntry)
        .filter(models.UserLibraryEntry.id == library_entry_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)

    candidate.library_entry_id = entry.id
    candidate.proposed_action = "add_to_existing"
    _confirm_add_to_existing(db, current_user, candidate)
    db.commit()

    tab_counts = _import_tab_counts(db, current_user.id)
    pending = sum(tab_counts.values())
    tab_counts["confirmed"] = _import_confirmed_count(db, current_user.id)
    toast = templates.get_template("partials/_toast.html").render(
        kind="success",
        body=f"Confirmed against {entry.release.game.display_title} — completions logged.",
    )
    counts = templates.get_template("partials/_import_counts_oob.html").render(tab_counts=tab_counts, pending=pending)
    return Response(content=toast + counts, media_type="text/html")


@router.post("/library/import/{candidate_id}/edit")
def import_candidate_edit(
    candidate_id: int,
    request: Request,
    raw_title: str = Form(...),
    # Empty string counts as "missing" for a required Form field, so a
    # cleared platform box silently 422'd — platform is legitimately
    # optional here (unmatched platforms are the needs_review case).
    raw_platform: str = Form(""),
    row_id: list[int] = Form([]),
    row_date: list[str] = Form([]),
    row_playthroughs: list[str] = Form([]),
    row_notes: list[str] = Form([]),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidate = (
        db.query(models.ImportCandidate)
        .filter(
            models.ImportCandidate.id == candidate_id,
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .options(joinedload(models.ImportCandidate.rows))
        .first()
    )
    if not candidate:
        return Response(status_code=404)

    candidate.raw_title = raw_title.strip()
    candidate.raw_platform = raw_platform.strip()
    candidate.platform_id = models.resolve_platform_id(db, candidate.raw_platform) if candidate.raw_platform else None

    # Per-row completion edits (date / playthroughs / notes). Parallel lists,
    # one slot per rendered row; an edited date becomes day-precision, an
    # untouched one round-trips equal and keeps its original precision.
    rows_by_id = {r.id: r for r in candidate.rows}
    for rid, dstr, plays, notes in zip(row_id, row_date, row_playthroughs, row_notes, strict=False):
        row = rows_by_id.get(rid)
        if not row:
            continue
        if dstr:
            try:
                d = datetime.date.fromisoformat(dstr)
            except ValueError:
                d = row.completed_at
            if d != row.completed_at:
                row.completed_at = d
                row.completed_at_precision = "day"
        else:
            row.completed_at = None
            row.completed_at_precision = None
        row.playthroughs = plays.strip() or None
        row.raw_notes = notes.strip() or None

    candidate_collection = next((r.raw_collection for r in candidate.rows if r.raw_collection), None)
    best_entry = importer._best_matching_entry(db, current_user.id, candidate.raw_title, candidate.platform_id, candidate_collection)
    if best_entry:
        candidate.library_entry_id = best_entry.id
        candidate.proposed_action = "add_to_existing"
    elif candidate.platform_id is None:
        candidate.library_entry_id = None
        candidate.proposed_action = "needs_review"
    else:
        candidate.library_entry_id = None
        candidate.proposed_action = "create_new"

    db.commit()
    tab_counts = _import_tab_counts(db, current_user.id)
    return templates.TemplateResponse(
        request=request,
        name="partials/_import_counts_oob.html",
        context={"tab_counts": tab_counts, "pending": sum(tab_counts.values())},
        headers={"HX-Reswap": "outerHTML", "HX-Retarget": f"#import-row-{candidate_id}"},
    )


@router.post("/library/import/{candidate_id}/dismiss")
def import_dismiss(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidate = (
        db.query(models.ImportCandidate)
        .filter(models.ImportCandidate.id == candidate_id, models.ImportCandidate.user_id == current_user.id)
        .first()
    )
    if not candidate:
        return Response(status_code=404)
    if candidate.status == "pending":
        # Only pending candidates can be dismissed — a stale duplicate row of
        # an already-confirmed candidate must not flip it to dismissed (that
        # would orphan its logged completions from the Reopen path).
        candidate.status = "dismissed"
        candidate.reviewed_at = datetime.datetime.now(datetime.UTC)
        db.commit()
    tab_counts = _import_tab_counts(db, current_user.id)
    return templates.TemplateResponse(
        request=request,
        name="partials/_import_counts_oob.html",
        context={"tab_counts": tab_counts, "pending": sum(tab_counts.values())},
        headers={"HX-Reswap": "outerHTML", "HX-Retarget": f"#import-row-{candidate_id}"},
    )


def _confirm_add_to_existing(db: Session, current_user: models.User, candidate) -> None:
    """Log the candidate's rows as completions against its matched library
    entry (skipping exact duplicates) and mark it confirmed. Caller commits."""
    for row in candidate.rows:
        if not row.completed_at:
            continue
        already_exists = (
            db.query(models.Completion)
            .filter(
                models.Completion.library_entry_id == candidate.library_entry_id,
                models.Completion.completed_at == row.completed_at,
                models.Completion.sort_order == row.row_number,
            )
            .first()
        )
        if already_exists:
            continue
        comp = models.Completion(
            user_id=current_user.id,
            library_entry_id=candidate.library_entry_id,
            completed_at=row.completed_at,
            completed_at_precision=row.completed_at_precision or "day",
            playthroughs=row.playthroughs,
            notes=row.raw_notes,
            sort_order=row.row_number,
        )
        db.add(comp)
        db.flush()
        # Stamp the linkage so Reopen can delete exactly this one.
        row.created_completion_id = comp.id
    candidate.status = "confirmed"
    candidate.reviewed_at = datetime.datetime.now(datetime.UTC)


@router.post("/library/import/confirm-bulk")
def confirm_import_bulk(
    request: Request,
    ids: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Confirm a batch of add_to_existing candidates in one shot (bulk-select
    mode on the review list). Only pending, matched, user-owned candidates
    in the id list are touched; anything else is silently skipped."""
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return Response(status_code=422)
    candidates = (
        db.query(models.ImportCandidate)
        .filter(
            models.ImportCandidate.id.in_(id_list),
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
            models.ImportCandidate.proposed_action == "add_to_existing",
            models.ImportCandidate.library_entry_id.isnot(None),
        )
        .options(joinedload(models.ImportCandidate.rows))
        .all()
    )
    for candidate in candidates:
        _confirm_add_to_existing(db, current_user, candidate)
    db.commit()

    tab_counts = _import_tab_counts(db, current_user.id)
    pending = sum(tab_counts.values())
    tab_counts["confirmed"] = _import_confirmed_count(db, current_user.id)
    toast = templates.get_template("partials/_toast.html").render(
        kind="success", body=f"Confirmed {len(candidates)} candidate{'s' if len(candidates) != 1 else ''}."
    )
    counts = templates.get_template("partials/_import_counts_oob.html").render(tab_counts=tab_counts, pending=pending)
    return Response(content=toast + counts, media_type="text/html")


@router.post("/library/import/dismiss-bulk")
def dismiss_import_bulk(
    request: Request,
    ids: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Dismiss a batch of pending candidates (bulk-select mode)."""
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return Response(status_code=422)
    candidates = (
        db.query(models.ImportCandidate)
        .filter(
            models.ImportCandidate.id.in_(id_list),
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .all()
    )
    now = datetime.datetime.now(datetime.UTC)
    for candidate in candidates:
        candidate.status = "dismissed"
        candidate.reviewed_at = now
    db.commit()

    tab_counts = _import_tab_counts(db, current_user.id)
    pending = sum(tab_counts.values())
    toast = templates.get_template("partials/_toast.html").render(
        kind="success", body=f"Dismissed {len(candidates)} candidate{'s' if len(candidates) != 1 else ''}."
    )
    counts = templates.get_template("partials/_import_counts_oob.html").render(tab_counts=tab_counts, pending=pending)
    return Response(content=toast + counts, media_type="text/html")


@router.post("/library/import/{candidate_id}/confirm")
def import_confirm(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidate = (
        db.query(models.ImportCandidate)
        .filter(models.ImportCandidate.id == candidate_id, models.ImportCandidate.user_id == current_user.id)
        .options(
            joinedload(models.ImportCandidate.rows),
        )
        .first()
    )
    if not candidate:
        return Response(status_code=404)

    if candidate.status != "pending":
        # Stale duplicate row (candidate already handled elsewhere) — don't
        # re-process; just remove the row and refresh the counts.
        tab_counts = _import_tab_counts(db, current_user.id)
        return templates.TemplateResponse(
            request=request,
            name="partials/_import_counts_oob.html",
            context={"tab_counts": tab_counts, "pending": sum(tab_counts.values())},
            headers={"HX-Reswap": "outerHTML", "HX-Retarget": f"#import-row-{candidate_id}"},
        )

    if candidate.proposed_action == "add_to_existing" and candidate.library_entry_id:
        _confirm_add_to_existing(db, current_user, candidate)
        db.commit()
        tab_counts = _import_tab_counts(db, current_user.id)
        pending = sum(tab_counts.values())
        tab_counts["confirmed"] = _import_confirmed_count(db, current_user.id)
        return templates.TemplateResponse(
            request=request,
            name="partials/_import_counts_oob.html",
            context={"tab_counts": tab_counts, "pending": pending},
            headers={"HX-Reswap": "outerHTML", "HX-Retarget": f"#import-row-{candidate_id}"},
        )

    # create_new / needs_review — redirect to library with modal pre-filled
    platform_name = candidate.platform.name if candidate.platform else candidate.raw_platform
    redirect_url = (
        f"/library?import_candidate={candidate_id}&prefill_title={candidate.raw_title}"
        f"&prefill_platform={platform_name}&return_tab={candidate.proposed_action}"
    )
    return Response(status_code=200, headers={"HX-Redirect": redirect_url})


@router.post("/library/import/{candidate_id}/reopen")
def reopen_import_candidate(
    request: Request,
    candidate_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Undo a confirm: delete the completions it created and put the
    candidate back in the review queue. A reopened create_new keeps its
    library entry and becomes add_to_existing so re-confirming logs against
    that entry instead of creating a duplicate."""
    candidate = (
        db.query(models.ImportCandidate)
        .filter(
            models.ImportCandidate.id == candidate_id,
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "confirmed",
        )
        .options(joinedload(models.ImportCandidate.rows))
        .first()
    )
    if not candidate:
        return Response(status_code=404)

    for row in candidate.rows:
        comp = None
        if row.created_completion_id:
            comp = db.get(models.Completion, row.created_completion_id)
        elif candidate.library_entry_id and row.completed_at:
            # Rows confirmed before the linkage column existed: match on the
            # exact fields confirm stamped (sort_order carries the sheet row
            # number, which sync/manual completions never set).
            comp = (
                db.query(models.Completion)
                .filter(
                    models.Completion.user_id == current_user.id,
                    models.Completion.library_entry_id == candidate.library_entry_id,
                    models.Completion.completed_at == row.completed_at,
                    models.Completion.sort_order == row.row_number,
                )
                .first()
            )
        if comp:
            db.delete(comp)
        row.created_completion_id = None

    if candidate.library_entry_id:
        candidate.proposed_action = "add_to_existing"
    candidate.status = "pending"
    candidate.reviewed_at = None
    db.commit()

    tab_counts = _import_tab_counts(db, current_user.id)
    pending = sum(tab_counts.values())
    tab_counts["confirmed"] = _import_confirmed_count(db, current_user.id)
    return templates.TemplateResponse(
        request=request,
        name="partials/_import_counts_oob.html",
        context={"tab_counts": tab_counts, "pending": pending},
        headers={"HX-Reswap": "outerHTML", "HX-Retarget": f"#import-row-{candidate_id}"},
    )


@router.post("/library/import/fetch-thumbnails")
def import_fetch_thumbnails(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    """Kick off a background pass fetching SGDB placeholder thumbnails for
    pending create_new/needs_review candidates that don't have one yet.

    Manual trigger for candidates already sitting in the queue from a past
    import (the automatic trigger only fires right after a fresh import
    finishes). No job-tracker entry, same as the automatic trigger — this
    is a quiet fill, not something worth a toast; thumbnails just appear
    in the review list as they're fetched."""
    if not current_user.steamgriddb_api_key:
        return Response(status_code=422, content="Set your SteamGridDB API key first.")
    asyncio.create_task(_run_import_thumbnails_job(current_user.id))
    return Response(status_code=202)


@router.post("/library/import/clear")
def import_clear_all(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Cancel running/queued import jobs, then delete pending candidates (confirmed stay)."""
    for j in jobs.active_jobs_for(current_user.id):
        if j.kind == "import_xlsx":
            jobs.request_cancel(j.id)
    for job_id, _ in jobs.queued_imports_for(current_user.id):
        jobs.cancel_queued_import(job_id, current_user.id)
    pending_ids = [
        r[0]
        for r in db.query(models.ImportCandidate.id)
        .filter(
            models.ImportCandidate.user_id == current_user.id,
            models.ImportCandidate.status == "pending",
        )
        .all()
    ]
    if pending_ids:
        db.query(models.ImportRow).filter(models.ImportRow.candidate_id.in_(pending_ids)).delete(synchronize_session=False)
        db.query(models.ImportCandidate).filter(models.ImportCandidate.id.in_(pending_ids)).delete(synchronize_session=False)
    db.commit()
    return Response(
        headers={"HX-Redirect": "/library/import"},
        status_code=200,
    )


@router.post("/library/import/recheck")
def import_recheck(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Re-run title matching against the current library for all pending
    candidates, without re-uploading the spreadsheet."""
    importer.rematch_pending_candidates(db, current_user.id)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


# --- Completions ---


COMPLETIONS_SORT_OPTIONS = ["date_desc", "date_asc", "title_asc", "title_desc"]


@router.get("/completions")
def completions_page(
    request: Request,
    q: str = Query(""),
    platform: str = Query(""),
    completed_from: str = Query(""),
    completed_to: str = Query(""),
    all_time: bool = Query(False),
    view_mode: str | None = Query(None),
    sort: str = Query("date_desc"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    # Resolved from query → cookie → default (see _resolve_view_mode docstring).
    view_mode = _resolve_view_mode(request, view_mode, "cgt-completions-view-mode")
    # Default to current calendar year if neither date filter is set — unless
    # all_time is explicitly set, which is the only way to distinguish "user
    # cleared both fields to see everything" from "fresh page load with no
    # filters yet" (both look identical as blank query params otherwise).
    if not completed_from and not completed_to and not all_time:
        completed_from = f"{datetime.date.today().year}-01-01"
    completions_q = (
        db.query(models.Completion)
        .join(models.Completion.library_entry)
        .join(models.UserLibraryEntry.release)
        .join(models.GameRelease.game)
        .options(
            # selectinload on artwork so the grid view + list-row thumbs don't N+1.
            contains_eager(models.Completion.library_entry)
            .contains_eager(models.UserLibraryEntry.release)
            .contains_eager(models.GameRelease.game),
            contains_eager(models.Completion.library_entry)
            .contains_eager(models.UserLibraryEntry.release)
            .selectinload(models.GameRelease.artwork),
            contains_eager(models.Completion.library_entry)
            .contains_eager(models.UserLibraryEntry.release)
            .joinedload(models.GameRelease.platform_obj),
            contains_eager(models.Completion.library_entry).selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter(models.Completion.user_id == current_user.id)
    )
    q = q.strip()
    if q:
        completions_q = completions_q.filter(
            or_(
                models.Game.title.ilike(f"%{q}%"),
                models.Game.display_name.ilike(f"%{q}%"),
            )
        )
    if platform:
        completions_q = completions_q.filter(models.GameRelease.platform == platform)
    if completed_from:
        try:
            completions_q = completions_q.filter(models.Completion.completed_at >= datetime.date.fromisoformat(completed_from))
        except ValueError:
            pass
    if completed_to:
        try:
            completions_q = completions_q.filter(models.Completion.completed_at <= datetime.date.fromisoformat(completed_to))
        except ValueError:
            pass
    if sort not in COMPLETIONS_SORT_OPTIONS:
        sort = "date_desc"
    # sort_order preserves the original spreadsheet row order for historical
    # imports (manual/sync completions have sort_order NULL). It's a tiebreaker
    # within an equal completed_at, never a substitute for it — two rows the
    # sheet listed 1-2-3 in the same month must stay in that relative order.
    # Critically, "that relative order" flips with the primary direction: in
    # a newest-first list, row 2 (completed later that same day) is the more
    # recent one and belongs ABOVE row 1, so the tiebreaker must also run
    # descending — sorting it ascending regardless of direction put row 1
    # above row 2 even under "newest first", which reads backwards. is_(None)
    # sorts False (has a value) before True (NULL) so nulls always land last
    # regardless of direction.
    newest_first = sort in ("date_desc", "title_asc", "title_desc")
    if sort in ("title_asc", "title_desc"):
        title_col = func.coalesce(models.Game.display_name, models.Game.title).collate("NOCASE")
        completions_q = completions_q.order_by(
            title_col.asc() if sort == "title_asc" else title_col.desc(),
            models.Completion.completed_at.desc(),
            models.Completion.sort_order.is_(None),
            models.Completion.sort_order.desc() if newest_first else models.Completion.sort_order.asc(),
        )
    else:
        completions_q = completions_q.order_by(
            models.Completion.completed_at.desc() if sort == "date_desc" else models.Completion.completed_at.asc(),
            models.Completion.sort_order.is_(None),
            models.Completion.sort_order.desc() if newest_first else models.Completion.sort_order.asc(),
            models.Completion.id.desc(),
        )
    completions = completions_q.all()
    # Reuse the library fallback helper — it expects a list of UserLibraryEntry
    # objects, so pass each completion's library_entry. Dedupe on entry.id so
    # entries with multiple completions don't get processed twice.
    _attach_parent_fallbacks(db, list({c.library_entry.id: c.library_entry for c in completions}.values()))
    comp_platforms = (
        db.query(models.GameRelease.platform)
        .join(models.UserLibraryEntry, models.GameRelease.id == models.UserLibraryEntry.release_id)
        .join(models.Completion, models.UserLibraryEntry.id == models.Completion.library_entry_id)
        .filter(models.Completion.user_id == current_user.id)
        .distinct()
        .order_by(models.GameRelease.platform)
        .all()
    )
    comp_platform_list = [p[0] for p in comp_platforms]
    return templates.TemplateResponse(
        request=request,
        name="completions.html",
        context={
            "current_user": current_user,
            "completions": completions,
            "today": datetime.date.today().isoformat(),
            "q": q,
            "platform": platform,
            "completed_from": completed_from,
            "completed_to": completed_to,
            "all_time": all_time,
            "comp_platforms": comp_platform_list,
            "view_mode": view_mode,
            "sort": sort,
            **_base_ctx(db, current_user),
        },
    )


@router.get("/completions/games/search")
def search_completion_games(
    request: Request,
    q: str = Query("", min_length=0),
    include_hidden: str = Query(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    q = q.strip()
    if not q:
        return templates.TemplateResponse(
            request=request,
            name="partials/completion_game_results.html",
            context={"results": []},
        )
    results = (
        db.query(models.UserLibraryEntry)
        .join(models.UserLibraryEntry.release)
        .join(models.GameRelease.game)
        .options(contains_eager(models.UserLibraryEntry.release).contains_eager(models.GameRelease.game))
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            *([models.UserLibraryEntry.is_hidden == False] if not include_hidden else []),  # noqa: E712
            or_(
                models.Game.title.ilike(f"{q}%"),
                models.Game.title.ilike(f"% {q}%"),
                models.Game.display_name.ilike(f"{q}%"),
                models.Game.display_name.ilike(f"% {q}%"),
            ),
        )
        .order_by(models.Game.title)
        .limit(40)
        .all()
    )
    seen: set[tuple] = set()
    unique: list[models.UserLibraryEntry] = []
    for entry in results:
        key = (entry.release.game_id, entry.release.platform)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return templates.TemplateResponse(
        request=request,
        name="partials/completion_game_results.html",
        context={"results": unique[:20]},
    )


@router.post("/completions/log")
def log_completion(
    request: Request,
    library_entry_id: int = Form(...),
    completed_at: str = Form(...),
    playthroughs: str = Form("1"),
    notes: str = Form(""),
    completion_id: int | None = Form(None),
    view_mode: str = Form("list"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    if completion_id:
        completion = (
            db.query(models.Completion)
            .filter(
                models.Completion.id == completion_id,
                models.Completion.user_id == current_user.id,
            )
            .first()
        )
        if not completion:
            return templates.TemplateResponse(
                request=request,
                name="partials/completion_row.html",
                context={},
                status_code=404,
            )
        completion.library_entry_id = library_entry_id
        completion.completed_at = datetime.date.fromisoformat(completed_at)
        completion.playthroughs = playthroughs.strip() or None
        completion.notes = notes.strip() or None
        db.commit()
        db.refresh(completion)
        _vm = view_mode if view_mode in ("list", "grid_h", "grid_v") else "list"
        _tmpl = "partials/completion_card.html" if _vm in ("grid_h", "grid_v") else "partials/completion_row.html"
        response = templates.TemplateResponse(
            request=request,
            name=_tmpl,
            context={"completion": completion, "view_mode": _vm},
        )
        response.headers["HX-Retarget"] = f"#completion-{completion.id}"
        response.headers["HX-Reswap"] = "outerHTML"
        return response

    completion = models.Completion(
        user_id=current_user.id,
        library_entry_id=library_entry_id,
        completed_at=datetime.date.fromisoformat(completed_at),
        playthroughs=playthroughs.strip() or None,
        notes=notes.strip() or None,
    )
    db.add(completion)
    db.commit()
    db.refresh(completion)

    from . import steamgriddb as sgdb

    entry = completion.library_entry
    sgdb.auto_fetch_grid(db, current_user, entry, orientation="h")
    sgdb.auto_fetch_grid(db, current_user, entry, orientation="v")
    db.refresh(entry)

    view_mode = view_mode if view_mode in ("list", "grid_h", "grid_v") else "list"
    if view_mode in ("grid_h", "grid_v"):
        tmpl = "partials/completion_card.html"
    else:
        tmpl = "partials/completion_row.html"

    return templates.TemplateResponse(
        request=request,
        name=tmpl,
        context={"completion": completion, "view_mode": view_mode},
    )


@router.post("/completions/{completion_id}/increment")
def increment_completion(
    request: Request,
    completion_id: int,
    view_mode: str = Form("list"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Bump playthroughs by 1 on an existing completion. Returns the updated row/card."""
    completion = (
        db.query(models.Completion)
        .options(
            joinedload(models.Completion.library_entry).joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.Completion.library_entry)
            .joinedload(models.UserLibraryEntry.release)
            .selectinload(models.GameRelease.artwork),
            joinedload(models.Completion.library_entry).selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=completion_id, user_id=current_user.id)
        .first()
    )
    if not completion:
        return Response(status_code=404)
    try:
        current = int(str(completion.playthroughs or "1").rstrip("+").strip())
    except ValueError:
        current = 1
    completion.playthroughs = str(current + 1)
    db.commit()
    db.refresh(completion)
    _vm = view_mode if view_mode in ("list", "grid_h", "grid_v") else "list"
    tmpl = "partials/completion_card.html" if _vm in ("grid_h", "grid_v") else "partials/completion_row.html"
    return templates.TemplateResponse(
        request=request,
        name=tmpl,
        context={"completion": completion, "view_mode": _vm},
    )


@router.get("/completions/check-existing")
def check_existing_completion(
    library_entry_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Check if a library entry has a completion in the current calendar year.
    Returns the most recent one if found, so the modal can show a nudge."""
    year_start = datetime.date(datetime.date.today().year, 1, 1)
    existing = (
        db.query(models.Completion)
        .filter(
            models.Completion.user_id == current_user.id,
            models.Completion.library_entry_id == library_entry_id,
            models.Completion.completed_at >= year_start,
        )
        .order_by(models.Completion.completed_at.desc())
        .first()
    )
    if not existing:
        return JSONResponse({"exists": False})
    return JSONResponse(
        {
            "exists": True,
            "id": existing.id,
            "completed_at": existing.completed_at.isoformat(),
            "playthroughs": existing.playthroughs or "1",
            "notes": existing.notes or "",
        }
    )


@router.delete("/completions/{completion_id}")
def delete_completion(
    completion_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    completion = (
        db.query(models.Completion)
        .filter(
            models.Completion.id == completion_id,
            models.Completion.user_id == current_user.id,
        )
        .first()
    )
    if completion:
        db.delete(completion)
        db.commit()
    return Response(status_code=200)


@router.get("/completions/{completion_id}/detail")
def completion_detail(
    request: Request,
    completion_id: int,
    fresh_open: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Slide-out detail pane for a Completion. Mirrors the library detail pane
    but focused on the completion (date, playthroughs, notes) rather than the
    library entry, with a link to view the parent library entry's pane."""
    completion = (
        db.query(models.Completion)
        .options(
            joinedload(models.Completion.library_entry).joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.Completion.library_entry).joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            joinedload(models.Completion.library_entry).selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=completion_id, user_id=current_user.id)
        .first()
    )
    if not completion:
        return Response(status_code=404)

    entry = completion.library_entry
    release = entry.release
    game = release.game

    # All this user's completions for the same library entry, so the pane can
    # show "this is your 2nd of 3 logged completions" context.
    sibling_completions = (
        db.query(models.Completion)
        .filter_by(library_entry_id=entry.id, user_id=current_user.id)
        .order_by(models.Completion.completed_at.desc())
        .all()
    )

    visuals = _build_detail_pane_visuals(db, entry, game, release)
    appdetails = (release.raw_data or {}).get("appdetails") or {}

    return templates.TemplateResponse(
        request=request,
        name="partials/completion_detail.html",
        context={
            "completion": completion,
            "entry": entry,
            "release": release,
            "game": game,
            "appdetails": appdetails,
            "steam_meta": _extract_steam_meta(appdetails),
            "igdb_meta": _extract_igdb_meta(release),
            "sibling_completions": sibling_completions,
            "needs_refresh": _needs_metadata_refresh(release),
            "fresh_open": fresh_open,
            "current_user": current_user,
            **visuals,
        },
    )
