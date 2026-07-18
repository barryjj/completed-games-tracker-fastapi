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

import httpx
from sqlalchemy.orm import Session

from . import models

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
    code = dict(httpx.QueryParams(location.split("redirect/", 1)[1])).get("code")
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


def _resolve_account_id(token: str, online_id: str) -> str:
    """Online ID → accountId via the legacy profile2 endpoint (what psn-api's
    getProfileFromUserName uses; the prototype resolved accountId this way)."""
    data = _bearer_get(
        token,
        _PROFILE_URL.format(online_id=online_id),
        params={"fields": "npId,onlineId,accountId"},
    )
    account_id = (data.get("profile") or {}).get("accountId")
    if not account_id:
        raise ValueError(f"Could not resolve PSN accountId for online id '{online_id}'.")
    return str(account_id)


# ─── Fetchers (all paginated — the prototype's biggest gap) ────────────────


def _fetch_purchased(token: str, account_id: str) -> list[dict]:
    """GraphQL getPurchasedGameList, paged 100 at a time (prototype-parity
    variables). Returns the raw game dicts."""
    out: list[dict] = []
    start = 0
    for _ in range(_MAX_PAGES):
        variables = {
            "isActive": True,
            "platform": ["ps4", "ps5"],
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
        games = ((data.get("data") or {}).get("purchasedTitlesRetrieve") or {}).get("games") or []
        out.extend(games)
        if len(games) < 100:
            break
        start += 100
        time.sleep(_PAGE_SLEEP_S)
    return out


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
_NON_GAME_ID_RE = re.compile(r"(DEMO\d*|BETA)$", re.IGNORECASE)


def _normalized_name(name: str | None) -> str:
    """Merge key: uppercase, Roman→Arabic (word-boundary), strip trademark
    glyphs and non-alphanumerics, lowercase. Port of the prototype's
    normalizedName."""
    if not name:
        return ""
    s = str(name).upper()
    for roman, arabic in _ROMAN:
        s = re.sub(rf"\b{roman}\b", arabic, s)
    s = re.sub(r"\(TM\)|™|®", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[^a-zA-Z0-9]+", "", s)
    return s.lower().strip()


def _display_name(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"\(TM\)|™|®", "", str(name), flags=re.IGNORECASE).strip()


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
        if existing is None:
            t_norm = _normalized_name(_item_name(t))
            existing = next(
                (v for v in current if v.get("normalizedName") and v["normalizedName"] == t_norm and _platforms_compatible(v, t)),
                None,
            )
        key = (
            (existing or {}).get("titleId")
            or (existing or {}).get("npCommunicationId")
            or t.get("npCommunicationId")
            or t.get("titleId")
            or _item_name(t)
        )
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
            "name": p.get("name") or p.get("localizedName") or (existing or {}).get("name"),
            "playCount": p.get("playCount", (existing or {}).get("playCount", 0)),
            "firstPlayed": p.get("firstPlayedDateTime") or (existing or {}).get("firstPlayed"),
            "lastPlayed": p.get("lastPlayedDateTime") or (existing or {}).get("lastPlayed"),
            "playDuration": p.get("playDuration") or (existing or {}).get("playDuration"),
            "category": p.get("category") or (existing or {}).get("category"),
            "sources": sorted(set((existing or {}).get("sources", []) + ["played"])),
            "platform": ((p.get("platform") or (existing or {}).get("platform") or "").upper() or None),
        }
        merged["normalizedName"] = _normalized_name(merged.get("name"))
        merged["displayName"] = _display_name(merged.get("name"))
        lib[key] = merged

    return {"merged": list(lib.values()), "filtered": filtered}


# ─── Snapshot + report (no library writes) ─────────────────────────────────


def snapshot_path(user_id: int) -> str:
    return os.path.join(DATA_DIR, f"psn_snapshot_user{user_id}.json")


def load_snapshot(user_id: int) -> dict | None:
    try:
        with open(snapshot_path(user_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def external_id_for(item: dict) -> str | None:
    """The id that will become GameRelease.external_id at import time —
    titleId first (joins purchased/played), npCommunicationId for trophy-only
    history, productId as last resort."""
    return item.get("titleId") or item.get("npCommunicationId") or item.get("productId") or None


def _build_report(db: Session, merged: list[dict], filtered: dict, totals: dict) -> dict:
    membership = Counter((m.get("membership") or "NONE") for m in merged if "purchased" in m.get("sources", []))
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
        "platforms": dict(platforms),
        "unresolvable_platforms": unresolvable,
        "no_external_id": sum(1 for i in ids if not i),
        "already_imported": already_imported,
        "new": len(merged) - already_imported,
        "sample": [
            {"name": m.get("displayName") or m.get("name"), "platform": m.get("platform"), "sources": m.get("sources")} for m in merged[:12]
        ],
    }


def fetch_snapshot(db: Session, user: models.User) -> dict:
    """Full crawl → merge → snapshot file → report dict. Touches NOTHING in
    the library — the import step is a separate, explicitly-triggered job
    (follow-up PR)."""
    if not user.psn_npsso:
        raise ValueError("A PSN NPSSO token is required.")
    if not user.psn_online_id:
        raise ValueError("Your PSN Online ID is required.")

    token = _exchange_npsso(user.psn_npsso)
    account_id = _resolve_account_id(token, user.psn_online_id)

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
    report = _build_report(db, result["merged"], result["filtered"], totals)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(snapshot_path(user.id), "w") as f:
        json.dump(
            {
                "fetched_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "report": report,
                "merged": result["merged"],
                "raw": {"purchased": purchased, "trophy_titles": titles, "played": played},
            },
            f,
        )
    return report
