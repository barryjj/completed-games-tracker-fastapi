"""Completions routes: the completions list/grid page, its game search, logging a
completion, the +1 increment and existing-check helpers, deletion, and the
completion detail pane.

Moved verbatim out of `pages.py` — no behaviour changes.
"""

import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from . import models
from . import steamgriddb as sgdb
from .models import get_db
from .pages_common import (
    _attach_parent_fallbacks,
    _base_ctx,
    _build_detail_pane_visuals,
    _extract_igdb_meta,
    _extract_steam_meta,
    _needs_metadata_refresh,
    _resolve_view_mode,
    get_web_user,
    templates,
)

router = APIRouter()


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
