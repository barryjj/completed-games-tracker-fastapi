import asyncio
import logging
import os

import httpx as _httpx

from fastapi import APIRouter, Depends, Form, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import jobs, models, steam, worker_state
from .models import SessionLocal, get_db
from .pages import get_web_user

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations")

TEMPLATES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _steam_counts(db: Session, user: models.User) -> dict | None:
    """Return {'games': N, 'dlc': N, 'total': N} for the user's Steam library, or None
    if Steam isn't connected."""
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


@router.get("")
def integrations_hub(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    return templates.TemplateResponse(
        request=request,
        name="integrations.html",
        context={
            "current_user": current_user,
            "steam_counts": _steam_counts(db, current_user),
        },
    )


@router.get("/steam")
def steam_page(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    return templates.TemplateResponse(
        request=request,
        name="integrations_steam.html",
        context={"current_user": current_user},
    )


@router.post("/steam/credentials")
def save_steam_credentials(
    request: Request,
    steam_id64: str = Form(""),
    steam_api_key: str = Form(""),
    steam_session_id: str = Form(""),
    steam_login_secure: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    current_user.steam_id64 = steam_id64.strip() or None
    current_user.steam_api_key = steam_api_key.strip() or None
    current_user.steam_session_id = steam_session_id.strip() or None
    current_user.steam_login_secure = steam_login_secure.strip() or None
    db.commit()
    # HX-Refresh reloads the page so the sync button appears/disappears correctly
    response = templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": "Steam credentials saved."},
    )
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/steam/test-cookies")
def test_steam_cookies(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    if not current_user.steam_session_id or not current_user.steam_login_secure:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": "No cookies saved — enter both sessionid and steamLoginSecure above and save first."},
        )
    try:
        resp = _httpx.get(
            "https://store.steampowered.com/dynamicstore/userdata/",
            cookies={
                "sessionid": current_user.steam_session_id,
                "steamLoginSecure": current_user.steam_login_secure,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        owned = data.get("rgOwnedApps", [])
        if not owned:
            return templates.TemplateResponse(
                request=request,
                name="partials/integrations_flash.html",
                context={"error": "Cookies appear invalid or expired — rgOwnedApps was empty. Try copying fresh values from your browser."},
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"message": f"Cookies valid — {len(owned):,} owned apps visible (games + DLC)."},
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": f"Request failed: {e}"},
        )


def _format_full_sync_result(result: dict) -> str:
    return (
        f"Sync complete — "
        f"{result['games_added']} games added, {result['games_updated']} updated "
        f"({result['games_total']} total) · "
        f"{result['dlc_added']} DLC added, {result['dlc_marked']} marked "
        f"({result['dlc_total']} total DLC owned)."
    )


def _format_games_sync_result(result: dict) -> str:
    return (
        f"Games sync complete — {result['added']} added, "
        f"{result['updated']} updated ({result['total']} total)."
    )


async def _run_sync_job(job_id: str, user_id: int, kind: str) -> None:
    """
    Background runner for a Steam sync job.
    - Creates a fresh DB session inside the task (can't borrow the request's).
    - Pauses the enrichment worker for the duration so we don't compete on
      Steam's rate limits.
    - Marks the job done/failed; the /jobs/poll endpoint picks up the result
      and surfaces a toast to the user (even if they've navigated away).
    """
    jobs.update(job_id, status=jobs.JobStatus.RUNNING)
    worker_state.enrichment_paused = True
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user is None:
            jobs.mark_failed(job_id, "User no longer exists.")
            return

        if kind == "steam_sync_full":
            result = await asyncio.to_thread(steam.sync_full_library, db, user)
            jobs.mark_done(job_id, _format_full_sync_result(result))
        elif kind == "steam_sync_games":
            result = await asyncio.to_thread(steam.sync_steam_library, db, user)
            jobs.mark_done(job_id, _format_games_sync_result(result))
        else:
            jobs.mark_failed(job_id, f"Unknown sync kind: {kind}")
    except ValueError as e:
        # Validation errors (missing credentials etc.) — show the literal message
        jobs.mark_failed(job_id, str(e))
    except Exception as e:
        _logger.exception("Sync job %s failed", job_id)
        jobs.mark_failed(job_id, f"Sync failed: {e}")
    finally:
        worker_state.enrichment_paused = False
        db.close()


def _kick_off_sync(request: Request, current_user: models.User, kind: str, started_message: str):
    """Create a job, schedule the background task, return a 'started' toast."""
    active = jobs.active_jobs_for(current_user.id)
    if active:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": "A sync is already running — please wait for it to finish."},
            status_code=409,
        )
    job = jobs.create(user_id=current_user.id, kind=kind)
    asyncio.create_task(_run_sync_job(job.id, current_user.id, kind))
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": started_message},
    )


def _credential_error(current_user: models.User, kind: str) -> str | None:
    """Pre-flight credential check so we 422 immediately instead of queueing a
    doomed background job."""
    if not current_user.steam_api_key or not current_user.steam_id64:
        return "Steam API key and Steam ID64 are required."
    if kind == "steam_sync_full":
        if not current_user.steam_session_id or not current_user.steam_login_secure:
            return "Browser cookies (sessionid + steamLoginSecure) are required for full sync."
    return None


@router.post("/steam/sync-all")
async def sync_steam_all(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    """Full sync (games + DLC). Runs in background; result is delivered via /jobs/poll.
    Must be async so we have access to the running event loop for asyncio.create_task."""
    err = _credential_error(current_user, "steam_sync_full")
    if err:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": err},
            status_code=422,
        )
    return _kick_off_sync(
        request, current_user, "steam_sync_full",
        "Steam sync started — feel free to navigate away. You'll see a toast when it finishes.",
    )


@router.post("/steam/sync")
async def sync_steam(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    """Games-only sync. Runs in background; result is delivered via /jobs/poll."""
    err = _credential_error(current_user, "steam_sync_games")
    if err:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": err},
            status_code=422,
        )
    return _kick_off_sync(
        request, current_user, "steam_sync_games",
        "Steam games sync started — feel free to navigate away. You'll see a toast when it finishes.",
    )


@router.get("/jobs/poll")
def jobs_poll(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    """
    Polled by every authenticated page (see base.html). Returns a fresh poller
    element plus OOB toasts for any of this user's jobs that have completed
    since the last poll. Idempotent — once a job is reported, it won't be
    reported again.
    """
    pending = jobs.pending_notifications_for(current_user.id)
    return templates.TemplateResponse(
        request=request,
        name="partials/job_poller.html",
        context={"completed_jobs": pending},
    )


@router.post("/steam/backfill-collection-flags")
def backfill_collection_flags(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    games = (
        db.query(models.Game)
        .join(models.GameRelease)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.Game.is_collection == False,
        )
        .all()
    )
    updated = 0
    for game in games:
        if steam._infer_is_collection(game.title):
            game.is_collection = True
            updated += 1
    db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": f"Backfill complete — {updated} game{'s' if updated != 1 else ''} flagged as collections."},
    )


@router.post("/steam/sync-dlc")
def steam_sync_dlc(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    try:
        result = steam.sync_dlc_flags(db, current_user)
    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": str(e)},
        )
    msg = (
        f"Checked {result['checked']} apps — "
        f"found {result['found_dlc']} DLC, "
        f"linked {result['linked']} to base games."
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": msg},
    )


@router.get("/steam/enrichment-status")
def steam_enrichment_status(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    pending = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.source == "steam",
            models.GameRelease.metadata_fetched_at == None,
        )
        .count()
    )
    total = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.source == "steam",
        )
        .count()
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/enrichment_status.html",
        context={"pending": pending, "total": total},
    )


@router.post("/steam/enrichment-refresh")
def steam_enrichment_refresh(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """
    Re-queue all Steam entries for metadata enrichment by nulling metadata_fetched_at.
    raw_data["appdetails"] is untouched — existing metadata stays visible until
    the background worker overwrites each entry with fresh data.
    """
    release_ids = [
        row[0]
        for row in db.query(models.GameRelease.id)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.source == "steam",
        )
        .all()
    ]
    updated = (
        db.query(models.GameRelease)
        .filter(models.GameRelease.id.in_(release_ids))
        .update({"metadata_fetched_at": None}, synchronize_session=False)
    )
    db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": f"Queued {updated} entries for metadata refresh — existing data stays in place until updated."},
    )


@router.post("/steam/backfill-display-names")
def backfill_steam_display_names(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    games = (
        db.query(models.Game)
        .join(models.GameRelease)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.source == "steam",
            models.Game.display_name == None,
        )
        .all()
    )
    updated = 0
    for game in games:
        cleaned = steam._clean_title(game.title)
        if cleaned != game.title:
            game.display_name = cleaned
            updated += 1
    db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": f"Backfill complete — {updated} game{'s' if updated != 1 else ''} updated."},
    )
