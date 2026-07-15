"""Historical-import routes: spreadsheet upload, the async import job and its
progress/status polling, the review queue (tabs, filters, card/list views), and
per-candidate actions (preview, edit, link, confirm, dismiss, reopen, recheck).

Moved verbatim out of `pages.py` — no behaviour changes. Parsing/matching logic
lives in `importer.py`; this module is the HTTP surface over it. The import-count
badge helpers (_import_tab_counts / _import_confirmed_count) moved to
pages_common because the home and library pages render them too.
"""

import asyncio
import datetime
from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from . import importer, jobs, models
from . import steamgriddb as sgdb
from .models import get_db
from .pages_common import (
    _IMPORT_TABS,
    _base_ctx,
    _build_detail_pane_visuals,
    _extract_igdb_meta,
    _extract_steam_meta,
    _get_all_platforms,
    _import_confirmed_count,
    _import_tab_counts,
    get_web_user,
    logger,
    templates,
)

router = APIRouter()


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

    # create_new / needs_review candidates are confirmed via the in-place
    # add-game modal (POST /library/games with import_candidate_id), not this
    # endpoint — reaching here would be an unexpected call.
    return Response(status_code=400)


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
