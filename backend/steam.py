import datetime
import logging
import time

import httpx
from sqlalchemy.orm import Session

from . import models

logger = logging.getLogger(__name__)

STEAM_API_BASE = "https://api.steampowered.com"
STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps"


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


def _artwork_url(appid: int, artwork_type: str) -> str | None:
    urls = {
        "header": f"{STEAM_CDN}/{appid}/header.jpg",
        "cover": f"{STEAM_CDN}/{appid}/library_600x900.jpg",
        "hero": f"{STEAM_CDN}/{appid}/library_hero.jpg",
    }
    return urls.get(artwork_type)


def sync_steam_library(db: Session, user: models.User) -> dict:
    if not user.steam_api_key or not user.steam_id64:
        raise ValueError("Steam API key and Steam ID are required.")

    games = get_owned_games(user.steam_api_key, user.steam_id64)

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

        # Find or create GameRelease keyed on source+external_id
        release = (
            db.query(models.GameRelease)
            .filter_by(source="steam", external_id=appid)
            .first()
        )

        if release is None:
            game = models.Game(title=title, is_dlc=False, is_collection=False)
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

            # Store predictable CDN artwork URLs — no extra API call needed
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
            # Keep raw_data fresh
            release.raw_data = g

        # Find or create library entry
        entry = (
            db.query(models.UserLibraryEntry)
            .filter_by(user_id=user.id, release_id=release.id)
            .first()
        )

        if entry is None:
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

    user.steam_last_synced_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()

    return {"added": added, "updated": updated, "total": len(games)}
