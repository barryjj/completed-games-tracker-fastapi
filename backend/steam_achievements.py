"""Steam achievements (#136): the Steam half of the achievement tables the
PSN trophy pass fills. Same shape as psn.sync_trophies -- definitions once
per set, progress per library entry, both self-gating so a re-run after a
sync that changed nothing spends no calls -- against Steam's WebAPI instead
of Sony's.

A set here is an appid. Three calls per set the first time:

    GetSchemaForGame                    what the game defines (name, icons)
    GetGlobalAchievementPercentagesForApp  rarity, which Steam keeps apart
    GetPlayerAchievements               what this account has earned

and one batched call per hundred apps ahead of them:

    IPlayerService/GetAchievementsProgress   unlocked/total per app, as YOU

The key-based endpoints see the profile the way a stranger does: a game
marked private in the library answers "Profile is not public" (Castle in
the Clouds, first hit). The batch call takes the access token the session
renewal mints daily and answers as the account, so it sees everything --
and it is what says which played games have any unlocks at all. Most do
not, and those get their rows written from the schema with no per-app
call, which is most of the run. A private game keeps its counts from the
batch; only its per-achievement rows are out of reach.

Only PLAYED games are fetched. An unplayed game has no progress, and its
definitions are not worth a call until it is played; on a library with a
big backlog that is most of it. The played gate is the entry's playtime,
which the games sync refreshes every run.
"""

import datetime
import logging
import time

import httpx
from sqlalchemy.orm import Session

from . import models, steam
from .steam import _HEADERS, STEAM_API_BASE

_logger = logging.getLogger(__name__)

_SLEEP_S = 0.25
_SCHEMA_URL = f"{STEAM_API_BASE}/ISteamUserStats/GetSchemaForGame/v2/"
_GLOBAL_URL = f"{STEAM_API_BASE}/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
_PLAYER_URL = f"{STEAM_API_BASE}/ISteamUserStats/GetPlayerAchievements/v1/"
_PROGRESS_URL = f"{STEAM_API_BASE}/IPlayerService/GetAchievementsProgress/v1/"
_PROGRESS_BATCH = 100

# Per-user counts from the batch call, on the release beside the schema
# marker -- the same place the PSN crawl keeps a set's earned counts, so a
# reader of either source finds them in one shape.
_PROGRESS_KEY = "achievement_progress"

# Marker written on the release once its schema has been read, so a game
# with no achievements (most of Steam) is not asked again every sync. The
# appdetails total, refreshed monthly by the enrichment worker, is what
# says the schema has grown since.
_SCHEMA_KEY = "achievement_schema"


def _get(url: str, params: dict) -> httpx.Response:
    """One WebAPI call. Returned raw: GetPlayerAchievements answers a game
    with no stats as HTTP 400 with a message, and the caller reads it."""
    return httpx.get(url, params=params, headers=_HEADERS, timeout=30)


def _post(url: str, params: dict, data: dict) -> httpx.Response:
    return httpx.post(url, params=params, data=data, headers=_HEADERS, timeout=30)


def _access_token(db: Session, user: models.User) -> str | None:
    """The JWT inside steamLoginSecure, renewed first if it is about to
    lapse -- the same trade a sync makes for the store call. None when
    there is no session to mint from, in which case the pass runs on the
    key alone and a private game is simply refused."""
    if not user.steam_login_secure:
        return None
    if user.steam_refresh_token and steam.access_token_stale(user):
        try:
            steam.renew_session(user)
            db.commit()
        except (steam.SteamCookiesExpiredError, httpx.HTTPError, RuntimeError):
            _logger.warning("Steam session renewal failed; achievement pass runs on the key alone", exc_info=True)
            return None
    return user.steam_login_secure.split("%7C%7C")[-1].split("||")[-1]


def _fetch_progress(token: str, steam_id64: str, appids: list[str]) -> dict[str, dict]:
    """{appid: {unlocked, total, percentage, cache_time}} for up to a batch
    of apps, as the account. A refused batch (token no good for this) is
    {} -- the pass falls back to the per-app call, it does not stop."""
    data = {"steamid": steam_id64, **{f"appids[{i}]": a for i, a in enumerate(appids)}}
    resp = _post(_PROGRESS_URL, {"access_token": token}, data)
    if resp.status_code != 200:
        _logger.warning("GetAchievementsProgress answered HTTP %s; running on the key alone", resp.status_code)
        return {}
    rows = ((resp.json() or {}).get("response") or {}).get("achievement_progress") or []
    out: dict[str, dict] = {}
    for r in rows:
        try:
            out[str(r["appid"])] = {
                "unlocked": int(r.get("unlocked") or 0),
                "total": int(r.get("total") or 0),
                "percentage": float(r.get("percentage") or 0),
                "cache_time": r.get("cache_time"),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _store_progress(release: models.GameRelease, progress: dict, *, private: bool | None = None) -> None:
    """The batch counts on the release. `private` True records that the
    per-app call was refused at this unlocked count (the number the retry
    gate compares against); None carries the last verdict forward; False
    clears it -- the call answered."""
    raw = dict(release.raw_data or {})
    kept = raw.get(_PROGRESS_KEY) if isinstance(raw.get(_PROGRESS_KEY), dict) else {}
    entry = {**progress, "fetched_at": datetime.datetime.now(datetime.UTC).isoformat()}
    if private is None:
        private = bool(kept.get("private", False))
        entry["refused_at_unlocked"] = kept.get("refused_at_unlocked")
    elif private:
        entry["refused_at_unlocked"] = progress.get("unlocked")
    entry["private"] = private
    if not private:
        entry.pop("refused_at_unlocked", None)
    raw[_PROGRESS_KEY] = entry
    release.raw_data = raw


def _played_entries(db: Session, user_id: int) -> list[models.UserLibraryEntry]:
    """Steam games this user has played. DLC has no achievements of its
    own (they live on the parent's set) and is skipped."""
    return (
        db.query(models.UserLibraryEntry)
        .join(models.GameRelease)
        .join(models.Game)
        .filter(
            models.UserLibraryEntry.user_id == user_id,
            models.GameRelease.source == "steam",
            models.Game.is_dlc == False,  # noqa: E712
            models.UserLibraryEntry.playtime_minutes > 0,
        )
        .order_by(models.UserLibraryEntry.id)
        .all()
    )


def _appdetails_total(release: models.GameRelease) -> int | None:
    """How many achievements the store page says the game has; None when
    the release has not been enriched yet. Steam omits the key entirely for
    a game with none, so an enriched record without it means zero."""
    raw = release.raw_data or {}
    details = raw.get("appdetails")
    if not isinstance(details, dict):
        return None
    return int((details.get("achievements") or {}).get("total") or 0)


# 403s in a row before the pass decides the PROFILE is the problem (game
# details set to private fail every app) rather than the app.
_PRIVATE_PROFILE_RUN = 3


def _mark_forbidden(db: Session, release: models.GameRelease) -> None:
    """Remember that Steam refuses this app's stats, so it is skipped rather
    than asked again every sync. Committed on its own: the rollback that
    precedes it has already dropped anything else in flight."""
    raw = dict(release.raw_data or {})
    raw[_SCHEMA_KEY] = {"total": 0, "forbidden": True, "fetched_at": datetime.datetime.now(datetime.UTC).isoformat()}
    release.raw_data = raw
    db.commit()


def _schema_total(release: models.GameRelease) -> int | None:
    """How many achievements the schema had when it was last read; None
    when it never was."""
    marker = (release.raw_data or {}).get(_SCHEMA_KEY)
    return int(marker.get("total") or 0) if isinstance(marker, dict) else None


def _definitions_current(release: models.GameRelease) -> bool:
    """The schema on file is the one the store still describes. No marker
    means never read; a marker whose total differs from the (newer)
    appdetails count means the game added achievements."""
    marker = (release.raw_data or {}).get(_SCHEMA_KEY)
    if isinstance(marker, dict) and marker.get("forbidden"):
        return True
    have = _schema_total(release)
    if have is None:
        return False
    want = _appdetails_total(release)
    return want is None or have == want


def _earned_current(db: Session, entry: models.UserLibraryEntry) -> bool:
    """This entry's progress was fetched after it was last played. Progress
    only moves while playing, and the games sync refreshes last_played_at
    every run, so this is the same gate as PSN's last-earned stamp."""
    latest = (
        db.query(models.UserAchievement.updated_at)
        .filter_by(library_entry_id=entry.id)
        .order_by(models.UserAchievement.updated_at.desc())
        .first()
    )
    if latest is None or latest[0] is None:
        return False
    fetched = latest[0]
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=datetime.UTC)
    played = entry.last_played_at
    if played is not None and played.tzinfo is None:
        played = played.replace(tzinfo=datetime.UTC)
    return played is None or fetched >= played


def _fetch_schema(api_key: str, appid: str) -> list[dict]:
    resp = _get(_SCHEMA_URL, {"key": api_key, "appid": appid, "l": "english"})
    resp.raise_for_status()
    game = (resp.json() or {}).get("game") or {}
    return (game.get("availableGameStats") or {}).get("achievements") or []


def _fetch_global_rates(appid: str) -> dict[str, float]:
    """{apiname: percent}. Public, no key. A game whose stats are hidden
    answers 403; rarity is decoration, so that is an empty map, not a
    failure."""
    resp = _get(_GLOBAL_URL, {"gameid": appid})
    if resp.status_code != 200:
        return {}
    rows = ((resp.json() or {}).get("achievementpercentages") or {}).get("achievements") or []
    out: dict[str, float] = {}
    for r in rows:
        try:
            out[str(r["name"])] = float(r["percent"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


class _NoStats(Exception):
    """GetPlayerAchievements' answer for a game with no achievements."""


def _fetch_player(api_key: str, steam_id64: str, appid: str) -> list[dict]:
    resp = _get(_PLAYER_URL, {"key": api_key, "steamid": steam_id64, "appid": appid, "l": "english"})
    if resp.status_code == 400:
        # {"playerstats": {"error": "Requested app has no stats", "success": false}}
        try:
            stats = resp.json().get("playerstats") or {}
        except ValueError:
            stats = {}
        if not stats.get("success", True):
            raise _NoStats(stats.get("error") or "no stats")
    resp.raise_for_status()
    return ((resp.json() or {}).get("playerstats") or {}).get("achievements") or []


def _definitions_for(db: Session, appid: str) -> dict[str, models.AchievementDefinition]:
    return {d.external_id: d for d in db.query(models.AchievementDefinition).filter_by(source="steam", set_id=appid).all()}


def _store_definitions(
    db: Session, release: models.GameRelease, schema: list[dict], rates: dict[str, float]
) -> dict[str, models.AchievementDefinition]:
    """Upsert the set's definitions and stamp the release with what was
    read; returns them by apiname. Rows Steam no longer lists are left
    alone, as with PSN -- sets grow, they do not retract."""
    appid = release.external_id
    existing = _definitions_for(db, appid)
    now = datetime.datetime.now(datetime.UTC)
    for i, a in enumerate(schema):
        ext = str(a.get("name") or "")
        if not ext:
            continue
        fields = {
            "name": a.get("displayName") or ext,
            "description": a.get("description"),
            "hidden": bool(a.get("hidden")),
            "icon_url": a.get("icon"),
            "icon_locked_url": a.get("icongray"),
            "rarity_pct": rates.get(ext),
            "sort_order": i,
            "raw_data": a,
            "updated_at": now,
        }
        row = existing.get(ext)
        if row is None:
            row = models.AchievementDefinition(source="steam", set_id=appid, external_id=ext, **fields)
            db.add(row)
            existing[ext] = row
        else:
            for k, v in fields.items():
                setattr(row, k, v)
    raw = dict(release.raw_data or {})
    raw[_SCHEMA_KEY] = {"total": len(schema), "fetched_at": now.isoformat()}
    release.raw_data = raw
    db.flush()
    return existing


def _store_earned(db: Session, entry: models.UserLibraryEntry, defs: dict[str, models.AchievementDefinition], earned: list[dict]) -> int:
    """Upsert this entry's progress against the set; returns how many are
    earned. unlocktime is 0 for anything not achieved."""
    existing = {a.definition_id: a for a in db.query(models.UserAchievement).filter_by(library_entry_id=entry.id).all()}
    now = datetime.datetime.now(datetime.UTC)
    count = 0
    for a in earned:
        d = defs.get(str(a.get("apiname") or ""))
        if d is None:
            continue
        is_earned = bool(a.get("achieved"))
        count += is_earned
        unlocked = a.get("unlocktime") or 0
        fields = {
            "earned": is_earned,
            "earned_at": datetime.datetime.fromtimestamp(unlocked, datetime.UTC) if is_earned and unlocked else None,
            "raw_data": a,
            "updated_at": now,
        }
        row = existing.get(d.id)
        if row is None:
            db.add(models.UserAchievement(library_entry_id=entry.id, definition_id=d.id, **fields))
        else:
            for k, v in fields.items():
                setattr(row, k, v)
    return count


def _store_none_earned(db: Session, entry: models.UserLibraryEntry, defs: dict[str, models.AchievementDefinition]) -> None:
    """The batch call said zero unlocked: every definition gets an unearned
    row, from the schema, with no per-app call. Present-and-unearned is
    what makes the set count as seen (see UserAchievement)."""
    existing = {a.definition_id: a for a in db.query(models.UserAchievement).filter_by(library_entry_id=entry.id).all()}
    now = datetime.datetime.now(datetime.UTC)
    for d in defs.values():
        row = existing.get(d.id)
        if row is None:
            db.add(models.UserAchievement(library_entry_id=entry.id, definition_id=d.id, earned=False, updated_at=now))
        else:
            row.earned, row.earned_at, row.updated_at = False, None, now


def _private_unchanged(release: models.GameRelease, progress: dict | None) -> bool:
    """A game whose per-achievement rows were refused last time, and whose
    unlocked count has not moved since: nothing to retry. When the count
    moves (or the game was unmarked), the per-app call is tried again."""
    kept = (release.raw_data or {}).get(_PROGRESS_KEY)
    if not isinstance(kept, dict) or not kept.get("private") or progress is None:
        return False
    return kept.get("refused_at_unlocked") == progress.get("unlocked")


def sync_achievements(db: Session, user: models.User, progress_callback=None, sleep: float = _SLEEP_S) -> dict:
    """Fetch every achievement set behind the user's played Steam games:
    what the game defines, once per app, and what this account has earned,
    per library entry. Chained after the Steam games sync; self-gating, so
    a sync that changed nothing costs no calls (#136).

    Stops on an answer that would repeat for every remaining app -- a bad
    key (401), a rate limit (429), a private profile (403 after 403 after
    403) -- and reports it, so the next run picks up where this left off.
    One 403 is an app, not the profile: Steam refuses the stats endpoints
    for some individual titles (age-restricted ones, seen first on Castle in
    the Clouds) whatever the key, and that app is marked forbidden and
    skipped from then on.
    """
    if not user.steam_api_key or not user.steam_id64:
        return {"checked": 0, "fetched": 0, "skipped": 0, "errored": 0, "skipped_no_credentials": True}
    out = {"checked": 0, "fetched": 0, "skipped": 0, "errored": 0, "earned": 0, "sets": 0, "no_achievements": 0}
    entries = _played_entries(db, user.id)

    # The batch first: counts for every played game, as the account. Cheap
    # (a call per hundred), and what makes the per-app loop mostly free.
    progress: dict[str, dict] = {}
    token = _access_token(db, user)
    if token:
        appids = [e.release.external_id for e in entries]
        for start in range(0, len(appids), _PROGRESS_BATCH):
            chunk = appids[start : start + _PROGRESS_BATCH]
            try:
                progress.update(_fetch_progress(token, user.steam_id64, chunk))
            except (httpx.HTTPError, ValueError):
                _logger.warning("GetAchievementsProgress batch failed; running on the key alone", exc_info=True)
                progress = {}
                break
            time.sleep(sleep)
        for e in entries:
            if e.release.external_id in progress:
                _store_progress(e.release, progress[e.release.external_id])
        db.commit()
        out["progress_known"] = len(progress)

    forbidden_run = 0
    for i, entry in enumerate(entries):
        release = entry.release
        appid = release.external_id
        out["checked"] += 1
        if progress_callback:
            progress_callback(i, len(entries), entry.title)
        need_defs = not _definitions_current(release)
        # A schema already read as empty is a game with nothing to fetch.
        if not need_defs and not _schema_total(release):
            out["skipped"] += 1
            continue
        need_earned = need_defs or not _earned_current(db, entry)
        if not need_defs and not need_earned:
            out["skipped"] += 1
            continue
        if not need_defs and _private_unchanged(release, progress.get(appid)):
            out["skipped"] += 1
            continue
        try:
            if need_defs:
                schema = _fetch_schema(user.steam_api_key, appid)
                time.sleep(sleep)
                rates = {}
                if schema:
                    rates = _fetch_global_rates(appid)
                    time.sleep(sleep)
                defs = _store_definitions(db, release, schema, rates)
                if not schema:
                    db.commit()
                    out["no_achievements"] += 1
                    continue
                out["sets"] += 1
            else:
                defs = _definitions_for(db, appid)
            if need_earned and defs:
                known = progress.get(appid)
                if known is not None and known["unlocked"] == 0:
                    # Nothing unlocked: the rows come from the schema, and
                    # the per-app call is not made.
                    _store_none_earned(db, entry, defs)
                    out["from_batch"] = out.get("from_batch", 0) + 1
                else:
                    try:
                        earned = _fetch_player(user.steam_api_key, user.steam_id64, appid)
                    except _NoStats:
                        earned = []
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code != 403 or known is None:
                            raise
                        # Marked private in the library: the key sees a
                        # stranger's view. The counts from the batch are
                        # what this game shows until it is unmarked.
                        _store_progress(release, known, private=True)
                        db.commit()
                        out["counts_only"] = out.get("counts_only", 0) + 1
                        continue
                    time.sleep(sleep)
                    out["earned"] += _store_earned(db, entry, defs, earned)
                    if known is not None:
                        _store_progress(release, known, private=False)
            db.commit()
            out["fetched"] += 1
            forbidden_run = 0
        except httpx.HTTPStatusError as e:
            db.rollback()
            out["errored"] += 1
            status = e.response.status_code
            _logger.warning("Achievement fetch failed for appid %s (%s): HTTP %s", appid, entry.title, status)
            if status == 403:
                # No batch counts to fall back on (no session): a lone 403
                # is still one app, a run of them is the profile.
                forbidden_run += 1
                if forbidden_run < _PRIVATE_PROFILE_RUN:
                    _mark_forbidden(db, release)
                    out["forbidden"] = out.get("forbidden", 0) + 1
                    continue
            if status in (401, 403, 429):
                out["stopped"] = status
                break
        except (httpx.HTTPError, ValueError):
            db.rollback()
            out["errored"] += 1
            _logger.warning("Achievement fetch failed for appid %s (%s)", appid, entry.title, exc_info=True)
    return out
