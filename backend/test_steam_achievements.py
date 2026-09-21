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


def _user(db) -> models.User:
    u = models.User(name="t", username="t", password_hash="x", api_token="tok", steam_api_key="KEY", steam_id64="76561198000000000")
    db.add(u)
    db.commit()
    return u


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


def _api(monkeypatch, *, schema=None, rates=None, player=None, player_status=200, schema_status=200, rates_status=200):
    """Canned WebAPI, keyed by endpoint. Records every call's (endpoint, appid)."""
    calls: list[tuple[str, str]] = []
    schema = {} if schema is None else schema
    rates = {} if rates is None else rates
    player = {} if player is None else player

    def fake_get(url, params):
        appid = str(params.get("appid") or params.get("gameid"))
        req = httpx.Request("GET", url)
        if url == sa._SCHEMA_URL:
            calls.append(("schema", appid))
            achievements = schema.get(appid)
            body = {"game": {"availableGameStats": {"achievements": achievements}} if achievements is not None else {}}
            return httpx.Response(schema_status, json=body, request=req)
        if url == sa._GLOBAL_URL:
            calls.append(("rates", appid))
            return httpx.Response(rates_status, json={"achievementpercentages": {"achievements": rates.get(appid, [])}}, request=req)
        if url == sa._PLAYER_URL:
            calls.append(("player", appid))
            if player_status != 200:
                return httpx.Response(player_status, json={"playerstats": {"error": "nope", "success": False}}, request=req)
            if appid not in player:
                return httpx.Response(400, json={"playerstats": {"error": "Requested app has no stats", "success": False}}, request=req)
            return httpx.Response(200, json={"playerstats": {"success": True, "achievements": player[appid]}}, request=req)
        raise AssertionError(url)

    monkeypatch.setattr(sa, "_get", fake_get)
    return calls


def test_sync_writes_definitions_per_app_and_progress_per_entry(db_session, monkeypatch):
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
    mine = {a.definition_id: a for a in entry.achievements}
    assert mine[defs[0].id].earned is True
    # SQLite hands naive datetimes back; the instant is what matters.
    assert mine[defs[0].id].earned_at.replace(tzinfo=datetime.UTC).timestamp() == 1700000000
    assert mine[defs[1].id].earned is False and mine[defs[1].id].earned_at is None
    assert calls == [("schema", "440"), ("rates", "440"), ("player", "440")]
    # The release remembers what was read, so the next pass can tell.
    assert entry.release.raw_data[sa._SCHEMA_KEY]["total"] == 2


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
    assert len(entry.achievements) == 3
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
    entry = _entry(db_session, user, "440")
    _api(monkeypatch, schema={"440": _SCHEMA}, rates_status=403)  # no player entry -> 400 no stats

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["fetched"] == 1 and out["errored"] == 0 and out["earned"] == 0
    defs = db_session.query(models.AchievementDefinition).filter_by(set_id="440").all()
    assert len(defs) == 2 and all(d.rarity_pct is None for d in defs)
    assert entry.achievements == []


def test_sync_stops_on_an_answer_that_would_repeat(db_session, monkeypatch):
    """A private profile (403) or a bad key would fail every remaining app
    the same way; stop, report, and let the next run continue."""
    user = _user(db_session)
    _entry(db_session, user, "1")
    _entry(db_session, user, "2")
    calls = _api(monkeypatch, schema={"1": _SCHEMA, "2": _SCHEMA}, player_status=403)

    out = sa.sync_achievements(db_session, user, sleep=0)
    assert out["stopped"] == 403 and out["errored"] == 1 and out["checked"] == 1
    assert [c for c in calls if c[0] == "player"] == [("player", "1")]
    # The rolled-back app is not marked read, so the next run tries again.
    assert db_session.query(models.AchievementDefinition).count() == 0


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
