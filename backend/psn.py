"""PSN library crawl (issue #135).

PR 1 scope: fetch + merge + snapshot + report ONLY — no library writes. The
import path (creating Game/GameRelease/UserLibraryEntry rows from a snapshot)
is a separate, explicitly-triggered step landing in the follow-up PR.

Everything here is a Python port of the validated prototype at
~/Coding/psn-library-generator (Electron + psn-api): same auth chain, same
endpoints, same merge heuristics — with two deliberate upgrades the prototype
lacked: full pagination on every dataset (its played-games fetch returned 10
of an API-reported 147), and category filtering so media apps (e.g. "SONY
PICTURES CORE") never count as games. Whether Sony actually serves more than
the prototype saw is an open question — the report shows fetched vs
API-reported totals per dataset so the truth is visible either way.

Auth is derived fresh per run (NPSSO → access code → bearer token); nothing
is stored. Access tokens live ~1h, one run fits comfortably. A dead NPSSO
raises PsnNpssoExpiredError so the job layer can tag the failure for the
desktop shell's re-capture loop (mirrors steam.SteamCookiesExpiredError).
"""

import datetime
import json
import logging
import os
import re
import time
from collections import Counter
from urllib.parse import parse_qsl, urlparse

import httpx
from sqlalchemy.orm import Session

from . import models, titles

_logger = logging.getLogger(__name__)

# ─── Auth (params validated by the prototype; see memory/docs) ─────────────

_AUTHORIZE_URL = "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
_TOKEN_URL = "https://ca.account.sony.com/api/authz/v3/oauth/token"
# Public client id of the PlayStation Android app — same identity psn-api,
# psnawp, and the prototype authenticate as. The Basic header is that
# client id + its fixed public secret, base64-encoded (from psn-api source).
_CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
_REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
_TOKEN_BASIC_AUTH = "Basic MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="

_GRAPHQL_URL = "https://web.np.playstation.com/api/graphql/v1/op"
# Persisted-query hash for getPurchasedGameList (from psn-api dist source).
_PURCHASED_QUERY_HASH = "827a423f6a8ddca4107ac01395af2ec0eafd8396fc7fa204aaf9b7ed2eefa168"
_TROPHY_TITLES_URL = "https://m.np.playstation.com/api/trophy/v1/users/{account_id}/trophyTitles"
_PLAYED_URL = "https://m.np.playstation.com/api/gamelist/v2/users/{account_id}/titles"
_PROFILE_URL = "https://us-prof.np.community.playstation.net/userProfile/v1/users/{online_id}/profile2"

_PAGE_SLEEP_S = 0.2
_MAX_PAGES = 100  # hard stop so an API quirk can never loop forever

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))


class PsnNpssoExpiredError(ValueError):
    """The stored NPSSO no longer authenticates. Subclasses ValueError so the
    job runner's existing catch keeps working; caught specifically to tag the
    failure with error_code='psn_npsso_expired' for the desktop shell's
    auto-re-capture loop."""


def _exchange_npsso(npsso: str) -> str:
    """NPSSO → access code → bearer access token. Raises PsnNpssoExpiredError
    when PSN won't issue a code (expired/invalid NPSSO)."""
    resp = httpx.get(
        _AUTHORIZE_URL,
        params={
            "access_type": "offline",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": "psn:mobile.v2.core psn:clientapp",
        },
        cookies={"npsso": npsso},
        follow_redirects=False,
        timeout=30,
    )
    location = resp.headers.get("location", "")
    if "?code=" not in location:
        raise PsnNpssoExpiredError("PSN NPSSO token has expired — sign in to PlayStation again and re-capture it, then retry.")
    # urlparse handles the custom scheme and keeps the '?' out of the query —
    # the prototype's JS URLSearchParams stripped a leading '?' silently, but
    # Python's query parsers don't (a naive split shipped 'code=None' to Sony
    # as a 400 once already).
    code = dict(parse_qsl(urlparse(location).query)).get("code")
    if not code:
        raise ValueError("PSN authorize redirect carried no access code — unexpected response shape.")
    token_resp = httpx.post(
        _TOKEN_URL,
        headers={"Authorization": _TOKEN_BASIC_AUTH, "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "grant_type": "authorization_code",
            "token_format": "jwt",
        },
        timeout=30,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json().get("access_token")
    if not access_token:
        raise ValueError("PSN token exchange returned no access token.")
    return access_token


def _bearer_get(token: str, url: str, params: dict | None = None) -> dict:
    resp = httpx.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# profile2 returns avatarUrls as [{size, avatarUrl}]; sizes seen: s/m/l/xl.
_AVATAR_SIZE_RANK = {"xl": 4, "l": 3, "m": 2, "s": 1}


def _largest_avatar_url(profile: dict) -> str | None:
    best, best_rank = None, -1
    for a in profile.get("avatarUrls") or []:
        url = a.get("avatarUrl")
        rank = _AVATAR_SIZE_RANK.get(str(a.get("size", "")).lower(), 0)
        if url and rank > best_rank:
            best, best_rank = url, rank
    return best


def _resolve_profile(token: str, online_id: str) -> tuple[str, str | None]:
    """Online ID → (accountId, avatar_url) via the legacy profile2 endpoint.
    avatarUrls rides along on the same call — one extra field, no extra request."""
    data = _bearer_get(
        token,
        _PROFILE_URL.format(online_id=online_id),
        params={"fields": "npId,onlineId,accountId,avatarUrls"},
    )
    profile = data.get("profile") or {}
    account_id = profile.get("accountId")
    if not account_id:
        raise ValueError(f"Could not resolve PSN accountId for online id '{online_id}'.")
    return str(account_id), _largest_avatar_url(profile)


def _resolve_account_id(token: str, online_id: str) -> str:
    return _resolve_profile(token, online_id)[0]


def refresh_avatar(db: Session, user: models.User) -> str | None:
    """Lightweight profile refresh (no library crawl): exchange NPSSO, resolve
    the profile, store the avatar URL. Returns the URL. Raises
    PsnNpssoExpiredError on a dead token."""
    if not user.psn_npsso or not user.psn_online_id:
        raise ValueError("A PSN NPSSO token and Online ID are required.")
    token = _exchange_npsso(user.psn_npsso)
    _, avatar_url = _resolve_profile(token, user.psn_online_id)
    if avatar_url:
        user.psn_avatar_url = avatar_url
        db.commit()
    return avatar_url


# ─── Fetchers (all paginated — the prototype's biggest gap) ────────────────


# Platforms asked of getPurchasedGameList. The original port requested only
# ps4/ps5, which meant every conclusion about "PSN doesn't return PS3/Vita-era
# purchases" was really a filter WE set (#181) — the snapshot contained PS4
# (583) and PS5 (161) and nothing else, because nothing else was asked for.
# Asking for the older platforms is the only way to find out.
_PURCHASED_PLATFORMS = ["ps3", "ps4", "ps5", "ps vita", "psp"]
# If Sony rejects the wider list (unknown token, changed enum), fall back to
# the pair known to work rather than failing the whole crawl.
_PURCHASED_PLATFORMS_FALLBACK = ["ps4", "ps5"]


def _fetch_purchased_pages(token: str, account_id: str, platforms: list[str]) -> list[dict]:
    """One full paged getPurchasedGameList run for a given platform list."""
    out: list[dict] = []
    start = 0
    for _ in range(_MAX_PAGES):
        variables = {
            "isActive": True,
            "platform": platforms,
            "size": 100,
            "start": start,
            "sortBy": "ACTIVE_DATE",
            "sortDirection": "desc",
            "accountId": account_id,
        }
        data = _bearer_get(
            token,
            _GRAPHQL_URL,
            params={
                "operationName": "getPurchasedGameList",
                "variables": json.dumps(variables),
                "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": _PURCHASED_QUERY_HASH}}),
            },
        )
        if data.get("errors") and start == 0:
            raise ValueError(f"PSN rejected the purchased query: {data['errors']}")
        games = ((data.get("data") or {}).get("purchasedTitlesRetrieve") or {}).get("games") or []
        out.extend(games)
        if len(games) < 100:
            break
        start += 100
        time.sleep(_PAGE_SLEEP_S)
    return out


def _fetch_purchased(token: str, account_id: str) -> list[dict]:
    """GraphQL getPurchasedGameList, paged 100 at a time. Asks for every
    PlayStation platform, falling back to ps4/ps5 if Sony won't take the wider
    list — a rejected token must not cost the user their whole crawl."""
    try:
        return _fetch_purchased_pages(token, account_id, _PURCHASED_PLATFORMS)
    except Exception as e:
        _logger.warning(
            "PSN purchased fetch failed for platforms %s (%s) — retrying with %s",
            _PURCHASED_PLATFORMS,
            e,
            _PURCHASED_PLATFORMS_FALLBACK,
        )
        return _fetch_purchased_pages(token, account_id, _PURCHASED_PLATFORMS_FALLBACK)


def _fetch_trophy_titles(token: str, account_id: str) -> tuple[list[dict], int | None]:
    """Trophy titles, paged via limit/offset. Returns (titles, api-reported
    total or None). The prototype fetched a single unpaged call (100 rows);
    whether the API serves more here is exactly what the report will show."""
    out: list[dict] = []
    offset = 0
    total: int | None = None
    for _ in range(_MAX_PAGES):
        data = _bearer_get(
            token,
            _TROPHY_TITLES_URL.format(account_id=account_id),
            params={"limit": 100, "offset": offset},
        )
        titles = data.get("trophyTitles") or []
        if data.get("totalItemCount") is not None:
            total = data["totalItemCount"]
        out.extend(titles)
        if not titles or (total is not None and len(out) >= total):
            break
        offset += len(titles)
        time.sleep(_PAGE_SLEEP_S)
    return out, total


def _fetch_played(token: str, account_id: str) -> tuple[list[dict], int | None]:
    """Played-games list, paged via nextOffset. The prototype stopped at the
    default first page (10 of an API-reported 147)."""
    out: list[dict] = []
    offset: int | None = 0
    total: int | None = None
    for _ in range(_MAX_PAGES):
        data = _bearer_get(
            token,
            _PLAYED_URL.format(account_id=account_id),
            params={"limit": 200, "offset": offset},
        )
        titles = data.get("titles") or []
        if data.get("totalItemCount") is not None:
            total = data["totalItemCount"]
        out.extend(titles)
        offset = data.get("nextOffset")
        if not titles or offset is None or (total is not None and len(out) >= total):
            break
        time.sleep(_PAGE_SLEEP_S)
    return out, total


# ─── Merge (port of the prototype's mergeLibrary + helpers) ────────────────

_ROMAN = [
    ("XX", "20"),
    ("XIX", "19"),
    ("XVIII", "18"),
    ("XVII", "17"),
    ("XVI", "16"),
    ("XV", "15"),
    ("XIV", "14"),
    ("XIII", "13"),
    ("XII", "12"),
    ("XI", "11"),
    ("X", "10"),
    ("IX", "9"),
    ("VIII", "8"),
    ("VII", "7"),
    ("VI", "6"),
    ("V", "5"),
    ("IV", "4"),
    ("III", "3"),
    ("II", "2"),
    ("I", "1"),
]

_NON_GAME_NAME_RE = re.compile(r"\b(demo|beta|trial version|trial edition|art of|soundtrack)\b", re.IGNORECASE)
# DEMO is anchored to the end of the id; BETA matches anywhere (Sony buries it
# mid-string, e.g. entitlementId "UP0002-CUSA30374_00-RENEGDBETAPS4000" for the
# Diablo IV beta). Matches the prototype's two separate checks — an earlier
# port wrongly anchored BETA too, letting betas through.
_NON_GAME_ID_RE = re.compile(r"DEMO\d*$|BETA", re.IGNORECASE)


def _normalized_name(name: str | None) -> str:
    """Merge key: the shared match-folding, spaceless.

    Spaceless because platforms disagree about word breaks ("SOULCALIBUR" vs
    "Soul Calibur") and that difference is never meaningful. The folding itself
    lives in titles.normalize_for_match so the spreadsheet importer and this
    path can't drift (#180).
    """
    return titles.normalize_for_match(name).replace(" ", "")


# Sony appends this to old trophy-set names (e.g. "God of War II Trophies",
# "TEKKEN 6 Trophy Set"). For games you own, the purchased name wins the merge
# so it's hidden — but trophy-only PS3/Vita history shows it. Strip it so the
# game reads as its real title.
# Matches a trailing trophy-set tag: bare ' Trophy'/' Trophies', or
# ' Trophy Set/Pack/Collection/List', with optional trailing punctuation
# ('STREET FIGHTER IV Trophy pack.').
_TROPHY_SUFFIX_RE = re.compile(r"\s+(?:trophies|trophy(?:\s+(?:set|pack|collection|list))?)[.!]?\s*$", re.IGNORECASE)


def _strip_trophy_suffix(name: str | None) -> str:
    return _TROPHY_SUFFIX_RE.sub("", name or "").strip()


def _display_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\(TM\)|™|®", "", str(name), flags=re.IGNORECASE)
    return _strip_trophy_suffix(cleaned)


def _item_name(item: dict) -> str:
    return item.get("name") or item.get("trophyTitleName") or item.get("titleName") or item.get("localizedName") or ""


def is_non_game(item: dict) -> bool:
    """Demo/beta/trial/soundtrack filter — name patterns plus DEMO/BETA
    suffixes on product/entitlement ids. Port of the prototype's isNonGame."""
    if _NON_GAME_NAME_RE.search(_item_name(item)):
        return True
    for key in ("productId", "entitlementId"):
        if _NON_GAME_ID_RE.search(item.get(key) or ""):
            return True
    return False


def is_game_category(item: dict) -> bool:
    """Played-list category filter the prototype lacked: keep *_game
    categories, drop media/web apps (e.g. 'ps5_web_based_media_app'). Items
    without a category (purchased/trophy datasets) pass."""
    category = item.get("category")
    if category is None:
        return True
    return "game" in str(category).lower() and "media" not in str(category).lower()


def _platform_of(item: dict) -> str | None:
    p = item.get("platform") or item.get("trophyTitlePlatform") or item.get("category")
    if not p:
        return None
    lc = str(p).lower()
    if "ps5" in lc:
        return "ps5"
    if "ps4" in lc:
        return "ps4"
    return None


def _platforms_compatible(a: dict, b: dict) -> bool:
    pa, pb = _platform_of(a), _platform_of(b)
    if not pa or not pb:
        return True
    return pa == pb


def _find_by_any_id(values: list[dict], ids: list) -> dict | None:
    for id_ in ids:
        if not id_:
            continue
        for v in values:
            if v.get("titleId") == id_ or v.get("npCommunicationId") == id_ or v.get("productId") == id_:
                return v
    return None


def merge_library(purchased: list[dict], titles: list[dict], played: list[dict]) -> dict:
    """Three-stage merge, port of the prototype's mergeLibrary: purchased is
    the foundation, trophy titles merge in by id then name+platform, played
    merges in the same way. Returns {"merged": [...], "filtered": {...counts}}.
    Each merged item keeps every id, the trophy block, the play block,
    membership, and image URLs (URLs are reference-only — art comes from SGDB
    by design; PSN native art is deliberately never written to GameArtwork)."""
    lib: dict[str, dict] = {}

    pre = {"purchased": len(purchased), "titles": len(titles), "played": len(played)}
    purchased = [p for p in purchased if not is_non_game(p)]
    titles = [t for t in titles if not is_non_game(t)]
    played_games = [p for p in played if is_game_category(p)]
    media_apps_filtered = len(played) - len(played_games)
    played = [p for p in played_games if not is_non_game(p)]
    filtered = {
        "non_game_purchased": pre["purchased"] - len(purchased),
        "non_game_titles": pre["titles"] - len(titles),
        "media_apps_played": media_apps_filtered,
        "non_game_played": len(played_games) - len(played),
    }

    def values() -> list[dict]:
        return list(lib.values())

    for p in purchased:
        key = p.get("titleId") or p.get("npCommunicationId") or p.get("productId") or p.get("name")
        lib[key] = {
            **p,
            "sources": ["purchased"],
            "normalizedName": _normalized_name(p.get("name")),
            "displayName": _display_name(p.get("name")),
            "platform": (p.get("platform") or "").upper() or None,
        }

    current = values()
    for t in titles:
        existing = _find_by_any_id(current, [t.get("npCommunicationId"), t.get("titleId"), t.get("productId")])
        # How the trophy set reached its purchased row matters downstream. An id
        # join is proof they're the same product; a name join is a guess, and a
        # wrong one whenever a game has several sets — Crimsonland's PS4
        # purchase and its 56m of ps4_game play both landed on the 90% set,
        # which is the Vita one, because names were all there was to match on.
        # Anything reasoning about platform has to know which kind it got.
        join = "id" if existing is not None else None
        if existing is None:
            t_norm = _normalized_name(_item_name(t))
            existing = next(
                (v for v in current if v.get("normalizedName") and v["normalizedName"] == t_norm and _platforms_compatible(v, t)),
                None,
            )
            if existing is not None:
                join = "name"
        # Never fold two trophy sets into one item. A second npCommunicationId
        # is a genuinely separate record — a different platform's progress, or
        # an outright different game sharing a name (Demon's Souls PS3 vs the
        # PS5 remake). Both used to match the same purchased row by name, and
        # the second silently overwrote the first, losing its progress (#163).
        key = (
            (existing or {}).get("titleId")
            or (existing or {}).get("npCommunicationId")
            or t.get("npCommunicationId")
            or t.get("titleId")
            or _item_name(t)
        )
        # The slot this would land in may already hold a DIFFERENT trophy set —
        # `current` is snapshotted before the loop, so two sets for one game both
        # match the same purchased row by name and resolve to the same key. The
        # second used to overwrite the first, silently losing its progress
        # (Crimsonland's 90% set) and merging genuinely different games
        # (Demon's Souls PS3 vs the PS5 remake). Give it its own slot instead.
        occupant = lib.get(key)
        if (
            occupant
            and occupant.get("npCommunicationId")
            and t.get("npCommunicationId")
            and occupant["npCommunicationId"] != t["npCommunicationId"]
        ):
            existing = None
            join = None
            key = t["npCommunicationId"]
        merged = {
            **(existing or {}),
            "titleId": (existing or {}).get("titleId") or t.get("titleId"),
            "npCommunicationId": (existing or {}).get("npCommunicationId") or t.get("npCommunicationId"),
            "productId": (existing or {}).get("productId") or t.get("productId"),
            "name": t.get("trophyTitleName") or (existing or {}).get("name") or _item_name(t),
            "trophies": t.get("definedTrophies") or (existing or {}).get("trophies"),
            "earnedTrophies": t.get("earnedTrophies") or (existing or {}).get("earnedTrophies"),
            "trophyProgress": t.get("progress", (existing or {}).get("trophyProgress")),
            "trophyLastUpdated": t.get("lastUpdatedDateTime") or (existing or {}).get("trophyLastUpdated"),
            "trophyIconUrl": t.get("trophyTitleIconUrl") or (existing or {}).get("trophyIconUrl"),
            "sources": sorted(set((existing or {}).get("sources", []) + ["titles"])),
            "platform": ((t.get("trophyTitlePlatform") or (existing or {}).get("platform") or "").upper() or None),
            "trophyJoin": join,
        }
        merged["normalizedName"] = _normalized_name(merged.get("name"))
        merged["displayName"] = _display_name(merged.get("name"))
        lib[key] = merged

    current = values()
    for p in played:
        ids = [p.get("titleId"), p.get("npCommunicationId"), p.get("productId")]
        existing = _find_by_any_id(current, ids)
        if existing is None and (p.get("concept") or {}).get("titleIds"):
            existing = _find_by_any_id(current, p["concept"]["titleIds"])
        if existing is None:
            p_norm = _normalized_name(_item_name(p))
            existing = next(
                (v for v in current if v.get("normalizedName") and v["normalizedName"] == p_norm and _platforms_compatible(v, p)),
                None,
            )
        key = (existing or {}).get("titleId") or (existing or {}).get("npCommunicationId") or p.get("titleId") or _item_name(p)
        merged = {
            **(existing or {}),
            "titleId": (existing or {}).get("titleId") or p.get("titleId"),
            "npCommunicationId": (existing or {}).get("npCommunicationId") or p.get("npCommunicationId"),
            "productId": (existing or {}).get("productId") or p.get("productId"),
            # The played feed is the LEAST authoritative name: Sony reports
            # activity under a concept/collection name, so both Uncharted 4 and
            # The Lost Legacy come back as "UNCHARTED: Legacy of Thieves
            # Collection" even though their trophy sets and store entries name
            # them correctly. Only fall back to it when nothing better exists
            # (played-only rows, where it's all we have).
            "name": (existing or {}).get("name") or p.get("name") or p.get("localizedName"),
            "playCount": p.get("playCount", (existing or {}).get("playCount", 0)),
            "firstPlayed": p.get("firstPlayedDateTime") or (existing or {}).get("firstPlayed"),
            "lastPlayed": p.get("lastPlayedDateTime") or (existing or {}).get("lastPlayed"),
            "playDuration": p.get("playDuration") or (existing or {}).get("playDuration"),
            "category": p.get("category") or (existing or {}).get("category"),
            "sources": sorted(set((existing or {}).get("sources", []) + ["played"])),
            "platform": ((p.get("platform") or (existing or {}).get("platform") or "").upper() or None),
        }
        # Keep the full set of play categories, not just the last one merged. A
        # game can have several play records (e.g. ps5_native_game + pspc_game);
        # the single `category` field can't tell "played natively on PS5 and
        # briefly on PC" from "only ever played on PC" — is_pc_copy needs all of
        # them to skip Steam duplicates without dropping real PlayStation entries.
        # Read the live lib[key] (not the pre-loop `existing`) so categories from
        # an earlier play record for the same game aren't lost.
        _cats = list((lib.get(key) or existing or {}).get("playCategories") or [])
        if p.get("category"):
            _cats.append(p["category"])
        merged["playCategories"] = sorted(set(_cats))
        # Minutes per play category. The merged playDuration is a single total,
        # but disambiguating a cross-play trophy set needs to know WHERE the
        # time went — 73h on PS5 vs a 14min PS4 cross-gen touch is the whole
        # signal (#163).
        _by_cat = dict((lib.get(key) or existing or {}).get("playByCategory") or {})
        if p.get("category"):
            _by_cat[p["category"]] = _by_cat.get(p["category"], 0) + (duration_to_minutes(p.get("playDuration")) or 0)
        merged["playByCategory"] = _by_cat
        merged["normalizedName"] = _normalized_name(merged.get("name"))
        merged["displayName"] = _display_name(merged.get("name"))
        lib[key] = merged

    return {"merged": list(lib.values()), "filtered": filtered}


# ─── Snapshot + report (no library writes) ─────────────────────────────────


# Path of the crawl dump. Write-only: nothing in the app reads it back — see
# _write_debug_dump. Kept so open PSN questions can be answered offline from
# the raw feeds (#181 was settled entirely from one of these files).
def snapshot_path(user_id: int) -> str:
    return os.path.join(DATA_DIR, f"psn_snapshot_user{user_id}.json")


def external_id_for(item: dict) -> str | None:
    """The id that will become GameRelease.external_id at import time —
    titleId first (joins purchased/played), npCommunicationId for trophy-only
    history, productId as last resort."""
    return item.get("titleId") or item.get("npCommunicationId") or item.get("productId") or None


def _build_report(db: Session, merged: list[dict], filtered: dict, totals: dict, purchased: list[dict] | None = None) -> dict:
    membership = Counter((m.get("membership") or "NONE") for m in merged if "purchased" in m.get("sources", []))
    # Which platforms the purchased feed actually returned. We now ask for the
    # old consoles too (#181) — this is how you see whether Sony honours it.
    purchased_platforms = Counter((p.get("platform") or "unknown") for p in (purchased or []))
    platforms = Counter((m.get("platform") or "unknown") for m in merged)
    platform_resolution = {}
    for name in platforms:
        if name == "unknown":
            platform_resolution[name] = None
            continue
        # Multi-platform trophy strings ("PS5,PSPC") resolve on their first segment.
        platform_resolution[name] = models.resolve_platform_id(db, name.split(",")[0])
    unresolvable = [name for name, pid in platform_resolution.items() if pid is None]

    existing_ids = {ext_id for (ext_id,) in db.query(models.GameRelease.external_id).filter(models.GameRelease.source == "psn").all()}
    ids = [external_id_for(m) for m in merged]
    already_imported = sum(1 for i in ids if i and i in existing_ids)

    return {
        "totals": totals,
        "merged_total": len(merged),
        "filtered": filtered,
        "membership": dict(membership),
        "purchased_platforms": dict(purchased_platforms),
        "platforms": dict(platforms),
        "unresolvable_platforms": unresolvable,
        "no_external_id": sum(1 for i in ids if not i),
        "already_imported": already_imported,
        "new": len(merged) - already_imported,
        "sample": [
            {"name": m.get("displayName") or m.get("name"), "platform": m.get("platform"), "sources": m.get("sources")} for m in merged[:12]
        ],
    }


def has_synced(db: Session, user_id: int) -> bool:
    """Whether a PSN sync has ever run for this user.

    Drives empty-state copy ("run a sync first" vs "nothing left to review").
    Reads the library and the review queue rather than the debug dump on disk,
    so a restored database never disagrees with what the app shows.
    """
    if db.query(models.PsnReviewCandidate).filter(models.PsnReviewCandidate.user_id == user_id).first():
        return True
    return (
        db.query(models.UserLibraryEntry.id)
        .join(models.GameRelease, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(models.UserLibraryEntry.user_id == user_id, models.GameRelease.source == "psn")
        .first()
        is not None
    )


def crawl(db: Session, user: models.User) -> tuple[list[dict], dict, dict]:
    """Full PSN crawl → (merged items, report, raw feeds). Writes nothing.

    Was `fetch_snapshot`, which also owned persistence. Splitting the two is
    what lets the sync hold the crawl in memory and put it straight into the
    library, instead of round-tripping through a file (#157).
    """
    if not user.psn_npsso:
        raise ValueError("A PSN NPSSO token is required.")
    if not user.psn_online_id:
        raise ValueError("Your PSN Online ID is required.")

    token = _exchange_npsso(user.psn_npsso)
    account_id, avatar_url = _resolve_profile(token, user.psn_online_id)
    if avatar_url and avatar_url != user.psn_avatar_url:
        user.psn_avatar_url = avatar_url
        db.commit()

    purchased = _fetch_purchased(token, account_id)
    titles, titles_total = _fetch_trophy_titles(token, account_id)
    played, played_total = _fetch_played(token, account_id)

    result = merge_library(purchased, titles, played)
    totals = {
        "purchased_fetched": len(purchased),
        "trophy_fetched": len(titles),
        "trophy_reported": titles_total,
        "played_fetched": len(played),
        "played_reported": played_total,
    }
    report = _build_report(db, result["merged"], result["filtered"], totals, purchased)
    raw = {"purchased": purchased, "trophy_titles": titles, "played": played}
    return result["merged"], report, raw


def _write_debug_dump(user_id: int, report: dict, merged: list[dict], raw: dict) -> None:
    """Dump the crawl to disk for debugging. NOTHING in the app reads this.

    Kept because the raw feeds are how open PSN questions get answered offline
    (the PS3/Vita purchased-platform investigation in #181 was settled entirely
    from this file). Best-effort: a sync must never fail because a debug write
    did.
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(snapshot_path(user_id), "w") as f:
            json.dump(
                {
                    "fetched_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "report": report,
                    "merged": merged,
                    "raw": raw,
                },
                f,
            )
    except OSError:
        _logger.warning("PSN debug dump failed for user %s", user_id, exc_info=True)


# ─── Import (PR 2 — inserts only, played-only rows via explicit review) ────

_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")


def duration_to_minutes(duration: str | None) -> int | None:
    """ISO-8601 play duration ('PT30H23M7S') → whole minutes. None/unparseable → None."""
    if not duration:
        return None
    m = _DURATION_RE.match(duration.strip())
    if not m:
        return None
    hours, minutes, seconds = (float(x) if x else 0 for x in m.groups())
    return int(hours * 60 + minutes + seconds / 60)


def _parse_played_at(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_played_only(item: dict) -> bool:
    """Activity-history rows with no purchased/trophy backing — the mixed bag
    of disc copies, demos, friend-pass sessions, and launch-and-quit noise.
    Never auto-imported; each goes through the per-row review on the PSN page."""
    return item.get("sources", []) == ["played"]


# Native PlayStation play categories read ps3_game / ps4_game / ps5_native_game;
# the PC one is pspc_game (PSN tracking a Steam/PC playthrough via its PC
# integration). ps[345] deliberately does NOT match "pspc".
_NATIVE_PS_CAT_RE = re.compile(r"ps[345]", re.IGNORECASE)


def is_pc_copy(item: dict, steam_keys: set[str]) -> bool:
    """True when this is the PC copy of a game, surfacing through PSN's PC
    integration rather than anything you own on a PlayStation.

    The test is evidence, not Sony's platform string, because Sony is not
    consistent about it: Stellar Blade came back PSPC-only while the Until Dawn
    remake came back "PS5,PSPC" for the identical situation. Requiring the set
    to be PC-ONLY therefore missed the second one and minted a phantom PS5
    entry for a game only owned on Steam.

    So: the set mentions PSPC, the title is already in the Steam library, and
    there is no PlayStation-side evidence — no purchase, and no play time under
    a native console category. Trophies alone prove the game was played, never
    which platform it ran on, so they are deliberately not evidence here.
    """
    plats = {p.strip().upper() for p in str(item.get("platform") or "").split(",") if p.strip()}
    cats = list(item.get("playCategories") or [])
    if not cats and item.get("category"):
        cats = [item["category"]]
    if "PSPC" not in plats and not any("pspc" in str(c).lower() for c in cats):
        return False
    if (item.get("normalizedName") or _normalized_name(item.get("name"))) not in steam_keys:
        return False
    if "purchased" in (item.get("sources") or []):
        return False
    if any(_NATIVE_PS_CAT_RE.search(str(c)) for c in cats):
        return False
    # PSPC minutes are PC minutes — the very thing being skipped — so only
    # console platforms count as evidence of a PlayStation copy.
    return not any(minutes for platform, minutes in played_minutes_by_platform(item).items() if platform != "PSPC")


def _steam_title_keys(db: Session, user_id: int) -> set[str]:
    """Normalized titles of the user's Steam library — used to recognize when a
    pspc (PC) game is already tracked as a Steam entry so we skip the duplicate."""
    rows = (
        db.query(models.Game.title)
        .join(models.GameRelease, models.GameRelease.game_id == models.Game.id)
        .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(models.UserLibraryEntry.user_id == user_id, models.GameRelease.source == "steam")
        .distinct()
    )
    return {_normalized_name(t[0]) for t in rows if t[0]}


def played_only_suggestion(item: dict) -> tuple[str, str]:
    """(suggested_action, reason) for a played-only row. Pre-selects the
    review default; the user's click decides. Signals (validated against the
    user's real data 2026-07-18):
      service=other + game category → no digital entitlement behind the
        session → essentially the disc signature ⇒ import.
      service=ps_plus + tiny playtime → catalog launch-and-quit ⇒ skip.
      otherwise ($0 demo/friend-pass entitlements look identical to paid
        ones) → playtime decides the lean."""
    service = str(item.get("service") or "").lower()
    minutes = duration_to_minutes(item.get("playDuration")) or 0
    if service == "other":
        return "import", "no digital entitlement behind the session — likely disc copy"
    if service == "ps_plus":
        if minutes < 60:
            return "skip", f"PS Plus catalog launch, only {minutes}m played"
        return "import", f"PS Plus catalog, {minutes // 60}h{minutes % 60:02d}m played"
    if minutes < 15:
        return "skip", f"entitlement play of {minutes}m — demo or launch-and-quit"
    return "skip", f"$0-entitlement pattern (demo/friend-pass), {minutes // 60}h{minutes % 60:02d}m played"


# ─── Cross-play platform resolution (#163) ─────────────────────────────────
# A shared trophy set reports every platform it covers ("PS3,PSVITA"), and the
# trophies alone never say which one you actually played. Taking the first
# token — what the merge used to do — tagged Shinovi Versus as PS3, a game that
# never had a PS3 release.

# Newest first. PSPC is deliberately absent: it's PSN's PC integration (a Steam
# copy), never a PlayStation platform.
_PS_PLATFORM_RANK = ["PS5", "PS4", "PS3", "PSVITA", "PSP"]

# Played-feed categories -> platform token.
_CATEGORY_PLATFORM = {"ps5": "PS5", "ps4": "PS4", "ps3": "PS3", "pspc": "PSPC"}

# Below this, a play record reads as a cross-gen upgrade touch or a launch-and-
# quit rather than "this is the version I played" (Forbidden West: 73h on PS5
# vs 14min on PS4).
_SUBSTANTIAL_MINUTES = 60


# ─── Cross-buy reference data ──────────────────────────────────────────────

REFERENCE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "reference"))
_CROSS_BUY_PATH = os.path.join(REFERENCE_DIR, "psn_cross_buy.json")
_cross_buy_cache: dict | None = None


def _load_cross_buy() -> dict:
    """Known exceptions to the cross-buy assumption, indexed for lookup.

    Sony's feeds cannot express whether a shared trophy list came with a shared
    entitlement, nor whether a set spanning three platforms is really one list
    or three — so this is hand-maintained reference data. See the _README in the
    file. Cached per process; a missing or malformed file degrades to "nothing
    known", never to a failed sync.
    """
    global _cross_buy_cache
    if _cross_buy_cache is not None:
        return _cross_buy_cache
    index: dict = {"by_npcomm": {}, "by_title": []}
    try:
        with open(_CROSS_BUY_PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        _logger.warning("PSN cross-buy reference unreadable at %s — proceeding with no known exceptions", _CROSS_BUY_PATH)
        _cross_buy_cache = index
        return index
    # Bulk confirmations first, so an explicit `titles` entry (which can carry
    # cross_buy=false) overwrites rather than being overwritten.
    for title in (raw.get("cross_buy_confirmed") or {}).get("titles") or []:
        index["by_title"].append(
            (
                titles.normalize_for_match(title),
                {"title": title, "shared_trophies": None, "cross_buy": True, "notes": "", "restricts": False},
            )
        )
    for row in raw.get("titles") or []:
        entry = {
            "title": row.get("title", ""),
            "shared_trophies": row.get("shared_trophies"),
            "cross_buy": row.get("cross_buy"),
            "notes": row.get("notes", ""),
        }
        # ONLY cross_buy=false restricts. Ownership is the criterion for putting
        # a row in the library, and separate trophy lists don't make you own the
        # game less — Axiom Verge and Bastion have per-platform lists AND
        # cross-buy, so you own both copies and both belong. shared_trophies is
        # kept as information (it decides which entry the trophy data actually
        # describes, for #136) but must not suppress an entry.
        entry["restricts"] = entry["cross_buy"] is False
        for npcomm in row.get("npcomm") or []:
            index["by_npcomm"][npcomm] = entry
        if entry["title"]:
            index["by_title"].append((titles.normalize_for_match(entry["title"]), entry))
    _cross_buy_cache = index
    return index


# Store SKU prefixes. Not a general platform oracle — Sony reuses CUSA on a
# handful of Vita/PS3 listings — but for a row we know was PURCHASED it names
# the SKU that was bought, which is the only per-platform entitlement evidence
# the feeds carry once a trophy set has overwritten the platform string.
_TITLEID_PLATFORM = {"CUSA": "PS4", "PPSA": "PS5", "PCSA": "PSVITA", "PCSB": "PSVITA", "PCSE": "PSVITA", "PCSF": "PSVITA", "PCSG": "PSVITA"}


def purchased_platform(item: dict) -> str | None:
    """Platform of the SKU actually bought, or None when nothing was purchased."""
    if "purchased" not in (item.get("sources") or []):
        return None
    tid = str(item.get("titleId") or "")
    return _TITLEID_PLATFORM.get(tid[:4].upper())


def cross_buy_exception(item: dict) -> dict | None:
    """The reference entry for this item, or None when nothing is known.

    npCommunicationId wins when present — it identifies a trophy set exactly.
    Title matching falls back to the shared normalizer, so ™, casing, subtitles
    and edition suffixes don't have to be reproduced in the file.
    """
    index = _load_cross_buy()
    hit = index["by_npcomm"].get(item.get("npCommunicationId"))
    if hit:
        return hit
    name = item.get("displayName") or item.get("name")
    if not name:
        return None
    key = titles.normalize_for_match(name)
    # Exact beats fuzzy, and an explicit entry beats a bulk confirmation — a
    # curated cross_buy=false must never lose to the confirmed-cross-buy list.
    best = None
    for ref_key, entry in index["by_title"]:
        tier = titles.titles_match(key, ref_key)
        if not tier:
            continue
        if entry["notes"] or entry["cross_buy"] is False:
            return entry
        if best is None or tier == "exact":
            best = entry
    return best


def platform_candidates(item: dict) -> list[str]:
    """PlayStation platforms a trophy set covers, newest first. PSPC dropped —
    that's a PC copy, handled separately by the Steam-duplicate skip."""
    raw = str(item.get("platform") or "")
    toks = {t.strip().upper() for t in raw.split(",") if t.strip()}
    toks.discard("PSPC")
    ranked = [p for p in _PS_PLATFORM_RANK if p in toks]
    return ranked + sorted(toks - set(ranked))


def _platform_of_category(category: str) -> str | None:
    c = str(category or "").lower()
    for prefix, platform in _CATEGORY_PLATFORM.items():
        if c.startswith(prefix):
            return platform
    return None


def played_minutes_by_platform(item: dict) -> dict[str, int]:
    """Minutes played per platform, derived from the play records' categories."""
    out: dict[str, int] = {}
    for category, minutes in (item.get("playByCategory") or {}).items():
        platform = _platform_of_category(category)
        if platform:
            out[platform] = out.get(platform, 0) + (minutes or 0)
    return out


def resolve_platform_choice(item: dict) -> tuple[str | None, str, bool]:
    """(platform, reason, confident) for a merged item.

    Confident results are applied on import; anything else is a *suggestion*
    the user confirms in the review list, because guessing wrong here creates a
    library entry on a console you never owned.
    """
    candidates = platform_candidates(item)
    if not candidates:
        return None, "No PlayStation platform reported", False
    if len(candidates) == 1:
        return candidates[0], "Only one platform on this trophy set", True

    played = {p: m for p, m in played_minutes_by_platform(item).items() if p in candidates}
    substantial = {p: m for p, m in played.items() if m >= _SUBSTANTIAL_MINUTES}

    if len(substantial) == 1:
        platform, minutes = next(iter(substantial.items()))
        return platform, f"Played {minutes // 60}h on {platform}", True
    if len(substantial) > 1:
        platform = max(substantial, key=lambda p: (substantial[p], -_PS_PLATFORM_RANK.index(p) if p in _PS_PLATFORM_RANK else 0))
        others = ", ".join(f"{p} {substantial[p] // 60}h" for p in substantial if p != platform)
        return platform, f"Most played on {platform} ({substantial[platform] // 60}h vs {others})", False
    if played:
        # Only trivial playtime — enough to say WHERE, not enough to be sure.
        platform = max(played, key=lambda p: played[p])
        return platform, f"Only {played[platform]}m played, on {platform}", False

    # Trophy-only. The modern purchased feed doesn't cover PS3/Vita-era
    # purchases, so a set spanning that era with no purchase record is almost
    # certainly the handheld/older copy — but it's a guess either way.
    oldest = candidates[-1]
    return oldest, f"No play history — trophy set covers {', '.join(candidates)}", False


def platform_for_item(db: Session, item: dict) -> int | None:
    """Merged item → platform row id.

    Cross-play trophy sets ("PS3,PSVITA") are resolved by play history rather
    than by taking the first token, which used to mislabel games (#163). An
    explicit user decision from the review list always wins.
    """
    chosen = item.get("platformDecision")
    if not chosen:
        candidates = platform_candidates(item)
        if candidates:
            chosen = resolve_platform_choice(item)[0]
    if not chosen:
        # Played-only rows carry no trophy platform string — fall back to the
        # played category.
        chosen = _platform_of_category(item.get("category") or "")
    if not chosen or chosen == "PSPC":
        return None
    return models.resolve_platform_id(db, chosen)


def _platform_label(db: Session, platform_id: int | None, item: dict) -> str:
    if platform_id:
        row = db.get(models.Platform, platform_id)
        if row:
            return row.display_name or row.name
    return item.get("platform") or "PSN"


def _import_one(db: Session, user: models.User, item: dict, platform_id: int) -> str:
    """Upsert one merged item as Game/GameRelease/UserLibraryEntry (mirrors
    steam._import_owned_games row-for-row). Returns 'added' | 'updated' |
    'conflict'. Deliberately writes NO GameArtwork — SGDB is the agreed art
    source; PSN URLs stay in raw_data."""
    external_id = external_id_for(item)
    # Strip the trophy-set suffix here too, so an existing snapshot (whose
    # displayName was computed before this fix) still imports the clean name
    # without a re-fetch.
    title = _strip_trophy_suffix(item.get("displayName") or item.get("name") or external_id)
    # Scoped to the platform: one trophy set can legitimately become several
    # releases (cross-buy — Shovel Knight on PS4 *and* Vita). external_id is
    # not unique, only (game_id, platform) is.
    release = db.query(models.GameRelease).filter_by(source="psn", external_id=external_id, platform_id=platform_id).first()

    if release is None:
        cleaned = titles._clean_title(title)
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
            if not game.display_name_user_set and game.display_name is None and cleaned != title:
                game.display_name = cleaned
        else:
            game = models.Game(
                title=title,
                display_name=cleaned if cleaned != title else None,
                is_dlc=False,
                is_collection=titles._infer_is_collection(title),
            )
            db.add(game)
            db.flush()
        label = _platform_label(db, platform_id, item)
        # A different release already occupies this game+platform slot
        # (UNIQUE(game_id, platform)). Happens when two items share a display
        # title on the same platform — a beta + the real game, cross-region
        # editions, etc. Skip this one instead of letting the IntegrityError
        # abort the whole import.
        if db.query(models.GameRelease).filter_by(game_id=game.id, platform=label).first() is not None:
            return "conflict"
        release = models.GameRelease(
            game_id=game.id,
            platform=label,
            platform_id=platform_id,
            source="psn",
            external_id=external_id,
            raw_data=item,
        )
        db.add(release)
        db.flush()
    else:
        raw = dict(release.raw_data or {})
        raw.update(item)
        release.raw_data = raw
        # Re-derive the clean title on re-import so title fixes (trophy-suffix
        # stripping, etc.) reach already-imported entries without a rebuild.
        # Library search matches Game.title, so update the title itself — not
        # just the display_name override — to keep search clean. Mirrors the
        # fresh-import path above; respects a user-set display name.
        game = release.game
        if not game.display_name_user_set:
            cleaned = titles._clean_title(title)
            game.title = title
            game.display_name = cleaned if cleaned != title else None

    playtime = duration_to_minutes(item.get("playDuration"))
    last_played = _parse_played_at(item.get("lastPlayed"))

    entry = db.query(models.UserLibraryEntry).filter_by(user_id=user.id, release_id=release.id).first()
    if entry is None:
        db.add(
            models.UserLibraryEntry(
                user_id=user.id,
                release_id=release.id,
                playtime_minutes=playtime or 0,
                last_played_at=last_played,
                import_source="psn_import",
            )
        )
        return "added"
    if playtime is not None:
        entry.playtime_minutes = playtime
    if last_played is not None:
        entry.last_played_at = last_played
    entry.updated_at = datetime.datetime.now(datetime.UTC)
    return "updated"


def _upsert_review_candidate(db: Session, user: models.User, item: dict, kind: str) -> models.PsnReviewCandidate | None:
    """Record a game the sync can't place as a review row, or refresh one.

    A decided row (confirmed/dismissed) keeps its status — that is how
    decisions survive a re-sync now, with no separate decisions file to carry
    across. Its crawl payload is still refreshed, since trophy progress and
    playtime move and the row is what the UI renders from.

    Returns the row only when it's pending, so callers can count real work.
    """
    ext_id = external_id_for(item)
    if not ext_id:
        return None
    row = (
        db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user.id, models.PsnReviewCandidate.external_id == ext_id)
        .first()
    )
    if row is None:
        row = models.PsnReviewCandidate(user_id=user.id, external_id=ext_id, kind=kind, status="pending")
        db.add(row)
    row.title = item.get("displayName") or item.get("name") or ext_id
    row.kind = kind
    row.raw_data = item
    return row if row.status == "pending" else None


def import_merged(db: Session, user: models.User, merged: list[dict]) -> dict:
    """Add every game whose platform PSN settles; route the rest to review.

    PS_PLUS included — completability is the criterion (user decision
    2026-07-18). INSERTS ONLY for existing library data; re-runs update the psn
    rows idempotently. Cross-play games PSN can't settle become review rows
    rather than guesses: import creates entries and has no way to un-create
    them, so a wrong platform would need manual cleanup. Played-only activity
    likewise. Ends by chaining the match-review scan so overlaps with
    manual/historical entries surface immediately.

    Takes the merged crawl in memory rather than reading it back off disk. The
    snapshot file was a proof-of-concept staging area from when a crawl was
    forbidden to touch the library; keeping it in the read path let it drift
    from the library it described (#157).
    """

    added = updated = 0
    skipped_no_platform = 0
    skipped_no_id = 0
    skipped_non_game = 0
    skipped_conflict = 0
    skipped_pc_dupe = 0
    needs_review = 0
    played_only = 0
    steam_keys = _steam_title_keys(db, user.id)
    # One query, not one per game — this loop runs over ~1000 items.
    decided = {
        r.external_id: r
        for r in db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user.id, models.PsnReviewCandidate.status != "pending")
        .all()
    }
    for item in merged:
        if is_played_only(item):
            if _upsert_review_candidate(db, user, item, "played_only") is not None:
                played_only += 1
            continue
        # Belt-and-suspenders: the non-game filter runs at crawl time, but
        # never import a beta/demo regardless of what reaches here.
        if is_non_game(item):
            skipped_non_game += 1
            continue
        # A pspc (PC) game already in the Steam library is the same copy showing
        # up through PSN's PC integration — skip it rather than mint a phantom
        # PlayStation entry (e.g. Stellar Blade, the Until Dawn remake).
        if is_pc_copy(item, steam_keys):
            skipped_pc_dupe += 1
            continue
        if not external_id_for(item):
            skipped_no_id += 1
            continue
        # An already-decided cross-play game re-imports its chosen platforms, so
        # a re-sync refreshes their playtime like any other psn row. A dismissed
        # one has an empty list and so imports nothing.
        row = decided.get(external_id_for(item))
        if row is None and len(platform_candidates(item)) > 1:
            if _upsert_review_candidate(db, user, item, "cross_play") is not None:
                needs_review += 1
            continue
        if row is not None:
            for choice in row.chosen_platforms or []:
                platform_id = models.resolve_platform_id(db, choice)
                if platform_id is None:
                    skipped_no_platform += 1
                    continue
                outcome = _import_one(db, user, item, platform_id)
                if outcome == "added":
                    added += 1
                elif outcome == "conflict":
                    skipped_conflict += 1
                else:
                    updated += 1
            continue

        platform_id = platform_for_item(db, item)
        if platform_id is None:
            skipped_no_platform += 1
            continue
        outcome = _import_one(db, user, item, platform_id)
        if outcome == "added":
            added += 1
        elif outcome == "conflict":
            skipped_conflict += 1
        else:
            updated += 1
    db.commit()

    from . import match_review

    scan = match_review.scan_for_matches(db, user)
    db.commit()

    return {
        "added": added,
        "updated": updated,
        "skipped_no_platform": skipped_no_platform,
        "skipped_no_id": skipped_no_id,
        "skipped_non_game": skipped_non_game,
        "skipped_conflict": skipped_conflict,
        "skipped_pc_dupe": skipped_pc_dupe,
        "needs_review": needs_review,
        "played_only_pending": played_only,
        "match_candidates": scan.get("candidates_added", 0),
    }


def sync_library(db: Session, user: models.User) -> dict:
    """Crawl PSN and add what it can place, in one step — Steam sync for PSN.

    The crawl stays in memory and goes straight into the library; the only
    thing written to disk is a debug dump of the raw feeds, which nothing in
    the app reads back. That ordering is the point of #157: the old flow
    staged a crawl in a JSON file and then read the app's state back out of it,
    which is not how any other integration here works and let the file drift
    from the library it claimed to describe.

    Returns the import's result with the crawl's report folded in, so one job
    completion can report the library delta AND every review queue.
    """
    merged, report, raw = crawl(db, user)
    result = import_merged(db, user, merged)
    user.psn_last_sync_report = report
    user.psn_last_synced_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    _write_debug_dump(user.id, report, merged, raw)
    return {**result, "report": report}


# ─── Played-only review actions ────────────────────────────────────────────


def played_only_rows(db: Session, user_id: int) -> list[dict]:
    """Played-only review rows: activity with no purchase or trophy behind it.

    A disc, a demo, someone else's account on your console — Sony's data can't
    tell them apart, so these are asked rather than guessed. Decided rows stay
    in the list showing what was chosen, since this queue lives inline on the
    PSN page rather than as its own queue.
    """
    rows = []
    for cand in (
        db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user_id, models.PsnReviewCandidate.kind == "played_only")
        .all()
    ):
        item = cand.raw_data or {}
        action, reason = played_only_suggestion(item)
        rows.append(
            {
                "external_id": cand.external_id,
                "name": cand.title,
                "category": item.get("category"),
                "service": item.get("service"),
                "minutes": duration_to_minutes(item.get("playDuration")) or 0,
                "play_count": item.get("playCount"),
                "first_played": (item.get("firstPlayed") or "")[:10],
                "last_played": (item.get("lastPlayed") or "")[:10],
                "suggested": action,
                "reason": reason,
                "decision": cand.chosen_platforms if cand.status != "pending" else None,
            }
        )
    return rows


def owned_platforms(db: Session, user_id: int) -> set[str]:
    """PlayStation consoles this account is PROVEN to own.

    Two things prove hardware. A game the sync could place on exactly one
    platform — you cannot have a PS3-only trophy set without a PS3 — and those
    are already library entries, so the evidence lives there. And a play
    record, whose category names the console it ran on.

    A cross-play set proves nothing on its own: it lists every platform the
    game shipped on, not the ones you own. Which is why those are the rows
    being asked about.

    Comparison goes through resolve_platform_id, NOT string munging: trophy
    feeds say "PSVITA" while the platforms table says "PlayStation Vita", so
    an earlier uppercase-and-strip-spaces version silently proved nothing for
    PS3 or Vita and left most rows defaulting to PS4 alone.
    """
    owned_ids = {
        pid
        for (pid,) in db.query(models.GameRelease.platform_id)
        .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(models.UserLibraryEntry.user_id == user_id, models.GameRelease.source == "psn")
        .distinct()
        .all()
        if pid
    }
    proven = {token for token in _PS_PLATFORM_RANK if models.resolve_platform_id(db, token) in owned_ids}
    for cand in (
        db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user_id, models.PsnReviewCandidate.status == "pending")
        .all()
    ):
        for platform, minutes in played_minutes_by_platform(cand.raw_data or {}).items():
            if minutes:
                proven.add(platform)
    return proven


def _trophy_hint_is_trustworthy(item: dict, sets_for_title: int) -> bool:
    """Whether an item's play history can be attributed to its trophy set.

    Two ways it can't. A name join means the play record reached this set on
    title alone, which is a guess. And a title with several sets makes the
    attribution unknowable regardless of how the join happened — the play
    record belongs to exactly one of them and nothing says which. Crimsonland
    fails both: its PS4 purchase and 56m of ps4_game play landed on the 90%
    set, which is the Vita one.

    The count test is the load-bearing one for snapshots fetched before
    `trophyJoin` existed, which have no join marker to check.
    """
    return item.get("trophyJoin") != "name" and sets_for_title < 2


def import_review_rows(db: Session, user_id: int) -> list[dict]:
    """Pending cross-play rows — one per trophy set, one checkbox per platform.

    A cross-play set covers several platforms and never says which you own, and
    cross-buy means the answer is often more than one (Shovel Knight: Treasure
    Trove is a single PS3,PSVITA,PS4 set) — so platforms are checkboxes,
    pre-ticked with every platform in the set this account is known to own (see
    `owned_platforms`). Unticking them all is the same as dismissing.

    **One row per trophy set, not per game.** Sony hands out several sets for
    one title — Crimsonland has NPWR06670 at 90% and NPWR06085 at 23%, both
    declaring the identical PS3,PSVITA,PS4 — and no field in any feed says
    which set covers which console. They are two real progress records wanting
    two library entries, so they are asked about separately and each row
    carries its trophy progress, the only thing that tells them apart.

    Confirming CREATES the entries, so a decided row leaves the list the way a
    merged pair leaves match review.
    """
    candidates = (
        db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user_id,
            models.PsnReviewCandidate.kind == "cross_play",
            models.PsnReviewCandidate.status == "pending",
        )
        .all()
    )
    owned = owned_platforms(db, user_id)
    # Sets sharing a normalized title are the ones that look like duplicates.
    # Count them up front so a row can say "set 1 of 2" rather than appearing
    # twice for no stated reason.
    by_name: dict[str, int] = {}
    for cand in candidates:
        by_name[(cand.raw_data or {}).get("normalizedName") or ""] = by_name.get((cand.raw_data or {}).get("normalizedName") or "", 0) + 1
    seen: dict[str, int] = {}

    rows = []
    for cand in candidates:
        item = cand.raw_data or {}
        options = platform_candidates(item)
        if not options:
            continue
        name = item.get("normalizedName") or ""
        seen[name] = seen.get(name, 0) + 1
        sets_for_title = by_name.get(name, 1)

        trusted = _trophy_hint_is_trustworthy(item, sets_for_title)
        minutes = played_minutes_by_platform(item) if trusted else {}
        if trusted:
            _resolved, reason, _confident = resolve_platform_choice(item)
        elif sets_for_title > 1:
            # Say what's known rather than quoting a play stat that may belong
            # to the other trophy set for the same game.
            reason = f"{sets_for_title} trophy sets for this title — PSN doesn't say which platform each covers"
        else:
            reason = "Matched by title only — PSN gives no platform for this trophy set"

        # Default: everything in the set this account can actually play.
        # Absence of evidence is not evidence of absence — PSN reports play data
        # only for PS4 and later, so a PS3 or Vita copy can never be confirmed
        # or denied from the feeds. Over-offering costs a row you delete;
        # under-offering costs a completion with nowhere to attach.
        default = [p for p in options if p in owned] or options

        # ...unless the reference file knows this title breaks that assumption:
        # separate trophy lists (so this set covers ONE platform) or separate
        # purchases (so owning one implies nothing). Then only platforms with
        # actual play evidence are pre-ticked, and the row says why — a wrong
        # reference entry must be visible and correctable, never silent.
        exception = cross_buy_exception(item)
        if exception and exception["restricts"]:
            # Evidence here means an entitlement or play time on THAT platform —
            # a purchase names the SKU bought, which is the only per-platform
            # ownership signal left once a trophy set has overwritten the
            # platform string. Without it a bought-on-PS4 row pre-ticks nothing.
            bought = purchased_platform(item)
            default = [p for p in options if minutes.get(p) or p == bought]
            why = "Sold separately per platform"
            reason = f"{why} — {exception['notes']}" if exception["notes"] else f"{why} — tick only what you own"

        earned = item.get("earnedTrophies") or {}
        defined = item.get("trophies") or {}
        rows.append(
            {
                "key": cand.external_id,
                "external_id": cand.external_id,
                "name": cand.title,
                "reason": reason,
                # SGDB grid first — PSN's own image is a square icon0.png, the
                # wrong shape for a review card's hero.
                "image": cand.thumbnail_url or (item.get("image") or {}).get("url") or item.get("trophyIconUrl"),
                "trophy_progress": item.get("trophyProgress"),
                "trophy_earned": sum(v or 0 for v in earned.values()),
                "trophy_defined": sum(v or 0 for v in defined.values()),
                "trophy_last_updated": (item.get("trophyLastUpdated") or "")[:10],
                "set_index": seen[name],
                "set_count": sets_for_title,
                "last_played": (item.get("lastPlayed") or "")[:10] if trusted else "",
                "total_minutes": sum(minutes.values()),
                "restricted": bool(exception and exception["restricts"]),
                "options": [{"platform": p, "selected": p in default, "minutes": minutes.get(p, 0)} for p in options],
            }
        )
    rows.sort(key=lambda r: ((r["name"] or "").casefold(), r["set_index"]))
    return rows


def review_thumbnail_gaps(db: Session, user_id: int) -> list[dict]:
    """Pending review rows with no art yet — {external_id, title} each.

    Fed to the SGDB placeholder fill. These rows have no library entry to
    borrow art from (entries exist only once a row is confirmed), which is
    exactly ImportCandidate's position — hence the same thumbnail_url column.
    """
    return [
        {"external_id": c.external_id, "title": c.title}
        for c in db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user_id,
            models.PsnReviewCandidate.status == "pending",
            models.PsnReviewCandidate.thumbnail_url.is_(None),
        )
        .all()
        if c.title
    ]


def save_review_thumbnails(db: Session, user_id: int, thumbs: dict[str, str]) -> int:
    """Cache SGDB thumbnail URLs onto the review rows."""
    if not thumbs:
        return 0
    written = 0
    for cand in (
        db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user_id, models.PsnReviewCandidate.external_id.in_(list(thumbs)))
        .all()
    ):
        cand.thumbnail_url = thumbs[cand.external_id]
        written += 1
    db.commit()
    return written


def review_pending_count(db: Session, user_id: int) -> int:
    """Rows still awaiting a decision, across both queues."""
    return (
        db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user_id, models.PsnReviewCandidate.status == "pending")
        .count()
    )


def _pending_candidate(db: Session, user_id: int, key: str, kind: str = "cross_play") -> models.PsnReviewCandidate | None:
    return (
        db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user_id,
            models.PsnReviewCandidate.external_id == key,
            models.PsnReviewCandidate.kind == kind,
            models.PsnReviewCandidate.status == "pending",
        )
        .first()
    )


def confirm_entry_decision(db: Session, user: models.User, key: str, platforms: list[str]) -> dict:
    """Confirm one review row: create its entries and retire the row.

    The review IS the action — match review merges on click, import review
    confirms on click. An empty platform list is a real decision ("own it on
    nothing"), which is exactly what Dismiss sends.
    """
    cand = _pending_candidate(db, user.id, key)
    if cand is None:
        raise ValueError("That trophy set is not in the PSN review queue.")
    item = cand.raw_data or {}
    # Only platforms the trophy set actually covers. A stale page can post
    # anything, and an entry on a platform Sony never listed is a wrong row
    # this function has no way to take back.
    allowed = set(platform_candidates(item))
    chosen = [p for p in platforms if p in allowed]

    created = 0
    for platform in chosen:
        platform_id = models.resolve_platform_id(db, platform)
        if platform_id is None:
            continue
        if _import_one(db, user, item, platform_id) == "added":
            created += 1

    cand.status = "confirmed" if chosen else "dismissed"
    cand.chosen_platforms = chosen
    cand.reviewed_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return {"name": cand.title, "created": created, "platforms": chosen}


def dismiss_entry_decision(db: Session, user: models.User, key: str) -> dict:
    """Dismiss one review row — records "own it on nothing" so it stops asking,
    and creates nothing."""
    return confirm_entry_decision(db, user, key, [])


def _record_decision(db: Session, user_id: int, external_id: str, decision: dict) -> None:
    """Retire a played-only row with what was decided about it."""
    cand = (
        db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user_id,
            models.PsnReviewCandidate.external_id == external_id,
            models.PsnReviewCandidate.kind == "played_only",
        )
        .first()
    )
    if cand is None:
        raise ValueError("Played-only entry is not in the review queue.")
    cand.status = "dismissed" if decision.get("action") == "skipped" else "confirmed"
    # Reuses chosen_platforms as the decision record: for played-only the
    # answer is an action, not a platform list, and a second JSON column for
    # one variant is not worth a migration.
    cand.chosen_platforms = decision
    cand.reviewed_at = datetime.datetime.now(datetime.UTC)
    db.commit()


def _find_played_only(db: Session, user_id: int, external_id: str) -> dict | None:
    cand = (
        db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user_id,
            models.PsnReviewCandidate.external_id == external_id,
            models.PsnReviewCandidate.kind == "played_only",
        )
        .first()
    )
    return (cand.raw_data or {}) if cand else None


def import_played_only(db: Session, user: models.User, external_id: str) -> str:
    """User-clicked: import one played-only row as a library entry."""
    item = _find_played_only(db, user.id, external_id)
    if not item:
        raise ValueError("Played-only entry is not in the review queue.")
    platform_id = platform_for_item(db, item)
    if platform_id is None:
        raise ValueError("Cannot resolve a platform for this entry.")
    _import_one(db, user, item, platform_id)
    db.commit()
    _record_decision(db, user.id, external_id, {"action": "imported"})
    return item.get("displayName") or item.get("name") or external_id


def skip_played_only(db: Session, user: models.User, external_id: str) -> None:
    """User-clicked: record a skip so the row stops asking."""
    _record_decision(db, user.id, external_id, {"action": "skipped"})


def attach_played_only(db: Session, user: models.User, external_id: str, entry_id: int) -> str:
    """User-clicked: attach a played-only row's play stats to an existing
    library entry (the DMC5-SE-on-disc case — activity row and the owned
    game wear different Sony names). Explicit user action, so mutating the
    chosen entry's play stats is the point."""
    item = _find_played_only(db, user.id, external_id)
    if not item:
        raise ValueError("Played-only entry is not in the review queue.")
    entry = (
        db.query(models.UserLibraryEntry).filter(models.UserLibraryEntry.id == entry_id, models.UserLibraryEntry.user_id == user.id).first()
    )
    if entry is None:
        raise ValueError("Library entry not found.")
    playtime = duration_to_minutes(item.get("playDuration"))
    last_played = _parse_played_at(item.get("lastPlayed"))
    if playtime is not None:
        entry.playtime_minutes = playtime
    if last_played is not None:
        entry.last_played_at = last_played
    release = entry.release
    raw = dict(release.raw_data or {})
    raw["psn_played"] = {
        k: item.get(k) for k in ("titleId", "playCount", "playDuration", "firstPlayed", "lastPlayed", "service", "category")
    }
    release.raw_data = raw
    db.commit()
    _record_decision(db, user.id, external_id, {"action": "attached", "entry_id": entry_id})
    game = release.game
    return game.display_name or game.title
