import asyncio
import logging
import os
import re
from urllib.parse import urlencode

import httpx as _httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from . import jobs, models, steam, worker_state
from . import steamgriddb as sgdb
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
    openid: str = "",
    current_user: models.User = Depends(get_web_user),
):
    # ?openid=ok|bad_claim|verify_failed|invalid_sig — set by the OpenID return
    # handler so the page can show a result toast/flash on the next render.
    return templates.TemplateResponse(
        request=request,
        name="integrations_steam.html",
        context={"current_user": current_user, "openid_status": openid},
    )


@router.post("/steam/credentials")
def save_steam_credentials(
    request: Request,
    steam_api_key: str = Form(""),
    steam_session_id: str = Form(""),
    steam_login_secure: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    # SteamID is owned by the OpenID flow — not exposed as a form field. The
    # "Clear Credentials" button below only clears the things you'd want to
    # rotate (API key + cookies); to forget your Steam sign-in entirely, use
    # the "Forget Steam sign-in" link.
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


# ─── Steam OpenID ("Sign in through Steam") ───────────────────────────────
# Steam still uses OpenID 2.0, not OAuth 2. The flow:
#   1. Redirect user to https://steamcommunity.com/openid/login?... with our
#      callback URL in openid.return_to.
#   2. User signs in on Steam's site.
#   3. Steam redirects back to our callback with signed params.
#   4. We POST those params back to Steam with mode=check_authentication to
#      verify the signature.
#   5. Parse SteamID from claimed_id (URL of the form .../openid/id/<steamid64>).
# What this DOES: gets the user's verified SteamID + persona name, no paste.
# What it DOES NOT: capture session cookies (would need Tauri) or issue an
# API key (Steam doesn't via OpenID). API key + cookies stay manual for now.

_STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
_OPENID_NS = "http://specs.openid.net/auth/2.0"
_OPENID_IDENTIFIER = "http://specs.openid.net/auth/2.0/identifier_select"
_CLAIMED_ID_RE = re.compile(r"https?://steamcommunity\.com/openid/id/(\d+)/?$")


def _openid_return_url(request: Request) -> str:
    """Absolute URL of our callback endpoint — Steam requires it to match
    exactly between the redirect and the verification round-trip."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/integrations/steam/openid/return"


def _openid_realm(request: Request) -> str:
    """OpenID realm — the scope of URLs the auth applies to. Use the app root."""
    return str(request.base_url).rstrip("/") + "/"


@router.get("/steam/openid/start")
def steam_openid_start(request: Request, current_user: models.User = Depends(get_web_user)):
    """Kick off the OpenID flow by redirecting to Steam's login page."""
    params = {
        "openid.ns": _OPENID_NS,
        "openid.mode": "checkid_setup",
        "openid.return_to": _openid_return_url(request),
        "openid.realm": _openid_realm(request),
        "openid.identity": _OPENID_IDENTIFIER,
        "openid.claimed_id": _OPENID_IDENTIFIER,
    }
    url = _STEAM_OPENID_URL + "?" + urlencode(params)
    return RedirectResponse(url, status_code=302)


@router.get("/steam/openid/return")
def steam_openid_return(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Validate Steam's signed response and persist the SteamID + persona name."""
    # Steam responded with all the openid.* params in the query string. We
    # echo them straight back to Steam with mode=check_authentication.
    payload = dict(request.query_params)
    claimed_id = payload.get("openid.claimed_id", "")
    match = _CLAIMED_ID_RE.match(claimed_id)
    if not match:
        return RedirectResponse("/integrations/steam?openid=bad_claim", status_code=302)
    steam_id64 = match.group(1)

    payload["openid.mode"] = "check_authentication"
    try:
        verify = _httpx.post(_STEAM_OPENID_URL, data=payload, timeout=15)
        verify.raise_for_status()
    except Exception as e:
        _logger.warning("Steam OpenID verify request failed: %s", e)
        return RedirectResponse("/integrations/steam?openid=verify_failed", status_code=302)

    if "is_valid:true" not in verify.text:
        _logger.warning("Steam OpenID signature did not validate")
        return RedirectResponse("/integrations/steam?openid=invalid_sig", status_code=302)

    # Best-effort persona name + avatar fetch (uses the user's existing API
    # key if set). If we don't have a key yet, we just leave them unset — the
    # UI falls back to the SteamID and a placeholder icon.
    persona_name = None
    avatar_url = None
    if current_user.steam_api_key:
        try:
            r = _httpx.get(
                f"{steam.STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/",
                params={"key": current_user.steam_api_key, "steamids": steam_id64},
                timeout=10,
            )
            r.raise_for_status()
            players = r.json().get("response", {}).get("players", [])
            if players:
                persona_name = players[0].get("personaname")
                # `avatarmedium` is 64x64 — right size for the configure-page
                # icon without forcing the browser to scale a 184x184 down.
                avatar_url = players[0].get("avatarmedium") or players[0].get("avatar")
        except Exception as e:
            _logger.info("Persona/avatar fetch failed (non-fatal): %s", e)

    current_user.steam_id64 = steam_id64
    if persona_name:
        current_user.steam_persona_name = persona_name
    if avatar_url:
        current_user.steam_avatar_url = avatar_url
    db.commit()

    return RedirectResponse("/integrations/steam?openid=ok", status_code=302)


@router.post("/steam/openid/forget")
def steam_openid_forget(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Drop the user's Steam identity (SteamID + persona name + avatar). API
    key and cookies are NOT touched — those are managed by the credentials
    form. Returns HX-Refresh so the page re-renders without the "Signed in"
    block."""
    current_user.steam_id64 = None
    current_user.steam_persona_name = None
    current_user.steam_avatar_url = None
    db.commit()
    response = templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": "Steam sign-in forgotten."},
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


# All Steam sync job kinds, mapped to their (sync function, started toast text) tuple.
# Adding a new kind here is the only place you need to wire up — endpoint just calls
# _kick_off_sync with the kind, and _run_sync_job picks the function out of this table.
_STEAM_KINDS: dict[str, dict] = {
    "steam_sync_full": {
        "fn": "sync_full_library",
        "needs_cookies": True,
        "started": "Steam sync started — feel free to navigate away. You'll see a toast when it finishes.",
        "label": "Sync",
    },
    "steam_sync_games": {
        "fn": "sync_steam_library",
        "needs_cookies": False,
        "started": "Steam games sync started — you'll see a toast when it finishes.",
        "label": "Games only",
    },
    "steam_sync_dlc": {
        "fn": "sync_dlc_only",
        "needs_cookies": True,
        "started": "Steam DLC sync started — you'll see a toast when it finishes.",
        "label": "DLC only",
    },
    "steam_refresh_catalog": {
        "fn": "refresh_app_catalog",
        "needs_cookies": False,
        "started": "Refreshing Steam app catalog — this takes a few seconds.",
        "label": "Refresh app catalog",
    },
}


def _format_sync_result(db: Session, user: models.User, kind: str, result: dict) -> str:
    """Unified multi-line message format for completion toasts. Newlines render
    as line breaks thanks to `white-space: pre-line` on .toast-body. Shape:

        {Header}
        {Delta}
        {Totals}

    Delta only mentions ADDITIONS (no "updated"/"linked" — those are internal
    mechanics, not news the user cares about in a confirmation toast). Totals
    are read from the DB after the sync, so they reflect real library state
    even when Steam returned 0 for some count this run.
    Platform prefix ("Steam") lets PSN drop in with the same shape later."""
    totals = _steam_counts(db, user) or {"games": 0, "dlc": 0, "total": 0}
    totals_line = f"{totals['games']:,} games · {totals['dlc']:,} DLC total"

    if kind == "steam_refresh_catalog":
        return f"Steam app catalog refreshed\n{result['app_count']:,} entries cached"

    if kind == "steam_sync_full":
        header = "Steam sync complete"
        added_games = result.get("games_added", 0)
        added_dlc = result.get("dlc_added", 0)
        if added_games and added_dlc:
            delta = f"+{added_games:,} games · +{added_dlc:,} DLC"
        elif added_games:
            delta = f"+{added_games:,} games"
        elif added_dlc:
            delta = f"+{added_dlc:,} DLC"
        else:
            delta = "No new items"
    elif kind == "steam_sync_games":
        header = "Steam games sync complete"
        added = result.get("added", 0)
        delta = f"+{added:,} games" if added else "No new games"
    elif kind == "steam_sync_dlc":
        header = "Steam DLC sync complete"
        added = result.get("dlc_added", 0)
        delta = f"+{added:,} DLC" if added else "No new DLC"
    else:
        return f"Steam job complete\n{totals_line}"

    return f"{header}\n{delta}\n{totals_line}"


async def _run_sync_job(job_id: str, user_id: int, kind: str) -> None:
    """
    Background runner for a Steam job (any sync or catalog refresh).
    Creates a fresh DB session, pauses the enrichment worker for the duration,
    dispatches to the right steam.* function based on kind.
    """
    spec = _STEAM_KINDS.get(kind)
    if spec is None:
        jobs.mark_failed(job_id, f"Unknown job kind: {kind}")
        return

    jobs.update(job_id, status=jobs.JobStatus.RUNNING)
    worker_state.enrichment_paused = True
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user is None:
            jobs.mark_failed(job_id, "User no longer exists.")
            return

        fn = getattr(steam, spec["fn"])
        if kind == "steam_refresh_catalog":
            result = await asyncio.to_thread(fn, user.steam_api_key)
        else:
            result = await asyncio.to_thread(fn, db, user)

        jobs.mark_done(job_id, _format_sync_result(db, user, kind, result))
    except ValueError as e:
        jobs.mark_failed(job_id, str(e))
    except Exception as e:
        _logger.exception("Job %s (%s) failed", job_id, kind)
        jobs.mark_failed(job_id, f"Job failed: {e}")
    finally:
        worker_state.enrichment_paused = False
        db.close()


def _kick_off_sync(request: Request, current_user: models.User, kind: str):
    """Create a job, schedule the background task, return a 'started' toast."""
    spec = _STEAM_KINDS[kind]
    err = _credential_error(current_user, kind)
    if err:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": err},
            status_code=422,
        )
    active = jobs.active_jobs_for(current_user.id)
    if active:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": "A Steam job is already running — please wait for it to finish."},
            status_code=409,
        )
    job = jobs.create(user_id=current_user.id, kind=kind)
    asyncio.create_task(_run_sync_job(job.id, current_user.id, kind))
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": spec["started"]},
    )


def _credential_error(current_user: models.User, kind: str) -> str | None:
    """Pre-flight credential check so we 422 immediately instead of queueing a
    doomed background job."""
    if not current_user.steam_api_key or not current_user.steam_id64:
        return "Steam API key and Steam ID64 are required."
    spec = _STEAM_KINDS.get(kind, {})
    if spec.get("needs_cookies"):
        if not current_user.steam_session_id or not current_user.steam_login_secure:
            return "Browser cookies (sessionid + steamLoginSecure) are required for this operation."
    return None


@router.post("/steam/sync-all")
async def sync_steam_all(request: Request, current_user: models.User = Depends(get_web_user)):
    """Full sync (games + DLC). Runs in background; result is delivered via /jobs/poll."""
    return _kick_off_sync(request, current_user, "steam_sync_full")


@router.post("/steam/sync")
async def sync_steam(request: Request, current_user: models.User = Depends(get_web_user)):
    """Games-only sync. Diagnostic / fallback for users without cookies."""
    return _kick_off_sync(request, current_user, "steam_sync_games")


@router.post("/steam/sync-dlc-only")
async def sync_steam_dlc_only(request: Request, current_user: models.User = Depends(get_web_user)):
    """DLC-only sync — uses already-synced games as the baseline. Power-user diagnostic."""
    return _kick_off_sync(request, current_user, "steam_sync_dlc")


@router.post("/steam/refresh-app-catalog")
async def refresh_steam_app_catalog(request: Request, current_user: models.User = Depends(get_web_user)):
    """Force a fresh GetAppList fetch by invalidating the 7-day cache."""
    return _kick_off_sync(request, current_user, "steam_refresh_catalog")


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
    msg = f"Checked {result['checked']} apps — found {result['found_dlc']} DLC, linked {result['linked']} to base games."
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


_APP_PLACEHOLDER_RE = re.compile(r"^App \d+$")


@router.post("/steam/backfill-display-names")
def backfill_steam_display_names(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Re-process every Steam game in the user's library:

    1. If the title is the "App NNNNNN" sync placeholder AND we have cached
       appdetails with a real name, replace the title with the real name.
       (Same path the enrichment worker now follows, but applied retroactively
       to entries that were enriched before that fix existed.)
    2. Re-apply the _clean_title heuristic to compute display_name. Updates
       display_name when the heuristic now produces a different / better
       output than what's stored, or clears it when title no longer needs
       cleaning.

    Skips games where display_name_user_set is True — manual edits stick.
    No network calls; reads from cached release.raw_data only."""
    rows = (
        db.query(models.Game, models.GameRelease)
        .join(models.GameRelease, models.GameRelease.game_id == models.Game.id)
        .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.source == "steam",
            models.Game.display_name_user_set.is_(False),
        )
        .all()
    )
    # Dedupe in case a game has multiple Steam releases — take the first
    # release we see for each game (any of them carries appdetails).
    games_by_id: dict[int, tuple[models.Game, models.GameRelease]] = {}
    for game, release in rows:
        games_by_id.setdefault(game.id, (game, release))

    updated = 0
    for game, release in games_by_id.values():
        changed = False

        # 1. App-NNNN placeholder → real name from cached appdetails.
        if _APP_PLACEHOLDER_RE.match(game.title):
            details = (release.raw_data or {}).get("appdetails") or {}
            real_name = (details.get("name") or "").strip()
            if real_name and real_name != game.title:
                game.title = real_name
                changed = True

        # 2. Re-apply title-cleaning heuristic.
        cleaned = steam._clean_title(game.title)
        if cleaned != game.title and cleaned != game.display_name:
            game.display_name = cleaned
            changed = True
        elif cleaned == game.title and game.display_name is not None:
            game.display_name = None
            changed = True

        if changed:
            updated += 1
        elif cleaned == game.title and game.display_name is not None:
            # Title no longer needs cleaning — drop the now-redundant display_name.
            game.display_name = None
            updated += 1
    db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": f"Display-name cleanup complete — {updated} entries updated."},
    )


# ─── SteamGridDB ──────────────────────────────────────────────────────────


@router.get("/steamgriddb")
def steamgriddb_page(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    return templates.TemplateResponse(
        request=request,
        name="integrations_steamgriddb.html",
        context={"current_user": current_user},
    )


@router.post("/steamgriddb/credentials")
def save_steamgriddb_credentials(
    request: Request,
    steamgriddb_api_key: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    current_user.steamgriddb_api_key = steamgriddb_api_key.strip() or None
    db.commit()
    response = templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": "SteamGridDB API key saved."},
    )
    response.headers["HX-Refresh"] = "true"
    return response


@router.get("/steamgriddb/search")
def steamgriddb_search(
    request: Request,
    entry_id: int,
    orientation: str,
    page: int = 0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Return cover-art candidates for a library entry. Used by the "Find cover"
    modal. Looks up by Steam appid when the entry came from Steam (better hit
    rate); falls back to title search otherwise.

    Pagination: `page` is zero-indexed. The picker's "Load more" button hits
    this endpoint with page+1 and appends the new batch to the existing grid.
    """
    if orientation not in ("v", "h"):
        return Response(status_code=400)
    if not current_user.steamgriddb_api_key:
        return templates.TemplateResponse(
            request=request,
            name="partials/sgdb_cover_results.html",
            context={"error": "Set your SteamGridDB API key on the integrations page first.", "candidates": []},
        )

    entry = (
        db.query(models.UserLibraryEntry)
        .options(joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game))
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)
    release = entry.release
    game = release.game

    try:
        sgdb_game = None
        if release.source == "steam" and release.external_id:
            sgdb_game = sgdb.lookup_by_steam_appid(current_user.steamgriddb_api_key, release.external_id)
        if not sgdb_game:
            results = sgdb.search_games(current_user.steamgriddb_api_key, game.display_title)
            sgdb_game = results[0] if results else None
        if not sgdb_game:
            candidates = []
        else:
            candidates = sgdb.get_grids_for_game(current_user.steamgriddb_api_key, sgdb_game["id"], orientation, page=page)
    except Exception as e:
        _logger.warning("SteamGridDB search failed: %s", e)
        return templates.TemplateResponse(
            request=request,
            name="partials/sgdb_cover_results.html",
            context={"error": f"SteamGridDB lookup failed: {e}", "candidates": []},
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/sgdb_cover_results.html",
        context={
            "candidates": candidates,
            "entry_id": entry_id,
            "orientation": orientation,
            "page": page,
            # If we got a full page back, assume there's probably more — the
            # "Load more" button will fetch the next batch.
            "has_more": len(candidates) >= sgdb._GRID_PAGE_SIZE,
        },
    )


async def _run_sgdb_bulk_fill_job(job_id: str, user_id: int, orientation: str) -> None:
    """Background runner for the SGDB bulk-fill job. Mirrors _run_sync_job's
    shape but doesn't pause enrichment — SGDB writes to cover_url_override_*,
    which enrichment never touches."""
    jobs.update(job_id, status=jobs.JobStatus.RUNNING)
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user is None:
            jobs.mark_failed(job_id, "User no longer exists.")
            return
        result = await asyncio.to_thread(sgdb.bulk_fill_missing, db, user, orientation)
        label = "vertical" if orientation == "v" else "horizontal"
        header = f"SteamGridDB {label} cover fill complete"
        delta = f"+{result['filled']:,} filled · {result['no_candidate']:,} no match · {result['skipped']:,} already had art"
        if result["errored"]:
            delta += f" · {result['errored']:,} errored"
        jobs.mark_done(job_id, f"{header}\n{delta}")
    except ValueError as e:
        jobs.mark_failed(job_id, str(e))
    except Exception as e:
        _logger.exception("SGDB bulk fill job %s failed", job_id)
        jobs.mark_failed(job_id, f"Job failed: {e}")
    finally:
        db.close()


@router.post("/steamgriddb/fill-missing")
async def steamgriddb_fill_missing(
    request: Request,
    orientation: str = Form(...),
    current_user: models.User = Depends(get_web_user),
):
    """Kick off the bulk fill job. Walks the user's library and SGDB-fills
    every entry that's missing a cover of the requested orientation."""
    if orientation not in ("v", "h"):
        return Response(status_code=400)
    if not current_user.steamgriddb_api_key:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": "Set your SteamGridDB API key first."},
            status_code=422,
        )
    active = jobs.active_jobs_for(current_user.id)
    if active:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": "Another job is already running — please wait for it to finish."},
            status_code=409,
        )
    kind = f"sgdb_fill_{orientation}"
    job = jobs.create(user_id=current_user.id, kind=kind)
    asyncio.create_task(_run_sgdb_bulk_fill_job(job.id, current_user.id, orientation))
    label = "vertical" if orientation == "v" else "horizontal"
    return templates.TemplateResponse(
        request=request,
        name="partials/integrations_flash.html",
        context={"message": f"SteamGridDB {label} cover fill started — you'll see a toast when it finishes."},
    )
