import datetime
import os

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

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
    when neither the user's override nor the release's artwork has a matching-
    orientation cover — no cross-orientation borrowing (stretched/squished
    art looks worse than a clean placeholder card).

    Resolution order:
      1. User override (cover_url_override_v / cover_url_override_h) — explicit
         choice from SGDB lookup, manual upload, etc.
      2. Release's GameArtwork — Steam CDN URLs populated at sync / enrichment.

    Vertical mode wants library_600x900.jpg (Steam's portrait library art).
    Horizontal mode wants header.jpg (Steam's landscape header)."""
    if orientation == "grid_v":
        if entry.cover_url_override_v:
            return entry.cover_url_override_v
        wanted = "cover"
    else:
        if entry.cover_url_override_h:
            return entry.cover_url_override_h
        wanted = "header"
    for art in entry.release.artwork:
        if art.artwork_type == wanted:
            return art.url
    return None


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


_STEAM_CDN_BASE = "https://cdn.akamai.steamstatic.com/steam/apps"


def _build_detail_pane_visuals(db: Session, entry, game, release) -> dict:
    """Compute the visual chrome (hero, logo, header, parent info) for a
    library detail pane render. Centralized so both library and completion
    detail endpoints use the same logic.

    Sources, in priority order:
      hero  = own hero artwork → parent's hero artwork
      logo  = own logo (constructed Steam URL) → parent's logo
      header = cover_url_override_h → own header artwork → parent's header
              (kept as a separate fallback so list-row / detail-pane code
              that wants the 460x215 image specifically still works)

    For DLC, the parent's hero/logo are the right default since Steam rarely
    issues distinct hero/logo for DLC appids — they share the parent's
    library identity.
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

    def _art_url(rel, art_type):
        if not rel:
            return None
        for art in rel.artwork:
            if art.artwork_type == art_type:
                return art.url
        return None

    def _steam_logo_url(rel):
        # Logo isn't captured during enrichment — construct it from the appid.
        # 404s for many entries, but the cover-fallback chain handles that
        # client-side by hiding the img.
        if not rel or rel.source != "steam" or not rel.external_id:
            return None
        return f"{_STEAM_CDN_BASE}/{rel.external_id}/logo.png"

    # Header (460x215). cover_url_override_h takes priority because the SGDB
    # picker writes to it; covers/replaces the Steam header.
    header_url = entry.cover_url_override_h or _art_url(release, "header")
    fallback_header_url = _art_url(parent_release, "header")

    # Hero (~1920x620). No override field yet — TODO when we add SGDB hero
    # picker variant.
    hero_url = _art_url(release, "hero")
    fallback_hero_url = _art_url(parent_release, "hero")

    # Logo (~600x400, transparent). Constructed URL; cover-fallback hides on 404.
    logo_url = _steam_logo_url(release)
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
    }


def _attach_parent_fallbacks(db: Session, entries) -> None:
    """For each entry whose game has a parent (DLC -> base game), look up the
    parent's Steam artwork and stamp `_fallback_v` / `_fallback_h` URLs onto
    the entry as transient attributes. Templates read these and emit them as
    `data-fallback` so cgtCoverFallback() can degrade to parent art when the
    DLC's own cover/header 404s.

    One batched query for all parents in the page — avoids N+1 across long
    lists. Entries without a parent get None on both attributes."""
    parent_ids = {e.release.game.parent_id for e in entries if e.release.game.parent_id}
    parent_art: dict[int, dict[str, str | None]] = {}
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
            d: dict[str, str | None] = {"cover": None, "header": None}
            for art in r.artwork:
                if art.artwork_type in d and not d[art.artwork_type]:
                    d[art.artwork_type] = art.url
            parent_art[r.game_id] = d

    for e in entries:
        parent_id = e.release.game.parent_id
        p = parent_art.get(parent_id) if parent_id else None
        e._fallback_v = (p or {}).get("cover")
        e._fallback_h = (p or {}).get("header")


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
    view_mode: str | None = Query(None),
    show_hidden: bool = Query(False),
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
        )
        .filter(models.UserLibraryEntry.user_id == current_user.id)
        # Sort on what's actually shown (display_name with title as fallback),
        # case-insensitive so "Apple"/"apple" cluster together. Sorting by raw
        # title caused rows like "Influent DLC…" to land before number-prefixed
        # games because of how ALL CAPS / leading-symbol titles compare to
        # cleaned display names.
        .order_by(func.coalesce(models.Game.display_name, models.Game.title).collate("NOCASE"))
    )
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
        # Games + collections; hide DLC and sub-collection games.
        # Manually added entries always show regardless of parent status.
        base_q = base_q.filter(
            or_(
                models.Game.parent_id == None,
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
    total = base_q.count()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    entries = base_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    # Attach parent-game artwork fallbacks to each DLC entry. The list/grid
    # cover img uses these via data-fallback + cgtCoverFallback() so a DLC
    # whose own header/cover URL 404s degrades to the base game's art instead
    # of vanishing. Done as a single batched query (vs. per-row joinedload) to
    # avoid N+1 across long pages.
    _attach_parent_fallbacks(db, entries)

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
    if show_hidden:
        filter_parts.append("show_hidden=true")
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
            "view_mode": view_mode,
            "show_hidden": show_hidden,
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
        context={"entries": entries, "q": q},
    )


@router.get("/library/entries/{entry_id}/detail")
def library_entry_detail(
    request: Request,
    entry_id: int,
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
            "child_entries": child_entries,
            "completions": sorted(entry.completions, key=lambda c: c.completed_at, reverse=True),
            "current_user": current_user,
            "needs_refresh": needs_refresh,
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
                game.is_dlc = False

        if app_type == "dlc" and game.parent_id is None and not game.parent_id_user_set:
            fullgame = details.get("fullgame", {})
            parent_appid = str(fullgame.get("appid", "")).strip()
            if parent_appid:
                parent_release = db.query(models.GameRelease).filter_by(source="steam", external_id=parent_appid).first()
                if parent_release:
                    game.parent_id = parent_release.game_id

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


@router.post("/library/entries/{entry_id}/cover-override")
def set_cover_override(
    request: Request,
    entry_id: int,
    orientation: str = Form(...),
    url: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Apply a custom cover override URL (typically a SteamGridDB pick) to a
    library entry. orientation is "v" (600x900) or "h" (460x215)."""
    if orientation not in ("v", "h"):
        return Response(status_code=400)
    url = url.strip()
    if not url:
        return Response(status_code=400)
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if not entry:
        return Response(status_code=404)
    if orientation == "v":
        entry.cover_url_override_v = url
        msg = "Custom vertical cover applied."
    else:
        entry.cover_url_override_h = url
        msg = "Custom horizontal cover applied."
    db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": msg},
    )


@router.post("/library/entries/{entry_id}/clear-cover-override")
def clear_cover_override(
    request: Request,
    entry_id: int,
    orientation: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Clear a custom cover override on a library entry. The pane / grid then
    falls back to whatever the release's GameArtwork has (Steam CDN art).
    orientation is "v" (vertical / library card) or "h" (horizontal / header)."""
    if orientation not in ("v", "h"):
        return Response(status_code=400)
    entry = db.query(models.UserLibraryEntry).filter_by(id=entry_id, user_id=current_user.id).first()
    if not entry:
        return Response(status_code=404)
    if orientation == "v":
        entry.cover_url_override_v = None
        msg = "Custom vertical cover cleared."
    else:
        entry.cover_url_override_h = None
        msg = "Custom horizontal cover cleared."
    db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": msg},
    )


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
            "sibling_completions": sibling_completions,
            "needs_refresh": _needs_metadata_refresh(release),
            **visuals,
        },
    )
