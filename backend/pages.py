"""Page routes.

Shared infrastructure (templates, filters, context/visual helpers) lives in
`pages_common`; match-review routes live in `pages_match_review`; historical-import
routes live in `pages_import`; completions routes live in `pages_completions`;
account/settings routes live in `pages_account`; library routes live in
`pages_library`. What remains here is auth, the home/tools dashboards, and the
per-entry logo-position routes.
"""

import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from . import models, users
from . import psn as _psn
from .models import get_db
from .pages_common import (
    _base_ctx,
    _build_lib_query,
    _import_tab_counts,
    get_web_user,
    templates,
)

router = APIRouter()


# --- Auth ---

# Explicit expiry so the session survives WebView restarts: the Tauri (WKWebView)
# shell discards no-expiry "session cookies" on every app quit — browsers restore
# them, which is why the missing max_age never showed up there.
_SESSION_MAX_AGE = 180 * 24 * 60 * 60  # 180 days


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
    response.set_cookie("session", u.api_token, httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE)
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
    response.set_cookie("session", user.api_token, httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# --- Account ---


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


def _psn_counts(db: Session, user: models.User) -> dict | None:
    """Return {'games': N} for the user's PSN library, or None if PSN isn't set
    up. Used by the Tools page's PSN sync card."""
    if not user.psn_npsso:
        return None
    total = (
        db.query(func.count(models.UserLibraryEntry.id))
        .join(models.GameRelease, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.source == "psn",
        )
        .scalar()
    )
    return {"games": total or 0}


def _psn_pending(db: Session, user: models.User) -> int:
    """Games waiting on a PSN decision — both queues, since they're two tabs of
    one page and a card showing only half the work is a card that lies."""
    if not user.psn_npsso:
        return 0
    # Counting by BUILDING every row cost 4.6s on a 567-row queue and was the
    # whole of the Tools page's load time — every other helper on that page
    # totals under 70ms.
    cross = _psn.count_pending_review_rows(db, user.id)
    played = len([r for r in _psn.played_only_rows(db, user.id) if not r["decision"]])
    return cross + played


# The tiers PSN defines, in the order the legend reads.
_TROPHY_TIERS = ("platinum", "gold", "silver", "bronze")
_ACHIEVEMENT_SOURCES = (("steam", "Steam"), ("psn", "PlayStation"))


def _psn_set_totals(db: Session, user_id: int) -> dict | None:
    """PlayStation figures from Sony's per-set counts, which the crawl puts
    on every set it sees -- library entries and the review queue alike --
    rather than from per-trophy rows, which exist only for confirmed
    entries. Most of a PSN library sits in review, so counting rows read
    727 earned against a profile that says 11,200. One set can appear on a
    release and a candidate (or two cross-buy releases); the NPWR id is
    the identity."""
    seen: dict[str, dict] = {}
    releases = (
        db.query(models.GameRelease.raw_data)
        .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(
            models.UserLibraryEntry.user_id == user_id,
            or_(
                models.GameRelease.source == "psn",
                # A game played on its Steam copy still earns PSN trophies, and
                # the set rides on the Steam release (psn.PC_SET_KEY).
                func.json_extract(models.GameRelease.raw_data, f"$.{_psn.PC_SET_KEY}").isnot(None),
            ),
        )
        .all()
    )
    candidates = db.query(models.PsnReviewCandidate.raw_data).filter_by(user_id=user_id, status="pending").all()
    for (raw,) in [*releases, *candidates]:
        item = _psn.trophy_item_in(raw)
        if item is None or not isinstance(item.get("earnedTrophies"), dict):
            continue
        seen.setdefault(item["npCommunicationId"], item)
    if not seen:
        return None
    tiers = {t: {"earned": 0, "total": 0} for t in _TROPHY_TIERS}
    perfect = 0
    for raw in seen.values():
        for t in _TROPHY_TIERS:
            tiers[t]["earned"] += int((raw.get("earnedTrophies") or {}).get(t) or 0)
            tiers[t]["total"] += int((raw.get("trophies") or {}).get(t) or 0)
        perfect += int(raw.get("trophyProgress") or 0) >= 100
    return {
        "earned": sum(t["earned"] for t in tiers.values()),
        "total": sum(t["total"] for t in tiers.values()),
        "perfect": perfect,
        "sets": len(seen),
        "platinums": tiers["platinum"]["earned"],
        "tiers": [{"tier": t, **tiers[t]} for t in _TROPHY_TIERS if tiers[t]["total"]],
    }


def _achievement_summary(db: Session, user_id: int) -> dict | None:
    """What the achievement passes have on file, for the Home widget (#136).

    None until something has been fetched -- the widget is absent rather
    than a card of zeros for a user with no Steam or PSN sync. Steam comes
    from the per-achievement rows (every played game has them); PlayStation
    from Sony's per-set counts (see _psn_set_totals). Sets at 100% and
    platinums are the completion-shaped figures until the detail pane
    learns to show a set; recent unlocks are rows from either source.
    """
    ua, ad, ule = models.UserAchievement, models.AchievementDefinition, models.UserLibraryEntry
    earned_n = func.sum(case((ua.earned == True, 1), else_=0))  # noqa: E712
    base = db.query(ua).join(ad, ua.definition_id == ad.id).join(ule, ua.library_entry_id == ule.id).filter(ule.user_id == user_id)
    steam_rows = base.filter(ad.source == "steam")
    earned, total = steam_rows.with_entities(earned_n, func.count(ua.id)).one()
    steam = None
    if total:
        per_entry = steam_rows.with_entities(ua.library_entry_id, earned_n, func.count(ua.id)).group_by(ua.library_entry_id).all()
        steam = {
            "earned": int(earned or 0),
            "total": int(total),
            "perfect": sum(1 for _, e, t in per_entry if t and int(e or 0) == int(t)),
            "sets": len(per_entry),
        }
    psn = _psn_set_totals(db, user_id)
    if steam is None and psn is None:
        return None
    recent = (
        base.filter(ua.earned == True, ua.earned_at.isnot(None))  # noqa: E712
        .options(
            joinedload(ua.definition),
            joinedload(ua.library_entry).joinedload(ule.release).joinedload(models.GameRelease.game),
        )
        .order_by(ua.earned_at.desc(), ua.id.desc())
        .limit(8)
        .all()
    )
    sources = [(label, side) for (key, label), side in zip(_ACHIEVEMENT_SOURCES, (steam, psn), strict=True) if side]
    return {
        "earned": sum(side["earned"] for _, side in sources),
        "total": sum(side["total"] for _, side in sources),
        "perfect": sum(side["perfect"] for _, side in sources),
        "tracked": sum(side["sets"] for _, side in sources),
        "platinums": psn["platinums"] if psn else 0,
        "sources": [
            {"label": label, "css": models._platform_heuristic_css(label), "earned": side["earned"], "total": side["total"]}
            for label, side in sources
        ],
        "tiers": psn["tiers"] if psn else [],
        "recent": recent,
    }


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
        # Twelve: the footer button is gone AND every card is forced to the
        # tallest one's height (grid-auto-rows: 1fr), so a short list here is
        # dead space rather than a tidy card.
        .limit(12)
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
    # Derived from what's already loaded — no extra queries. Fills the space the
    # "Open completions" button used to occupy.
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
            "psn_review_pending": _psn_pending(db, current_user),
            "missing_covers": _build_lib_query(db, current_user, "", "", "default", "name", False, True, "grid_v")[0].count(),
            "achievements": _achievement_summary(db, current_user.id),
            **_base_ctx(db, current_user, request),
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
            "psn_counts": _psn_counts(db, current_user),
            "psn_review_pending": _psn_pending(db, current_user),
            # The same numbers the PSN configure page's Review card shows, so
            # the two tiles say the same thing.
            "psn_review": _psn.page_counts(db, current_user) if current_user.psn_npsso else None,
            "import_counts": import_counts,
            "import_pending": sum(import_counts.values()),
            "missing_covers": missing_q.count(),
            **_base_ctx(db, current_user, request),
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
