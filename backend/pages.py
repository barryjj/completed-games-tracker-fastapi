import datetime
import html as _html
import os

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload, selectinload

from . import models, users
from .models import get_db

router = APIRouter()

TEMPLATES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _platform_color_class(platform: str) -> str:
    p = platform.lower()
    if "steam" in p:
        return "tag-platform-steam"
    if "ps" in p or "playstation" in p or "psn" in p:
        return "tag-platform-playstation"
    if "switch" in p or "nintendo" in p:
        return "tag-platform-nintendo"
    if "xbox" in p:
        return "tag-platform-xbox"
    if "ios" in p or "mac" in p or "apple" in p or "iphone" in p or "ipad" in p:
        return "tag-platform-apple"
    if "pc" in p or "windows" in p:
        return "tag-platform-pc"
    return "tag-platform-other"


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
    Horizontal mode wants cover_h (header.jpg / landscape header)."""
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
        """Best valid GameArtwork URL for a release. Native sources preferred."""
        if not rel:
            return None
        native = None
        sgdb = None
        for art in rel.artwork:
            if art.artwork_type == art_type and art.is_valid and art.url:
                if art.source in ("steam", "psn"):
                    if native is None:
                        native = art.url
                elif sgdb is None:
                    sgdb = art.url
        return native or sgdb

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


PLATFORMS = ["Steam", "PS5", "PS4", "PS3", "Switch", "Xbox", "iOS", "Android", "Other"]

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
    response = RedirectResponse("/library", status_code=302)
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
    response = RedirectResponse("/library", status_code=302)
    response.set_cookie("session", user.api_token, httponly=True, samesite="lax")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# --- Account ---


@router.get("/account")
def account_page(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={"current_user": current_user},
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


PAGE_SIZE = 100

# --- Library ---

VIEW_OPTIONS = ["default", "dlc", "collections", "in_collection", "manual", "all"]
SORT_OPTIONS = ["name", "recently_played"]


VIEW_MODES = {"list", "grid_v", "grid_h"}


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

    base_q = (
        db.query(models.UserLibraryEntry)
        .join(models.UserLibraryEntry.release)
        .join(models.GameRelease.game)
        .options(
            # Both branches start with contains_eager(release) — same strategy
            # on the shared path prefix, then diverge. Adding a separate
            # joinedload(release) would conflict.
            contains_eager(models.UserLibraryEntry.release).contains_eager(models.GameRelease.game),
            contains_eager(models.UserLibraryEntry.release).selectinload(models.GameRelease.artwork),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter(models.UserLibraryEntry.user_id == current_user.id)
        # Default order — overridden below when sort != "name".
        .order_by(func.coalesce(models.Game.display_name, models.Game.title).collate("NOCASE"))
    )

    # Normalise sort param
    if sort not in SORT_OPTIONS:
        sort = "name"
    if sort == "recently_played":
        # NULL last so entries with no play date (manual, un-launched games)
        # sink to the bottom rather than surfacing at the top.
        base_q = base_q.order_by(None).order_by(models.UserLibraryEntry.last_played_at.desc().nulls_last())
    # Hidden entries (soundtracks, artbooks, etc.) are excluded by default.
    # The "Show hidden" toggle flips this off so the user can review or unhide.
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
        base_q = base_q.filter(models.GameRelease.platform == platform)

    # Normalise view param — unknown values fall back to default
    if view not in VIEW_OPTIONS:
        view = "default"

    if view == "default":
        # Show all non-DLC entries. Games that are part of a collection have a
        # parent_id set but are still completable games — hiding them from
        # Default just because they're organised into a collection is wrong.
        # The manual-entry exception keeps manually added DLC visible (those
        # were added intentionally and the user knows what they are).
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
    # "all" — no additional filter

    # Missing-artwork filter — orientation-aware.
    # grid_v → needs cover_v; grid_h / list → needs cover_h.
    # An entry "has art" if it has a UserArtwork pick OR a valid GameArtwork row.
    if missing_art:
        art_type = "cover_v" if view_mode == "grid_v" else "cover_h"
        # Entry IDs that already have a UserArtwork pick for this type
        has_user_art_ids = (
            db.query(models.UserArtwork.entry_id)
            .filter(
                models.UserArtwork.artwork_type == art_type,
                models.UserArtwork.entry_id.isnot(None),
                models.UserArtwork.url.isnot(None),
            )
            .scalar_subquery()
        )
        # Release IDs that have a valid GameArtwork row of this type
        has_art_release_ids = (
            db.query(models.GameArtwork.release_id)
            .filter(models.GameArtwork.artwork_type == art_type, models.GameArtwork.is_valid.is_(True))
            .scalar_subquery()
        )
        base_q = base_q.filter(
            models.UserLibraryEntry.id.not_in(has_user_art_ids),
            models.GameRelease.id.not_in(has_art_release_ids),
        )

    total = base_q.count()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    entries = base_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    # Attach parent-game artwork fallbacks to each DLC entry. The list/grid
    # cover img uses these via data-fallback + cgtCoverFallback() so a DLC
    # whose own header/cover URL 404s degrades to the base game's art instead
    # of vanishing. Done as a single batched query (vs. per-row joinedload) to
    # avoid N+1 across long pages.
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
        lib_platforms = (
            db.query(models.GameRelease.platform)
            .join(models.UserLibraryEntry)
            .filter(models.UserLibraryEntry.user_id == current_user.id)
            .distinct()
            .order_by(models.GameRelease.platform)
            .all()
        )
        lib_platform_list = [p[0] for p in lib_platforms]

    # Build filter_qs for pagination links (preserves active filters)
    filter_parts = []
    if q:
        filter_parts.append(f"q={q}")
    if platform:
        filter_parts.append(f"platform={platform}")
    if view != "default":
        filter_parts.append(f"view={view}")
    if sort != "name":
        filter_parts.append(f"sort={sort}")
    if show_hidden:
        filter_parts.append("show_hidden=true")
    if missing_art:
        filter_parts.append("missing_art=true")
    if view_mode != "list":
        filter_parts.append(f"view_mode={view_mode}")
    filter_qs = ("&" + "&".join(filter_parts)) if filter_parts else ""

    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "current_user": current_user,
            "entries": entries,
            "collections": collections,
            "base_game_options": base_game_options,
            "platforms": PLATFORMS,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "q": q,
            "platform": platform,
            "view": view,
            "sort": sort,
            "view_mode": view_mode,
            "show_hidden": show_hidden,
            "missing_art": missing_art,
            "filter_qs": filter_qs,
            "lib_platforms": lib_platform_list,
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

    game = models.Game(
        title=title_clean,
        display_name=display_clean,
        is_dlc=is_dlc,
        is_collection=is_collection,
        parent_id=parent_id,
        # Manual entries are inherently user-set on every field we collect.
        display_name_user_set=True,
        is_dlc_user_set=True,
        is_collection_user_set=True,
        parent_id_user_set=True,
    )
    db.add(game)
    db.flush()

    release = models.GameRelease(game_id=game.id, platform=platform, source="manual")
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

    db.commit()
    db.refresh(entry)
    return templates.TemplateResponse(
        request=request,
        name="partials/library_row.html",
        context={"entry": entry},
    )


@router.delete("/library/entries/{entry_id}")
def delete_library_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if entry:
        db.delete(entry)
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
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Search user's library games by title, optionally filtered by type."""
    query = (
        db.query(models.UserLibraryEntry)
        .join(models.GameRelease)
        .join(models.Game)
        .filter(models.UserLibraryEntry.user_id == current_user.id)
    )

    if q.strip():
        query = query.filter(models.Game.title.ilike(f"%{q}%"))

    if is_dlc is not None:
        query = query.filter(models.Game.is_dlc == is_dlc)

    if is_collection is not None:
        query = query.filter(models.Game.is_collection == is_collection)

    entries = query.order_by(models.Game.title).limit(15).all()

    return templates.TemplateResponse(
        request=request,
        name="partials/library_game_results.html",
        context={"entries": entries, "q": q, "callback": callback},
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
            .options(joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game))
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


# --- Completions ---


@router.get("/completions")
def completions_page(
    request: Request,
    q: str = Query(""),
    platform: str = Query(""),
    completed_from: str = Query(""),
    completed_to: str = Query(""),
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    # Resolved from query → cookie → default (see _resolve_view_mode docstring).
    view_mode = _resolve_view_mode(request, view_mode, "cgt-completions-view-mode")
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
    completions = completions_q.order_by(models.Completion.id.desc()).all()
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
            "comp_platforms": comp_platform_list,
            "view_mode": view_mode,
        },
    )


@router.get("/completions/games/search")
def search_completion_games(
    request: Request,
    q: str = Query("", min_length=0),
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
        response = templates.TemplateResponse(
            request=request,
            name="partials/completion_row.html",
            context={"completion": completion},
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

    return templates.TemplateResponse(
        request=request,
        name="partials/completion_row.html",
        context={"completion": completion},
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
            "sibling_completions": sibling_completions,
            "needs_refresh": _needs_metadata_refresh(release),
            "current_user": current_user,
            **visuals,
        },
    )
