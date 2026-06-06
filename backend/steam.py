import datetime
import html
import json
import logging
import os
import re
import time

import httpx
from sqlalchemy import Integer, cast
from sqlalchemy.orm import Session, joinedload

from . import models

# Symbols that Steam appends to titles but are meaningless for display
_JUNK_RE = re.compile(r"[™®©]+")

# Collection-detection regex.  Only word-boundary matches to avoid false
# positives from words like "Recollection" or "Legacy".  "collection" is
# anchored to end-of-title (optionally followed by a volume indicator like
# "Vol.1") so "Master Collection Vol.1 Bonus Content" is NOT flagged — the
# trailing words push it past the anchor.
_COLLECTION_RE = re.compile(
    r"""
    \btrilogy\b |
    \bcompilation\b |
    \bcomplete\s+pack\b |
    # "collection" only qualifies at/near end of title — e.g. "Mega Man
    # Legacy Collection" yes, "Post Modern Collection" (DLC) handled by
    # the is_dlc guard in _infer_is_collection.
    \bcollection\b ( \s* (vol\.?\s*\d+ | volume\s+\d+) )? \s* $
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Explicit allowlist of acronyms and Roman numerals to preserve as uppercase
# during title-case normalization. Length-based "looks like an acronym" rules
# produced false positives (OF, OPS, etc. — short English words that happen to
# render in all caps when shouting). Better to leave new acronyms title-cased
# and let the user fix them manually (display_name_user_set protects the edit).
_PRESERVE_UPPER = {
    # Roman numerals I–XX
    "I",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "VII",
    "VIII",
    "IX",
    "X",
    "XI",
    "XII",
    "XIII",
    "XIV",
    "XV",
    "XVI",
    "XVII",
    "XVIII",
    "XIX",
    "XX",
    # Common gaming acronyms / franchise IDs
    "GTA",
    "FTL",
    "RE",
    "MGS",
    "COD",
    "BFG",
    "FPS",
    "RPG",
    "MMO",
    "MMORPG",
    "JRPG",
    "ARPG",
    "VR",
    "AR",
    "AI",
    "HD",
    "UHD",
    "DLC",
    "OST",
    "GOTY",
    "NPC",
    "HUD",
    "UI",
    "PVE",
    "PVP",
    "PUBG",
    "TES",
    "GTAV",
}


def _is_loud_caps(s: str) -> bool:
    """True when a string looks like SHOUTING that should be title-cased.
    Single-word and short titles (DOOM, FTL) are left alone."""
    if len(s) < 8 or " " not in s:
        return False
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) > 0.95


def _smart_title_case(s: str) -> str:
    """Title-case a string while preserving listed acronyms / Roman numerals
    and apostrophe contractions ("Assassin's" not "Assassin'S"). Idempotent."""
    out = []
    for word in s.split(" "):
        alpha = "".join(c for c in word if c.isalpha())
        if word.isupper() and alpha and alpha in _PRESERVE_UPPER:
            out.append(word)
        else:
            tc = word.title()
            # str.title() does "Don'T" — lowercase the letter after apostrophe
            tc = re.sub(r"'(\w)", lambda m: "'" + m.group(1).lower(), tc)
            out.append(tc)
    return " ".join(out)


def _infer_is_collection(title: str, is_dlc: bool = False) -> bool:
    """DLC can never be a collection regardless of title keywords."""
    if is_dlc:
        return False
    return bool(_COLLECTION_RE.search(title or ""))


# DLC-only patterns: cosmetic packs, season passes, artbooks, soundtracks,
# etc.  These are gated on is_dlc=True in _should_auto_hide — a real game
# won't have "Skin Pack" or "Season Pass" in its title in a way we'd hide.
# Trying to detect individual character names ("Mileena") is too brittle;
# we leave those for manual hide.
_AUTO_HIDE_DLC_RE = re.compile(
    r"\b("
    # Audio / book / wallpaper content
    r"soundtrack|ost|original\s+sound(track)?|"
    r"art\s*book|wallpapers?(\s*set)?|"
    # Standalone cosmetic items — word alone is enough; the *pack variants
    # below also catch compound forms like "Skin Pack" / "Costume Pack".
    r"skin|costume|outfit|"
    # Generic DLC pack / pass suffixes.
    # "pack" catches "Relic Rune Pack", "Starter Pack", etc.
    # "pass" catches "Year One Pass", "Battle Pass", "Annual Pass", etc.
    r"pack|pass|"
    r"cosmetic\s*pack|emotes?\s*pack|customization(\s+item)?\s*pack|"
    r"(skin|costume|outfit)\s*pack|cinematic\s*pack|"
    # Pass-suffix DLC (heavy in fighting games)
    r"(season|character|ultimate|stage|kombat)\s*pass|"
    r"character\s*\&\s*stage\s*pass|"
    # Avatar / profile cosmetics
    r"avatar\s*(skin|costume)|"
    # "DLC Playable Character" (Inti Creates pattern)
    r"dlc\s*playable\s*character|"
    # "Ultimate Add-On Bundle" / "Bonus Content" (purchase wrappers)
    r"add[- ]?on\s*bundle|"
    r"bonus\s+content|"
    # "Deluxe Edition Upgrade" pattern
    r"deluxe.*upgrade"
    r")\b",
    re.IGNORECASE,
)

# Title patterns that auto-hide regardless of is_dlc.
# - beta: real games don't ship with "beta" in the title.
# - public test: same idea as beta; "Public Test" is Steam's label for
#   opt-in beta branches surfaced as separate library entries.
# - friend's pass: Steam reports these as type=game, but they are co-op
#   access tokens, not completable games. Must be Tier 1 (gate-free) because
#   the DLC-only tier never fires when is_dlc=False.
_AUTO_HIDE_TITLE_RE = re.compile(r"\bbeta\b|\bpublic\s+test\b|\bfriend'?s\s+pass\b", re.IGNORECASE)

# Steam app types that are gate-free auto-hidden (no is_dlc check needed).
# If Steam tags something as one of these types there is no completable game
# inside it — hide unconditionally regardless of is_dlc.
_AUTO_HIDE_TYPES_GATE_FREE: frozenset[str] = frozenset({"music", "video", "episode"})

# Steam type "beta" always hides.
# Steam type "demo" hides only when "demo" also appears in the title (to
# preserve edge cases like "RE7: Beginning Hour" that Steam tags demo but
# that users may legitimately want to log).
_AUTO_HIDE_DEMO_TITLE_RE = re.compile(r"\bdemo\b", re.IGNORECASE)


def _should_auto_hide(title: str, appdetails: dict | None, is_dlc: bool) -> bool:
    """True if this entry should be hidden as non-completable content.

    Two-tier check:

    Tier 1 — gate-free (fires regardless of is_dlc):
      * app type is music / video / episode → always hide.
      * app type is beta → always hide.
      * app type is demo AND "demo" appears in the title → hide (keeps
        edge-cases like RE7: Beginning Hour visible if not named "demo").
      * title contains \\bbeta\\b → hide (catches beta builds Steam mislabels
        as type=game, e.g. "Elden Ring NIGHTREIGN Network Test Beta").

    Tier 2 — DLC-only (fires only when is_dlc=True):
      * title matches _AUTO_HIDE_DLC_RE (soundtracks, cosmetic packs,
        season passes, bonus content, deluxe upgrades, etc.).
    """
    app_type = (appdetails or {}).get("type", "")

    # --- Tier 1: gate-free (fire regardless of is_dlc) ---
    if app_type in _AUTO_HIDE_TYPES_GATE_FREE:
        return True
    if app_type == "beta":
        return True
    if app_type == "demo" and _AUTO_HIDE_DEMO_TITLE_RE.search(title or ""):
        return True
    if _AUTO_HIDE_TITLE_RE.search(title or ""):
        return True

    # --- Tier 2: DLC-only ---
    if not is_dlc:
        return False
    return bool(_AUTO_HIDE_DLC_RE.search(title or ""))


def _clean_title(title: str) -> str:
    """Return title with HTML entities unescaped, trademark/copyright symbols
    stripped, and whitespace normalised. Idempotent.

    Steam's name catalog sometimes includes HTML-encoded characters
    (e.g. ``&amp;`` for ``&``, ``&quot;`` for ``"``).  We unescape those
    before stripping junk so display_name shows clean text.

    We used to also title-case loud ALL-CAPS titles ("ELDEN RING NIGHTREIGN"
    → "Elden Ring Nightreign") but that produced inconsistent results when
    titles mixed cases (only whole-string ALL CAPS triggered, so DLC names
    like "ELDEN RING NIGHTREIGN The Forsaken Hollows" passed through
    unchanged). Decision: leave Steam's casing alone. If a user dislikes a
    SHOUTING title, the edit modal lets them override display_name."""
    return _JUNK_RE.sub("", html.unescape(title)).strip()


logger = logging.getLogger(__name__)

STEAM_API_BASE = "https://api.steampowered.com"
STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps"

# Identify ourselves on every Steam request — Steam has been seen returning
# 404s to default Python User-Agents on some endpoints.
_HEADERS = {"User-Agent": "completed-games-tracker/1.0 (+https://github.com/barryjj/completed-games-tracker-fastapi)"}

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
    response = httpx.get(url, params=params, headers=_HEADERS, timeout=15)
    response.raise_for_status()
    data = response.json()
    games = data.get("response", {}).get("games", [])
    games.sort(key=lambda g: g.get("name", "").lower())
    return games


def get_app_list(api_key: str) -> dict[int, str]:
    """
    Return {appid: name} for every app on Steam.
    Cached to disk for 7 days; loaded into memory for the server's lifetime.

    Steam moved this endpoint: the old unauthenticated
    `ISteamApps/GetAppList/v2/` was deprecated in favor of
    `IStoreService/GetAppList/v1/`, which requires the API key and is paginated.
    Page size cap is 50,000; the full catalog is ~200k apps so this is ~4 calls.
    """
    global _app_list_memory, _app_list_cached_at
    now = datetime.datetime.now(datetime.UTC)

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

    # Fetch from Steam (paginated)
    logger.info("Fetching Steam app list from IStoreService/GetAppList...")
    app_dict: dict[int, str] = {}
    last_appid = 0
    page = 0
    while True:
        params = {
            "key": api_key,
            "max_results": 50000,
            "include_games": "true",
            "include_dlc": "true",
            "include_software": "true",
            "include_hardware": "false",
            "include_videos": "false",
        }
        if last_appid:
            params["last_appid"] = last_appid

        resp = httpx.get(
            f"{STEAM_API_BASE}/IStoreService/GetAppList/v1/",
            params=params,
            headers=_HEADERS,
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json().get("response", {})
        apps = body.get("apps", [])
        if not apps:
            break
        for a in apps:
            app_dict[a["appid"]] = a.get("name", "")
        page += 1
        logger.info("GetAppList page %d: +%d apps (total %d)", page, len(apps), len(app_dict))

        if not body.get("have_more_results"):
            break
        last_appid = body.get("last_appid", 0)
        if not last_appid:
            # Defensive: avoid infinite loop if Steam ever omits last_appid
            break

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
        "cover_h": f"{STEAM_CDN}/{appid}/header.jpg",
        "cover_v": f"{STEAM_CDN}/{appid}/library_600x900.jpg",
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
        last_played = datetime.datetime.fromtimestamp(last_played_ts, tz=datetime.UTC) if last_played_ts else None

        release = db.query(models.GameRelease).filter_by(source="steam", external_id=appid).first()

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
                # Heuristic skip: respect user override on display_name.
                if not game.display_name_user_set and game.display_name is None and cleaned != title:
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
                platform_id=models.resolve_platform_id(db, "Steam"),
                source="steam",
                external_id=appid,
                raw_data=g,
            )
            db.add(release)
            db.flush()

            for artwork_type in ("cover_h", "cover_v", "hero"):
                url = _artwork_url(int(appid), artwork_type)
                if url:
                    db.add(
                        models.GameArtwork(
                            release_id=release.id,
                            artwork_type=artwork_type,
                            source="steam",
                            url=url,
                        )
                    )
        else:
            raw = dict(release.raw_data or {})
            raw.update(g)
            release.raw_data = raw

        entry = db.query(models.UserLibraryEntry).filter_by(user_id=user.id, release_id=release.id).first()

        if entry is None:
            db.add(
                models.UserLibraryEntry(
                    user_id=user.id,
                    release_id=release.id,
                    playtime_minutes=playtime,
                    last_played_at=last_played,
                    import_source="steam_import",
                )
            )
            added += 1
        else:
            entry.playtime_minutes = playtime
            entry.last_played_at = last_played
            entry.updated_at = datetime.datetime.now(datetime.UTC)
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
        row[0] for row in db.query(models.UserLibraryEntry.release_id).filter(models.UserLibraryEntry.user_id == user.id).all()
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
                if not release.game.is_collection_user_set:
                    release.game.is_collection = False
                newly_marked += 1
            if release.id not in user_release_ids:
                db.add(
                    models.UserLibraryEntry(
                        user_id=user.id,
                        release_id=release.id,
                        import_source="steam_import",
                    )
                )
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
                platform_id=models.resolve_platform_id(db, "Steam"),
                source="steam",
                external_id=appid_str,
            )
            db.add(release)
            db.flush()

            for artwork_type in ("cover_h", "cover_v", "hero"):
                url = _artwork_url(appid, artwork_type)
                if url:
                    db.add(
                        models.GameArtwork(
                            release_id=release.id,
                            artwork_type=artwork_type,
                            source="steam",
                            url=url,
                        )
                    )

            db.add(
                models.UserLibraryEntry(
                    user_id=user.id,
                    release_id=release.id,
                    import_source="steam_import",
                )
            )
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
    user.steam_last_synced_at = datetime.datetime.now(datetime.UTC)
    db.commit()

    # Map old keys for backward compat
    return {"added": result["games_added"], "updated": result["games_updated"], "total": result["games_total"]}


def _fetch_owned_appids(user: models.User) -> set[int]:
    """Hit dynamicstore/userdata/ with the user's session cookies and return the
    set of every appid they own (games + DLC + tools + everything)."""
    if not user.steam_session_id or not user.steam_login_secure:
        raise ValueError("Browser cookies (sessionid + steamLoginSecure) are required.")
    resp = httpx.get(
        "https://store.steampowered.com/dynamicstore/userdata/",
        cookies={
            "sessionid": user.steam_session_id,
            "steamLoginSecure": user.steam_login_secure,
        },
        headers=_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return set(resp.json().get("rgOwnedApps", []))


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
    all_owned = _fetch_owned_appids(user)

    # DLC = apps owned but not in the games list
    dlc_appids = all_owned - game_appids
    logger.info("Found %d owned apps, %d games, %d DLC", len(all_owned), len(game_appids), len(dlc_appids))

    # 3. App name index
    app_names = get_app_list(user.steam_api_key)

    # 4. Import DLC
    dlc_result = _import_dlc(db, user, dlc_appids, app_names)

    now = datetime.datetime.now(datetime.UTC)
    user.steam_last_synced_at = now
    user.steam_last_dlc_synced_at = now
    db.commit()

    return {**game_result, **dlc_result}


def sync_dlc_only(db: Session, user: models.User) -> dict:
    """
    Diagnostic: refresh DLC ownership without touching games. Uses the already-
    synced games in the DB as the baseline (no GetOwnedGames call), so this only
    needs cookies + the GetAppList catalog. Useful when you've already synced
    your games and only want to refresh DLC after Steam updates the catalog.
    """
    if not user.steam_api_key or not user.steam_id64:
        raise ValueError("Steam API key and Steam ID64 are required.")
    if not user.steam_session_id or not user.steam_login_secure:
        raise ValueError("Browser cookies (sessionid + steamLoginSecure) are required.")

    # Existing Steam games in the user's library — those appids are "games", not DLC
    rows = (
        db.query(models.GameRelease.external_id)
        .join(models.UserLibraryEntry)
        .join(models.Game)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.source == "steam",
            models.Game.is_dlc == False,
        )
        .all()
    )
    game_appids = {int(r[0]) for r in rows if r[0] and r[0].isdigit()}

    all_owned = _fetch_owned_appids(user)
    dlc_appids = all_owned - game_appids
    logger.info("DLC-only sync: %d owned, %d known games, %d DLC", len(all_owned), len(game_appids), len(dlc_appids))

    app_names = get_app_list(user.steam_api_key)
    dlc_result = _import_dlc(db, user, dlc_appids, app_names)

    user.steam_last_dlc_synced_at = datetime.datetime.now(datetime.UTC)
    db.commit()

    return dlc_result


def refresh_app_catalog(api_key: str) -> dict:
    """Force a re-fetch of the GetAppList catalog by invalidating both the
    memory and disk caches. Returns the new count."""
    global _app_list_memory, _app_list_cached_at
    _app_list_memory = {}
    _app_list_cached_at = None
    try:
        if os.path.exists(_APP_LIST_CACHE_PATH):
            os.remove(_APP_LIST_CACHE_PATH)
    except OSError as e:
        logger.warning("Could not remove app list cache file: %s", e)

    app_dict = get_app_list(api_key)
    return {"app_count": len(app_dict)}


def _sync_header_artwork_from_appdetails(db: Session, release: "models.GameRelease", details: dict) -> None:
    """If appdetails contains a header_image URL, persist it as the release's
    'cover_h' GameArtwork row (creating or updating as needed). This fixes
    coverage gaps for DLC entries whose legacy CDN URL 404s on the new
    hashed-path Steam assets."""
    header_url = (details or {}).get("header_image")
    if not header_url:
        return
    for art in release.artwork:
        if art.artwork_type == "cover_h":
            if art.url != header_url:
                art.url = header_url
                art.source = "steam"
                # Reset validation state — the URL changed, so any prior
                # is_valid=False from the verification pass is stale.
                art.is_valid = True
                art.verified_at = None
            return
    # No cover_h row yet — create one
    db.add(
        models.GameArtwork(
            release_id=release.id,
            artwork_type="cover_h",
            source="steam",
            url=header_url,
        )
    )


def _promote_dlc_children(db: Session, parent_game_id: int, dlc_appids: list[int]) -> None:
    """Cross-reference a parent game's dlc[] array against our library.

    Steam includes a `dlc` list on the parent game's appdetails even when the
    child entries return type=game themselves (e.g. standalone expansions like
    DOOM Eternal: The Ancient Gods).  When we see this list we can mark owned
    children as DLC and link their parent_id without waiting for the child's
    own appdetails to say type=dlc.

    Respects is_dlc_user_set and parent_id_user_set so manual overrides win.
    """
    for appid in dlc_appids:
        child_release = db.query(models.GameRelease).filter_by(source="steam", external_id=str(appid)).first()
        if not child_release:
            continue
        child_game = child_release.game
        if not child_game.is_dlc_user_set and not child_game.is_dlc:
            child_game.is_dlc = True
            if not child_game.is_collection_user_set:
                child_game.is_collection = False
        if not child_game.parent_id_user_set and child_game.parent_id is None:
            child_game.parent_id = parent_game_id


_STEAMSPY_BASE = "https://steamspy.com/api.php"
_STEAMSPY_SLEEP = 0.7  # stay under their 1 req/sec guideline


def _lookup_steamspy_name(appid: str) -> str | None:
    """Look up a game name from SteamSpy by Steam appid.

    SteamSpy caches data from Steam directly and retains it for delisted apps,
    making it a reliable fallback when Steam's own appdetails returns success=false.
    Returns the name string on success, None if not found or on error.
    """
    try:
        resp = httpx.get(
            _STEAMSPY_BASE,
            params={"request": "appdetails", "appid": appid},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        name = (data.get("name") or "").strip()
        return name if name else None
    except Exception as e:
        logger.debug("SteamSpy lookup failed for appid %s: %s", appid, e)
        return None


def backfill_steamspy_names(
    db: Session,
    user: models.User,
    on_progress=None,
) -> dict:
    """Fix 'App NNNNNN' placeholder titles using SteamSpy.

    Finds all distinct games in the user's Steam library whose title is still
    the sync-time fallback (App + 5+ digit appid) and queries SteamSpy for the
    real name.  Only processes entries the user hasn't manually named
    (display_name_user_set=False).

    Calls on_progress(done, total, current_title) if supplied.
    Returns {"fixed": N, "not_found": M, "total": P}.
    """
    # Distinct (game_id, external_id) pairs — one SteamSpy call per unique game.
    pairs = (
        db.query(models.Game.id, models.GameRelease.external_id)
        .join(models.GameRelease, models.GameRelease.game_id == models.Game.id)
        .join(
            models.UserLibraryEntry,
            models.UserLibraryEntry.release_id == models.GameRelease.id,
        )
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.source == "steam",
            models.GameRelease.external_id.isnot(None),
            models.Game.title.op("GLOB")("App [0-9][0-9][0-9][0-9][0-9]*"),
            models.Game.display_name_user_set == False,
        )
        .distinct()
        .all()
    )

    # Deduplicate: one lookup per unique game_id (take first external_id seen).
    seen: dict[int, str] = {}
    for game_id, ext_id in pairs:
        if game_id not in seen:
            seen[game_id] = ext_id

    total = len(seen)
    fixed = 0
    not_found = 0

    for done, (game_id, appid) in enumerate(seen.items(), start=1):
        game = db.query(models.Game).filter(models.Game.id == game_id).first()
        if game is None:
            continue

        if on_progress:
            on_progress(done, total, game.title)

        spy_name = _lookup_steamspy_name(appid)
        if spy_name:
            game.title = spy_name
            cleaned = _clean_title(spy_name)
            game.display_name = cleaned if cleaned != spy_name else None
            db.commit()
            fixed += 1
            logger.debug("SteamSpy: fixed App %s → %s", appid, spy_name)
        else:
            not_found += 1

        time.sleep(_STEAMSPY_SLEEP)

    return {"fixed": fixed, "not_found": not_found, "total": total}


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
        headers=_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json().get(str(appid), {})
    if result.get("success"):
        return result.get("data", {})
    return None


# Sleep durations for the enrichment worker. Steam's appdetails endpoint is
# documented at roughly 200 requests per 5 minutes (~1 request every 1.5s).
# We use a safety-margined steady-state sleep and a much longer backoff on 429.
_ENRICH_SLEEP_OK = 2.0  # normal pace between successful requests
_ENRICH_SLEEP_429 = 60.0  # how long to wait when Steam rate-limits us


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

    now = datetime.datetime.now(datetime.UTC)

    for release in entries:
        try:
            details = _fetch_appdetails(int(release.external_id))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning(
                    "Steam rate-limited (429) for appid %s — backing off %.0fs",
                    release.external_id,
                    _ENRICH_SLEEP_429,
                )
                time.sleep(_ENRICH_SLEEP_429)
                return _pending_count(db)  # bail out of this batch; loop again later
            logger.warning(
                "appdetails fetch failed for appid %s (transient, will retry): %s",
                release.external_id,
                e,
            )
            time.sleep(_ENRICH_SLEEP_OK)
            continue
        except Exception as e:
            logger.warning(
                "appdetails fetch failed for appid %s (transient, will retry): %s",
                release.external_id,
                e,
            )
            time.sleep(_ENRICH_SLEEP_OK)
            continue

        if details is None:
            # Steam confirmed unavailable (success=false). Try SteamSpy for
            # the name if the title is still an "App NNNNNN" placeholder so
            # delisted/obscure apps get a human-readable title.
            game = release.game
            _is_placeholder = game.title.startswith("App ") and game.title[4:].strip().isdigit() and len(game.title[4:].strip()) >= 5
            if _is_placeholder and not game.display_name_user_set:
                spy_name = _lookup_steamspy_name(release.external_id)
                if spy_name:
                    game.title = spy_name
                    cleaned = _clean_title(spy_name)
                    game.display_name = cleaned if cleaned != spy_name else None
                    logger.debug(
                        "SteamSpy fallback: App %s → %s",
                        release.external_id,
                        spy_name,
                    )

        if details is not None:
            raw = dict(release.raw_data or {})
            raw["appdetails"] = details
            raw["appdetails_type"] = details.get("type", "game")
            release.raw_data = raw

            game = release.game
            app_type = details.get("type", "game")

            # Title backfill from appdetails. Sync-time fallback when an
            # appid wasn't in the Steam catalog cache stamps the title as
            # f"App {appid}" — appdetails has the real name (DLC's "Deluxe
            # Upgrade Pack" etc.), so use it once we have it. Respect manual
            # overrides via display_name_user_set.
            real_name = (details.get("name") or "").strip()
            if real_name and game.title.startswith("App ") and game.title[4:].strip().isdigit():
                game.title = real_name
                if not game.display_name_user_set:
                    cleaned = _clean_title(real_name)
                    game.display_name = cleaned if cleaned != real_name else None

            # is_dlc reconciliation in BOTH directions:
            #   appdetails type=dlc  + is_dlc=False → promote True
            #   appdetails type=game + is_dlc=True  → demote False
            # Either direction respects is_dlc_user_set so manual overrides win.
            if not game.is_dlc_user_set:
                if app_type == "dlc" and not game.is_dlc:
                    game.is_dlc = True
                    if not game.is_collection_user_set:
                        game.is_collection = False
                elif app_type == "game" and game.is_dlc:
                    # Steam sometimes tags season passes and bundle wrappers as
                    # type=game even though they are not completable games.
                    # Three signals mean "definitely DLC, don't demote":
                    #   1. game.parent_id set → already resolved to a parent in
                    #      our DB; strongest signal, beats the appdetails type.
                    #   2. appdetails has a fullgame object → Steam itself links
                    #      it to a parent game.
                    #   3. Title matches auto-hide patterns (pass, pack, etc.)
                    #      → purchase wrapper Steam mislabels as a game.
                    # Otherwise trust appdetails when it says "game".
                    has_parent = game.parent_id is not None
                    has_fullgame = bool((details.get("fullgame") or {}).get("appid"))
                    looks_like_dlc = _should_auto_hide(game.title, details, is_dlc=True)
                    if not has_parent and not has_fullgame and not looks_like_dlc:
                        game.is_dlc = False
                elif app_type == "game" and not game.is_dlc:
                    # Re-promote entries previously demoted before the guard
                    # above existed. If fullgame is present or the title matches
                    # auto-hide patterns, Steam mislabelled this — flip it back
                    # to DLC so auto-hide can fire.
                    has_fullgame = bool((details.get("fullgame") or {}).get("appid"))
                    looks_like_dlc = _should_auto_hide(game.title, details, is_dlc=True)
                    if has_fullgame or looks_like_dlc:
                        game.is_dlc = True
                        if not game.is_collection_user_set:
                            game.is_collection = False

            # Link DLC to its base game if not already linked — but respect
            # user override on parent_id.
            if app_type == "dlc" and game.parent_id is None and not game.parent_id_user_set:
                fullgame = details.get("fullgame", {})
                parent_appid = str(fullgame.get("appid", "")).strip()
                if parent_appid:
                    parent_release = db.query(models.GameRelease).filter_by(source="steam", external_id=parent_appid).first()
                    if parent_release:
                        game.parent_id = parent_release.game_id

            # Push DLC status down to children listed in the parent's dlc[].
            # Steam returns type=game on some standalone expansions (e.g. DOOM
            # Eternal: The Ancient Gods) even though the parent lists them as
            # DLC — so we need to look at the parent side, not just the child.
            if app_type == "game":
                dlc_appids = details.get("dlc") or []
                if dlc_appids:
                    _promote_dlc_children(db, game.id, dlc_appids)

            # Auto-hide soundtracks / artbooks / cosmetic packs etc.
            # GATED on is_dlc=True — games are never auto-hidden.
            if _should_auto_hide(game.title, details, game.is_dlc):
                for entry in release.library_entries:
                    if not entry.is_hidden and not entry.is_hidden_user_set:
                        entry.is_hidden = True

            # Use appdetails' header_image URL when available — Steam migrated
            # some assets (newer DLC especially) to hashed paths on
            # shared.fastly.steamstatic.com that our legacy constructed
            # cdn.akamai.steamstatic.com URL doesn't match. appdetails returns
            # the actual current URL.
            _sync_header_artwork_from_appdetails(db, release, details)

        release.metadata_fetched_at = now
        db.commit()
        time.sleep(_ENRICH_SLEEP_OK)

    return _pending_count(db)


def _pending_count(db: Session) -> int:
    return (
        db.query(models.GameRelease)
        .filter(
            models.GameRelease.source == "steam",
            models.GameRelease.metadata_fetched_at == None,
        )
        .count()
    )


# How old metadata must be before the idle worker re-queues it.
_METADATA_STALE_DAYS = 30
# How many entries to re-queue per idle cycle. At _ENRICH_SLEEP_OK per entry
# this is ~100s of work — well within Steam's rate limits.
_METADATA_REQUEUE_BATCH = 50


def requeue_stale_metadata(db: Session) -> int:
    """
    Re-queue Steam entries whose metadata is older than _METADATA_STALE_DAYS.

    Called by the enrichment worker when the NULL queue is fully drained (idle).
    Orders by appid DESC so recently released games — most likely to have
    changing URLs, titles, or CDN paths — cycle through first. Older catalog
    entries drift to the back naturally and still get refreshed, just later.

    Returns the number of entries re-queued (0 means nothing stale yet).
    """
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=_METADATA_STALE_DAYS)
    releases = (
        db.query(models.GameRelease)
        .filter(
            models.GameRelease.source == "steam",
            models.GameRelease.metadata_fetched_at != None,
            models.GameRelease.metadata_fetched_at < cutoff,
        )
        .order_by(cast(models.GameRelease.external_id, Integer).desc())
        .limit(_METADATA_REQUEUE_BATCH)
        .all()
    )
    for release in releases:
        release.metadata_fetched_at = None
    if releases:
        db.commit()
        logger.debug(
            "Enrichment idle: re-queued %d stale entries (oldest cutoff %s)",
            len(releases),
            cutoff.strftime("%Y-%m-%d"),
        )
    return len(releases)


# ---------------------------------------------------------------------------
# Artwork URL verification
# ---------------------------------------------------------------------------

_VERIFY_SLEEP_OK = 0.5  # between successful HEAD requests
_VERIFY_SLEEP_ERR = 2.0  # after a network error (transient)
_verify_logger = logging.getLogger("steam.artwork_verification")


def verify_artwork_batch(db: Session, batch_size: int = 50) -> int:
    """
    HEAD-check the next batch of GameArtwork rows that haven't been verified yet.

    - HTTP 2xx/3xx → is_valid=True
    - HTTP 4xx (404, 410, …) → is_valid=False  (URL is definitively broken)
    - Network / 5xx errors → skip; leave verified_at null so we retry next cycle.

    Returns the count of rows still pending verification (verified_at IS NULL).
    """
    # Only verify native platform sources (Steam CDN, PSN, etc.).
    # SGDB art lives in UserArtwork now; old GameArtwork rows with source="sgdb"
    # would hammer SGDB's rate limit for no benefit — skip them.
    _NATIVE_SOURCES = ("steam", "psn")
    rows = (
        db.query(models.GameArtwork)
        .filter(
            models.GameArtwork.verified_at == None,
            models.GameArtwork.source.in_(_NATIVE_SOURCES),
        )
        .order_by(models.GameArtwork.id.asc())
        .limit(batch_size)
        .all()
    )

    now = datetime.datetime.now(datetime.UTC)

    for art in rows:
        if not art.url:
            # No URL to check — stamp it so we don't keep visiting it.
            art.verified_at = now
            art.is_valid = False
            db.commit()
            continue

        try:
            resp = httpx.head(art.url, follow_redirects=True, timeout=10)
            if resp.status_code < 400:
                art.is_valid = True
            else:
                art.is_valid = False
                _verify_logger.debug(
                    "Artwork %d marked invalid: %s → HTTP %d",
                    art.id,
                    art.url,
                    resp.status_code,
                )
            art.verified_at = now
            db.commit()
            time.sleep(_VERIFY_SLEEP_OK)
        except Exception as e:
            _verify_logger.debug(
                "Artwork %d HEAD check failed (transient, will retry): %s",
                art.id,
                e,
            )
            time.sleep(_VERIFY_SLEEP_ERR)
            continue

    return _artwork_pending_count(db)


def _artwork_pending_count(db: Session) -> int:
    return (
        db.query(models.GameArtwork)
        .filter(
            models.GameArtwork.verified_at == None,
            models.GameArtwork.source.in_(("steam", "psn")),
        )
        .count()
    )


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
            time.sleep(_ENRICH_SLEEP_OK)
            continue
        time.sleep(_ENRICH_SLEEP_OK)

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
            parent_release = db.query(models.GameRelease).filter_by(source="steam", external_id=parent_appid).first()
            if parent_release:
                game.parent_id = parent_release.game_id
                linked += 1

    user.steam_last_dlc_synced_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return {"checked": checked, "found_dlc": found_dlc, "linked": linked}
