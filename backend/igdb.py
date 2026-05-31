"""IGDB / Twitch API integration.

Authentication uses the Twitch Client Credentials flow — no user login required.
The access token is fetched on demand and cached in-process (expires after 1 hour;
we re-fetch when the cache is stale rather than implementing refresh tokens since
client credentials tokens are cheap to replace).

Public API surface:
  get_token(client_id, client_secret)  → bearer token string (cached)
  search_games(client_id, secret, query, limit) → list of game dicts
  fetch_cover(client_id, secret, igdb_game_id) → cover URL or None
  save_igdb_cover(db, entry, igdb_game_id, client_id, secret) → GameArtwork or None
"""

import datetime
import logging

import httpx

from . import models

logger = logging.getLogger(__name__)

_TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_IGDB_BASE = "https://api.igdb.com/v4"

# In-process token cache keyed by (client_id, client_secret).
# Each entry: {"token": str, "expires_at": datetime}
_token_cache: dict[tuple[str, str], dict] = {}

# Buffer: treat the token as expired 60s before Twitch says it is, to avoid
# racing the clock between the cache check and the actual API call.
_EXPIRY_BUFFER_SECONDS = 60


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


def get_token(client_id: str, client_secret: str) -> str:
    """Return a valid Twitch bearer token, fetching a new one if needed."""
    key = (client_id, client_secret)
    cached = _token_cache.get(key)
    now = datetime.datetime.now(datetime.UTC)
    if cached and cached["expires_at"] > now:
        return cached["token"]

    resp = httpx.post(
        _TWITCH_TOKEN_URL,
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    expires_at = now + datetime.timedelta(seconds=expires_in - _EXPIRY_BUFFER_SECONDS)
    _token_cache[key] = {"token": token, "expires_at": expires_at}
    logger.debug("IGDB: fetched new Twitch token (expires in %ds)", expires_in)
    return token


def _igdb_headers(client_id: str, token: str) -> dict:
    return {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
    }


# ---------------------------------------------------------------------------
# Game search
# ---------------------------------------------------------------------------


def search_games(
    client_id: str,
    client_secret: str,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Search IGDB for games matching `query`.

    Returns a list of dicts with keys: id, name, cover_url, platforms, year.
    Results are sorted by IGDB's relevance score.
    """
    token = get_token(client_id, client_secret)
    body = (
        f'search "{query}"; '
        f"fields id, name, cover.url, platforms.name, first_release_date, version_parent; "
        f"where version_parent = null; "  # exclude version variants (GOTY etc.) from top results
        f"limit {limit};"
    )
    resp = httpx.post(
        f"{_IGDB_BASE}/games",
        headers=_igdb_headers(client_id, token),
        content=body,
        timeout=10,
    )
    resp.raise_for_status()
    games = resp.json()

    results = []
    for g in games:
        cover_url = None
        raw_cover = g.get("cover")
        if raw_cover and raw_cover.get("url"):
            # IGDB returns protocol-relative URLs like //images.igdb.com/...
            # and thumbnail sizes. We want the full 720p cover (t_cover_big).
            cover_url = _igdb_image_url(raw_cover["url"], "t_cover_big")

        year = None
        ts = g.get("first_release_date")
        if ts:
            year = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).year

        platforms = [p["name"] for p in (g.get("platforms") or [])]

        results.append(
            {
                "id": g["id"],
                "name": g["name"],
                "cover_url": cover_url,
                "platforms": platforms,
                "year": year,
            }
        )
    return results


def _igdb_image_url(raw_url: str, size: str) -> str:
    """Convert an IGDB image URL to the requested size variant.

    IGDB returns URLs like //images.igdb.com/igdb/image/upload/t_thumb/co1234.jpg.
    We replace the size slug and ensure https://.
    """
    url = raw_url.lstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    # Replace whatever size slug IGDB gave us with the one we want.
    import re

    url = re.sub(r"/t_[^/]+/", f"/{size}/", url)
    return url


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------


def fetch_cover_url(
    client_id: str,
    client_secret: str,
    igdb_game_id: int,
    size: str = "t_cover_big",
) -> str | None:
    """Fetch the cover art URL for an IGDB game ID. Returns None if not found."""
    token = get_token(client_id, client_secret)
    body = f"fields url; where game = {igdb_game_id}; limit 1;"
    resp = httpx.post(
        f"{_IGDB_BASE}/covers",
        headers=_igdb_headers(client_id, token),
        content=body,
        timeout=10,
    )
    resp.raise_for_status()
    covers = resp.json()
    if not covers:
        return None
    return _igdb_image_url(covers[0]["url"], size)


def save_igdb_cover(
    db,
    entry: "models.UserLibraryEntry",
    igdb_game_id: int,
    client_id: str,
    client_secret: str,
) -> "models.GameArtwork | None":
    """Fetch IGDB cover art for a game and write it to GameArtwork.

    Writes a vertical cover (cover_v) since IGDB covers are portrait-oriented
    (roughly 3:4). Skips if a valid cover_v already exists from any source.
    Returns the GameArtwork row on success, None if no cover found.
    """
    release = entry.release
    # Don't overwrite an existing valid cover.
    existing = next(
        (a for a in release.artwork if a.artwork_type == "cover_v" and a.is_valid and a.source != "igdb"),
        None,
    )
    if existing:
        return None

    url = fetch_cover_url(client_id, client_secret, igdb_game_id, size="t_cover_big")
    if not url:
        return None

    # Upsert: update existing igdb row or insert a new one.
    art = next(
        (a for a in release.artwork if a.artwork_type == "cover_v" and a.source == "igdb"),
        None,
    )
    if art:
        art.url = url
        art.is_valid = True
        art.verified_at = datetime.datetime.now(datetime.UTC)
    else:
        art = models.GameArtwork(
            release_id=release.id,
            game_id=release.game_id,
            artwork_type="cover_v",
            source="igdb",
            url=url,
            is_valid=True,
            verified_at=datetime.datetime.now(datetime.UTC),
        )
        db.add(art)

    db.commit()
    db.refresh(art)
    logger.debug("IGDB: saved cover_v for release %d from IGDB game %d", release.id, igdb_game_id)
    return art


# ---------------------------------------------------------------------------
# Credential test
# ---------------------------------------------------------------------------


def test_credentials(client_id: str, client_secret: str) -> tuple[bool, str]:
    """Try fetching a token and making a trivial API call.

    Returns (ok: bool, message: str).
    """
    try:
        token = get_token(client_id, client_secret)
    except httpx.HTTPStatusError as e:
        return False, f"Token fetch failed: {e.response.status_code}"
    except Exception as e:
        return False, f"Token fetch error: {e}"

    try:
        resp = httpx.post(
            f"{_IGDB_BASE}/games",
            headers=_igdb_headers(client_id, token),
            content="fields id, name; where id = 1942; limit 1;",  # The Witcher 3
            timeout=10,
        )
        resp.raise_for_status()
        games = resp.json()
        if games:
            return True, f"Connected — IGDB is responding (found: {games[0]['name']})"
        return True, "Connected — IGDB is responding"
    except httpx.HTTPStatusError as e:
        return False, f"IGDB query failed: {e.response.status_code}"
    except Exception as e:
        return False, f"IGDB query error: {e}"
