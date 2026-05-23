import pytest
from unittest.mock import patch, MagicMock
from backend import models


def _signup_and_login(client, username="testuser", password="testpass"):
    client.post("/signup", data={"username": username, "password": password, "password_confirm": password})
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    client.cookies.set("session", r.cookies["session"])
    return r.cookies["session"]


def test_integrations_hub_loads(client):
    _signup_and_login(client)
    r = client.get("/integrations")
    assert r.status_code == 200
    assert b"Steam" in r.content


def test_steam_page_loads(client):
    _signup_and_login(client)
    r = client.get("/integrations/steam")
    assert r.status_code == 200
    assert b"Steam API Key" in r.content


def test_save_steam_credentials(client, db_session):
    token = _signup_and_login(client)
    r = client.post("/integrations/steam/credentials", data={
        "steam_api_key": "TESTAPIKEY123",
        "steam_id64": "76561197960287930",
    })
    assert r.status_code == 200
    assert b"saved" in r.content.lower()

    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.refresh(user)
    assert user.steam_api_key == "TESTAPIKEY123"
    assert user.steam_id64 == "76561197960287930"


def test_save_steam_credentials_clears_on_empty(client, db_session):
    token = _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "KEY",
        "steam_id64": "123",
    })
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "",
        "steam_id64": "",
    })
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.refresh(user)
    assert user.steam_api_key is None
    assert user.steam_id64 is None


def test_sync_requires_credentials(client):
    _signup_and_login(client)
    r = client.post("/integrations/steam/sync")
    assert r.status_code == 422
    assert b"required" in r.content.lower()


def test_sync_kickoff_returns_started_toast_and_creates_job(client, db_session):
    """The POST returns immediately with a 'started' toast and a Job is registered.
    The actual sync mechanics are covered by test_run_sync_job_full below."""
    from backend import jobs
    jobs.clear_all()

    _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "FAKEKEY",
        "steam_id64": "76561197960287930",
    })

    # Patch the sync function so even if the background task starts running, it doesn't
    # try to hit Steam. We're testing the kickoff response, not the sync itself.
    with patch("backend.steam.sync_steam_library", return_value={"added": 0, "updated": 0, "total": 0}):
        r = client.post("/integrations/steam/sync")

    assert r.status_code == 200
    assert b"started" in r.content.lower()

    # A job for this user should exist (may already have completed in the background)
    user = db_session.query(models.User).first()
    all_jobs = [j for j in jobs._jobs.values() if j.user_id == user.id]
    assert len(all_jobs) == 1
    assert all_jobs[0].kind == "steam_sync_games"


def test_sync_kickoff_rejects_concurrent_run(client, db_session):
    """If a sync is already active for the user, kickoff returns 409."""
    from backend import jobs
    jobs.clear_all()

    _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "FAKEKEY", "steam_id64": "76561197960287930",
    })
    user = db_session.query(models.User).first()
    # Pretend a sync is already running
    jobs.create(user_id=user.id, kind="steam_sync_games")
    jobs.update(list(jobs._jobs.keys())[0], status=jobs.JobStatus.RUNNING)

    r = client.post("/integrations/steam/sync")
    assert r.status_code == 409
    assert b"already running" in r.content.lower()


def test_run_sync_job_full_imports_games_and_dlc(db_session):
    """End-to-end test of the background runner: mocks Steam, runs the async
    job directly, verifies the DB state and job completion message."""
    import asyncio
    from backend import jobs
    from backend.integrations import _run_sync_job
    jobs.clear_all()

    user = models.User(
        name="t", username="t", password_hash="x", api_token="tok",
        steam_api_key="FAKEKEY", steam_id64="76561197960287930",
        steam_session_id="sess", steam_login_secure="login",
    )
    db_session.add(user)
    db_session.commit()

    fake_games = [
        {"appid": 100, "name": "Game One", "playtime_forever": 0, "rtime_last_played": 0},
        {"appid": 200, "name": "Game Two", "playtime_forever": 60, "rtime_last_played": 0},
    ]
    fake_userdata = MagicMock()
    fake_userdata.json.return_value = {"rgOwnedApps": [100, 200, 300, 400]}
    fake_userdata.raise_for_status.return_value = None
    fake_app_names = {100: "Game One", 200: "Game Two", 300: "Game One - DLC", 400: "Game Two - DLC"}

    job = jobs.create(user_id=user.id, kind="steam_sync_full")

    with patch("backend.steam.get_owned_games", return_value=fake_games), \
         patch("backend.steam.httpx.get", return_value=fake_userdata), \
         patch("backend.steam.get_app_list", return_value=fake_app_names), \
         patch("backend.integrations.SessionLocal", return_value=db_session):
        # Stop SessionLocal-as-context-manager from closing our test session
        db_session.close = lambda: None
        asyncio.run(_run_sync_job(job.id, user.id, "steam_sync_full"))

    final = jobs.get(job.id)
    assert final.status == jobs.JobStatus.DONE
    assert "games added" in final.message
    assert "DLC" in final.message

    # 2 games + 2 DLC = 4 library entries
    db_session.expire_all()
    assert db_session.query(models.UserLibraryEntry).count() == 4
    assert db_session.query(models.Game).filter_by(is_dlc=True).count() == 2


def test_jobs_poll_returns_completed_toasts_once(client, db_session):
    """A completed job appears in the next poll, then is suppressed on
    subsequent polls (notified flag prevents repeat toasts)."""
    from backend import jobs
    jobs.clear_all()

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    job = jobs.create(user_id=user.id, kind="steam_sync_games")
    jobs.mark_done(job.id, "Games sync complete — 3 added.")

    r = client.get("/integrations/jobs/poll")
    assert r.status_code == 200
    assert b"3 added" in r.content
    # The poller element comes back so polling continues
    assert b'id="job-poller"' in r.content

    # Second poll: notification was consumed, no toast this time
    r2 = client.get("/integrations/jobs/poll")
    assert r2.status_code == 200
    assert b"3 added" not in r2.content
    assert b'id="job-poller"' in r2.content


def test_jobs_poll_failure_toast_is_danger(client, db_session):
    from backend import jobs
    jobs.clear_all()

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    job = jobs.create(user_id=user.id, kind="steam_sync_games")
    jobs.mark_failed(job.id, "Sync failed: connection refused.")

    r = client.get("/integrations/jobs/poll")
    assert r.status_code == 200
    assert b"toast-danger" in r.content
    assert b"connection refused" in r.content


def test_enrichment_status_returns_counts(client, db_session):
    _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "K", "steam_id64": "1",
    })
    r = client.get("/integrations/steam/enrichment-status")
    assert r.status_code == 200
    # Empty library — both numbers are zero
    assert b"0" in r.content


def test_enrichment_refresh_nulls_timestamps(client, db_session):
    """The bug we just fixed: this endpoint used to 500 on a join+update."""
    from backend import steam
    _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "FAKEKEY", "steam_id64": "76561197960287930",
    })
    user = db_session.query(models.User).first()
    fake_games = [{"appid": 100, "name": "G", "playtime_forever": 0, "rtime_last_played": 0}]
    # Seed a Steam release directly (used to go through the HTTP sync endpoint,
    # but that's now async and the import happens in a background task)
    with patch("backend.steam.get_owned_games", return_value=fake_games):
        steam.sync_steam_library(db_session, user)

    # Pretend the worker has enriched it
    import datetime
    release = db_session.query(models.GameRelease).first()
    release.metadata_fetched_at = datetime.datetime.now(datetime.timezone.utc)
    db_session.commit()

    r = client.post("/integrations/steam/enrichment-refresh")
    assert r.status_code == 200
    assert b"Queued 1 entries" in r.content

    db_session.expire_all()
    release = db_session.query(models.GameRelease).first()
    assert release.metadata_fetched_at is None


def test_enrichment_transient_failure_leaves_entry_unstamped(db_session):
    """Network errors must NOT mark entries as enriched — they have to be retried."""
    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="G")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="999")
    db_session.add(release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    with patch("backend.steam._fetch_appdetails", side_effect=Exception("network down")), \
         patch("backend.steam.time.sleep", return_value=None):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    release = db_session.query(models.GameRelease).first()
    # Transient failure: must remain unstamped so the worker retries
    assert release.metadata_fetched_at is None


def test_enrichment_permanent_failure_stamps_entry(db_session):
    """Steam-confirmed unavailable (success=false) should stamp so we stop retrying."""
    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok2")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="G")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="999")
    db_session.add(release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    with patch("backend.steam._fetch_appdetails", return_value=None), \
         patch("backend.steam.time.sleep", return_value=None):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    release = db_session.query(models.GameRelease).first()
    assert release.metadata_fetched_at is not None


def test_enrichment_429_triggers_backoff_and_unstamps(db_session):
    """A 429 from Steam must trigger long backoff AND not stamp the entry as done."""
    import httpx
    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok429")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="G")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="999")
    db_session.add(release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    fake_resp = MagicMock(status_code=429)
    err = httpx.HTTPStatusError("429", request=MagicMock(), response=fake_resp)
    sleep_mock = MagicMock()

    with patch("backend.steam._fetch_appdetails", side_effect=err), \
         patch("backend.steam.time.sleep", sleep_mock):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    release = db_session.query(models.GameRelease).first()
    # 429 must NOT stamp — Steam was rate-limiting, not confirming the app is gone
    assert release.metadata_fetched_at is None
    # And we should have slept at least once for the long backoff window
    assert any(call.args[0] >= 30 for call in sleep_mock.call_args_list), \
        f"Expected a >=30s backoff sleep, got: {[c.args[0] for c in sleep_mock.call_args_list]}"


def test_get_app_list_paginates(monkeypatch, tmp_path):
    """GetAppList uses cursor-style pagination via last_appid — verify we walk all pages."""
    from backend import steam

    # Use an isolated cache file so we don't poison the real one
    monkeypatch.setattr(steam, "_APP_LIST_CACHE_PATH", str(tmp_path / "applist.json"))
    monkeypatch.setattr(steam, "_app_list_memory", {})
    monkeypatch.setattr(steam, "_app_list_cached_at", None)

    page1 = MagicMock()
    page1.json.return_value = {"response": {
        "apps": [{"appid": 1, "name": "A"}, {"appid": 2, "name": "B"}],
        "have_more_results": True,
        "last_appid": 2,
    }}
    page1.raise_for_status.return_value = None
    page2 = MagicMock()
    page2.json.return_value = {"response": {
        "apps": [{"appid": 3, "name": "C"}],
        "have_more_results": False,
    }}
    page2.raise_for_status.return_value = None

    with patch("backend.steam.httpx.get", side_effect=[page1, page2]) as get_mock:
        result = steam.get_app_list("FAKEKEY")

    assert result == {1: "A", 2: "B", 3: "C"}
    # Two calls: first with no last_appid, second with last_appid=2
    assert get_mock.call_count == 2
    second_params = get_mock.call_args_list[1].kwargs["params"]
    assert second_params["last_appid"] == 2
    assert second_params["key"] == "FAKEKEY"


def test_sync_updates_playtime_on_resync(client, db_session):
    """Tests the steam.sync_steam_library logic directly (the HTTP endpoint now
    queues a background task; the underlying sync function is what actually
    upserts the data)."""
    from backend import steam
    _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "FAKEKEY",
        "steam_id64": "76561197960287930",
    })
    user = db_session.query(models.User).first()

    game_v1 = [{"appid": 1245620, "name": "Elden Ring", "playtime_forever": 100, "rtime_last_played": 0}]
    game_v2 = [{"appid": 1245620, "name": "Elden Ring", "playtime_forever": 250, "rtime_last_played": 0}]

    with patch("backend.steam.get_owned_games", return_value=game_v1):
        steam.sync_steam_library(db_session, user)
    with patch("backend.steam.get_owned_games", return_value=game_v2):
        steam.sync_steam_library(db_session, user)

    entries = db_session.query(models.UserLibraryEntry).all()
    assert len(entries) == 1
    db_session.refresh(entries[0])
    assert entries[0].playtime_minutes == 250
