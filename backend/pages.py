"""Page routes.

Shared infrastructure (templates, filters, context/visual helpers) lives in
`pages_common`; match-review routes live in `pages_match_review`; historical-import
routes live in `pages_import`; completions routes live in `pages_completions`.
"""

import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload, selectinload

from . import models, users
from .models import get_db
from .pages_common import (
    VIEW_MODES,
    _attach_parent_fallbacks,
    _base_ctx,
    _build_detail_pane_visuals,
    _extract_igdb_meta,
    _extract_steam_meta,
    _get_all_platforms,
    _import_confirmed_count,
    _import_tab_counts,
    _needs_metadata_refresh,
    _resolve_view_mode,
    get_web_user,
    logger,
    templates,
)

router = APIRouter()


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
