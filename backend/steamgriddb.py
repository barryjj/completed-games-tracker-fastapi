"""Thin SteamGridDB API client. Used by the integration to look up cover art
candidates for library entries that don't have Steam-CDN art or have ugly art.

API docs: https://www.steamgriddb.com/api/v2
Auth: Bearer token (the user's API key).
"""

from __future__ import annotations

import logging

import httpx

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
