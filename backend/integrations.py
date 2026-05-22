import os

import httpx as _httpx

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models, steam
from .models import get_db
from .pages import get_web_user

router = APIRouter(prefix="/integrations")

TEMPLATES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _steam_game_count(db: Session, user: models.User) -> int | None:
    if not user.steam_id64:
        return None
    return (
        db.query(models.UserLibraryEntry)
        .join(models.GameRelease)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.source == "steam",
        )
        .count()
    )


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
            "steam_count": _steam_game_count(db, current_user),
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


@router.post("/steam/sync-all")
def sync_steam_all(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Full sync: games + DLC. Requires API key, Steam ID64, and browser cookies."""
    try:
        result = steam.sync_full_library(db, current_user)
        msg = (
            f"Sync complete — "
            f"{result['games_added']} games added, {result['games_updated']} updated "
            f"({result['games_total']} total) · "
            f"{result['dlc_added']} DLC added, {result['dlc_marked']} marked "
            f"({result['dlc_total']} total DLC owned)."
        )
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"message": msg},
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": str(e)},
            status_code=422,
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": f"Sync failed: {e}"},
            status_code=500,
        )


@router.post("/steam/sync")
def sync_steam(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Games-only sync via GetOwnedGames. Fallback when cookies aren't configured."""
    try:
        result = steam.sync_steam_library(db, current_user)
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={
                "message": f"Games sync complete — {result['added']} added, {result['updated']} updated ({result['total']} total).",
                "last_synced": current_user.steam_last_synced_at,
            },
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": str(e)},
            status_code=422,
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="partials/integrations_flash.html",
            context={"error": f"Sync failed: {e}"},
            status_code=500,
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
