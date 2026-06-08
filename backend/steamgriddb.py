"""Thin SteamGridDB API client. Used by the integration to look up cover art
candidates for library entries that don't have Steam-CDN art or have ugly art.

API docs: https://www.steamgriddb.com/api/v2
Auth: Bearer token (the user's API key).
"""

from __future__ import annotations

import logging
import re

import httpx
from sqlalchemy.orm import Session, joinedload

from . import models

logger = logging.getLogger(__name__)

_BASE = "https://www.steamgriddb.com/api/v2"

# SGDB returns multiple aspect ratios. We restrict each search to the one that
# matches our two cover orientations so we don't show the user a wall of
# squished thumbnails they'd never pick.
_DIMENSIONS_V = "600x900"  # portrait library art (matches Steam library_600x900)
_DIMENSIONS_H = "460x215,920x430"  # landscape header art (Steam's header.jpg ratio + 2x)


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "User-Agent": "completed-games-tracker/1.0"}


def lookup_by_steam_appid(api_key: str, appid: int | str) -> dict | None:
    """Resolve a Steam appid → SGDB game record. Returns None when SGDB has no
    matching entry (some obscure / delisted apps aren't catalogued)."""
    resp = httpx.get(
        f"{_BASE}/games/steam/{appid}",
        headers=_headers(api_key),
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("data")


def search_games(api_key: str, query: str) -> list[dict]:
    """Title-based search for entries that don't have a Steam appid (manual
    entries, PSN, etc.). Returns up to ~10 candidate games."""
    resp = httpx.get(
        f"{_BASE}/search/autocomplete/{httpx.URL(query).path or query}",
        headers=_headers(api_key),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


def _find_sgdb_game_for_entry(api_key: str, entry: models.UserLibraryEntry) -> dict | None:
    """Resolve a library entry → SGDB game record. Steam entries try the
    appid endpoint first (most reliable); other sources fall back to the
    title autocomplete and take the top hit. Returns None when nothing
    matches."""
    release = entry.release
    game = release.game
    if release.source == "steam" and release.external_id:
        sgdb_game = lookup_by_steam_appid(api_key, release.external_id)
        if sgdb_game:
            return sgdb_game
    results = search_games(api_key, game.display_title)
    return results[0] if results else None


_GRID_PAGE_SIZE = 20

# Image type constants — used throughout to identify which SGDB endpoint and
# which override column to target. "v" and "h" are cover grids; "hero" and
# "logo" are the detail-pane art types.
IMAGE_TYPES = ("v", "h", "hero", "logo")


def get_grids_for_game(
    api_key: str,
    sgdb_game_id: int,
    orientation: str,
    page: int = 0,
) -> list[dict]:
    """Fetch grid (cover) candidates for a SGDB game ID in the requested
    orientation. Each result has `url` (full-size), `thumb` (preview), `id`,
    and metadata about author / score. Sorted by SGDB's popularity score.

    Pagination: SGDB's API uses `page` (zero-indexed) with `limit`. Caller
    passes page=0 for the first batch (default), page=1 for the next, etc.
    The picker's "Load more" button bumps the page parameter."""
    dimensions = _DIMENSIONS_V if orientation == "v" else _DIMENSIONS_H
    resp = httpx.get(
        f"{_BASE}/grids/game/{sgdb_game_id}",
        params={
            "dimensions": dimensions,
            "types": "static,animated",
            "limit": str(_GRID_PAGE_SIZE),
            "page": str(page),
        },
        headers=_headers(api_key),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


def get_heroes_for_game(
    api_key: str,
    sgdb_game_id: int,
    page: int = 0,
) -> list[dict]:
    """Fetch hero image candidates (~1920x620) for a SGDB game ID.
    Heroes have no dimension filter — SGDB only serves one aspect ratio for
    heroes. Pagination mirrors get_grids_for_game."""
    resp = httpx.get(
        f"{_BASE}/heroes/game/{sgdb_game_id}",
        params={"types": "static,animated", "limit": str(_GRID_PAGE_SIZE), "page": str(page)},
        headers=_headers(api_key),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


def get_logos_for_game(
    api_key: str,
    sgdb_game_id: int,
    page: int = 0,
) -> list[dict]:
    """Fetch logo candidates (transparent PNG, variable size) for a SGDB game ID.
    Pagination mirrors get_grids_for_game."""
    resp = httpx.get(
        f"{_BASE}/logos/game/{sgdb_game_id}",
        params={"types": "static,animated", "limit": str(_GRID_PAGE_SIZE), "page": str(page)},
        headers=_headers(api_key),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


def fetch_images_for_game(
    api_key: str,
    sgdb_game_id: int,
    image_type: str,
    page: int = 0,
) -> list[dict]:
    """Dispatch to the right SGDB endpoint based on image_type.
    image_type: 'v' | 'h' | 'hero' | 'logo'"""
    if image_type in ("v", "h"):
        return get_grids_for_game(api_key, sgdb_game_id, image_type, page=page)
    if image_type == "hero":
        return get_heroes_for_game(api_key, sgdb_game_id, page=page)
    if image_type == "logo":
        return get_logos_for_game(api_key, sgdb_game_id, page=page)
    raise ValueError(f"Unknown image_type {image_type!r}")


_IMAGE_TYPE_TO_ARTWORK_TYPE = {
    "v": "cover_v",
    "h": "cover_h",
    "hero": "hero",
    "logo": "logo",
}


def _entry_already_has_image(entry: models.UserLibraryEntry, image_type: str) -> bool:
    """True when the entry already has art for this image_type — either a
    UserArtwork row (explicit pick) or a valid release-level GameArtwork row.
    Used by bulk fill to skip entries that don't need help."""
    art_type = _IMAGE_TYPE_TO_ARTWORK_TYPE[image_type]
    # 1. UserArtwork explicit pick for this entry
    if any(ua.artwork_type == art_type and ua.url for ua in entry.user_artwork):
        return True
    if image_type == "logo":
        # Logos have no GameArtwork rows — constructed from CDN on-the-fly.
        return False
    # 2. Valid GameArtwork row for this release
    return any(a.artwork_type == art_type and a.is_valid and a.url for a in entry.release.artwork)


def _upsert_user_artwork(
    db: Session,
    entry: models.UserLibraryEntry,
    image_type: str,
    url: str,
    source: str = "sgdb",
) -> None:
    """Write art to UserArtwork for this entry. Creates the row if it doesn't
    exist; updates url/source if it does."""
    art_type = _IMAGE_TYPE_TO_ARTWORK_TYPE[image_type]
    # Check in-memory collection first to avoid a redundant query
    for ua in entry.user_artwork:
        if ua.artwork_type == art_type:
            ua.url = url
            ua.source = source
            return
    # New row
    ua = models.UserArtwork(
        user_id=entry.user_id,
        entry_id=entry.id,
        artwork_type=art_type,
        source=source,
        url=url,
    )
    db.add(ua)


def auto_fetch_logo(db: Session, user: models.User, entry: models.UserLibraryEntry) -> str | None:
    """Try to fetch a logo for a single entry from SGDB and store it as a
    UserArtwork row. Returns the URL on success, None if nothing found.

    Called automatically when the detail pane detects a missing/404 logo —
    not a user-initiated pick, so we always take the top result. The user
    can open the logo picker afterwards to swap it for a different one."""
    if not user.steamgriddb_api_key:
        return None
    # Already have one — return it so the caller can update the img src.
    existing = next((ua for ua in entry.user_artwork if ua.artwork_type == "logo" and ua.url), None)
    if existing:
        return existing.url
    try:
        sgdb_game = _find_sgdb_game_for_entry(user.steamgriddb_api_key, entry)
        if not sgdb_game:
            return None
        logos = get_logos_for_game(user.steamgriddb_api_key, sgdb_game["id"])
        if not logos:
            return None
        url = logos[0].get("url")
        if not url:
            return None
        _upsert_user_artwork(db, entry, "logo", url)
        db.commit()
        return url
    except Exception as e:
        logger.warning("SGDB auto-fetch logo failed for entry %s: %s", entry.id, e)
        return None


def auto_fetch_hero(db: Session, user: models.User, entry: models.UserLibraryEntry) -> str | None:
    """Try to fetch a hero image for a single entry from SGDB and store it as
    a UserArtwork row. Returns the URL on success, None if nothing found.

    Mirrors auto_fetch_logo — called automatically when the detail pane has no
    hero URL. The user can open the hero picker afterwards to swap it."""
    if not user.steamgriddb_api_key:
        return None
    existing = next((ua for ua in entry.user_artwork if ua.artwork_type == "hero" and ua.url), None)
    if existing:
        return existing.url
    try:
        sgdb_game = _find_sgdb_game_for_entry(user.steamgriddb_api_key, entry)
        if not sgdb_game:
            return None
        heroes = get_heroes_for_game(user.steamgriddb_api_key, sgdb_game["id"])
        if not heroes:
            return None
        url = heroes[0].get("url")
        if not url:
            return None
        _upsert_user_artwork(db, entry, "hero", url)
        db.commit()
        return url
    except Exception as e:
        logger.warning("SGDB auto-fetch hero failed for entry %s: %s", entry.id, e)
        return None


def auto_fetch_grid(db: Session, user: models.User, entry: models.UserLibraryEntry, orientation: str = "h") -> str | None:
    """Try to fetch a grid cover (h or v) for a single entry from SGDB and
    store it as a UserArtwork row. Returns the URL on success, None if nothing
    found. Called automatically when logging a completion for an entry with no cover."""
    art_type = "cover_h" if orientation == "h" else "cover_v"
    if not user.steamgriddb_api_key:
        return None
    existing = next((ua for ua in entry.user_artwork if ua.artwork_type == art_type and ua.url), None)
    if existing:
        return existing.url
    try:
        sgdb_game = _find_sgdb_game_for_entry(user.steamgriddb_api_key, entry)
        if not sgdb_game:
            return None
        grids = get_grids_for_game(user.steamgriddb_api_key, sgdb_game["id"], orientation=orientation)
        if not grids:
            return None
        url = grids[0].get("url")
        if not url:
            return None
        _upsert_user_artwork(db, entry, art_type, url)
        db.commit()
        return url
    except Exception as e:
        logger.warning("SGDB auto-fetch grid (%s) failed for entry %s: %s", orientation, entry.id, e)
        return None


def bulk_fill_missing(
    db: Session,
    user: models.User,
    image_type: str,
    progress_callback=None,
) -> dict:
    """Walk every visible library entry for `user`, and for each one missing
    art of the given type: hit SGDB, take the top candidate, and write it to
    the matching override column.

    Skips entries that already have either a user override OR a release-level
    artwork row of the right type (covers/heroes). Logos have no GameArtwork
    row, so only the override column is checked for those.

    Hidden entries are skipped. DLC is included. Entries are processed
    alphabetically by display title.

    image_type: 'v' | 'h' | 'hero' | 'logo'
    progress_callback: optional callable(done: int, total: int, title: str)
    Returns: {"filled": N, "no_candidate": N, "skipped": N, "errored": N}
    """
    if image_type not in IMAGE_TYPES:
        raise ValueError(f"image_type must be one of {IMAGE_TYPES}, got {image_type!r}")
    if not user.steamgriddb_api_key:
        raise ValueError("User has no SteamGridDB API key set.")

    api_key = user.steamgriddb_api_key
    entries = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            joinedload(models.UserLibraryEntry.user_artwork),
        )
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.UserLibraryEntry.is_hidden.is_(False),
        )
        .all()
    )

    # Sort alphabetically by display title so progress is predictable
    def _sort_key(e):
        g = e.release.game if e.release else None
        return (g.display_title or g.title or "").casefold() if g else ""

    entries.sort(key=_sort_key)
    total = len(entries)

    filled = 0
    no_candidate = 0
    skipped = 0
    errored = 0

    for i, entry in enumerate(entries):
        if progress_callback:
            g = entry.release.game if entry.release else None
            title = (g.display_title or g.title or "") if g else ""
            progress_callback(i, total, title)
        if _entry_already_has_image(entry, image_type):
            skipped += 1
            continue
        # Skip entries whose title is still an "App NNNNNN" placeholder —
        # searching SGDB by "App 317740" returns nothing useful or, worse,
        # a random wrong match. Let the title get fixed first.
        _game = entry.release.game if entry.release else None
        if _game and re.match(r"^App \d+$", _game.title or ""):
            skipped += 1
            continue
        try:
            sgdb_game = _find_sgdb_game_for_entry(api_key, entry)
            if not sgdb_game:
                no_candidate += 1
                continue
            images = fetch_images_for_game(api_key, sgdb_game["id"], image_type)
            if not images:
                no_candidate += 1
                continue
            top_url = images[0].get("url")
            if not top_url:
                no_candidate += 1
                continue
            _upsert_user_artwork(db, entry, image_type, top_url)
            filled += 1
        except Exception as e:
            logger.warning("SGDB bulk fill failed for entry %s: %s", entry.id, e)
            errored += 1
            continue

    db.commit()
    return {"filled": filled, "no_candidate": no_candidate, "skipped": skipped, "errored": errored}
