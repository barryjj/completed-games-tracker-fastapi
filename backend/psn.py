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
import sqlalchemy as sa
from sqlalchemy.orm import Session

from . import models, psn_store, titles

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


# Streaming and media apps entitled through the STORE. is_game_category catches
# these in the played feed via its category ("ps5_web_based_media_app"), but the
# purchased feed carries no category at all — Amazon Prime Video and Bloodborne
# are byte-for-byte the same shape there: GameLibraryTitle, isDownloadable true,
# conceptId null. Sony gives nothing to tell them apart, so the list is explicit.
#
# Matched on the WHOLE normalized title, never a substring: "Netflix" as a
# substring is harmless, but the same shortcut on other entries eats "Zen
# Pinball 2" (pi-NBA-ll) and "Shadow Complex" (com-PLEX). Whole-title matching
# also keeps "NBA Playgrounds", a real game.
_MEDIA_APP_TITLES = frozenset(
    titles.normalize_for_match(n).replace(" ", "")
    for n in (
        "Amazon Prime Video",
        "Netflix",
        "HBO GO",
        "HBO Max",
        "Max",
        "Hulu",
        "Twitch",
        "YouTube",
        "Spotify",
        "Crunchyroll",
        "Funimation",
        "Disney+",
        "Peacock",
        "Plex",
        "Vudu",
        "PlayStation Vue",
        "PlayStation Video",
        "SONY PICTURES CORE",
        "Apple TV",
        "Tidal",
        "Pandora",
        "TuneIn",
        "WWE Network",
        "Redbox",
        "Media Player",
        # Sony's own utilities. Bought/entitled like games and indistinguishable
        # in the purchased feed — Share Factory Studio even carries a release
        # date and publisher — but there is nothing to complete.
        "Share Factory Studio",
        "SHAREfactory",
        "PlayStation App",
        "Remote Play",
        "PS Remote Play",
        "Media Gallery",
        "Web Browser",
        "Internet Browser",
    )
)


def is_media_app(item: dict) -> bool:
    """A streaming app dressed as a purchase — flagged for review, not dropped.

    Whole-title match, compared SPACELESS as the normalizer's docstring
    recommends: "PlayStation™Vue" loses its glyph and becomes one word, so a
    spaced comparison misses it.
    """
    return titles.normalize_for_match(_item_name(item)).replace(" ", "") in _MEDIA_APP_TITLES


def is_non_game(item: dict) -> bool:
    """Demo/beta/trial/soundtrack filter — name patterns plus DEMO/BETA
    suffixes on product/entitlement ids. Port of the prototype's isNonGame.

    Media apps are NOT dropped here. A demo is never a completion, so silently
    filtering it is safe; "do you want Netflix in your library" is a preference
    and not ours to decide. They go to review instead — see is_media_app.
    """
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


# Every PlayStation platform, not just the two the merge used to know about.
# "ps4" is checked before "ps3" only so ordering never matters for substring
# collisions; each token is matched independently.
_PLATFORM_TOKENS = (
    ("ps5", "ps5"),
    ("ps4", "ps4"),
    ("ps3", "ps3"),
    ("psvita", "psvita"),
    ("vita", "psvita"),
    ("psp", "psp"),
)


def _platforms_of(item: dict) -> set[str]:
    """Every PlayStation platform an item names.

    A SET because a trophy set legitimately covers several ("PS3,PSVITA,PS4")
    and collapsing that to one value threw away the only thing that makes a
    cross-buy title distinguishable from a false match.
    """
    p = item.get("platform") or item.get("trophyTitlePlatform") or item.get("category")
    if not p:
        return set()
    lc = str(p).lower()
    return {norm for token, norm in _PLATFORM_TOKENS if token in lc}


def _platforms_compatible(a: dict, b: dict) -> bool:
    """Can these two rows be the same release?

    Requires the platform sets to OVERLAP. The old version recognised only PS4
    and PS5, so PS3 and Vita returned None and "no opinion" was read as
    compatible — which merged a PS4 purchase into a PS3 trophy set by name
    alone. In the real library that mislabelled every one of 11 PS3/Vita
    entries, overwrote titles ("Gravity Rush Remastered" became "GRAVITY RUSH"),
    and suppressed the cross-play question on games that genuinely have it:
    Sound Shapes' PS3 set merged into its PS4 purchase, so the row looked
    single-platform and nothing ever asked.

    Unknown on either side still means "no opinion" — a played row with no
    category must not be blocked from merging.
    """
    pa, pb = _platforms_of(a), _platforms_of(b)
    if not pa or not pb:
        return True
    return bool(pa & pb)


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
            # concept.titleIds is the whole FAMILY — every region, edition and
            # bonus SKU Sony ever shipped under this concept (37 of them for
            # ELDEN RING). So it matches things that are emphatically not the
            # game: the "ELDEN RING Adventure Guide" pre-order bonus carries
            # titleId CUSA30022_00, which is in that list, and founded a
            # purchased record under it. The 140-hour PS5 play record then
            # merged INTO the guide, so the library showed the bonus item's
            # name and the real game survived only as an orphaned trophy row.
            #
            # Platform overlap is the guard, and the name-match fallback below
            # has always required it — the concept branch simply skipped it.
            # PS5 activity cannot be the PS4 guide, and a genuine cross-gen
            # pair shares the platform its play record names.
            compatible = [v for v in current if _platforms_compatible(v, p)]
            existing = _find_by_any_id(compatible, p["concept"]["titleIds"])
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


def is_pc_copy(item: dict) -> bool:
    """True when this is the PC copy of a game, surfacing through PSN's PC
    integration rather than anything you own on a PlayStation.

    The test is evidence, not Sony's platform string, because Sony is not
    consistent about it: Stellar Blade came back PSPC-only while the Until Dawn
    remake came back "PS5,PSPC" for the identical situation. Requiring the set
    to be PC-ONLY therefore missed the second one and minted a phantom PS5
    entry for a game only owned on Steam.

    So: the set mentions PSPC and there is no PlayStation-side evidence — no
    purchase, and no play time under a native console category. Trophies alone
    prove the game was played, never which platform it ran on, so they are
    deliberately not evidence here.

    The Steam library used to be REQUIRED as corroboration, which made the
    answer depend on whether Steam happened to be synced yet: MARVEL Tōkon
    (PS5,PSPC set, pspc_game play only, no purchase) minted a phantom PS5 entry
    purely because its Steam copy had not been imported. Steam adds confidence
    but nothing the absence of native play does not already establish — with no
    purchase and no console play there is no PlayStation copy to have an
    opinion about.

    Conflicting evidence still disqualifies: play under a native category, or
    any minutes on a console platform, means a PlayStation copy exists and this
    is not merely the PC one.
    """
    plats = {p.strip().upper() for p in str(item.get("platform") or "").split(",") if p.strip()}
    cats = list(item.get("playCategories") or [])
    if not cats and item.get("category"):
        cats = [item["category"]]
    if "PSPC" not in plats and not any("pspc" in str(c).lower() for c in cats):
        return False
    if "purchased" in (item.get("sources") or []):
        return False
    if any(_NATIVE_PS_CAT_RE.search(str(c)) for c in cats):
        return False
    # PSPC minutes are PC minutes — the very thing being skipped — so only
    # console platforms count as evidence of a PlayStation copy.
    return not any(minutes for platform, minutes in played_minutes_by_platform(item).items() if platform != "PSPC")


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
        # `aliases` exist because one product ships under several names across
        # regions and re-releases (The Sly Trilogy / The Sly Collection).
        for name in [entry["title"], *(row.get("aliases") or [])]:
            if name:
                index["by_title"].append((titles.normalize_for_match(name), entry))
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


def review_verdict(cand, item: dict) -> dict:
    """What the sync WOULD have done — reported, not acted on (#180).

    Two independent questions, and a row is routinely certain about one and not
    the other: ELDEN RING is settled on both, while HITMAN 2 Expansion has
    exactly one platform and no IGDB identity at all. So "would auto-import"
    requires BOTH, and when it does not hold the badge names the half that is
    missing rather than saying a useless "not sure".

    Identity counts as settled only when IGDB returned an id AND had no rename
    to propose — a pending rename is a judgement call by definition, and a
    wrong id attaches wrong metadata quietly.

    NOTHING acts on this yet. It exists so the verdict can be watched being
    right across a real library before a single row is allowed to import on its
    own; flipping that on later is one branch on `auto`.
    """
    # Settled means the IMPORT would not have had to ask, which is candidate
    # COUNT — not resolve_platform_choice's confidence. Those disagree: a
    # cross-buy set with play time on one console reports a confident platform
    # while still being queued, because the question "which do you own" is not
    # answered by "where you played". Reporting that row as platform-settled
    # would promise automation the import would never actually perform.
    platform_ok = len(platform_candidates(item)) <= 1
    identity_ok = bool(cand.proposed_igdb_id) and not cand.proposed_title
    if platform_ok and identity_ok:
        return {"auto": True, "holdout": None, "label": "Would auto-import"}
    if not platform_ok and not identity_ok:
        holdout = "platform and identity"
    elif platform_ok:
        holdout = "identity"
    else:
        holdout = "platform"
    return {"auto": False, "holdout": holdout, "label": f"Needs you: {holdout}"}


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
                # Present only when an IGDB proposal was accepted in review
                # (#180). It is the payload that later unblocks metadata (#161)
                # and the different-igdb_id match veto, which is inert today
                # because no PSN entry carries one.
                igdb_id=item.get("igdbId"),
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
    # Cleaned here rather than trusting the merge to have done it: the queue is
    # where a human reads the name, and "God of War II Trophies" is exactly the
    # trophy-set noise this review exists to strip (#180).
    row.title = _display_name(item.get("displayName") or item.get("name")) or ext_id
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
    reopened = 0  # decided rows Sony can now tell us more about (#180)
    played_only = 0
    # Only hold back rows that would be NEWLY created. A trophy-only entry
    # already in the library still needs its re-sync refresh — diverting it to
    # review would not un-import it, just stop it updating (#180).
    existing_psn_ids = {
        ext
        for (ext,) in db.query(models.GameRelease.external_id)
        .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(models.UserLibraryEntry.user_id == user.id, models.GameRelease.source == "psn")
        .distinct()
        .all()
        if ext
    }
    # One query, not one per game — this loop runs over ~1000 items.
    decided = {
        r.external_id: r
        for r in db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user.id, models.PsnReviewCandidate.status != "pending")
        .all()
    }
    # Identity vetting needs somewhere to check identity AGAINST. With no IGDB
    # credentials the queue would ask questions nothing can answer, so the
    # pre-creation review only switches on when a lookup is actually available.
    vetting = bool(user.twitch_client_id and user.twitch_client_secret)
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
        if is_pc_copy(item):
            skipped_pc_dupe += 1
            continue
        if not external_id_for(item):
            skipped_no_id += 1
            continue
        # An already-decided cross-play game re-imports its chosen platforms, so
        # a re-sync refreshes their playtime like any other psn row. A dismissed
        # one has an empty list and so imports nothing.
        row = decided.get(external_id_for(item))
        # Sony's data improves over time — a lapsed PS+ that gets renewed puts
        # purchases back in the feed. When that gives us a way to identify
        # something we could not before, the sync has a change to propose, so
        # the row returns to the queue instead of sitting wrong forever.
        if row is not None and vetting and _can_do_better_now(row, item):
            _reopen_for_better_data(row, item)
            reopened += 1
            continue
        if row is None and len(platform_candidates(item)) > 1:
            if _upsert_review_candidate(db, user, item, "cross_play") is not None:
                needs_review += 1
            continue
        # Trophy-only rows are named after their trophy SET, and those names are
        # frequently wrong — localized, abbreviated, or missing a franchise
        # prefix. Sony can never improve them: no store record exists for these
        # generations (#181) and there is no playtime or metadata to fall back
        # on. Importing one silently is how "SF3: Online Edition" became a
        # library entry nobody knew was wrong.
        #
        # So they are held back for review rather than created. The entry is
        # made on confirm, under whichever name the user approves — there is no
        # rename path because nothing is written under the bad name first.
        if row is None and external_id_for(item) not in existing_psn_ids and is_media_app(item):
            if _upsert_review_candidate(db, user, item, "media_app") is not None:
                needs_review += 1
            continue
        # EVERY new title is vetted before it lands, not just trophy-only ones.
        #
        # The original rule here was `is_trophy_only(...)`, on the theory that
        # Sony's STORE names are fine and only trophy-set names go wrong. The
        # PS4/PS5 import disproved that: "Ghost of Tsushima" arrived named
        # "Ghost of Tsushima Legends" because Sony retitled the store page,
        # "Mass Effect: Andromeda - Super Deluxe Edition" arrived with edition
        # words nobody wants, and "ELDEN RING Adventure Guide" arrived as if it
        # were a game. All three are store-backed, and all three used to import
        # silently — leaving the user to find them in their own library and
        # clean up by hand.
        #
        # So identity is settled BEFORE creation, for everything. Confirming
        # creates the entry under the approved name; nothing is ever written
        # under a name we already suspect. Rows that are obviously right still
        # cost a click today — that is deliberate (shadow mode), so the verdict
        # can be watched being correct before any of it is automated.
        #
        # WITHOUT IGDB credentials none of that is possible, so none of it is
        # imposed: there is nothing to check the name against, Sony's name is
        # the only name there is, and parking the whole library behind a click
        # would buy the user clicking and no information. They get the direct
        # import, exactly as it behaved before this change. Adding credentials
        # later is not a dead end either — the "IGDB match" job walks entries
        # that are already in the library and catches them up.
        # Trophy-only rows are held back either way: their names come from a
        # trophy SET, Sony has no store record to improve them from (#181), and
        # that was true before IGDB was ever in the picture. IGDB only widens
        # the net to store-backed titles.
        held_back = vetting or is_trophy_only(external_id_for(item))
        if held_back and row is None and external_id_for(item) not in existing_psn_ids:
            if _upsert_review_candidate(db, user, item, "title_fix") is not None:
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
        "reopened": reopened,
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
    # The lookup belongs HERE, not behind a second button. A row that says only
    # "held back for review" is a chore, not a review: the user still has to
    # work out what the game actually is. Arriving with IGDB's answer already
    # attached is what makes the queue answerable in one pass — and the verdict
    # has nothing to report until this has run.
    #
    # Self-gating on credentials and on rows that already have a proposal, so a
    # re-sync costs nothing and a credential-less user skips it entirely.
    proposals = fill_review_proposals(db, user)
    result = {**result, "proposals": proposals}
    user.psn_last_sync_report = report
    user.psn_last_synced_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    _write_debug_dump(user.id, report, merged, raw)
    return {**result, "report": report}


# ─── Played-only review actions ────────────────────────────────────────────


def decided_rows(db: Session, user_id: int) -> list[dict]:
    """Rows already confirmed or dismissed, so a decision can be revisited.

    The cross-play queue was the only review surface where a decision vanished:
    import review keeps a Confirmed tab, played-only leaves decided rows inline,
    and this one filtered them out with no way back. Dismissing something by
    mistake was unrecoverable from the UI (#180).
    """
    rows = (
        db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user_id,
            models.PsnReviewCandidate.kind.in_(_QUEUE_KINDS),
            models.PsnReviewCandidate.status != "pending",
        )
        .all()
    )
    out = []
    for cand in rows:
        out.append(
            {
                "key": cand.external_id,
                "external_id": cand.external_id,
                "name": cand.proposed_title or cand.title,
                "kind": cand.kind,
                "status": cand.status,
                "chosen_platforms": cand.chosen_platforms or [],
                "image": cand.thumbnail_url,
                "hero": cand.hero_url or cand.thumbnail_url,
                "logo": cand.logo_url,
                "reviewed_at": cand.reviewed_at,
            }
        )
    out.sort(key=lambda r: (r["name"] or "").casefold())
    return out


def reopen_decision(db: Session, user: models.User, key: str) -> dict:
    """Put a decided row back in the queue.

    Only the row is reopened — entries the confirm created are left alone. A
    re-confirm upserts them rather than duplicating, and silently deleting
    library rows from an "undo" is a bigger surprise than leaving them.
    """
    cand = (
        db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user.id,
            models.PsnReviewCandidate.external_id == key,
            models.PsnReviewCandidate.kind.in_(_QUEUE_KINDS),
        )
        .first()
    )
    if cand is None:
        raise ValueError("That row is not in the PSN review queue.")
    cand.status = "pending"
    cand.chosen_platforms = None
    cand.reviewed_at = None
    db.commit()
    return {"name": cand.proposed_title or cand.title}


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
                # Same art the cross-play rows get — these are rows of the same
                # table and the fill job never distinguished them; only the
                # templates did, which made one queue look unfinished.
                "image": cand.thumbnail_url or (item.get("image") or {}).get("url"),
                "hero": cand.hero_url or cand.thumbnail_url,
                "logo": cand.logo_url,
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
    """Pending review rows — one per trophy set, one checkbox per platform.

    ONE queue. A row can be waiting on its platforms (cross_play), on its name
    (title_fix), or on both, and a row needing both must be decided once rather
    than chased through two lists (#180).

    A cross-play set covers several platforms and never says which you own, and
    cross-buy means the answer is usually ALL of them (Shovel Knight: Treasure
    Trove is a single PS3,PSVITA,PS4 set) — so platforms are checkboxes,
    pre-ticked with every platform in the set. Not filtered by hardware owned:
    one cross-buy purchase puts every version on the account whether or not the
    console was ever in the house. Narrowing comes from the two things that
    actually know something — a cross-buy exception (sold separately per
    platform) and an accepted IGDB proposal, which restricts to the platforms
    the game really shipped on. Unticking them all is the same as dismissing.

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
            # ONE queue. A trophy set can need its name approved, its
            # platforms chosen, or BOTH — and a row needing both must be
            # decided once, in one place. Splitting them would mean a game
            # required visiting two queues to import, with no ordering
            # guarantee between them (#180).
            models.PsnReviewCandidate.kind.in_(("cross_play", "title_fix", "media_app", "igdb_link")),
            models.PsnReviewCandidate.status == "pending",
        )
        .all()
    )

    # Siblings = every cross-play set sharing a normalized title, DECIDED ONES
    # INCLUDED. Two things need them:
    #
    #   1. "Set 1 of 2" has to keep saying 2 after one is confirmed, or the
    #      surviving row stops explaining why it looked like a duplicate.
    #   2. A platform a sibling already claimed is spoken for. Both Crimsonland
    #      sets declare the identical PS3,PSVITA,PS4 — nothing distinguishes
    #      them — so without this both could claim the same platform and
    #      _import_one would silently drop the second as a (game, platform)
    #      conflict. That's how the 90% set's data vanished and Vita ended up
    #      showing 23%.
    siblings: dict[str, list] = {}
    for cand in (
        db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user_id, models.PsnReviewCandidate.kind == "cross_play")
        .all()
    ):
        siblings.setdefault((cand.raw_data or {}).get("normalizedName") or "", []).append(cand)
    for group in siblings.values():
        group.sort(key=lambda c: c.external_id or "")

    claimed_by: dict[str, dict[str, str]] = {}
    for name, group in siblings.items():
        for pos, cand in enumerate(group):
            if cand.status == "pending":
                continue
            for platform in cand.chosen_platforms or []:
                claimed_by.setdefault(name, {})[platform] = f"set {pos + 1}"

    rows = []
    for cand in candidates:
        item = cand.raw_data or {}
        options = platform_candidates(item)
        if not options:
            continue
        name = item.get("normalizedName") or ""
        group = siblings.get(name, [])
        sets_for_title = len(group) or 1
        set_index = next((i + 1 for i, c in enumerate(group) if c.external_id == cand.external_id), 1)
        taken = claimed_by.get(name, {})

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

        # A pending IGDB proposal NARROWS the choice to the platforms that game
        # actually shipped on. Shinovi Versus' trophy set claims PS3,PSVITA and
        # IGDB says Vita — the phantom PS3 vanishing is the correction, so
        # offering it anyway would be offering the bad data back.
        #
        # Rejecting restores the full set: the proposal was stored alongside
        # options, never over them.
        proposed_opts = [p for p in options if _platform_in_igdb_set(db, p, cand.proposed_platforms)]
        if cand.proposal_status in ("pending", "matched") and proposed_opts:
            options = proposed_opts

        # Default: EVERY platform the trophy set covers.
        #
        # Not filtered by hardware owned. A cross-buy purchase puts every
        # version on the account whether or not you ever had the console — the
        # PS3 copy of a PS3/PS4/Vita set is yours either way — so "do you own a
        # PS3" is the wrong question and answering it narrowed the pre-ticks
        # for no reason.
        #
        # Ambiguity is handled by the two mechanisms that actually know
        # something: a cross-buy EXCEPTION (sold separately per platform)
        # restricts to evidenced platforms below, and an accepted IGDB proposal
        # narrows the options to the platforms that game genuinely shipped on.
        default = list(options)
        # Never pre-tick a platform a sibling set already took. It stays
        # offered — you might know something the data doesn't — but choosing it
        # is now a deliberate override rather than the path of least resistance.
        default = [p for p in default if p not in taken] or [p for p in options if p not in taken]

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

        # The reason line has to answer "why is THIS row in front of me". It
        # only ever reported platform reasoning, so a media app read "Only one
        # platform on this trophy set" — true, and not remotely why it is here.
        # The queue became multi-kind; this text never did.
        if cand.kind == "media_app":
            reason = "Looks like a media app rather than a game"
        elif cand.kind == "igdb_link":
            # Already in the library — confirming UPDATES it rather than
            # creating anything, so the row must not read like a new import.
            reason = "Already in your library — confirming links it to IGDB"
        elif cand.kind == "title_fix" and len(options) <= 1:
            reason = "Confirming what this is before it lands in your library"

        verdict = review_verdict(cand, item)
        earned = item.get("earnedTrophies") or {}
        defined = item.get("trophies") or {}
        rows.append(
            {
                "key": cand.external_id,
                "external_id": cand.external_id,
                "name": cand.title,
                "reason": reason,
                # Shadow mode: what the sync would have done on its own.
                "verdict_auto": verdict["auto"],
                "verdict_label": verdict["label"],
                # SGDB grid first — PSN's own image is a square icon0.png, the
                # wrong shape for a review card's hero.
                "image": cand.thumbnail_url or (item.get("image") or {}).get("url") or item.get("trophyIconUrl"),
                # Card view shows a hero with the logo over it, like every other
                # review card. Falls back to the grid so a row whose hero fetch
                # failed still renders something rather than an empty box.
                "hero": cand.hero_url or cand.thumbnail_url or (item.get("image") or {}).get("url"),
                "logo": cand.logo_url,
                "trophy_progress": item.get("trophyProgress"),
                "trophy_earned": sum(v or 0 for v in earned.values()),
                "trophy_defined": sum(v or 0 for v in defined.values()),
                # Per-tier, Sony's own order. The feed already carries this and
                # a summed chip threw it away — it's also the shape trophy
                # tracking (#136) needs, so surfacing it now is a step toward
                # that rather than decoration.
                "trophy_tiers": [
                    {"tier": tier, "earned": earned.get(tier) or 0, "defined": defined.get(tier) or 0}
                    for tier in ("platinum", "gold", "silver", "bronze")
                    if defined.get(tier)
                ],
                "trophy_last_updated": (item.get("trophyLastUpdated") or "")[:10],
                "set_index": set_index,
                "set_count": sets_for_title,
                "last_played": (item.get("lastPlayed") or "")[:10] if trusted else "",
                "total_minutes": sum(minutes.values()),
                "restricted": bool(exception and exception["restricts"]),
                # ── IGDB name proposal (#180) ──────────────────────────────
                # "pending" = a suggestion to accept or reject.
                # "none"    = looked up and NOT identified. Still shown: an
                #             unidentified row kept invisible is exactly how
                #             "SF3: Online Edition" reached the library.
                # A row can be here for its NAME (title_fix) or its
                # PLATFORMS (cross_play) or both — one queue, so the view has
                # to know which questions this row is actually asking.
                "kind_is_title_fix": cand.kind == "title_fix",
                "proposal_status": cand.proposal_status,
                # "matched" rows are identified and correctly named — nothing
                # to decide, so the row must not nag. Only a "pending" rename
                # is a question.
                "igdb_matched": cand.proposal_status in ("matched", "pending") and bool(cand.proposed_igdb_id),
                "proposed_title": cand.proposed_title,
                "proposed_igdb_id": cand.proposed_igdb_id,
                # Accepting narrows the platform choice to what IGDB confirms.
                # Shinovi Versus' trophy set claims PS3,PSVITA; IGDB says Vita,
                # and the phantom PS3 disappearing IS the correction.
                "proposed_platforms": proposed_opts,
                "options": [
                    {"platform": p, "selected": p in default, "minutes": minutes.get(p, 0), "claimed_by": taken.get(p)} for p in options
                ],
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
            # hero_url too: rows cached before the card used hero art have a
            # thumbnail but no hero, and would otherwise never be topped up.
            sa.or_(models.PsnReviewCandidate.thumbnail_url.is_(None), models.PsnReviewCandidate.hero_url.is_(None)),
        )
        .all()
        if c.title
    ]


def save_review_thumbnails(db: Session, user_id: int, art: dict[str, dict]) -> int:
    """Cache SGDB art onto the review rows. Values are {thumbnail_url, hero_url,
    logo_url} — the list view wants the grid, the card wants hero + logo."""
    if not art:
        return 0
    written = 0
    for cand in (
        db.query(models.PsnReviewCandidate)
        .filter(models.PsnReviewCandidate.user_id == user_id, models.PsnReviewCandidate.external_id.in_(list(art)))
        .all()
    ):
        row = art[cand.external_id]
        cand.thumbnail_url = row.get("thumbnail_url") or cand.thumbnail_url
        cand.hero_url = row.get("hero_url") or cand.hero_url
        cand.logo_url = row.get("logo_url") or cand.logo_url
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


# The review queue holds two kinds and they act identically: a row can be
# waiting on its platforms, its name, or both (#180). Confirm/dismiss/reject
# must reach either, or the title_fix rows — the large majority — silently fail
# on click with "no longer in the queue".
_QUEUE_KINDS = ("cross_play", "title_fix", "media_app", "igdb_link")


def _pending_candidate(db: Session, user_id: int, key: str, kind: str | tuple[str, ...] = _QUEUE_KINDS):
    kinds = (kind,) if isinstance(kind, str) else kind
    return (
        db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user_id,
            models.PsnReviewCandidate.external_id == key,
            models.PsnReviewCandidate.kind.in_(kinds),
            models.PsnReviewCandidate.status == "pending",
        )
        .first()
    )


def rename_candidate(db: Session, user: models.User, key: str, title: str, igdb_id: int | None = None) -> dict:
    """Set the name a review row will be created under (#180).

    Stored as the proposal so confirm picks it up through the existing path —
    there is one place that decides the final name, not two.

    Marked "accepted" because a name the user typed is, by definition, decided.
    The igdb_id is dropped unless the typed name IS the suggestion: overruling
    the match means its metadata belongs to a different game.
    """
    cand = _pending_candidate(db, user.id, key)
    if cand is None:
        raise ValueError("That trophy set is not in the PSN review queue.")
    typed = (title or "").strip()
    if not typed:
        raise ValueError("A name is required.")
    # An id picked from the modal's IGDB search is a deliberate CORRECTION of the
    # automatic match and wins outright. Otherwise the existing id survives only
    # while the name still matches what it was for — a hand-typed name that
    # overrules the match must not keep metadata for a different game.
    if igdb_id is not None:
        cand.proposed_igdb_id = igdb_id
    else:
        keeps_id = bool(cand.proposed_igdb_id) and (
            titles.normalize_for_match(typed) == titles.normalize_for_match(cand.proposed_title or cand.title)
        )
        if not keeps_id:
            cand.proposed_igdb_id = None
    cand.proposed_title = None if titles.normalize_for_match(typed) == titles.normalize_for_match(cand.title) else typed
    cand.proposal_status = "accepted" if cand.proposed_title else ("matched" if cand.proposed_igdb_id else "none")
    db.commit()
    return {"name": cand.proposed_title or cand.title}


def reject_proposal(db: Session, user: models.User, key: str) -> dict:
    """Reject an IGDB suggestion — the row reverts to raw PSN data (#180).

    The whole match goes, not just the name. A lookup that got the name wrong
    has no claim to be right about the platforms it returned: a bad match for
    "閃乱カグラ SHINOVI VERSUS" could plausibly return some Shinobi title on
    PS3, and honouring its platform list would then map the row to the wrong
    name AND the wrong platform — worse than the trophy data we started with,
    and harder to spot because it looks authoritative.

    Lossless by construction: the proposal was only ever stored alongside the
    raw title and its original platform options, never applied over them.
    """
    cand = _pending_candidate(db, user.id, key)
    if cand is None:
        raise ValueError("That trophy set is not in the PSN review queue.")
    cand.proposed_title = None
    cand.proposed_igdb_id = None
    cand.proposed_platforms = None
    cand.proposal_status = "rejected"
    db.commit()
    return {"name": cand.title}


def confirm_entry_decision(
    db: Session,
    user: models.User,
    key: str,
    platforms: list[str],
    use_proposed: bool = False,
    custom_title: str = "",
) -> dict:
    """Confirm one review row: create its entries and retire the row.

    The review IS the action — match review merges on click, import review
    confirms on click. An empty platform list is a real decision ("own it on
    nothing"), which is exactly what Dismiss sends.
    """
    cand = _pending_candidate(db, user.id, key)
    if cand is None:
        raise ValueError("That trophy set is not in the PSN review queue.")

    # An igdb_link row is ALREADY in the library — it imported cleanly and only
    # its identity is in question. Confirming attaches the id to the existing
    # entry; there is nothing to create, and creating would duplicate it (#180).
    if cand.kind == "igdb_link":
        chosen = cand.proposed_igdb_id
        rel = (
            db.query(models.GameRelease)
            .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
            .filter(
                models.UserLibraryEntry.user_id == user.id,
                models.GameRelease.source == "psn",
                models.GameRelease.external_id == cand.external_id,
            )
            .first()
        )
        if rel is not None and rel.game is not None and chosen:
            rel.game.igdb_id = chosen
        cand.status = "confirmed"
        cand.proposal_status = "accepted"
        cand.reviewed_at = datetime.datetime.now(datetime.UTC)
        db.commit()
        return {"name": cand.title, "created": 0, "platforms": [], "siblings_stale": False}

    item = dict(cand.raw_data or {})
    # Name resolution, most specific first:
    #   1. a name typed in the review row — when Sony's trophy-set name AND the
    #      IGDB suggestion are both wrong, the fix belongs here. Otherwise the
    #      only route is accepting a name you know is bad to create the entry,
    #      then editing it in the library afterwards.
    #   2. the accepted IGDB suggestion
    #   3. Sony's own trophy-set name
    # Applied AT CREATION in every case, so a bad name is never written.
    typed = (custom_title or "").strip()
    accepted = bool(use_proposed and cand.proposed_title)
    # A "matched" row has no rename to approve — IGDB knows the game and our
    # name is already right — but its id is the payload and must still attach.
    if cand.proposal_status == "matched" and cand.proposed_igdb_id and not typed:
        item["igdbId"] = cand.proposed_igdb_id
    same_as_proposed = accepted and titles.normalize_for_match(typed) == titles.normalize_for_match(cand.proposed_title)
    if typed:
        item["displayName"] = typed
        item["name"] = typed
        # The id only travels with the name IGDB actually proposed. A typed
        # name is the user overruling the match, so claiming its id would
        # attach metadata for a game they just said this isn't.
        if same_as_proposed:
            item["igdbId"] = cand.proposed_igdb_id
    elif accepted:
        item["displayName"] = cand.proposed_title
        item["name"] = cand.proposed_title
        item["igdbId"] = cand.proposed_igdb_id
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

    if accepted:
        cand.proposal_status = "accepted"
    cand.status = "confirmed" if chosen else "dismissed"
    cand.chosen_platforms = chosen
    cand.reviewed_at = datetime.datetime.now(datetime.UTC)
    db.commit()

    # Sibling sets for the same title are already rendered, and their platform
    # options just changed — what this row claimed is no longer free. The caller
    # refreshes the queue rather than leaving stale cards offering platforms
    # that are now spoken for.
    name = (item.get("normalizedName") or "").strip()
    stale = bool(name) and any(
        (c.raw_data or {}).get("normalizedName") == name
        for c in db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user.id,
            models.PsnReviewCandidate.kind == "cross_play",
            models.PsnReviewCandidate.status == "pending",
        )
        .all()
    )
    return {"name": cand.title, "created": created, "platforms": chosen, "siblings_stale": stale}


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


# ─────────────────────────────────────────────────────────────────────────────
# IGDB title proposals for trophy-only entries (#180)
# ─────────────────────────────────────────────────────────────────────────────

# A trophy-only row's external_id is an npCommunicationId. Those rows have no
# store record behind them — the purchased feed can never return PS3/Vita/PSP
# entitlements (#181) — so their name comes from the trophy SET, and trophy set
# names are frequently localized, abbreviated, or missing a franchise prefix.
_TROPHY_ONLY_PREFIX = "NPWR"

# Latin letters/digits/punctuation. Used to pull the searchable run out of a
# mixed-script title: IGDB returns NOTHING for "閃乱カグラ SHINOVI VERSUS" but
# matches "SHINOVI VERSUS" exactly. Verified against the live API 2026-08-07.
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 '&:!.\-]*")


def is_trophy_only(external_id: str | None) -> bool:
    """True for rows named after a trophy set rather than a store listing."""
    return bool(external_id) and str(external_id).upper().startswith(_TROPHY_ONLY_PREFIX)


def search_terms(title: str) -> list[str]:
    """Search terms to try for a title, most-specific first.

    The raw title first. Then, only if the title mixes scripts, its Latin run —
    because IGDB's search returns zero results for a mixed Japanese/Latin string
    and an exact hit for the Latin part alone. Splitting is free; romanizing the
    non-Latin run would need a transliteration dependency and buys nothing until
    a title turns up with no Latin run at all.
    """
    if not title:
        return []
    terms = [title.strip()]
    latin = " ".join(m.group(0).strip() for m in _LATIN_RUN_RE.finditer(title)).strip()
    # Only a fallback when it is genuinely a SUBSET — for a pure-Latin title the
    # run equals the title and retrying it would just repeat the same query.
    if latin and latin.casefold() != title.strip().casefold() and len(latin) >= 3:
        terms.append(latin)
    return terms


def igdb_platform_ids(db: Session, platform_tokens: list[str]) -> list[int]:
    """Trophy platform tokens ("PS3", "PSVITA") -> IGDB platform ids.

    Goes through resolve_platform_id and the platforms table's own igdb_id
    rather than matching names: the app's platform rows already carry IGDB ids
    (74 of 75; the exception is the Steam construct), so there is nothing to map.
    """
    out: list[int] = []
    for token in platform_tokens:
        pid = models.resolve_platform_id(db, token)
        if not pid:
            continue
        row = db.query(models.Platform).filter(models.Platform.id == pid).first()
        if row and row.igdb_id and row.igdb_id not in out:
            out.append(row.igdb_id)
    return out


# IGDB's own game_type. Authoritative where guessing from the title was not:
# a hand-rolled word list called "Devil May Cry HD Collection" a different
# product (right) and "Assassin's Creed Chronicles Trilogy Pack" one too
# (wrong), because both contain "collection"/"pack". IGDB just says which is a
# bundle.
_IGDB_MAIN_GAME = 0
# An episode of an episodic game. Resolves to its series via parent_game.
_IGDB_EPISODE = 6
# Things you buy on top of a game you already own, or that package several: not
# a better NAME for one trophy set. "Metal Gear Solid 4 Database" is type 1 and
# was outranking "Metal Gear Solid 4: Guns of the Patriots" purely because it
# added fewer words to the title.
_IGDB_NOT_A_GAME_ON_ITS_OWN = {1, 2, 3, 5, 7, 13, 14}


def _is_same_game(searched: str, found: str) -> bool:
    """Is `found` a better NAME for `searched`, or a different thing entirely?

    Title-shape check only — what KIND of release it is now comes from IGDB's
    game_type, which is authoritative. This just stops an unrelated title
    sneaking through when IGDB's search reaches for something loosely similar.

    Accepted:
      * the same title once folded (differing only in punctuation/case)
      * the found title EXTENDS the searched one — "Senran Kagura: Shinovi
        Versus" for "SHINOVI VERSUS", "Grand Theft Auto IV" for "GTA IV"
    """
    a = titles.normalize_for_match(searched)
    b = titles.normalize_for_match(found)
    if not a or not b:
        return False
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return True
    a_toks, b_toks = a.split(), b.split()
    # A numeral appended DIRECTLY after our title is a different installment,
    # not a fuller name for the same game: "Stealth Inc." -> "Stealth Inc 2: A
    # Game of Clones". Position is what distinguishes it from a franchise
    # prefix, which puts its numeral BEFORE our words — "Skyrim" -> "The Elder
    # Scrolls V: Skyrim" is the same game and must survive.
    if len(b_toks) > len(a_toks) and b_toks[: len(a_toks)] == a_toks and b_toks[len(a_toks)].isdigit():
        return False
    if set(a_toks).issubset(set(b_toks)):
        return True
    # The reverse: IGDB returned the CANONICAL name where ours was the
    # abbreviation ("GTA IV" -> "Grand Theft Auto IV"). Nothing to compare
    # token-wise, so require the numerals to agree at least.
    a_nums = [t for t in a_toks if t.isdigit()]
    b_nums = [t for t in b_toks if t.isdigit()]
    return bool(a_nums) and a_nums == b_nums


def _added_words(searched: str, found: str) -> int:
    """How many words `found` adds to `searched`, both ALREADY normalized.

    A ranking signal, not a similarity score. A canonical name is SUPPOSED to
    add words — "Call of Duty: Modern Warfare 2" for "Modern Warfare 2" — so
    scoring by overlap would punish the corrections we want. Used only to
    separate candidates of the same game_type.
    """
    return len(set(found.split()) - set(searched.split()))


def _collapse_edition(hit: dict) -> dict:
    """Fold an IGDB *edition* back onto the game it repackages (#180).

    IGDB has two parent links and they do not mean the same thing:

      version_parent + game_type main -> the same game in a different box.
        "Mass Effect: Andromeda - Super Deluxe Edition" IS Mass Effect:
        Andromeda, and the edition words are noise nobody wants in a library.

      parent_game -> derived but genuinely distinct. "Devil May Cry 5: Special
        Edition" (expanded) and "Ghost of Tsushima: Legends" (standalone
        expansion) are their own products and keep their own names and ids.

    Collapsing here rather than filtering means the rest of the matcher sees
    the parent, so an edition hit can exact-match a plain trophy title instead
    of being discarded. The old `version_parent = null` query filter could not
    make this distinction: it hid editions correctly but also hid DMC5SE, which
    is why that game reported "no IGDB match" while existing on IGDB.
    """
    parent = hit.get("version_parent")
    if hit.get("game_type") == _IGDB_MAIN_GAME and isinstance(parent, dict) and parent.get("id") and parent.get("name"):
        return {**hit, "id": parent["id"], "name": parent["name"], "collapsed_from": hit.get("name")}

    # An EPISODE resolves to the series it belongs to. A concept page names the
    # current SKU, and for an episodic game that is episode one — searched
    # verbatim, IGDB returns only "Batman: The Telltale Series - Episode 1:
    # Realm of Shadows" and we would propose one chapter as the whole game.
    #
    # Following IGDB's own parent_game rather than stripping "Episode N" off
    # the string: the relationship is authoritative, and it cannot misfire on a
    # title where "Episode" is genuinely part of the name (Episode Gladiolus,
    # Episode Prologue). Verified on Telltale Batman episodes 1 and 5, both of
    # which point at the same series id.
    if hit.get("game_type") == _IGDB_EPISODE:
        series = hit.get("parent_game")
        if isinstance(series, dict) and series.get("id") and series.get("name"):
            return {
                **hit,
                "id": series["id"],
                "name": series["name"],
                # Inherit the parent's type so ranking treats the resolved hit
                # as the main game it now is, not as the episode it came from.
                "game_type": series.get("game_type", _IGDB_MAIN_GAME),
                "collapsed_from": hit.get("name"),
            }
    return hit


def build_proposal(title: str, trophy_platforms: list[str], igdb_ids: list[int], search_fn, store_title: str = "") -> dict | None:
    """Ask IGDB what this trophy set really is. Returns None when unsure.

    search_fn(term, platform_ids) -> [{id, name, platform_ids}], so the network
    call is injectable and this stays testable.

    Considers EVERY hit and takes the closest, rather than the first that
    passes. IGDB's relevance ranking is not identity: searching "ELDEN RING"
    returns "Elden Ring Nightreign" above the real game, and first-match logic
    proposed renaming the library entry to the spin-off. Scanning all hits also
    means an exact match anywhere in the results correctly yields NO proposal —
    the name is already right.

    Acceptance is platform OVERLAP, never equality. A trophy set claiming
    "PS3,PSVITA" against an IGDB entry listing only Vita is the NORMAL shape for
    that era, and the vanishing PS3 is the correction.

    No confident answer means no proposal. A row left raw is recoverable; a
    confident wrong rename looks authoritative and is not.
    """
    if not igdb_ids:
        return None
    # We may hold TWO names for the same game and neither is reliably better.
    # Sony's feed name can be uselessly sparse ("Batman") where the store page
    # names it properly ("Batman: The Telltale Series"); the store SKU can be
    # repurposed ("Ghost of Tsushima" -> a page titled "Ghost of Tsushima:
    # Legends") where the feed name is right. Picking a winner up front means
    # being wrong half the time, so both are offered to IGDB and IGDB arbitrates
    # — an exact match beats a near-miss, and a main game beats a derivative.
    ours = titles.normalize_for_match(title)
    ours_variants = {ours}
    if store_title and store_title.strip():
        ours_variants.add(titles.normalize_for_match(store_title))
    best = None
    exact_best = None
    for term in search_terms(title) + (search_terms(store_title) if store_title.strip() else []):
        for hit in search_fn(term, igdb_ids) or []:
            hit = _collapse_edition(hit)
            name = (hit.get("name") or "").strip()
            hit_ids = [p for p in (hit.get("platform_ids") or []) if p in igdb_ids]
            if not name or not hit_ids:
                continue
            # A collapsed hit matched the search under its ORIGINAL name, and
            # the link to its parent is IGDB's own assertion rather than a
            # fuzzy guess. So both names count as "what this hit is".
            #
            # Without this, an episode resolved to its series is then REJECTED
            # for being shorter than the term that found it — the Telltale
            # Batman episode resolves to "Batman: The Telltale Series", fails
            # the similarity gate against the episode-length term, and the
            # dart-throw match for the sparse name "Batman" wins instead.
            matched_as = hit.get("collapsed_from") or name
            theirs = titles.normalize_for_match(name)
            theirs_variants = {theirs, titles.normalize_for_match(matched_as)}
            # The name we already have is right — found anywhere in the results,
            # not just in first place. NOT a dead end: the id is the whole
            # point. Discarding it here threw away the match for ~279 rows whose
            # only "problem" was already being correctly named, and reported
            # them as "not identified" — the opposite of what happened.
            spaceless_ours = {v.replace(" ", "") for v in ours_variants}
            if (theirs_variants & ours_variants) or ({t.replace(" ", "") for t in theirs_variants} & spaceless_ours):
                # COLLECT, don't return. Several hits can match exactly, and the
                # first one back is not necessarily the right one: "Ghost of
                # Tsushima" (main game) and "Ghost of Tsushima: Legends"
                # (standalone expansion) are both exact matches for a name we
                # hold, and returning whichever IGDB listed first is how the
                # spin-off wins. A main game beats a derivative; ties keep the
                # earlier hit, so behaviour is stable when nothing distinguishes
                # them.
                rank = 0 if hit.get("game_type") == _IGDB_MAIN_GAME else 1
                if exact_best is None or rank < exact_best[0]:
                    exact_best = (rank, hit.get("id"), hit_ids, term, name)
                continue
            if not _is_same_game(term, matched_as):
                continue
            gtype = hit.get("game_type")
            # A DLC, bundle or pack is never a better name for a trophy set.
            if gtype in _IGDB_NOT_A_GAME_ON_ITS_OWN:
                continue
            # Rank: a main game beats a remaster/port/expanded edition, and
            # within the same kind the fewest added words wins. This is what
            # picks "Guns of the Patriots" over the "Database" companion, and
            # the original "Marvel vs. Capcom 3" over "Ultimate".
            score = (0 if gtype == _IGDB_MAIN_GAME else 1, _added_words(titles.normalize_for_match(term), theirs))
            if best is None or score < best[0]:
                best = (score, name, hit.get("id"), hit_ids, term)
    # An exact match still beats any ranked near-miss — the name we hold is
    # already right and the id is the payload.
    if exact_best is not None:
        _rank, igdb_id, hit_ids, term, name = exact_best
        # An exact match on the STORE name is still a rename for us: the row is
        # held under Sony's feed name, and that sparse name is what the library
        # would otherwise show. Matching our own name proposes nothing.
        rename = None if titles.normalize_for_match(name) == ours else name
        return {
            "proposed_title": rename,
            "proposed_igdb_id": igdb_id,
            "proposed_platforms": hit_ids,
            "matched_term": term,
            "exact": True,
        }
    if best is None:
        return None
    _score, name, igdb_id, hit_ids, term = best
    return {
        "proposed_title": name,
        "proposed_igdb_id": igdb_id,
        "proposed_platforms": hit_ids,
        "matched_term": term,
    }


def _igdb_search_adapter(client_id: str, client_secret: str):
    """search_fn for build_proposal, bound to real IGDB credentials.

    Filters on platform in the query itself (`where platforms=(9,46)`). That is
    not a nicety: an unfiltered search for "Modern Warfare 2" returns the 2022
    PS4/PS5 game FIRST and the 2009 PS3 one second, so the top proposal would be
    the wrong game. With the filter, the wrong one is not returned at all.
    """
    from . import igdb

    def search(term: str, platform_ids: list[int]) -> list[dict]:
        rows = igdb.search_games_on_platforms(client_id, client_secret, term, platform_ids, limit=5)
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "platform_ids": r.get("platform_ids") or [],
                "game_type": r.get("game_type"),
                # BOTH parent links have to survive the reshape. Dropping them
                # here silently disabled _collapse_edition in production while
                # every test still passed, because tests hand-build hits with
                # the fields present. An edition never collapsed onto its game
                # and an episode never resolved to its series.
                "version_parent": r.get("version_parent"),
                "parent_game": r.get("parent_game"),
            }
            for r in rows
        ]

    return search


def _can_do_better_now(cand, item: dict) -> bool:
    """Has Sony handed us something since this row was decided? (#180)

    The upsert case. A game confirmed under a sparse name — "Batman" — gets no
    better until Sony gives us a way to look it up. Renewing PS+ puts the
    purchase back in the feed, which restores a productId, which unlocks a
    store name, which unlocks an IGDB match. The sync then genuinely wants to
    change something, so the row goes back in the queue as a proposed change
    rather than requiring the user to know a relink button exists.

    Deliberately narrow, because a queue that re-asks settled questions is
    worse than one that misses a few:
      - a DISMISSAL is an answer, not a gap. Dismissed rows stay dismissed.
      - a row that already carries an IGDB id is identified. Nothing to add.
      - only a store source we did NOT have before counts as new. Re-running a
        sync with the same data must never resurface anything.
    """
    if cand.status != "confirmed" or cand.proposed_igdb_id:
        return False
    raw = cand.raw_data or {}
    had = bool(raw.get("productId") or (raw.get("concept") or {}).get("id"))
    now = bool(item.get("productId") or (item.get("concept") or {}).get("id"))
    return now and not had


def _reopen_for_better_data(cand, item: dict) -> None:
    """Put a decided row back in the queue, carrying its refusals forward.

    A name the user already turned down must survive being reopened, or the
    next lookup proposes it again and the rejection is silently undone.
    """
    refused = cand.proposed_title if cand.proposal_status == "rejected" else (cand.raw_data or {}).get("rejectedTitle")
    raw = dict(item)
    if refused:
        raw["rejectedTitle"] = refused
    cand.raw_data = raw
    cand.status = "pending"
    cand.proposal_status = None
    cand.proposed_title = None


def _store_title_for(cand, sleep: float = 1.0) -> str:
    """Sony's own store name for a review row, fetched once and cached (#180).

    This is the second name IGDB arbitrates against — the one that rescues a
    sparse feed name like "Batman". It is fetched HERE, during the sync, rather
    than by the store-metadata job, because that job works on library ENTRIES
    and these rows are not entries yet. Waiting until after import is what
    forced a correct-then-relink dance across three buttons.

    Cached on the row (including a cached miss) so a re-sync spends nothing.
    Transient failures are NOT cached, so a flaky fetch retries next time
    instead of permanently deciding this game has no store name. Rows with no
    productId — trophy-only generations, and anything that left the purchased
    feed when PS+ lapsed — simply have no store page to ask about.
    """
    raw = dict(cand.raw_data or {})
    if "storeTitle" in raw:
        return raw["storeTitle"] or ""
    product_id = raw.get("productId")
    # No productId means no purchase — a trophy-only generation, or a game that
    # dropped out of the purchased feed when PS+ lapsed. Those are exactly the
    # rows with the sparsest names, so falling back to the CONCEPT page is what
    # stops the neediest cases getting nothing. Sony's PS4 "Batman" has no
    # product but concept 221667 names it "Batman: The Telltale Series".
    concept_id = (raw.get("concept") or {}).get("id")
    if not product_id and not concept_id:
        return ""
    try:
        if product_id:
            meta = psn_store.fetch_product(product_id)
        else:
            meta = psn_store.fetch_concept(concept_id)
        name = psn_store._clean_store_title(meta.get("name"))
    except psn_store.ProductNotFound:
        name = ""  # delisted: a real answer, worth caching
    except Exception:
        _logger.exception("PS Store lookup failed for %s", product_id or f"concept {concept_id}")
        return ""  # transient — leave uncached so a later sync retries
    raw["storeTitle"] = name
    cand.raw_data = raw
    if sleep:
        time.sleep(sleep)
    return name


def fill_review_proposals(db: Session, user: models.User, progress_callback=None, store_sleep: float = 1.0) -> dict:
    """Ask IGDB what each suspect trophy-set name really is (#180), phase 1.

    Operates on pending review CANDIDATES — rows the sync held back rather than
    imported. Nothing in the library is touched and nothing is renamed: the
    entry is created on confirm under whichever name the user approves.

    (Phase 2 — walking the existing PSN library to attach igdb_ids to entries
    that are already there — is #161 and is a button, not part of sync.)

    Self-gating: decided rows and rows already looked up are skipped, so a
    re-sync spends nothing on settled work.
    """
    if not (user.twitch_client_id and user.twitch_client_secret):
        return {"checked": 0, "proposed": 0, "no_match": 0, "errored": 0, "skipped_no_credentials": True}

    search = _igdb_search_adapter(user.twitch_client_id, user.twitch_client_secret)
    rows = (
        db.query(models.PsnReviewCandidate)
        .filter(
            models.PsnReviewCandidate.user_id == user.id,
            models.PsnReviewCandidate.status == "pending",
            models.PsnReviewCandidate.proposal_status.is_(None),
        )
        .all()
    )
    # EVERY pending row is looked up, not just trophy-only ones. This used to
    # filter to `is_trophy_only`, on the theory that a store-backed row "got its
    # name from a store listing and is fine as-is" — the same assumption the
    # PS4/PS5 import disproved (Ghost of Tsushima arriving as Legends, edition
    # suffixes, a bonus guide arriving as a game). With the sync now holding
    # store-backed rows back too, leaving that filter in place would queue them
    # and then never ask IGDB anything about them.

    out = {"checked": 0, "proposed": 0, "matched": 0, "no_match": 0, "errored": 0}
    for i, cand in enumerate(rows):
        if progress_callback:
            progress_callback(i, len(rows), cand.title)
        item = cand.raw_data or {}
        tokens = [t for t in str(item.get("platform") or item.get("trophyTitlePlatform") or "").split(",") if t.strip()]
        igdb_ids = igdb_platform_ids(db, tokens)
        out["checked"] += 1
        try:
            proposal = build_proposal(cand.title, tokens, igdb_ids, search, store_title=_store_title_for(cand, store_sleep))
        except Exception:
            _logger.exception("IGDB proposal lookup failed for %s", cand.external_id)
            out["errored"] += 1
            continue
        if proposal:
            cand.proposed_title = proposal["proposed_title"]
            cand.proposed_igdb_id = proposal["proposed_igdb_id"]
            cand.proposed_platforms = proposal["proposed_platforms"]
            # "matched" = IGDB knows this game and our name is already right, so
            # there is nothing to approve — but the id still attaches on confirm.
            # "pending" = a rename is being suggested and needs a decision.
            if proposal.get("exact"):
                cand.proposal_status = "matched"
                out["matched"] = out.get("matched", 0) + 1
            elif proposal["proposed_title"] and proposal["proposed_title"] == item.get("rejectedTitle"):
                # Already turned this exact name down. Re-asking it every sync
                # is how a queue becomes noise you learn to ignore — and then a
                # row reappearing stops meaning anything changed.
                cand.proposal_status = "rejected"
                out["re_refused"] = out.get("re_refused", 0) + 1
            else:
                cand.proposal_status = "pending"
                out["proposed"] += 1
        else:
            # "none" is distinct from NULL (never looked up), so a re-sync does
            # not pay for the same miss again. The row still shows in the queue —
            # an unidentified row kept invisible is the original problem.
            cand.proposal_status = "none"
            out["no_match"] += 1
        db.commit()
        time.sleep(_PAGE_SLEEP_S)
    return out


def _platform_in_igdb_set(db: Session, token: str, igdb_ids: list | None) -> bool:
    """Is this Sony platform token one of the IGDB platform ids on a proposal?

    Goes through the platforms table's own igdb_id rather than comparing names —
    the rows already carry IGDB's ids, so there is nothing to map (#180).
    """
    if not igdb_ids:
        return False
    pid = models.resolve_platform_id(db, token)
    if not pid:
        return False
    row = db.query(models.Platform).filter(models.Platform.id == pid).first()
    return bool(row and row.igdb_id and row.igdb_id in igdb_ids)


def link_igdb_ids(db: Session, user: models.User, progress_callback=None) -> dict:
    """Phase 2: attach an IGDB id to PSN entries already in the library (#180).

    Phase 1 handles trophy-only rows, which are held back and named on confirm.
    These are store-backed entries that imported directly — Sony's store name is
    fine, so there is nothing to rename, but without an id there is no metadata
    (#161) and the different-igdb_id match veto stays inert.

    A confident match attaches silently. An ambiguous one becomes a review row
    (kind="igdb_link") asking you to confirm which game it is, rather than
    guessing — same contract as everything else in that queue.

    Confident means IGDB returned a name that matches ours exactly once the
    titles are folded. Anything else is a judgement call: a wrong id attaches
    wrong metadata, which is quiet and only visible if you go looking.

    Self-gating on game.igdb_id and on an existing candidate, so a re-run costs
    nothing.
    """
    if not (user.twitch_client_id and user.twitch_client_secret):
        return {"checked": 0, "linked": 0, "queued": 0, "no_match": 0, "errored": 0, "skipped_no_credentials": True}

    search = _igdb_search_adapter(user.twitch_client_id, user.twitch_client_secret)
    releases = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .join(models.Game, models.Game.id == models.GameRelease.game_id)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.source == "psn",
            models.Game.igdb_id.is_(None),
        )
        .all()
    )
    seen_ext = {c.external_id for c in db.query(models.PsnReviewCandidate).filter(models.PsnReviewCandidate.user_id == user.id).all()}

    out = {"checked": 0, "linked": 0, "queued": 0, "no_match": 0, "errored": 0}
    for i, rel in enumerate(releases):
        if rel.external_id in seen_ext:
            continue
        title = (rel.game.display_name or rel.game.title) if rel.game else ""
        if not title:
            continue
        if progress_callback:
            progress_callback(i, len(releases), title)
        item = rel.raw_data or {}
        tokens = [t for t in str(item.get("platform") or "").split(",") if t.strip()]
        igdb_ids = igdb_platform_ids(db, tokens)
        out["checked"] += 1
        try:
            hits = search(title, igdb_ids) if igdb_ids else []
        except Exception:
            _logger.exception("IGDB link lookup failed for %s", rel.external_id)
            out["errored"] += 1
            continue

        ours = titles.normalize_for_match(title).replace(" ", "")
        exact = [
            h
            for h in hits
            if titles.normalize_for_match(h.get("name") or "").replace(" ", "") == ours
            and h.get("game_type") not in _IGDB_NOT_A_GAME_ON_ITS_OWN
        ]
        if len(exact) == 1:
            rel.game.igdb_id = exact[0]["id"]
            out["linked"] += 1
            db.commit()
            time.sleep(_PAGE_SLEEP_S)
            continue

        best = next((h for h in hits if h.get("game_type") not in _IGDB_NOT_A_GAME_ON_ITS_OWN), None)
        if not best:
            out["no_match"] += 1
            db.commit()
            time.sleep(_PAGE_SLEEP_S)
            continue

        # Ambiguous — several exact matches, or the closest is only a near miss.
        # Ask rather than guess.
        db.add(
            models.PsnReviewCandidate(
                user_id=user.id,
                external_id=rel.external_id,
                title=title,
                kind="igdb_link",
                status="pending",
                proposed_title=None,
                proposed_igdb_id=best["id"],
                proposed_platforms=[p for p in (best.get("platform_ids") or []) if p in igdb_ids],
                proposal_status="pending",
                raw_data=item or None,
            )
        )
        seen_ext.add(rel.external_id)
        out["queued"] += 1
        db.commit()
        time.sleep(_PAGE_SLEEP_S)
    return out
