"""The Steam half of #136: definitions once per app, progress per entry,
self-gating on the appdetails total and the last-played stamp."""

import datetime

import httpx
import pytest

from backend import models
from backend import steam_achievements as sa

_SCHEMA = [
    {
        "name": "ACH_WIN",
        "displayName": "Winner",
        "hidden": 0,
        "description": "Win once",
        "icon": "https://x/win.jpg",
        "icongray": "https://x/win_g.jpg",
    },
    {"name": "ACH_SECRET", "displayName": "Secret", "hidden": 1, "icon": "https://x/s.jpg", "icongray": "https://x/s_g.jpg"},
]
_RATES = [{"name": "ACH_WIN", "percent": 61.5}, {"name": "ACH_SECRET", "percent": "3.25"}]
_PLAYER = [
    {"apiname": "ACH_WIN", "achieved": 1, "unlocktime": 1700000000},
    {"apiname": "ACH_SECRET", "achieved": 0, "unlocktime": 0},
]


def _user(db, *, session=False) -> models.User:
    u = models.User(name="t", username="t", password_hash="x", api_token="tok", steam_api_key="KEY", steam_id64="76561198000000000")
    if session:
        # A store cookie with a day left, so no renewal is attempted.
        exp = int((datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=20)).timestamp())
        u.steam_session_id = "sess"
        u.steam_login_secure = f"76561198000000000%7C%7Cheader.{_b64({'exp': exp})}.sig"
        u.steam_refresh_token = "refresh"
    db.add(u)
    db.commit()
    return u


def _b64(claims: dict) -> str:
    import base64
    import json

    return base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")


def _entry(
    db, user, appid: str, *, playtime: int = 60, last_played=None, appdetails=None, is_dlc=False, title=None
) -> models.UserLibraryEntry:
    game = models.Game(title=title or f"Game {appid}", is_dlc=is_dlc)
    db.add(game)
    db.flush()
    raw = {"appid": int(appid)}
    if appdetails is not None:
        raw["appdetails"] = appdetails
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id=appid, raw_data=raw)
    db.add(release)
    db.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, playtime_minutes=playtime, last_played_at=last_played)
    db.add(entry)
    db.commit()
    return entry


def _rows(db, user, appid: str) -> list[models.UserAchievement]:
    """This account's unlock rows for one app's set."""
    return (
        db.query(models.UserAchievement)
        .join(models.AchievementDefinition)
        .filter(
            models.UserAchievement.user_id == user.id,
            models.AchievementDefinition.source == "steam",
            models.AchievementDefinition.set_id == appid,
        )
        .order_by(models.AchievementDefinition.sort_order)
        .all()
    )


def _set(db, user, appid: str) -> models.UserAchievementSet | None:
    return db.query(models.UserAchievementSet).filter_by(user_id=user.id, source="steam", set_id=appid).first()


def _api(
    monkeypatch,
    *,
    schema=None,
    rates=None,
    player=None,
    player_status=200,
    schema_status=200,
    rates_status=200,
    schema_status_by_app=None,
    player_status_by_app=None,
    progress=None,
    progress_status=200,
):
    """Canned WebAPI, keyed by endpoint. Records every call's (endpoint, appid);
    the batch progress call records ("progress", "<n apps>")."""
    calls: list[tuple[str, str]] = []
    schema = {} if schema is None else schema
    rates = {} if rates is None else rates
    player = {} if player is None else player
    schema_status_by_app = schema_status_by_app or {}
    player_status_by_app = player_status_by_app or {}
    progress = {} if progress is None else progress

    def fake_post(url, params, data):
        assert url == sa._PROGRESS_URL and "access_token" in params
        appids = [v for k, v in data.items() if k.startswith("appids[")]
        calls.append(("progress", str(len(appids))))
        rows = [{"appid": int(a), **progress[a], "cache_time": 1} for a in appids if a in progress]
        body = {"response": {"achievement_progress": rows}}
        return httpx.Response(progress_status, json=body if progress_status == 200 else {}, request=httpx.Request("POST", url))

    monkeypatch.setattr(sa, "_post", fake_post)

    def fake_get(url, params):
        appid = str(params.get("appid") or params.get("gameid"))
        req = httpx.Request("GET", url)
        if url == sa._SCHEMA_URL:
            calls.append(("schema", appid))
            achievements = schema.get(appid)
            body = {"game": {"availableGameStats": {"achievements": achievements}} if achievements is not None else {}}
            return httpx.Response(schema_status_by_app.get(appid, schema_status), json=body, request=req)
        if url == sa._GLOBAL_URL:
            calls.append(("rates", appid))
            return httpx.Response(rates_status, json={"achievementpercentages": {"achievements": rates.get(appid, [])}}, request=req)
        if url == sa._PLAYER_URL:
            calls.append(("player", appid))
            status = player_status_by_app.get(appid, player_status)
            if status != 200:
                return httpx.Response(status, json={"playerstats": {"error": "nope", "success": False}}, request=req)
            if appid not in player:
                return httpx.Response(400, json={"playerstats": {"error": "Requested app has no stats", "success": False}}, request=req)
            return httpx.Response(200, json={"playerstats": {"success": True, "achievements": player[appid]}}, request=req)
        raise AssertionError(url)

    monkeypatch.setattr(sa, "_get", fake_get)
    return calls


def test_sync_writes_definitions_per_app_and_unlocks_per_account(db_session, monkeypatch):
    user = _user(db_session)
    entry = _entry(db_session, user, "440")
    calls = _api(monkeypatch, schema={"440": _SCHEMA}, rates={"440": _RATES}, player={"440": _PLAYER})

    out = sa.sync_achievements(db_session, user, sleep=0)

    assert out == {"checked": 1, "fetched": 1, "skipped": 0, "errored": 0, "earned": 1, "sets": 1, "no_achievements": 0}
    q = db_session.query(models.AchievementDefinition).filter_by(source="steam", set_id="440")
    defs = q.order_by(models.AchievementDefinition.sort_order).all()
    assert [(d.external_id, d.name, d.hidden, d.tier, d.rarity_pct) for d in defs] == [
        ("ACH_WIN", "Winner", False, None, 61.5),
        ("ACH_SECRET", "Secret", True, None, 3.25),
    ]
    assert defs[0].icon_url == "https://x/win.jpg" and defs[0].icon_locked_url == "https://x/win_g.jpg"
    mine = {a.definition_id: a for a in _rows(db_session, user, "440")}
    assert mine[defs[0].id].earned is True
    # SQLite hands naive datetimes back; the instant is what matters.
    assert mine[defs[0].id].earned_at.replace(tzinfo=datetime.UTC).timestamp() == 1700000000
    assert mine[defs[1].id].earned is False and mine[defs[1].id].earned_at is None
    assert calls == [("schema", "440"), ("rates", "440"), ("player", "440")]
    # The release remembers what was read, so the next pass can tell.
    assert entry.release.raw_data[sa._SCHEMA_KEY]["total"] == 2
    # No session, so no batch summary: the rows written are the summary.
    summary = _set(db_session, user, "440")
    assert (summary.earned, summary.total, summary.progress, summary.title) == (1, 2, 50, "Game 440")


def test_sync_is_current_after_one_pass_and_refetches_progress_when_played_again(db_session, monkeypatch):
    """The gate is last_played_at against the rows on file: a quiet re-run
    spends nothing; a games sync that moves the stamp refetches progress and
    only progress."""
    user = _user(db_session)
    played = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
    entry = _entry(db_session, user, "440", last_played=played)
    calls = _api(monkeypatch, schema={"440": _SCHEMA}, rates={"440": _RATES}, player={"440": _PLAYER})
    sa.sync_achievements(db_session, user, sleep=0)
    calls.clear()

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["skipped"] == 1 and out["fetched"] == 0 and calls == []

    entry.last_played_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    db_session.commit()
    _PLAYER[1]["achieved"] = 1
    _PLAYER[1]["unlocktime"] = 1700005000
    try:
        out = sa.sync_achievements(db_session, user, sleep=0)
    finally:
        _PLAYER[1]["achieved"] = 0
        _PLAYER[1]["unlocktime"] = 0
    assert out["fetched"] == 1 and out["earned"] == 2
    assert calls == [("player", "440")], "definitions were current; only progress refetched"


def test_sync_refetches_definitions_when_the_store_total_grows(db_session, monkeypatch):
    """appdetails.achievements.total is the store's count, refreshed
    monthly by the enrichment worker; a game that added achievements shows
    up as a total that no longer matches the schema on file."""
    user = _user(db_session)
    entry = _entry(db_session, user, "440", appdetails={"achievements": {"total": 2}})
    _api(monkeypatch, schema={"440": _SCHEMA}, rates={"440": _RATES}, player={"440": _PLAYER})
    sa.sync_achievements(db_session, user, sleep=0)
    assert sa.sync_achievements(db_session, user, sleep=0)["skipped"] == 1

    release = entry.release
    release.raw_data = {**release.raw_data, "appdetails": {"achievements": {"total": 3}}}
    db_session.commit()
    bigger = _SCHEMA + [{"name": "ACH_DLC", "displayName": "Expansion"}]
    player = _PLAYER + [{"apiname": "ACH_DLC", "achieved": 0}]
    calls = _api(monkeypatch, schema={"440": bigger}, rates={"440": _RATES}, player={"440": player})

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["sets"] == 1 and calls[0] == ("schema", "440")
    assert db_session.query(models.AchievementDefinition).filter_by(set_id="440").count() == 3
    assert len(_rows(db_session, user, "440")) == 3
    assert sa.sync_achievements(db_session, user, sleep=0)["skipped"] == 1


def test_games_without_achievements_are_asked_once(db_session, monkeypatch):
    """Most of Steam has no achievements. The empty schema is remembered on
    the release; an enriched record that says zero is never asked at all."""
    user = _user(db_session)
    _entry(db_session, user, "10", title="No achievements")
    _entry(db_session, user, "20", title="Enriched, none", appdetails={"type": "game"})
    calls = _api(monkeypatch)

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["no_achievements"] == 2 and out["fetched"] == 0
    assert calls == [("schema", "10"), ("schema", "20")], "no rates or player call for an empty set"
    calls.clear()
    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["skipped"] == 2 and calls == []


def test_only_played_base_games_are_considered(db_session, monkeypatch):
    user = _user(db_session)
    _entry(db_session, user, "1", playtime=0, title="Backlog")
    _entry(db_session, user, "2", playtime=None, title="Never launched")
    _entry(db_session, user, "3", is_dlc=True, title="A DLC")
    _entry(db_session, user, "4", title="Played")
    calls = _api(monkeypatch, schema={"4": _SCHEMA}, player={"4": _PLAYER})

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["checked"] == 1
    assert {appid for _, appid in calls} == {"4"}


def test_no_stats_answer_and_hidden_rates_are_not_failures(db_session, monkeypatch):
    """GetPlayerAchievements says HTTP 400 "no stats" for a game whose
    schema is nonetheless non-empty (it happens); the global-rates call
    answers 403 when a game hides its stats. Neither is worth an error."""
    user = _user(db_session)
    _entry(db_session, user, "440")
    _api(monkeypatch, schema={"440": _SCHEMA}, rates_status=403)  # no player entry -> 400 no stats

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["fetched"] == 1 and out["errored"] == 0 and out["earned"] == 0
    defs = db_session.query(models.AchievementDefinition).filter_by(set_id="440").all()
    assert len(defs) == 2 and all(d.rarity_pct is None for d in defs)
    assert _rows(db_session, user, "440") == []


def test_one_refused_app_is_skipped_and_remembered(db_session, monkeypatch):
    """Steam refuses the stats endpoints for some individual titles (Castle
    in the Clouds, age-restricted) whatever the key. One 403 is that app's
    problem: it is marked forbidden, the pass moves on, and later syncs do
    not ask again."""
    user = _user(db_session)
    refused = _entry(db_session, user, "1281160", title="Castle in the Clouds", appdetails={"achievements": {"total": 33}})
    _entry(db_session, user, "2", title="Next one")
    calls = _api(monkeypatch, schema={"2": _SCHEMA}, player={"2": _PLAYER}, schema_status_by_app={"1281160": 403})

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert "stopped" not in out
    assert out["forbidden"] == 1 and out["errored"] == 1 and out["sets"] == 1 and out["checked"] == 2
    assert ("schema", "2") in calls, "the pass continued past the refused app"
    assert refused.release.raw_data[sa._SCHEMA_KEY]["forbidden"] is True

    calls.clear()
    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["skipped"] == 2 and calls == [], "a forbidden app is not asked again, even though the store says 33"


def test_sync_stops_on_an_answer_that_would_repeat(db_session, monkeypatch):
    """A bad key (401) or rate limit (429) stops at once. A private profile
    is a 403 on every app, so three in a row is the profile, and the pass
    stops there rather than marking the whole library forbidden."""
    user = _user(db_session)
    for appid in ("1", "2", "3", "4"):
        _entry(db_session, user, appid)
    calls = _api(monkeypatch, schema={a: _SCHEMA for a in "1234"}, player_status=403)

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["stopped"] == 403 and out["checked"] == 3
    assert out["forbidden"] == 2, "the first two were treated as apps; the third made it the profile"
    assert [c for c in calls if c[0] == "player"] == [("player", "1"), ("player", "2"), ("player", "3")]
    # Nothing was written for the app that stopped the run, so the next run tries it again.
    assert db_session.query(models.AchievementDefinition).count() == 0

    calls = _api(monkeypatch, schema={a: _SCHEMA for a in "1234"}, player_status=429)
    for e in db_session.query(models.UserLibraryEntry).all():
        raw = dict(e.release.raw_data or {})
        raw.pop(sa._SCHEMA_KEY, None)
        e.release.raw_data = raw
    db_session.commit()
    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["stopped"] == 429 and out["checked"] == 1 and "forbidden" not in out


def test_batch_progress_writes_unearned_rows_without_a_per_app_call(db_session, monkeypatch):
    """With a session, the pass asks GetAchievementsProgress for every played
    game first, as the account. A game at 0 unlocked gets its rows from the
    schema -- no GetPlayerAchievements call -- and the counts land on the
    release beside the schema marker, the way PSN keeps a set's counts."""
    user = _user(db_session, session=True)
    zero = _entry(db_session, user, "1", title="Untouched")
    some = _entry(db_session, user, "2", title="Started")
    calls = _api(
        monkeypatch,
        schema={"1": _SCHEMA, "2": _SCHEMA},
        player={"2": _PLAYER},
        progress={"1": {"unlocked": 0, "total": 2, "percentage": 0}, "2": {"unlocked": 1, "total": 2, "percentage": 50}},
    )

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert calls[0] == ("progress", "2"), "the batch goes first"
    assert ("player", "1") not in calls and ("player", "2") in calls
    assert out["from_batch"] == 1 and out["fetched"] == 2 and out["progress_known"] == 2
    assert [a.earned for a in _rows(db_session, user, "1")] == [False, False], "unearned rows from the schema"
    assert zero.release.raw_data[sa._PROGRESS_KEY]["unlocked"] == 0
    assert some.release.raw_data[sa._PROGRESS_KEY]["unlocked"] == 1
    # The batch is every played game's summary on the account.
    assert (_set(db_session, user, "1").earned, _set(db_session, user, "1").total) == (0, 2)
    assert (_set(db_session, user, "2").earned, _set(db_session, user, "2").total) == (1, 2)
    # Current after one pass, like any other set.
    calls.clear()
    assert sa.sync_achievements(db_session, user, sleep=0)["skipped"] == 2
    assert calls == [("progress", "2")], "only the batch, which is one call per hundred"


def test_private_game_keeps_its_counts_and_is_not_retried_until_they_move(db_session, monkeypatch):
    """A game marked private in the library: the key call says "Profile is
    not public" while the batch (as the account) has the counts. Counts
    only, no forbidden marker, and the pass continues. It is asked again
    only when the unlocked count moves -- or the game is unmarked."""
    user = _user(db_session, session=True)
    private = _entry(db_session, user, "1281160", title="Castle in the Clouds", appdetails={"achievements": {"total": 2}})
    _entry(db_session, user, "2", title="Next one")
    calls = _api(
        monkeypatch,
        schema={"1281160": _SCHEMA, "2": _SCHEMA},
        player={"2": _PLAYER},
        player_status_by_app={"1281160": 403},
        progress={"1281160": {"unlocked": 2, "total": 2, "percentage": 100}, "2": {"unlocked": 1, "total": 2, "percentage": 50}},
    )

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["counts_only"] == 1 and "forbidden" not in out and "stopped" not in out
    assert out["sets"] == 2 and ("player", "2") in calls, "continued past it"
    kept = private.release.raw_data[sa._PROGRESS_KEY]
    assert kept["unlocked"] == 2 and kept["private"] is True
    assert _rows(db_session, user, "1281160") == [], "no per-achievement rows could be had"
    assert sa._SCHEMA_KEY in private.release.raw_data and "forbidden" not in private.release.raw_data[sa._SCHEMA_KEY]
    # ...but it counts: the account's summary came from the batch.
    assert (_set(db_session, user, "1281160").earned, _set(db_session, user, "1281160").total) == (2, 2)

    calls.clear()
    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["skipped"] == 2 and ("player", "1281160") not in calls, "same counts, nothing to retry"

    # Unmarked private, and the count moved: tried again, and this time it answers.
    _api(
        monkeypatch,
        schema={"1281160": _SCHEMA, "2": _SCHEMA},
        player={"1281160": _PLAYER, "2": _PLAYER},
        progress={"1281160": {"unlocked": 1, "total": 2, "percentage": 50}, "2": {"unlocked": 1, "total": 2, "percentage": 50}},
    )
    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["fetched"] == 1 and "counts_only" not in out
    assert len(_rows(db_session, user, "1281160")) == 2
    assert private.release.raw_data[sa._PROGRESS_KEY]["private"] is False


def test_a_refused_batch_falls_back_to_the_key_alone(db_session, monkeypatch):
    user = _user(db_session, session=True)
    _entry(db_session, user, "1")
    calls = _api(monkeypatch, schema={"1": _SCHEMA}, player={"1": _PLAYER}, progress_status=401)

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["fetched"] == 1 and out["progress_known"] == 0
    assert ("player", "1") in calls, "no batch counts, so the per-app call is made as before"


def test_access_token_is_renewed_when_stale(db_session, monkeypatch):
    from backend import steam

    user = _user(db_session, session=True)
    gone = int((datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)).timestamp())
    user.steam_login_secure = f"76561198000000000%7C%7Cheader.{_b64({'exp': gone})}.sig"
    db_session.commit()

    def fake_renew(u):
        u.steam_login_secure = "76561198000000000%7C%7Cfresh.jwt.sig"

    monkeypatch.setattr(steam, "renew_session", fake_renew)
    assert sa._access_token(db_session, user) == "fresh.jwt.sig"
    db_session.expire(user)
    assert user.steam_login_secure.endswith("fresh.jwt.sig"), "committed"

    # No session at all: the pass runs on the key.
    bare = models.User(name="b", username="b", password_hash="x", api_token="tok2", steam_api_key="KEY", steam_id64="1")
    assert sa._access_token(db_session, bare) is None


def test_sync_without_credentials_reports_it():
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")
    assert sa.sync_achievements(None, user)["skipped_no_credentials"] is True


def test_steam_games_sync_chains_the_achievement_pass(db_session, monkeypatch):
    import asyncio

    from backend import integrations, jobs

    jobs.clear_all()
    ran = []

    async def _fake_sync_job(job_id, user_id, kind):
        ran.append(kind)

    monkeypatch.setattr(integrations, "_run_sync_job", _fake_sync_job)
    asyncio.run(integrations._run_steam_followups(1))
    assert ran == ["steam_achievements"]
    assert [j.kind for j in jobs.active_jobs_for(1)] == ["steam_achievements"], "a job of its own, so its toast is its own"
    jobs.clear_all()


def test_achievements_job_is_a_steam_job_with_its_own_report():
    from backend import integrations

    assert integrations.job_service("steam_achievements") == "steam"
    spec = integrations._STEAM_KINDS["steam_achievements"]
    assert spec["module"] == "steam_achievements" and spec["progress"] is True
    report = lambda result: integrations._format_sync_result(None, None, "steam_achievements", result)  # noqa: E731
    text = report({"sets": 3, "earned": 12, "skipped": 40, "errored": 0, "fetched": 3, "checked": 43})
    assert "3 sets fetched" in text and "12 achievements earned" in text and "40 already current" in text
    assert "private" in report({"sets": 0, "earned": 0, "skipped": 0, "errored": 1, "stopped": 403})
    refused = report({"sets": 1, "earned": 0, "skipped": 0, "errored": 1, "forbidden": 1})
    assert "1 refused by Steam" in refused and "errored" not in refused, "a refused app is not also an error"
    assert "API key" in report({"skipped_no_credentials": True})


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({}, None),
        ({"appdetails": {"type": "game"}}, 0),
        ({"appdetails": {"achievements": {"total": 17}}}, 17),
        ({"appdetails": "not a dict"}, None),
    ],
)
def test_appdetails_total_reads_the_store_count(raw, expected):
    assert sa._appdetails_total(models.GameRelease(platform="Steam", source="steam", external_id="1", raw_data=raw)) == expected
