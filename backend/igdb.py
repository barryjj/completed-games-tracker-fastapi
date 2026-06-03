"""IGDB / Twitch API integration.

Authentication uses the Twitch Client Credentials flow — no user login required.
The access token is fetched on demand and cached in-process (expires after 1 hour;
we re-fetch when the cache is stale rather than implementing refresh tokens since
client credentials tokens are cheap to replace).

Public API surface:
  get_token(client_id, client_secret)  → bearer token string (cached)
  search_games(client_id, secret, query, limit) → list of game dicts
  fetch_cover_url(client_id, secret, igdb_game_id) → cover URL or None
  save_igdb_cover(db, entry, igdb_game_id, client_id, secret) → GameArtwork or None
  fetch_game_details(client_id, secret, igdb_game_id) → metadata dict
  save_igdb_metadata(db, release, igdb_game_id, client_id, secret) → None
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
        f"where version_parent = null; "  # exclude regional/hardware variants from search
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
# Direct game lookup (by ID)
# ---------------------------------------------------------------------------


def fetch_game_brief(
    client_id: str,
    client_secret: str,
    igdb_game_id: int,
) -> dict | None:
    """Fetch name/cover/platforms for a single game by IGDB ID.

    Returns a dict with the same shape as each item in search_games results:
    {id, name, cover_url, platforms, year}.  Returns None if not found.
    Used by the "paste ID → Lookup" flow so users can confirm a game before linking.
    """
    token = get_token(client_id, client_secret)
    body = f"fields id, name, cover.url, platforms.name, first_release_date; where id = {igdb_game_id}; limit 1;"
    resp = httpx.post(
        f"{_IGDB_BASE}/games",
        headers=_igdb_headers(client_id, token),
        content=body,
        timeout=10,
    )
    resp.raise_for_status()
    games = resp.json()
    if not games:
        return None
    g = games[0]

    cover_url = None
    raw_cover = g.get("cover")
    if raw_cover and raw_cover.get("url"):
        cover_url = _igdb_image_url(raw_cover["url"], "t_cover_big")

    year = None
    ts = g.get("first_release_date")
    if ts:
        year = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).year

    platforms = [p["name"] for p in (g.get("platforms") or [])]

    return {
        "id": g["id"],
        "name": g["name"],
        "cover_url": cover_url,
        "platforms": platforms,
        "year": year,
    }


# ---------------------------------------------------------------------------
# Platform list
# ---------------------------------------------------------------------------

# IGDB platform category IDs → human-readable label.
_PLATFORM_CATEGORIES = {
    1: "console",
    2: "arcade",
    3: "platform",
    4: "operating_system",
    5: "portable_console",
    6: "computer",
}


def fetch_platforms(client_id: str, client_secret: str) -> list[dict]:
    """Return IGDB's full platform list sorted by id.

    Each dict has: id, name, category (human label), generation (int or None).
    Fetches up to 500 rows — IGDB's platform catalogue is well under that.
    """
    token = get_token(client_id, client_secret)
    body = "fields id, name, category, generation; sort id asc; limit 500;"
    resp = httpx.post(
        f"{_IGDB_BASE}/platforms",
        headers=_igdb_headers(client_id, token),
        content=body,
        timeout=15,
    )
    resp.raise_for_status()
    platforms = resp.json()
    return [
        {
            "id": p["id"],
            "name": p.get("name", ""),
            "category": _PLATFORM_CATEGORIES.get(p.get("category"), "unknown"),
            "generation": p.get("generation"),
        }
        for p in platforms
    ]


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
# Rich metadata
# ---------------------------------------------------------------------------


def fetch_game_details(
    client_id: str,
    client_secret: str,
    igdb_game_id: int,
) -> dict:
    """Fetch rich metadata for a single IGDB game.

    Returns a dict with keys:
        summary      – plain-text description (may be "")
        genres       – list of genre name strings (may be [])
        year         – release year int or None
        artwork_urls – list of landscape artwork URLs (1080p, may be [])
    """
    token = get_token(client_id, client_secret)
    body = f"fields summary, genres.name, first_release_date, artworks.url; where id = {igdb_game_id}; limit 1;"
    resp = httpx.post(
        f"{_IGDB_BASE}/games",
        headers=_igdb_headers(client_id, token),
        content=body,
        timeout=10,
    )
    resp.raise_for_status()
    games = resp.json()
    if not games:
        return {}
    g = games[0]

    year = None
    ts = g.get("first_release_date")
    if ts:
        year = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).year

    genres = [genre["name"] for genre in (g.get("genres") or [])]

    artwork_urls = []
    for art in g.get("artworks") or []:
        if art.get("url"):
            artwork_urls.append(_igdb_image_url(art["url"], "t_1080p"))

    return {
        "summary": (g.get("summary") or "").strip(),
        "genres": genres,
        "year": year,
        "artwork_urls": artwork_urls,
    }


def save_igdb_metadata(
    db,
    release: "models.GameRelease",
    igdb_game_id: int,
    client_id: str,
    client_secret: str,
) -> None:
    """Fetch IGDB metadata for a game and persist it on the release.

    Writes:
      - summary  → release.description (only if not already set)
      - genres, year → release.raw_data['igdb']
      - first landscape artwork → GameArtwork(type='hero', source='igdb')

    Commits to DB if anything changed. Skips gracefully on empty IGDB response.
    """
    details = fetch_game_details(client_id, client_secret, igdb_game_id)
    if not details:
        return

    changed = False

    # Summary → release.description (don't overwrite user-provided descriptions).
    if details["summary"] and not release.description:
        release.description = details["summary"]
        changed = True

    # Genres + year → release.raw_data['igdb'].
    if details["genres"] or details["year"]:
        raw = dict(release.raw_data or {})
        igdb_block = dict(raw.get("igdb", {}))
        if details["genres"]:
            igdb_block["genres"] = details["genres"]
        if details["year"]:
            igdb_block["year"] = details["year"]
        raw["igdb"] = igdb_block
        release.raw_data = raw
        changed = True

    # First landscape artwork → hero GameArtwork.
    if details["artwork_urls"]:
        url = details["artwork_urls"][0]
        existing = next(
            (a for a in release.artwork if a.artwork_type == "hero" and a.source == "igdb"),
            None,
        )
        if existing:
            existing.url = url
            existing.is_valid = True
            existing.verified_at = datetime.datetime.now(datetime.UTC)
        else:
            db.add(
                models.GameArtwork(
                    release_id=release.id,
                    game_id=release.game_id,
                    artwork_type="hero",
                    source="igdb",
                    url=url,
                    is_valid=True,
                    verified_at=datetime.datetime.now(datetime.UTC),
                )
            )
        changed = True

    if changed:
        db.commit()
        db.refresh(release)
        logger.debug(
            "IGDB: saved metadata for release %d (igdb game %d) — summary=%s genres=%s hero=%s",
            release.id,
            igdb_game_id,
            bool(details["summary"]),
            details["genres"],
            bool(details["artwork_urls"]),
        )


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
