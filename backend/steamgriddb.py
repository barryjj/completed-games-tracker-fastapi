"""Thin SteamGridDB API client. Used by the integration to look up cover art
candidates for library entries that don't have Steam-CDN art or have ugly art.

API docs: https://www.steamgriddb.com/api/v2
Auth: Bearer token (the user's API key).
"""

from __future__ import annotations

import logging

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


def get_grids_for_game(api_key: str, sgdb_game_id: int, orientation: str) -> list[dict]:
    """Fetch grid (cover) candidates for a SGDB game ID in the requested
    orientation. Each result has `url` (full-size), `thumb` (preview), `id`,
    and metadata about author / score. Sorted by SGDB's popularity score."""
    dimensions = _DIMENSIONS_V if orientation == "v" else _DIMENSIONS_H
    resp = httpx.get(
        f"{_BASE}/grids/game/{sgdb_game_id}",
        params={"dimensions": dimensions, "limit": "20"},
        headers=_headers(api_key),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


def _entry_already_has_cover(entry: models.UserLibraryEntry, orientation: str) -> bool:
    """True when the entry already has a cover for this orientation — either a
    user-set override or a release-level GameArtwork row of the right type.
    Used by the bulk fill to skip entries that don't need help."""
    wanted_art_type = "cover" if orientation == "v" else "header"
    if orientation == "v" and entry.cover_url_override_v:
        return True
    if orientation == "h" and entry.cover_url_override_h:
        return True
    for art in entry.release.artwork:
        if art.artwork_type == wanted_art_type and art.url:
            return True
    return False


def bulk_fill_missing(db: Session, user: models.User, orientation: str) -> dict:
    """Walk every visible library entry for `user`, and for each one missing a
    cover in the requested orientation: hit SGDB, take the top candidate, and
    write it to the matching cover_url_override column.

    Skips entries that already have either a user override OR a release-level
    artwork row of the right type — we don't want to stomp Steam CDN art that's
    already working.

    Hidden entries are skipped (no point spending API calls on stuff the user
    doesn't see). DLC is included — DLC frequently lacks portrait art and
    benefits the most from this.

    Returns: {"filled": N, "no_candidate": N, "skipped": N, "errored": N}
    """
    if orientation not in ("v", "h"):
        raise ValueError(f"orientation must be 'v' or 'h', got {orientation!r}")
    if not user.steamgriddb_api_key:
        raise ValueError("User has no SteamGridDB API key set.")

    api_key = user.steamgriddb_api_key
    entries = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
        )
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.UserLibraryEntry.is_hidden.is_(False),
        )
        .all()
    )

    filled = 0
    no_candidate = 0
    skipped = 0
    errored = 0

    for entry in entries:
        if _entry_already_has_cover(entry, orientation):
            skipped += 1
            continue
        try:
            sgdb_game = _find_sgdb_game_for_entry(api_key, entry)
            if not sgdb_game:
                no_candidate += 1
                continue
            grids = get_grids_for_game(api_key, sgdb_game["id"], orientation)
            if not grids:
                no_candidate += 1
                continue
            top_url = grids[0].get("url")
            if not top_url:
                no_candidate += 1
                continue
            if orientation == "v":
                entry.cover_url_override_v = top_url
            else:
                entry.cover_url_override_h = top_url
            filled += 1
        except Exception as e:
            # Don't abort the whole bulk job for one bad lookup — log it and
            # move on. SGDB 5xx on individual titles shouldn't waste the
            # progress made on the rest of the library.
            logger.warning("SGDB bulk fill failed for entry %s: %s", entry.id, e)
            errored += 1
            continue

    # One commit at the end keeps the run atomic-ish — if we crashed mid-loop
    # we'd lose the in-memory progress, but the user can just re-run and
    # _entry_already_has_cover will skip everything we already covered.
    db.commit()

    return {"filled": filled, "no_candidate": no_candidate, "skipped": skipped, "errored": errored}
