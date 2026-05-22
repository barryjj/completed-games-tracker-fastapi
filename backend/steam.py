import datetime
import json
import logging
import os
import re
import time

import httpx
from sqlalchemy.orm import Session, joinedload

from . import models

# Symbols that Steam appends to titles but are meaningless for display
_JUNK_RE = re.compile(r"[™®©]+")

COLLECTION_KEYWORDS = [
    "collection", "anthology", "trilogy", "compilation",
    "complete edition", "complete pack", "bundle", "chronicles",
    "archives", "legacy", "origins",
]


def _infer_is_collection(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in COLLECTION_KEYWORDS)


def _clean_title(title: str) -> str:
    """Return title with trademark/copyright symbols stripped and whitespace normalised."""
    return _JUNK_RE.sub("", title).strip()


logger = logging.getLogger(__name__)

STEAM_API_BASE = "https://api.steampowered.com"
STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps"

# App list cache — refreshed from disk/API when stale
_APP_LIST_CACHE_PATH = os.path.join(os.path.dirname(__file__), "steam_applist_cache.json")
_APP_LIST_CACHE_TTL = datetime.timedelta(days=7)
_app_list_memory: dict[int, str] = {}
_app_list_cached_at: datetime.datetime | None = None


def get_owned_games(api_key: str, steam_id64: str) -> list[dict]:
    url = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": api_key,
        "steamid": steam_id64,
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "format": "json",
    }
    response = httpx.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    games = data.get("response", {}).get("games", [])
    games.sort(key=lambda g: g.get("name", "").lower())
    return games


def get_app_list() -> dict[int, str]:
    """
    Return {appid: name} for every app on Steam.
    Cached to disk for 7 days; loaded into memory for the server's lifetime.
    """
    global _app_list_memory, _app_list_cached_at
    now = datetime.datetime.now(datetime.timezone.utc)

    # Memory hit
    if _app_list_memory and _app_list_cached_at and (now - _app_list_cached_at) < _APP_LIST_CACHE_TTL:
        return _app_list_memory

    # Disk hit
    if os.path.exists(_APP_LIST_CACHE_PATH):
        try:
            with open(_APP_LIST_CACHE_PATH) as f:
                data = json.load(f)
            cached_at = datetime.datetime.fromisoformat(data["cached_at"])
            if (now - cached_at) < _APP_LIST_CACHE_TTL:
                _app_list_memory = {int(k): v for k, v in data["apps"].items()}
                _app_list_cached_at = cached_at
                logger.info("Steam app list loaded from disk cache (%d apps)", len(_app_list_memory))
                return _app_list_memory
        except Exception as e:
            logger.warning("Failed to read app list cache: %s", e)

    # Fetch from Steam
    logger.info("Fetching Steam app list from API...")
    resp = httpx.get(
        f"{STEAM_API_BASE}/ISteamApps/GetAppList/v2/",
        timeout=60,
    )
    resp.raise_for_status()
    apps = resp.json()["applist"]["apps"]
    app_dict = {a["appid"]: a["name"] for a in apps}
    logger.info("Fetched %d apps from Steam", len(app_dict))

    try:
        with open(_APP_LIST_CACHE_PATH, "w") as f:
            json.dump(
                {"cached_at": now.isoformat(), "apps": {str(k): v for k, v in app_dict.items()}},
                f,
            )
    except Exception as e:
        logger.warning("Failed to write app list cache: %s", e)

    _app_list_memory = app_dict
    _app_list_cached_at = now
    return _app_list_memory


def _artwork_url(appid: int, artwork_type: str) -> str | None:
    urls = {
        "header": f"{STEAM_CDN}/{appid}/header.jpg",
        "cover": f"{STEAM_CDN}/{appid}/library_600x900.jpg",
        "hero": f"{STEAM_CDN}/{appid}/library_hero.jpg",
    }
    return urls.get(artwork_type)


def _import_owned_games(db: Session, user: models.User, games: list[dict]) -> dict:
    """
    Upsert Game/GameRelease/UserLibraryEntry rows for a list of GetOwnedGames entries.
    Returns counts; does NOT commit.
    """
    added = 0
    updated = 0

    for g in games:
        appid = str(g["appid"])
        title = g.get("name", f"App {appid}")
        playtime = g.get("playtime_forever", 0)
        last_played_ts = g.get("rtime_last_played")
        last_played = (
            datetime.datetime.fromtimestamp(last_played_ts, tz=datetime.timezone.utc)
            if last_played_ts
            else None
        )

        release = (
            db.query(models.GameRelease)
            .filter_by(source="steam", external_id=appid)
            .first()
        )

        if release is None:
            cleaned = _clean_title(title)

            existing_game = (
                db.query(models.Game)
                .join(models.GameRelease)
                .join(models.UserLibraryEntry)
                .filter(
                    models.UserLibraryEntry.user_id == user.id,
                    models.Game.title == title,
                )
                .first()
            )

            if existing_game is not None:
                game = existing_game
                if game.display_name is None and cleaned != title:
                    game.display_name = cleaned
            else:
                game = models.Game(
                    title=title,
                    display_name=cleaned if cleaned != title else None,
                    is_dlc=False,
                    is_collection=_infer_is_collection(title),
                )
                db.add(game)
                db.flush()

            release = models.GameRelease(
                game_id=game.id,
                platform="Steam",
                source="steam",
                external_id=appid,
                raw_data=g,
            )
            db.add(release)
            db.flush()

            for artwork_type in ("header", "cover", "hero"):
                url = _artwork_url(int(appid), artwork_type)
                if url:
                    db.add(models.GameArtwork(
                        release_id=release.id,
                        artwork_type=artwork_type,
                        source="steam",
                        url=url,
                    ))
        else:
            raw = dict(release.raw_data or {})
            raw.update(g)
            release.raw_data = raw

        entry = (
            db.query(models.UserLibraryEntry)
            .filter_by(user_id=user.id, release_id=release.id)
            .first()
        )

        if entry is None:
            existing_entry = (
                db.query(models.UserLibraryEntry)
                .join(models.GameRelease)
                .filter(
                    models.UserLibraryEntry.user_id == user.id,
                    models.GameRelease.game_id == release.game_id,
                )
                .first()
            )
            if existing_entry is not None:
                existing_entry.playtime_minutes = playtime
                existing_entry.last_played_at = last_played
                existing_entry.updated_at = datetime.datetime.now(datetime.timezone.utc)
                updated += 1
            else:
                db.add(models.UserLibraryEntry(
                    user_id=user.id,
                    release_id=release.id,
                    playtime_minutes=playtime,
                    last_played_at=last_played,
                    import_source="steam_import",
                ))
                added += 1
        else:
            entry.playtime_minutes = playtime
            entry.last_played_at = last_played
            entry.updated_at = datetime.datetime.now(datetime.timezone.utc)
            updated += 1

    return {"games_added": added, "games_updated": updated, "games_total": len(games)}


def _import_dlc(db: Session, user: models.User, dlc_appids: set[int], app_names: dict[int, str]) -> dict:
    """
    Upsert DLC entries for the given app IDs.
    - Existing releases: mark game.is_dlc=True; ensure user has a library entry.
    - New releases: create Game (is_dlc=True) + GameRelease + UserLibraryEntry.
    Does NOT commit.
    """
    # Bulk-load all existing Steam releases (avoids N+1 queries for 8k+ DLC)
    existing_releases: dict[str, models.GameRelease] = {
        r.external_id: r
        for r in db.query(models.GameRelease)
        .options(joinedload(models.GameRelease.game))
        .filter(models.GameRelease.source == "steam")
        .all()
    }

    # Bulk-load user's existing library entry release IDs
    user_release_ids: set[int] = {
        row[0]
        for row in db.query(models.UserLibraryEntry.release_id)
        .filter(models.UserLibraryEntry.user_id == user.id)
        .all()
    }

    newly_marked = 0
    newly_added = 0
    FLUSH_EVERY = 500

    for i, appid in enumerate(dlc_appids):
        appid_str = str(appid)
        release = existing_releases.get(appid_str)

        if release is not None:
            if not release.game.is_dlc:
                release.game.is_dlc = True
                newly_marked += 1
            if release.id not in user_release_ids:
                db.add(models.UserLibraryEntry(
                    user_id=user.id,
                    release_id=release.id,
                    import_source="steam_import",
                ))
                user_release_ids.add(release.id)
                newly_added += 1
        else:
            title = app_names.get(appid) or f"App {appid}"
            cleaned = _clean_title(title)

            game = models.Game(
                title=title,
                display_name=cleaned if cleaned != title else None,
                is_dlc=True,
                is_collection=False,
            )
            db.add(game)
            db.flush()

            release = models.GameRelease(
                game_id=game.id,
                platform="Steam",
                source="steam",
                external_id=appid_str,
            )
            db.add(release)
            db.flush()

            for artwork_type in ("header", "cover", "hero"):
                url = _artwork_url(appid, artwork_type)
                if url:
                    db.add(models.GameArtwork(
                        release_id=release.id,
                        artwork_type=artwork_type,
                        source="steam",
                        url=url,
                    ))

            db.add(models.UserLibraryEntry(
                user_id=user.id,
                release_id=release.id,
                import_source="steam_import",
            ))
            existing_releases[appid_str] = release
            user_release_ids.add(release.id)
            newly_added += 1

        if (i + 1) % FLUSH_EVERY == 0:
            db.flush()

    return {
        "dlc_total": len(dlc_appids),
        "dlc_marked": newly_marked,
        "dlc_added": newly_added,
    }


def sync_steam_library(db: Session, user: models.User) -> dict:
    """Sync base games only via GetOwnedGames. Kept for backward compatibility."""
    if not user.steam_api_key or not user.steam_id64:
        raise ValueError("Steam API key and Steam ID are required.")

    games = get_owned_games(user.steam_api_key, user.steam_id64)
    result = _import_owned_games(db, user, games)
    user.steam_last_synced_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()

    # Map old keys for backward compat
    return {"added": result["games_added"], "updated": result["games_updated"], "total": result["games_total"]}


def sync_full_library(db: Session, user: models.User) -> dict:
    """
    Full sync: games via GetOwnedGames + DLC via rgOwnedApps + GetAppList name lookup.
    Requires API key, Steam ID64, and browser cookies.
    3 API calls total — no per-game scraping.
    """
    if not user.steam_api_key or not user.steam_id64:
        raise ValueError("Steam API key and Steam ID64 are required.")
    if not user.steam_session_id or not user.steam_login_secure:
        raise ValueError("Browser cookies (sessionid + steamLoginSecure) are required for full sync.")

    # 1. Base games with playtime
    logger.info("Fetching owned games for user %s", user.steam_id64)
    games = get_owned_games(user.steam_api_key, user.steam_id64)
    game_result = _import_owned_games(db, user, games)
    game_appids = {g["appid"] for g in games}

    # 2. All owned app IDs (games + DLC) via cookies
    logger.info("Fetching rgOwnedApps for user %s", user.steam_id64)
    userdata = httpx.get(
        "https://store.steampowered.com/dynamicstore/userdata/",
        cookies={
            "sessionid": user.steam_session_id,
            "steamLoginSecure": user.steam_login_secure,
        },
        timeout=30,
    )
    userdata.raise_for_status()
    all_owned = set(userdata.json().get("rgOwnedApps", []))

    # DLC = apps owned but not in the games list
    dlc_appids = all_owned - game_appids
    logger.info("Found %d owned apps, %d games, %d DLC", len(all_owned), len(game_appids), len(dlc_appids))

    # 3. App name index
    app_names = get_app_list()

    # 4. Import DLC
    dlc_result = _import_dlc(db, user, dlc_appids, app_names)

    now = datetime.datetime.now(datetime.timezone.utc)
    user.steam_last_synced_at = now
    user.steam_last_dlc_synced_at = now
    db.commit()

    return {**game_result, **dlc_result}


def _fetch_appdetails(appid: int) -> dict | None:
    """
    Fetch app metadata from the Steam store API.
    - Returns the data dict when Steam reports success.
    - Returns None when Steam responds with {"success": false} — the app is
      delisted, region-locked, or otherwise permanently unavailable.
    - Raises on transient errors (network failure, HTTP 5xx, rate limits) so
      the caller can decide whether to retry rather than stamping the entry
      as enriched with no data.
    """
    resp = httpx.get(
        "https://store.steampowered.com/api/appdetails",
        params={"appids": appid},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json().get(str(appid), {})
    if result.get("success"):
        return result.get("data", {})
    return None


def enrich_next_batch(db: Session, batch_size: int = 5) -> int:
    """
    Fetch appdetails for the next batch of Steam entries missing metadata.
    - On Steam success: store payload, update DLC flag + parent link, stamp.
    - On Steam-confirmed unavailability (success=false): stamp anyway (no point retrying).
    - On transient errors (network/HTTP 5xx): leave metadata_fetched_at null
      so the worker picks the entry back up on the next cycle.
    Returns count still pending.
    """
    entries = (
        db.query(models.GameRelease)
        .options(joinedload(models.GameRelease.game))
        .filter(
            models.GameRelease.source == "steam",
            models.GameRelease.metadata_fetched_at == None,
        )
        .order_by(models.GameRelease.created_at.asc())
        .limit(batch_size)
        .all()
    )

    now = datetime.datetime.now(datetime.timezone.utc)

    for release in entries:
        try:
            details = _fetch_appdetails(int(release.external_id))
        except Exception as e:
            logger.warning(
                "appdetails fetch failed for appid %s (transient, will retry): %s",
                release.external_id, e,
            )
            time.sleep(0.3)
            continue

        if details is not None:
            raw = dict(release.raw_data or {})
            raw["appdetails"] = details
            raw["appdetails_type"] = details.get("type", "game")
            release.raw_data = raw

            game = release.game
            app_type = details.get("type", "game")

            if app_type == "dlc" and not game.is_dlc:
                game.is_dlc = True

            # Link DLC to its base game if not already linked
            if app_type == "dlc" and game.parent_id is None:
                fullgame = details.get("fullgame", {})
                parent_appid = str(fullgame.get("appid", "")).strip()
                if parent_appid:
                    parent_release = (
                        db.query(models.GameRelease)
                        .filter_by(source="steam", external_id=parent_appid)
                        .first()
                    )
                    if parent_release:
                        game.parent_id = parent_release.game_id

        release.metadata_fetched_at = now
        db.commit()
        time.sleep(0.3)

    pending = (
        db.query(models.GameRelease)
        .filter(
            models.GameRelease.source == "steam",
            models.GameRelease.metadata_fetched_at == None,
        )
        .count()
    )
    return pending


def sync_dlc_flags(db: Session, user: models.User) -> dict:
    """
    Legacy DLC detection via appdetails — one API call per game.
    Fallback for users without browser cookies. Slow for large libraries.
    """
    releases_to_check = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry)
        .join(models.Game)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.source == "steam",
            models.Game.is_dlc == False,
        )
        .all()
    )

    checked = 0
    found_dlc = 0
    linked = 0

    for release in releases_to_check:
        if (release.raw_data or {}).get("appdetails_type"):
            checked += 1
            continue

        try:
            details = _fetch_appdetails(int(release.external_id))
        except Exception as e:
            logger.warning("appdetails fetch failed for appid %s: %s", release.external_id, e)
            time.sleep(0.3)
            continue
        time.sleep(0.3)

        if details is None:
            continue

        app_type = details.get("type", "game")

        raw = dict(release.raw_data or {})
        raw["appdetails_type"] = app_type
        release.raw_data = raw
        checked += 1

        if app_type != "dlc":
            continue

        game = release.game
        game.is_dlc = True
        found_dlc += 1

        fullgame = details.get("fullgame", {})
        parent_appid = str(fullgame.get("appid", "")).strip()
        if parent_appid:
            parent_release = (
                db.query(models.GameRelease)
                .filter_by(source="steam", external_id=parent_appid)
                .first()
            )
            if parent_release:
                game.parent_id = parent_release.game_id
                linked += 1

    user.steam_last_dlc_synced_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    return {"checked": checked, "found_dlc": found_dlc, "linked": linked}
