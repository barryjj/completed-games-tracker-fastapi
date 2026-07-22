from unittest.mock import MagicMock, patch

import pytest

from backend import models


def _signup_and_login(client, username="testuser", password="testpass"):
    client.post("/signup", data={"username": username, "password": password, "password_confirm": password})
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    client.cookies.set("session", r.cookies["session"])
    return r.cookies["session"]


def _setup_steam_connected(client, db_session, api_key="FAKEKEY", steam_id="76561197960287930"):
    """Test helper: sign up + log in + set Steam credentials and identity to a
    "fully connected" state. SteamID is set directly on the User row because
    after the OpenID rework, the credentials form doesn't accept it."""
    _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={"steam_api_key": api_key})
    user = db_session.query(models.User).first()
    user.steam_id64 = steam_id
    db_session.commit()
    return user


def test_integrations_hub_redirects_to_tools(client):
    """The old hub's action cards moved to /tools; the URL redirects there."""
    _signup_and_login(client)
    r = client.get("/integrations", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/tools"


def test_tools_page_loads(client):
    _signup_and_login(client)
    r = client.get("/tools")
    assert r.status_code == 200
    assert b"Steam sync" in r.content
    assert b"Match review" in r.content
    assert b"Spreadsheet import" in r.content


def test_steam_page_loads(client):
    _signup_and_login(client)
    r = client.get("/integrations/steam")
    assert r.status_code == 200
    assert b"Steam API Key" in r.content


def test_save_steam_credentials(client, db_session):
    """Credentials form persists API key + cookies. SteamID is owned by the
    OpenID flow and is no longer accepted via this endpoint."""
    token = _signup_and_login(client)
    r = client.post(
        "/integrations/steam/credentials",
        data={"steam_api_key": "TESTAPIKEY123"},
    )
    assert r.status_code == 200
    assert b"saved" in r.content.lower()

    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.refresh(user)
    assert user.steam_api_key == "TESTAPIKEY123"


def test_save_steam_credentials_clears_on_empty(client, db_session):
    """Saving empty values clears API key + cookies. SteamID survives —
    use the openid/forget endpoint to drop the sign-in itself."""
    token = _signup_and_login(client)
    # Pretend OpenID already populated SteamID — should survive a credentials clear.
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.steam_id64 = "76561197960287930"
    user.steam_api_key = "KEY"
    user.steam_session_id = "sess"
    user.steam_login_secure = "login"
    db_session.commit()

    client.post(
        "/integrations/steam/credentials",
        data={"steam_api_key": "", "steam_session_id": "", "steam_login_secure": ""},
    )
    db_session.refresh(user)
    assert user.steam_api_key is None
    assert user.steam_session_id is None
    assert user.steam_login_secure is None
    # SteamID untouched — only the openid/forget endpoint can drop it.
    assert user.steam_id64 == "76561197960287930"


def test_cookies_only_endpoint_saves_cookies_but_not_api_key(client, db_session):
    """POST /integrations/steam/cookies (desktop auto-recovery) updates the two
    cookie columns and must leave the API key alone — the recovery JS runs from
    whatever page the failure toast landed on and has no form to read it from."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.steam_api_key = "KEEP-ME"
    user.steam_session_id = "stale-sess"
    user.steam_login_secure = "stale-login"
    db_session.commit()

    r = client.post(
        "/integrations/steam/cookies",
        data={"steam_session_id": "fresh-sess", "steam_login_secure": "fresh-login"},
    )
    assert r.status_code == 200
    db_session.refresh(user)
    assert user.steam_session_id == "fresh-sess"
    assert user.steam_login_secure == "fresh-login"
    assert user.steam_api_key == "KEEP-ME"

    # Blank values are rejected rather than silently clearing credentials.
    r = client.post(
        "/integrations/steam/cookies",
        data={"steam_session_id": "  ", "steam_login_secure": ""},
    )
    assert r.status_code == 422
    db_session.refresh(user)
    assert user.steam_session_id == "fresh-sess"


def test_cookie_expiry_failure_tags_error_code_and_poll_carries_retry_metadata(client, db_session, monkeypatch):
    """A sync failing with SteamCookiesExpiredError must (1) tag the job with
    the machine-readable error_code and (2) surface data-error-code +
    data-retry-url on the poll's failure toast — the hooks the desktop shell's
    auto-recovery loop keys on. Plain ValueError failures must carry neither."""
    import asyncio

    from backend import jobs, steam
    from backend.integrations import _run_sync_job

    jobs.clear_all()
    _setup_steam_connected(client, db_session)
    user = db_session.query(models.User).first()
    user.steam_session_id = "sess"
    user.steam_login_secure = "login"
    db_session.commit()

    # _run_sync_job opens its own SessionLocal (bound to the real DB URL, not
    # the test engine) — patch it to the test session, and stop the runner's
    # finally-close from killing that session (same dance as the sync tests).
    db_session.close = lambda: None

    def _expired(db, u):
        raise steam.SteamCookiesExpiredError("Steam session cookies have expired — retry.")

    monkeypatch.setattr(steam, "sync_full_library", _expired)
    job = jobs.create(user_id=user.id, kind="steam_sync_full", label="Sync")
    with patch("backend.integrations.SessionLocal", return_value=db_session):
        asyncio.run(_run_sync_job(job.id, user.id, "steam_sync_full"))
    assert jobs.get(job.id).status == jobs.JobStatus.FAILED
    assert jobs.get(job.id).error_code == "steam_cookies_expired"

    r = client.get("/integrations/jobs/poll")
    assert r.status_code == 200
    assert 'data-error-code="steam_cookies_expired"' in r.text
    assert 'data-retry-url="/integrations/steam/sync-all"' in r.text

    # Generic failures don't grow recovery attributes.
    def _other(db, u):
        raise ValueError("something else broke")

    monkeypatch.setattr(steam, "sync_full_library", _other)
    job2 = jobs.create(user_id=user.id, kind="steam_sync_full", label="Sync")
    with patch("backend.integrations.SessionLocal", return_value=db_session):
        asyncio.run(_run_sync_job(job2.id, user.id, "steam_sync_full"))
    assert jobs.get(job2.id).error_code is None
    r2 = client.get("/integrations/jobs/poll")
    assert "something else broke" in r2.text
    assert "data-error-code" not in r2.text


def test_psn_page_loads(client):
    _signup_and_login(client)
    r = client.get("/integrations/psn")
    assert r.status_code == 200
    assert b"PlayStation Network" in r.content
    assert b"NPSSO" in r.content


def test_save_psn_credentials_sets_captured_at_only_on_token_change(client, db_session):
    token = _signup_and_login(client)
    r = client.post(
        "/integrations/psn/credentials",
        data={"psn_online_id": "corrosivefrost", "psn_npsso": "a" * 64},
    )
    assert r.status_code == 200
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.refresh(user)
    assert user.psn_online_id == "corrosivefrost"
    assert user.psn_npsso == "a" * 64
    first_captured = user.psn_npsso_captured_at
    assert first_captured is not None

    # Re-saving the same token (e.g. editing only the online id) must not
    # move the captured_at freshness signal.
    client.post(
        "/integrations/psn/credentials",
        data={"psn_online_id": "newname", "psn_npsso": "a" * 64},
    )
    db_session.refresh(user)
    assert user.psn_online_id == "newname"
    assert user.psn_npsso_captured_at == first_captured

    # Clearing wipes token + timestamp.
    client.post("/integrations/psn/credentials", data={"psn_online_id": "", "psn_npsso": ""})
    db_session.refresh(user)
    assert user.psn_npsso is None
    assert user.psn_npsso_captured_at is None
    assert user.psn_online_id is None


def test_psn_test_token_paths(client, db_session):
    """No token → error flash; authorize 302 with ?code= → valid; without → invalid."""
    token = _signup_and_login(client)
    r = client.post("/integrations/psn/test-token")
    assert b"No NPSSO token saved" in r.content

    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "x" * 64
    db_session.commit()

    valid = MagicMock()
    valid.headers = {"location": "com.scee.psxandroid.scecompcall://redirect/?code=v3.AbCdEf"}
    with patch("backend.integrations._httpx.get", return_value=valid) as mocked:
        r = client.post("/integrations/psn/test-token")
    assert b"valid" in r.content
    assert mocked.call_args.kwargs["cookies"] == {"npsso": "x" * 64}
    assert mocked.call_args.kwargs["follow_redirects"] is False

    invalid = MagicMock()
    invalid.headers = {"location": "https://ca.account.sony.com/some-error"}
    with patch("backend.integrations._httpx.get", return_value=invalid):
        r = client.post("/integrations/psn/test-token")
    assert b"invalid or expired" in r.content


def test_openid_forget_clears_identity_but_not_credentials(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.steam_id64 = "76561197960287930"
    user.steam_persona_name = "corrosivefrost"
    user.steam_avatar_url = "https://avatars.example/x.jpg"
    user.steam_api_key = "KEEP-ME"
    db_session.commit()

    r = client.post("/integrations/steam/openid/forget")
    assert r.status_code == 200

    db_session.refresh(user)
    assert user.steam_id64 is None
    assert user.steam_persona_name is None
    assert user.steam_avatar_url is None
    # Credentials untouched
    assert user.steam_api_key == "KEEP-ME"


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

    _setup_steam_connected(client, db_session)

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

    user = _setup_steam_connected(client, db_session)
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
        name="t",
        username="t",
        password_hash="x",
        api_token="tok",
        steam_api_key="FAKEKEY",
        steam_id64="76561197960287930",
        steam_session_id="sess",
        steam_login_secure="login",
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

    with (
        patch("backend.steam.get_owned_games", return_value=fake_games),
        patch("backend.steam.httpx.get", return_value=fake_userdata),
        patch("backend.steam.get_app_list", return_value=fake_app_names),
        patch("backend.integrations.SessionLocal", return_value=db_session),
    ):
        # Stop SessionLocal-as-context-manager from closing our test session
        db_session.close = lambda: None
        asyncio.run(_run_sync_job(job.id, user.id, "steam_sync_full"))

    final = jobs.get(job.id)
    assert final.status == jobs.JobStatus.DONE
    # New format: "Steam sync complete\n+2 games · +2 DLC\n2 games · 2 DLC total"
    assert "Steam sync complete" in final.message
    assert "+2 games" in final.message
    assert "+2 DLC" in final.message

    # 2 games + 2 DLC = 4 library entries
    db_session.expire_all()
    assert db_session.query(models.UserLibraryEntry).count() == 4
    assert db_session.query(models.Game).filter_by(is_dlc=True).count() == 2


def test_run_sync_job_dlc_only(db_session):
    """DLC-only sync uses already-synced games as the baseline (no GetOwnedGames call)."""
    import asyncio

    from backend import jobs
    from backend.integrations import _run_sync_job

    jobs.clear_all()

    user = models.User(
        name="t",
        username="t",
        password_hash="x",
        api_token="tok-dlc",
        steam_api_key="FAKEKEY",
        steam_id64="76561197960287930",
        steam_session_id="sess",
        steam_login_secure="login",
    )
    db_session.add(user)
    db_session.flush()

    # Seed a game so it gets excluded from the DLC set
    game = models.Game(title="Existing Game", is_dlc=False)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="100")
    db_session.add(release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    fake_userdata = MagicMock()
    fake_userdata.json.return_value = {"rgOwnedApps": [100, 500, 600]}
    fake_userdata.raise_for_status.return_value = None
    fake_app_names = {100: "Existing Game", 500: "DLC A", 600: "DLC B"}

    job = jobs.create(user_id=user.id, kind="steam_sync_dlc")

    with (
        patch("backend.steam.httpx.get", return_value=fake_userdata),
        patch("backend.steam.get_app_list", return_value=fake_app_names),
        patch("backend.integrations.SessionLocal", return_value=db_session),
    ):
        db_session.close = lambda: None
        asyncio.run(_run_sync_job(job.id, user.id, "steam_sync_dlc"))

    final = jobs.get(job.id)
    assert final.status == jobs.JobStatus.DONE
    assert "DLC sync complete" in final.message
    # 1 existing game + 2 new DLC = 3 library entries
    db_session.expire_all()
    assert db_session.query(models.UserLibraryEntry).count() == 3
    assert db_session.query(models.Game).filter_by(is_dlc=True).count() == 2


def test_run_sync_job_refresh_catalog(db_session):
    """Catalog refresh invalidates caches and re-fetches; toast reports count."""
    import asyncio

    from backend import jobs
    from backend.integrations import _run_sync_job

    jobs.clear_all()

    user = models.User(
        name="t",
        username="t",
        password_hash="x",
        api_token="tok-cat",
        steam_api_key="FAKEKEY",
        steam_id64="76561197960287930",
    )
    db_session.add(user)
    db_session.commit()

    job = jobs.create(user_id=user.id, kind="steam_refresh_catalog")

    with (
        patch("backend.steam.get_app_list", return_value={1: "A", 2: "B", 3: "C"}),
        patch("backend.integrations.SessionLocal", return_value=db_session),
    ):
        db_session.close = lambda: None
        asyncio.run(_run_sync_job(job.id, user.id, "steam_refresh_catalog"))

    final = jobs.get(job.id)
    assert final.status == jobs.JobStatus.DONE
    assert "catalog refreshed" in final.message
    assert "3" in final.message


def test_run_sync_job_unknown_kind_marks_failed(db_session):
    import asyncio

    from backend import jobs
    from backend.integrations import _run_sync_job

    jobs.clear_all()

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-bad")
    db_session.add(user)
    db_session.commit()

    job = jobs.create(user_id=user.id, kind="steam_bogus")
    asyncio.run(_run_sync_job(job.id, user.id, "steam_bogus"))

    final = jobs.get(job.id)
    assert final.status == jobs.JobStatus.FAILED
    assert "Unknown" in final.error


def test_sync_kickoff_concurrent_runs_blocked_across_kinds(client, db_session):
    """Catalog refresh and library sync share the same active-job lock."""
    from backend import jobs

    jobs.clear_all()

    user = _setup_steam_connected(client, db_session, api_key="K", steam_id="1")
    # Pretend a catalog refresh is in progress
    j = jobs.create(user_id=user.id, kind="steam_refresh_catalog")
    jobs.update(j.id, status=jobs.JobStatus.RUNNING)

    r = client.post("/integrations/steam/sync")
    assert r.status_code == 409


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


def test_enrichment_status_empty_when_nothing_pending(client, db_session):
    """When pending==0, the partial renders nothing (no 'X enriched' line) —
    cleaner hub card. The endpoint still returns 200 so the polling HTMX
    swap doesn't error; the body is just whitespace."""
    _signup_and_login(client)
    r = client.get("/integrations/steam/enrichment-status")
    assert r.status_code == 200
    # Nothing pending → no status line; should not surface the old
    # "Metadata up to date — X enriched" wording.
    assert b"enriched" not in r.content
    assert b"Enriching" not in r.content


def test_enrichment_refresh_nulls_timestamps(client, db_session):
    """The bug we just fixed: this endpoint used to 500 on a join+update."""
    from backend import steam

    user = _setup_steam_connected(client, db_session)
    fake_games = [{"appid": 100, "name": "G", "playtime_forever": 0, "rtime_last_played": 0}]
    # Seed a Steam release directly (used to go through the HTTP sync endpoint,
    # but that's now async and the import happens in a background task)
    with patch("backend.steam.get_owned_games", return_value=fake_games):
        steam.sync_steam_library(db_session, user)

    # Pretend the worker has enriched it
    import datetime

    release = db_session.query(models.GameRelease).first()
    release.metadata_fetched_at = datetime.datetime.now(datetime.UTC)
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

    with (
        patch("backend.steam._fetch_appdetails", side_effect=Exception("network down")),
        patch("backend.steam.time.sleep", return_value=None),
    ):
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

    with patch("backend.steam._fetch_appdetails", return_value=None), patch("backend.steam.time.sleep", return_value=None):
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

    with patch("backend.steam._fetch_appdetails", side_effect=err), patch("backend.steam.time.sleep", sleep_mock):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    release = db_session.query(models.GameRelease).first()
    # 429 must NOT stamp — Steam was rate-limiting, not confirming the app is gone
    assert release.metadata_fetched_at is None
    # And we should have slept at least once for the long backoff window
    assert any(call.args[0] >= 30 for call in sleep_mock.call_args_list), (
        f"Expected a >=30s backoff sleep, got: {[c.args[0] for c in sleep_mock.call_args_list]}"
    )


def test_get_app_list_paginates(monkeypatch, tmp_path):
    """GetAppList uses cursor-style pagination via last_appid — verify we walk all pages."""
    from backend import steam

    # Use an isolated cache file so we don't poison the real one
    monkeypatch.setattr(steam, "_APP_LIST_CACHE_PATH", str(tmp_path / "applist.json"))
    monkeypatch.setattr(steam, "_app_list_memory", {})
    monkeypatch.setattr(steam, "_app_list_cached_at", None)

    page1 = MagicMock()
    page1.json.return_value = {
        "response": {
            "apps": [{"appid": 1, "name": "A"}, {"appid": 2, "name": "B"}],
            "have_more_results": True,
            "last_appid": 2,
        }
    }
    page1.raise_for_status.return_value = None
    page2 = MagicMock()
    page2.json.return_value = {
        "response": {
            "apps": [{"appid": 3, "name": "C"}],
            "have_more_results": False,
        }
    }
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

    user = _setup_steam_connected(client, db_session)

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


# ─── Heuristic / user-override tests ─────────────────────────────────────────


def test_clean_title_strips_trademark_symbols():
    from backend.steam import _clean_title

    # Trademark/copyright glyphs stripped; casing preserved as-is.
    assert _clean_title("ELDEN RING™") == "ELDEN RING"
    assert _clean_title("Halo®: Combat Evolved") == "Halo: Combat Evolved"


def test_clean_title_preserves_casing():
    """We used to title-case loud ALL-CAPS titles. Decision: leave Steam's
    casing alone — the heuristic was inconsistent (only fired on whole-string
    ALL CAPS, missed mixed-case DLC names) and the edit modal lets users
    override display_name when they don't like a shouting title."""
    from backend.steam import _clean_title

    assert _clean_title("ELDEN RING") == "ELDEN RING"
    assert _clean_title("ELDEN RING NIGHTREIGN The Forsaken Hollows") == "ELDEN RING NIGHTREIGN The Forsaken Hollows"
    assert _clean_title("DOOM Eternal") == "DOOM Eternal"
    assert _clean_title("Halo: Combat Evolved") == "Halo: Combat Evolved"


def test_clean_title_is_idempotent():
    from backend.steam import _clean_title

    # Running on already-cleaned title is a no-op.
    assert _clean_title("Elden Ring") == "Elden Ring"
    assert _clean_title("ELDEN RING") == "ELDEN RING"


def test_clean_title_unescapes_html_entities():
    from backend.steam import _clean_title

    assert _clean_title("Fish &amp; Chips") == "Fish & Chips"
    assert _clean_title("Game &quot;Subtitle&quot;") == 'Game "Subtitle"'
    assert _clean_title("A&amp;B") == "A&B"
    # Combined with trademark stripping
    assert _clean_title("Game™ &amp; DLC") == "Game & DLC"


def test_infer_is_collection_regex():
    from backend.steam import _infer_is_collection

    # Should match — real multi-game sets
    assert _infer_is_collection("The Bioshock Collection") is True
    assert _infer_is_collection("Mass Effect Legendary Edition Trilogy") is True
    assert _infer_is_collection("Metal Gear Solid: Master Collection Vol.1") is True
    assert _infer_is_collection("Metal Gear Solid: Master Collection Vol. 2") is True
    assert _infer_is_collection("The Dark Knight Trilogy") is True
    assert _infer_is_collection("Games Compilation") is True
    assert _infer_is_collection("Game Complete Pack") is True

    # Should NOT match — false positives removed from keyword list
    assert _infer_is_collection("Castlevania Anthology") is False  # "anthology" removed — series name, not a set
    assert _infer_is_collection("Mass Effect Complete Edition") is False  # "complete edition" removed — bundling, not a set
    assert _infer_is_collection("Dark Pictures Anthology: House of Ashes") is False
    assert _infer_is_collection("Dirt 3 Complete Edition") is False
    assert _infer_is_collection("Elven Legacy") is False
    assert _infer_is_collection("Assassin's Creed Origins") is False
    assert _infer_is_collection("Lost Chronicles of Zerzura") is False
    assert _infer_is_collection("Bundle of Joy") is False
    assert _infer_is_collection("Archives of the Deep") is False
    assert _infer_is_collection("Recollection") is False  # sub-word of "collection"

    # DLC is never a collection regardless of title
    assert _infer_is_collection("Cepheus Protocol Post Modern Collection", is_dlc=True) is False
    assert _infer_is_collection("The Bioshock Collection", is_dlc=True) is False

    # "collection" must be at/near end of title — "Bonus Content" after kills it
    assert _infer_is_collection("Metal Gear Solid: Master Collection Vol.1 Bonus Content") is False


def test_should_auto_hide_only_fires_for_dlc():
    """Auto-hide uses a two-tier check.

    Tier 1 (gate-free): music/video/episode type, beta type, beta in title,
    demo type + demo in title.  These fire regardless of is_dlc.

    Tier 2 (DLC-only): soundtracks, artbooks, cosmetic packs, passes, bonus
    content, deluxe upgrades.  These require is_dlc=True.
    """
    from backend.steam import _should_auto_hide

    # --- Tier 2 (DLC-only): title patterns ---
    assert _should_auto_hide("Elden Ring - Soundtrack", None, is_dlc=True) is True
    assert _should_auto_hide("Game OST", None, is_dlc=True) is True
    assert _should_auto_hide("Game Artbook", None, is_dlc=True) is True
    assert _should_auto_hide("Cosmetic Pack", None, is_dlc=True) is True
    assert _should_auto_hide("Wallpaper Set", None, is_dlc=True) is True
    assert _should_auto_hide("TEKKEN 8 - Season 1 Character Pass", None, is_dlc=True) is True
    assert _should_auto_hide("TEKKEN 8 - Season 2 Character & Stage Pass", None, is_dlc=True) is True
    assert _should_auto_hide("Street Fighter 6 - Year 1 Ultimate Pass", None, is_dlc=True) is True
    assert _should_auto_hide("Mortal Kombat 11 Klassic Skin Pack", None, is_dlc=True) is True
    assert _should_auto_hide("Mortal Kombat 11 Cinematic Pack", None, is_dlc=True) is True
    assert _should_auto_hide("MK11 Ultimate Add-On Bundle", None, is_dlc=True) is True
    assert _should_auto_hide("Blaster Master Zero 2 - DLC Playable Character: Copen", None, is_dlc=True) is True
    assert _should_auto_hide("TEKKEN 8 - Avatar Skin: Tetsujin", None, is_dlc=True) is True
    assert _should_auto_hide("Game - Digital Deluxe Edition Upgrade", None, is_dlc=True) is True
    # "Bonus Content" DLC purchase wrapper
    assert _should_auto_hide("Metal Gear Solid: Master Collection Vol.1 Bonus Content", None, is_dlc=True) is True
    # Standalone costume / skin / outfit
    assert _should_auto_hide("Castlevania: Lords of Shadow 2 - Armored Dracula Costume", None, is_dlc=True) is True
    assert _should_auto_hide("Castlevania: Lords of Shadow 2 - Dark Dracula Costume", None, is_dlc=True) is True
    assert _should_auto_hide("Some Game - Legendary Outfit", None, is_dlc=True) is True
    assert _should_auto_hide("Some Game - Hero Skin", None, is_dlc=True) is True
    # Standalone pack / pass
    assert _should_auto_hide("Castlevania: Lords of Shadow 2 - Relic Rune Pack", None, is_dlc=True) is True
    assert _should_auto_hide("Some Game - Starter Pack", None, is_dlc=True) is True
    assert _should_auto_hide("DOOM Eternal Year One Pass", None, is_dlc=True) is True
    assert _should_auto_hide("Some Game - Annual Pass", None, is_dlc=True) is True
    assert _should_auto_hide("Some Game - Battle Pass Season 3", None, is_dlc=True) is True

    # --- Tier 2 DLC-only: NOT hidden when is_dlc=False ---
    assert _should_auto_hide("Elden Ring - Soundtrack", None, is_dlc=False) is False
    assert _should_auto_hide("Cosmetic Pack", None, is_dlc=False) is False

    # --- Tier 1 (gate-free): music / video / episode types ---
    assert _should_auto_hide("Some Soundtrack App", {"type": "music"}, is_dlc=False) is True
    assert _should_auto_hide("Some Soundtrack App", {"type": "music"}, is_dlc=True) is True
    assert _should_auto_hide("Amnesia Fortnight 2017", {"type": "video"}, is_dlc=False) is True
    assert _should_auto_hide("Some Series Episode 1", {"type": "episode"}, is_dlc=False) is True
    assert _should_auto_hide("Documentary", {"type": "video"}, is_dlc=True) is True

    # --- Tier 1: beta type ---
    assert _should_auto_hide("Some Game Beta", {"type": "beta"}, is_dlc=False) is True
    assert _should_auto_hide("Some Game Beta", {"type": "beta"}, is_dlc=True) is True

    # --- Tier 1: "beta" in title (catches network tests mislabelled type=game) ---
    assert _should_auto_hide("Elden Ring NIGHTREIGN Network Test Beta", None, is_dlc=False) is True
    assert _should_auto_hide("Some Game - Open Beta", {"type": "game"}, is_dlc=False) is True
    # No word boundary → should NOT match
    assert _should_auto_hide("Roberta Williams Collection", None, is_dlc=False) is False

    # --- Tier 1: "public test" in title ---
    assert _should_auto_hide("For Honor - Public Test", None, is_dlc=False) is True
    assert _should_auto_hide("Some MMO Public Test Server", {"type": "game"}, is_dlc=False) is True

    # --- Tier 1: demo type + "demo" in title ---
    assert _should_auto_hide("DOOM Eternal Demo", {"type": "demo"}, is_dlc=False) is True
    # demo type but no "demo" in title → keep visible (RE7: Beginning Hour etc.)
    assert _should_auto_hide("RE7: Beginning Hour", {"type": "demo"}, is_dlc=False) is False

    # --- Real content DLC should NOT be hidden by tier-2 patterns ---
    assert _should_auto_hide("Castlevania: Lords of Shadow 2 - Revelations", None, is_dlc=True) is False
    assert _should_auto_hide("DOOM Eternal: The Ancient Gods - Part One", None, is_dlc=True) is False

    # Real games shouldn't match (even when is_dlc=True, no pattern word)
    assert _should_auto_hide("Elden Ring", None, is_dlc=True) is False
    assert _should_auto_hide("Doom Eternal", None, is_dlc=True) is False


def test_enrichment_demotes_is_dlc_when_appdetails_says_game(db_session):
    """rgOwnedApps subtraction can misclassify; when appdetails explicitly says
    'game' and the user hasn't overridden, demote is_dlc to False."""
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-demote")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="1 Screen Platformer", is_dlc=True)  # imported as DLC, wrongly
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="791180")
    db_session.add(release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    # Steam says it's a game. We should demote.
    with patch("backend.steam._fetch_appdetails", return_value={"type": "game"}), patch("backend.steam.time.sleep", return_value=None):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    assert db_session.query(models.Game).first().is_dlc is False


def test_enrichment_does_not_demote_season_pass_mislabeled_as_game(db_session):
    """Steam sometimes tags season passes / bundle wrappers as type=game.
    We should not demote is_dlc to False when the title matches auto-hide
    patterns — keep it as DLC so auto-hide can fire."""
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-pass-guard")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="DOOM Eternal Year One Pass", is_dlc=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="1098291")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    # Steam says type=game (its mislabelling), no fullgame field.
    with (
        patch("backend.steam._fetch_appdetails", return_value={"type": "game", "name": "DOOM Eternal Year One Pass"}),
        patch("backend.steam.time.sleep", return_value=None),
    ):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    game = db_session.query(models.Game).filter_by(title="DOOM Eternal Year One Pass").first()
    entry = db_session.query(models.UserLibraryEntry).first()
    assert game.is_dlc is True  # not demoted
    assert entry.is_hidden is True  # and auto-hidden


def test_enrichment_repromotes_previously_demoted_season_pass(db_session):
    """Entries that were already demoted to is_dlc=False before the guard
    existed should be re-promoted when refreshed if their title matches
    auto-hide patterns."""
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-repromote")
    db_session.add(user)
    db_session.flush()
    # Simulates an entry that was already demoted by a previous enrichment run.
    game = models.Game(title="DOOM Eternal Year One Pass", is_dlc=False)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="1098292")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    with (
        patch("backend.steam._fetch_appdetails", return_value={"type": "game", "name": "DOOM Eternal Year One Pass"}),
        patch("backend.steam.time.sleep", return_value=None),
    ):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    game = db_session.query(models.Game).filter_by(title="DOOM Eternal Year One Pass").first()
    entry = db_session.query(models.UserLibraryEntry).first()
    assert game.is_dlc is True  # re-promoted
    assert entry.is_hidden is True  # and auto-hidden


def test_enrichment_demotion_respects_user_override(db_session):
    """Even if appdetails says 'game', a user who manually marked is_dlc=True
    sticks — their is_dlc_user_set flag blocks the demotion."""
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-demote-block")
    db_session.add(user)
    db_session.flush()
    # User has explicitly said "this IS DLC" — for some reason
    game = models.Game(title="Weirdo Entry", is_dlc=True, is_dlc_user_set=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="999")
    db_session.add(release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    with patch("backend.steam._fetch_appdetails", return_value={"type": "game"}), patch("backend.steam.time.sleep", return_value=None):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    # User's True wins.
    assert db_session.query(models.Game).first().is_dlc is True


def test_enrichment_respects_is_dlc_user_set(db_session):
    """If the user has manually marked a game as not-DLC, the worker must
    not re-promote it to DLC on enrichment."""
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-user-set")
    db_session.add(user)
    db_session.flush()
    # User has explicitly said "this is NOT DLC" (e.g., for a base game Steam
    # incorrectly tagged as DLC)
    game = models.Game(title="G", is_dlc=False, is_dlc_user_set=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="500")
    db_session.add(release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    # Steam says this is DLC. User says no. User wins.
    with patch("backend.steam._fetch_appdetails", return_value={"type": "dlc"}), patch("backend.steam.time.sleep", return_value=None):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    assert db_session.query(models.Game).first().is_dlc is False


def test_enrichment_auto_hides_soundtrack_but_respects_user_unhide(db_session):
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-hide")
    db_session.add(user)
    db_session.flush()
    # is_dlc=True because soundtracks come in as DLC from the rgOwnedApps
    # subtraction during sync. Auto-hide is gated on is_dlc=True.
    game = models.Game(title="Elden Ring Soundtrack", is_dlc=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="700")
    db_session.add(release)
    db_session.flush()
    # User has explicitly UNHID this (they actually want the soundtrack visible)
    entry = models.UserLibraryEntry(
        user_id=user.id,
        release_id=release.id,
        import_source="steam_import",
        is_hidden=False,
        is_hidden_user_set=True,
    )
    db_session.add(entry)
    db_session.commit()

    with patch("backend.steam._fetch_appdetails", return_value={"type": "music"}), patch("backend.steam.time.sleep", return_value=None):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    # User's unhide stands — auto-hide heuristic skipped this entry.
    assert db_session.query(models.UserLibraryEntry).first().is_hidden is False


def test_backfill_hidden_endpoint(client, db_session):
    """The one-shot backfill applies the auto-hide heuristic across existing
    library entries, skipping user_set ones."""
    from backend.test_pages import _add_game, _signup_and_login

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    # Soundtrack — should get auto-hidden (is_dlc=True is required for the
    # heuristic gate; in real sync, soundtracks land in is_dlc=True naturally)
    s = _add_game(db_session, user, title="Game OST")
    s.release.game.is_dlc = True
    # Regular game — should NOT auto-hide even if its title happened to match
    g = _add_game(db_session, user, title="Elden Ring")
    # Soundtrack the user explicitly unhid — should be left alone
    user_set = _add_game(db_session, user, title="Cool Game Soundtrack")
    user_set.release.game.is_dlc = True
    user_set.is_hidden_user_set = True
    db_session.commit()

    r = client.post("/library/backfill-hidden")
    assert r.status_code == 200
    assert b"1 entries hidden" in r.content

    db_session.expire_all()
    assert db_session.query(models.UserLibraryEntry).filter_by(id=s.id).first().is_hidden is True
    assert db_session.query(models.UserLibraryEntry).filter_by(id=g.id).first().is_hidden is False
    assert db_session.query(models.UserLibraryEntry).filter_by(id=user_set.id).first().is_hidden is False


def test_enrichment_syncs_header_url_from_appdetails(db_session):
    """When appdetails returns header_image, store it as the release's header
    artwork. Newer DLC assets live on hashed paths our legacy constructed CDN
    URL doesn't match — using the appdetails URL fixes this."""
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-hdr")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="Test DLC", is_dlc=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="3531720")
    db_session.add(release)
    db_session.flush()
    # Pre-existing artwork with the old legacy CDN URL (as sync would create it)
    db_session.add(
        models.GameArtwork(
            release_id=release.id,
            artwork_type="cover_h",
            source="steam",
            url="https://cdn.akamai.steamstatic.com/steam/apps/3531720/header.jpg",
        )
    )
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    new_url = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/3531720/HASH/header.jpg"
    with (
        patch("backend.steam._fetch_appdetails", return_value={"type": "dlc", "header_image": new_url}),
        patch("backend.steam.time.sleep", return_value=None),
    ):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    art = db_session.query(models.GameArtwork).filter_by(release_id=release.id, artwork_type="cover_h").first()
    assert art is not None
    assert art.url == new_url


def test_enrichment_creates_header_artwork_if_missing(db_session):
    """Entry imported without a header GameArtwork row gets one created from
    appdetails on first enrichment."""
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-hdr2")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="No Art Yet", is_dlc=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="999")
    db_session.add(release)
    db_session.flush()
    # Intentionally NO header artwork yet
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    url = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/999/HASH/header.jpg"
    with (
        patch("backend.steam._fetch_appdetails", return_value={"type": "dlc", "header_image": url}),
        patch("backend.steam.time.sleep", return_value=None),
    ):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    art = db_session.query(models.GameArtwork).filter_by(release_id=release.id, artwork_type="cover_h").first()
    assert art is not None
    assert art.url == url


def test_enrichment_promotes_dlc_children_via_parent_dlc_array(db_session):
    """When a parent game's appdetails includes a dlc[] list, any of those
    appids we own should be marked is_dlc=True and linked to the parent —
    even if the child's own appdetails returns type=game (e.g. DOOM Eternal:
    The Ancient Gods standalone expansions)."""
    from unittest.mock import patch

    from backend import steam

    user = models.User(name="t", username="t", password_hash="x", api_token="tok-dlcpush")
    db_session.add(user)
    db_session.flush()

    # Parent game
    parent_game = models.Game(title="DOOM Eternal", is_dlc=False)
    db_session.add(parent_game)
    db_session.flush()
    parent_release = models.GameRelease(game_id=parent_game.id, platform="Steam", source="steam", external_id="782330")
    db_session.add(parent_release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=parent_release.id, import_source="steam_import"))

    # Child entries — both is_dlc=False because Steam returned type=game on them
    child_game1 = models.Game(title="DOOM Eternal: The Ancient Gods - Part One", is_dlc=False)
    child_game2 = models.Game(title="DOOM Eternal: The Ancient Gods - Part Two", is_dlc=False)
    db_session.add_all([child_game1, child_game2])
    db_session.flush()
    child_release1 = models.GameRelease(
        game_id=child_game1.id,
        platform="Steam",
        source="steam",
        external_id="1098292",
        metadata_fetched_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    child_release2 = models.GameRelease(
        game_id=child_game2.id,
        platform="Steam",
        source="steam",
        external_id="1098293",
        metadata_fetched_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    db_session.add_all([child_release1, child_release2])
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=child_release1.id, import_source="steam_import"))
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=child_release2.id, import_source="steam_import"))
    db_session.commit()

    parent_details = {
        "type": "game",
        "name": "DOOM Eternal",
        "dlc": [1098292, 1098293],
    }
    with (
        patch("backend.steam._fetch_appdetails", return_value=parent_details),
        patch("backend.steam.time.sleep", return_value=None),
    ):
        steam.enrich_next_batch(db_session, batch_size=5)

    db_session.expire_all()
    child1 = db_session.query(models.Game).filter_by(title="DOOM Eternal: The Ancient Gods - Part One").first()
    child2 = db_session.query(models.Game).filter_by(title="DOOM Eternal: The Ancient Gods - Part Two").first()
    assert child1.is_dlc is True, "Part One should be promoted to DLC via parent dlc[] array"
    assert child1.parent_id == parent_game.id
    assert child2.is_dlc is True, "Part Two should be promoted to DLC via parent dlc[] array"
    assert child2.parent_id == parent_game.id


# ─── Steam OpenID ─────────────────────────────────────────────────────────


def test_openid_start_redirects_to_steam(client):
    _signup_and_login(client)
    r = client.get("/integrations/steam/openid/start", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("https://steamcommunity.com/openid/login?")
    # Required OpenID params
    assert "openid.mode=checkid_setup" in location
    assert "openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select" in location
    # Our callback URL is in there
    assert "openid.return_to=" in location
    assert "%2Fintegrations%2Fsteam%2Fopenid%2Freturn" in location


def test_openid_return_persists_steam_id_on_valid_signature(client, db_session):
    """Valid signature: Steam responds 'is_valid:true', we parse SteamID from
    claimed_id and save it. No persona name lookup if no API key is set."""
    from unittest.mock import patch

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    assert user.steam_id64 is None

    fake_verify = MagicMock()
    fake_verify.text = "ns:http://specs.openid.net/auth/2.0\nis_valid:true\n"
    fake_verify.raise_for_status.return_value = None

    with patch("backend.integrations._httpx.post", return_value=fake_verify):
        r = client.get(
            "/integrations/steam/openid/return",
            params={
                "openid.claimed_id": "https://steamcommunity.com/openid/id/76561197960287930",
                "openid.identity": "https://steamcommunity.com/openid/id/76561197960287930",
                "openid.mode": "id_res",
                "openid.sig": "fake-signature",
                "openid.signed": "signed,op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle",
                "openid.response_nonce": "fake-nonce",
                "openid.assoc_handle": "fake-handle",
                "openid.return_to": "http://testserver/integrations/steam/openid/return",
                "openid.op_endpoint": "https://steamcommunity.com/openid/login",
            },
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert "openid=ok" in r.headers["location"]

    db_session.refresh(user)
    assert user.steam_id64 == "76561197960287930"


def test_openid_return_rejects_bad_claimed_id(client, db_session):
    """Garbage claimed_id → redirect with error flag, no DB writes."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()

    r = client.get(
        "/integrations/steam/openid/return",
        params={"openid.claimed_id": "https://evil.example.com/openid/id/123"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "openid=bad_claim" in r.headers["location"]
    db_session.refresh(user)
    assert user.steam_id64 is None


def test_openid_return_rejects_invalid_signature(client, db_session):
    """Steam responds is_valid:false → don't save anything."""
    from unittest.mock import patch

    _signup_and_login(client)
    user = db_session.query(models.User).first()

    fake_verify = MagicMock()
    fake_verify.text = "ns:http://specs.openid.net/auth/2.0\nis_valid:false\n"
    fake_verify.raise_for_status.return_value = None

    with patch("backend.integrations._httpx.post", return_value=fake_verify):
        r = client.get(
            "/integrations/steam/openid/return",
            params={
                "openid.claimed_id": "https://steamcommunity.com/openid/id/76561197960287930",
                "openid.mode": "id_res",
                "openid.sig": "tampered-sig",
            },
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert "openid=invalid_sig" in r.headers["location"]
    db_session.refresh(user)
    assert user.steam_id64 is None


def test_openid_return_fetches_persona_when_api_key_set(client, db_session):
    """If the user has an API key on file, the return handler also fetches
    their Steam persona name and stores it for the UI."""
    from unittest.mock import patch

    _signup_and_login(client)
    # Pre-set an API key
    client.post(
        "/integrations/steam/credentials",
        data={"steam_api_key": "FAKEKEY", "steam_id64": ""},
    )

    fake_verify = MagicMock()
    fake_verify.text = "is_valid:true\n"
    fake_verify.raise_for_status.return_value = None

    fake_personas = MagicMock()
    fake_personas.json.return_value = {"response": {"players": [{"personaname": "corrosivefrost"}]}}
    fake_personas.raise_for_status.return_value = None

    with (
        patch("backend.integrations._httpx.post", return_value=fake_verify),
        patch("backend.integrations._httpx.get", return_value=fake_personas),
    ):
        r = client.get(
            "/integrations/steam/openid/return",
            params={
                "openid.claimed_id": "https://steamcommunity.com/openid/id/76561197960287930",
                "openid.mode": "id_res",
                "openid.sig": "ok",
            },
            follow_redirects=False,
        )
    assert r.status_code == 302
    user = db_session.query(models.User).first()
    db_session.refresh(user)
    assert user.steam_persona_name == "corrosivefrost"
    assert user.steam_id64 == "76561197960287930"


# ─── SteamGridDB ──────────────────────────────────────────────────────────


def test_sgdb_credentials_save_and_clear(client, db_session):
    _signup_and_login(client)
    r = client.post("/integrations/steamgriddb/credentials", data={"steamgriddb_api_key": "sgdb-key-123"})
    assert r.status_code == 200
    user = db_session.query(models.User).first()
    db_session.refresh(user)
    assert user.steamgriddb_api_key == "sgdb-key-123"

    # Clear
    client.post("/integrations/steamgriddb/credentials", data={"steamgriddb_api_key": ""})
    db_session.refresh(user)
    assert user.steamgriddb_api_key is None


def test_sgdb_search_requires_api_key(client, db_session):
    """No SGDB key set → returns an error message via the partial, not a 401."""
    from unittest.mock import patch

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    game = models.Game(title="G")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="42")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="manual")
    db_session.add(entry)
    db_session.commit()

    with patch("backend.steamgriddb.lookup_by_steam_appid") as m:
        r = client.get(f"/integrations/steamgriddb/search?entry_id={entry.id}&image_type=v")
        assert r.status_code == 200
        assert "SteamGridDB API key" in r.text
        m.assert_not_called()


def test_sgdb_search_uses_steam_appid_when_available(client, db_session):
    from unittest.mock import patch

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    user.steamgriddb_api_key = "sgdb-key"
    game = models.Game(title="Half-Life 2")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="220")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    with (
        patch("backend.steamgriddb.lookup_by_steam_appid", return_value={"id": 999}) as m_lookup,
        patch(
            "backend.steamgriddb.get_grids_for_game",
            return_value=[{"url": "https://cdn.sgdb/full.png", "thumb": "https://cdn.sgdb/t.png", "id": 1}],
        ) as m_grids,
    ):
        r = client.get(f"/integrations/steamgriddb/search?entry_id={entry.id}&image_type=v")
    assert r.status_code == 200
    m_lookup.assert_called_once_with("sgdb-key", "220")
    m_grids.assert_called_once_with("sgdb-key", 999, "v", page=0)
    assert "https://cdn.sgdb/t.png" in r.text


def test_sgdb_search_falls_back_to_title_for_non_steam(client, db_session):
    from unittest.mock import patch

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    user.steamgriddb_api_key = "sgdb-key"
    game = models.Game(title="Bloodborne")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="PS4", source="manual", external_id=None)
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="manual")
    db_session.add(entry)
    db_session.commit()

    with (
        patch("backend.steamgriddb.lookup_by_steam_appid") as m_lookup,
        patch("backend.steamgriddb.search_games", return_value=[{"id": 555, "name": "Bloodborne"}]) as m_search,
        patch("backend.steamgriddb.get_grids_for_game", return_value=[]) as m_grids,
    ):
        r = client.get(f"/integrations/steamgriddb/search?entry_id={entry.id}&image_type=h")
    assert r.status_code == 200
    m_lookup.assert_not_called()
    m_search.assert_called_once_with("sgdb-key", "Bloodborne")
    m_grids.assert_called_once_with("sgdb-key", 555, "h", page=0)


def test_set_cover_override_applies_to_correct_orientation(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    game = models.Game(title="G")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="1")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="manual")
    db_session.add(entry)
    db_session.commit()

    r = client.post(
        f"/library/entries/{entry.id}/cover-override",
        data={"image_type": "v", "url": "https://cdn.sgdb/cover-v.png"},
    )
    assert r.status_code == 200
    ua_v = db_session.query(models.UserArtwork).filter_by(entry_id=entry.id, artwork_type="cover_v").first()
    assert ua_v is not None
    assert ua_v.url == "https://cdn.sgdb/cover-v.png"
    ua_h = db_session.query(models.UserArtwork).filter_by(entry_id=entry.id, artwork_type="cover_h").first()
    assert ua_h is None

    r = client.post(
        f"/library/entries/{entry.id}/cover-override",
        data={"image_type": "h", "url": "https://cdn.sgdb/cover-h.png"},
    )
    assert r.status_code == 200
    ua_h = db_session.query(models.UserArtwork).filter_by(entry_id=entry.id, artwork_type="cover_h").first()
    assert ua_h is not None
    assert ua_h.url == "https://cdn.sgdb/cover-h.png"


def test_set_cover_override_rejects_bad_orientation(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    game = models.Game(title="G")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="1")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="manual")
    db_session.add(entry)
    db_session.commit()

    r = client.post(
        f"/library/entries/{entry.id}/cover-override",
        data={"image_type": "diagonal", "url": "https://x/y.png"},
    )
    assert r.status_code == 400


def test_sgdb_bulk_fill_skips_entries_with_existing_artwork(db_session):
    """An entry with a valid release-level GameArtwork cover_v row should be
    skipped — we don't want to stomp Steam CDN art that works."""
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="Half-Life 2")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="220")
    db_session.add(release)
    db_session.flush()
    db_session.add(models.GameArtwork(release_id=release.id, artwork_type="cover_v", source="steam", url="https://steam/cover.jpg"))
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    result = steamgriddb.bulk_fill_missing(db_session, user, "v")
    assert result == {"filled": 0, "no_candidate": 0, "skipped": 1, "errored": 0}
    ua = db_session.query(models.UserArtwork).filter_by(entry_id=entry.id).first()
    assert ua is None


def test_sgdb_bulk_fill_applies_top_candidate(db_session, monkeypatch):
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="Bloodborne")
    db_session.add(game)
    db_session.flush()
    # Manual entry, no artwork → eligible for fill
    release = models.GameRelease(game_id=game.id, platform="PS4", source="manual", external_id=None)
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="manual")
    db_session.add(entry)
    db_session.commit()

    monkeypatch.setattr(steamgriddb, "search_games", lambda k, q: [{"id": 555, "name": "Bloodborne"}])
    monkeypatch.setattr(
        steamgriddb,
        "get_grids_for_game",
        lambda k, gid, o, page=0: [{"url": "https://sgdb/top.png", "thumb": "https://sgdb/t.png"}],
    )

    result = steamgriddb.bulk_fill_missing(db_session, user, "v")
    assert result["filled"] == 1
    ua = db_session.query(models.UserArtwork).filter_by(entry_id=entry.id, artwork_type="cover_v").first()
    assert ua is not None
    assert ua.url == "https://sgdb/top.png"


def test_sgdb_bulk_fill_counts_no_candidate(db_session, monkeypatch):
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="ObscureUnknownGame")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="99999999")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    monkeypatch.setattr(steamgriddb, "lookup_by_steam_appid", lambda k, a: None)
    monkeypatch.setattr(steamgriddb, "search_games", lambda k, q: [])

    result = steamgriddb.bulk_fill_missing(db_session, user, "v")
    assert result["no_candidate"] == 1
    assert result["filled"] == 0


def test_sgdb_bulk_fill_one_error_doesnt_abort_run(db_session, monkeypatch):
    """If SGDB blows up on one entry, the rest of the library should still
    get processed."""
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    games = [models.Game(title=f"G{i}") for i in range(3)]
    for g in games:
        db_session.add(g)
    db_session.flush()
    entries = []
    for i, g in enumerate(games):
        r = models.GameRelease(game_id=g.id, platform="Steam", source="steam", external_id=str(100 + i))
        db_session.add(r)
        db_session.flush()
        e = models.UserLibraryEntry(user_id=user.id, release_id=r.id, import_source="steam_import")
        db_session.add(e)
        entries.append(e)
    db_session.commit()

    calls = {"n": 0}

    def lookup(api_key, appid):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("SGDB 500")
        return {"id": int(appid) * 10}

    monkeypatch.setattr(steamgriddb, "lookup_by_steam_appid", lookup)
    monkeypatch.setattr(steamgriddb, "get_grids_for_game", lambda k, gid, o, page=0: [{"url": f"https://sgdb/{gid}.png"}])

    result = steamgriddb.bulk_fill_missing(db_session, user, "v")
    assert result["filled"] == 2
    assert result["errored"] == 1


def test_sgdb_bulk_fill_skips_hidden_entries(db_session, monkeypatch):
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="HiddenGame")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="42")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import", is_hidden=True)
    db_session.add(entry)
    db_session.commit()

    called = {"yes": False}

    def lookup(api_key, appid):
        called["yes"] = True
        return {"id": 1}

    monkeypatch.setattr(steamgriddb, "lookup_by_steam_appid", lookup)

    result = steamgriddb.bulk_fill_missing(db_session, user, "v")
    assert called["yes"] is False
    assert result == {"filled": 0, "no_candidate": 0, "skipped": 0, "errored": 0}


def test_sgdb_fill_missing_endpoint_kicks_off_job(client, db_session, monkeypatch):
    """The endpoint should create a job and return a progress div — it
    shouldn't run the fill synchronously."""
    import asyncio

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    user.steamgriddb_api_key = "sgdb-key"
    db_session.commit()

    created_tasks = []

    def fake_create_task(coro):
        # Don't actually run the background coroutine in the test — we just
        # care that the endpoint queued one. Close it to suppress the
        # "coroutine was never awaited" warning.
        coro.close()
        created_tasks.append(True)
        return None

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    r = client.post("/integrations/steamgriddb/fill-missing", data={"image_type": "v"})
    assert r.status_code == 200
    assert "sgdb-fill-status" in r.text  # progress div rendered
    assert created_tasks == [True]


def test_sgdb_fill_missing_endpoint_requires_api_key(client, db_session):
    _signup_and_login(client)
    r = client.post("/integrations/steamgriddb/fill-missing", data={"image_type": "v"})
    assert r.status_code == 422
    assert "API key" in r.text


def test_fetch_owned_appids_raises_on_empty_result(db_session, monkeypatch):
    """Steam returns HTTP 200 with well-formed but empty rgOwnedApps when the
    session cookies are stale/expired, instead of an auth error — must be
    treated as a failure, not "0 apps owned", or DLC sync silently no-ops."""
    from backend import steam

    user = models.User(
        name="t",
        username="t",
        password_hash="x",
        api_token="tok",
        steam_session_id="sess",
        steam_login_secure="login",
    )
    fake_userdata = MagicMock()
    fake_userdata.json.return_value = {"rgOwnedApps": []}
    fake_userdata.raise_for_status.return_value = None
    monkeypatch.setattr(steam.httpx, "get", lambda *a, **k: fake_userdata)

    with pytest.raises(ValueError, match="expired"):
        steam._fetch_owned_appids(user)


def test_fill_import_candidate_thumbnails_applies_top_result(db_session, monkeypatch):
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    candidate = models.ImportCandidate(
        user_id=user.id,
        raw_title="Castlevania 3",
        raw_platform="NES",
        status="pending",
        proposed_action="create_new",
    )
    db_session.add(candidate)
    db_session.commit()

    monkeypatch.setattr(steamgriddb, "search_games", lambda k, q: [{"id": 555, "name": "Castlevania III"}])
    monkeypatch.setattr(steamgriddb, "get_grids_for_game", lambda k, gid, orientation, page=0: [{"url": "https://sgdb/top.png"}])

    result = steamgriddb.fill_import_candidate_thumbnails(db_session, user)
    assert result == {"filled": 1, "no_candidate": 0, "errored": 0}
    db_session.refresh(candidate)
    assert candidate.thumbnail_url == "https://sgdb/top.png"


def test_fill_import_candidate_thumbnails_skips_already_filled_and_confirmed(db_session, monkeypatch):
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    already_filled = models.ImportCandidate(
        user_id=user.id,
        raw_title="Already filled",
        raw_platform="NES",
        status="pending",
        proposed_action="create_new",
        thumbnail_url="https://sgdb/existing.png",
    )
    confirmed = models.ImportCandidate(
        user_id=user.id,
        raw_title="Confirmed entry",
        raw_platform="NES",
        status="confirmed",
        proposed_action="create_new",
    )
    matched = models.ImportCandidate(
        user_id=user.id,
        raw_title="Already matched",
        raw_platform="NES",
        status="pending",
        proposed_action="add_to_existing",
    )
    db_session.add_all([already_filled, confirmed, matched])
    db_session.commit()

    calls = []
    monkeypatch.setattr(steamgriddb, "search_games", lambda k, q: calls.append(q) or [])

    result = steamgriddb.fill_import_candidate_thumbnails(db_session, user)
    assert result == {"filled": 0, "no_candidate": 0, "errored": 0}
    assert calls == []


def test_fill_import_candidate_thumbnails_counts_no_candidate(db_session, monkeypatch):
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    candidate = models.ImportCandidate(
        user_id=user.id,
        raw_title="Totally Obscure Game",
        raw_platform="NES",
        status="pending",
        proposed_action="needs_review",
    )
    db_session.add(candidate)
    db_session.commit()

    monkeypatch.setattr(steamgriddb, "search_games", lambda k, q: [])

    result = steamgriddb.fill_import_candidate_thumbnails(db_session, user)
    assert result == {"filled": 0, "no_candidate": 1, "errored": 0}
    db_session.refresh(candidate)
    assert candidate.thumbnail_url is None


def test_sgdb_bulk_fill_all_resolves_game_once_and_fills_every_type(db_session, monkeypatch):
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="Bloodborne")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="PS4", source="manual", external_id=None)
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="manual")
    db_session.add(entry)
    db_session.commit()

    calls = {"search": 0}

    def fake_search(k, q):
        calls["search"] += 1
        return [{"id": 555, "name": "Bloodborne"}]

    monkeypatch.setattr(steamgriddb, "search_games", fake_search)
    monkeypatch.setattr(steamgriddb, "get_grids_for_game", lambda k, gid, o, page=0: [{"url": f"https://sgdb/{o}.png"}])
    monkeypatch.setattr(steamgriddb, "get_heroes_for_game", lambda k, gid, page=0: [{"url": "https://sgdb/hero.png"}])
    monkeypatch.setattr(steamgriddb, "get_logos_for_game", lambda k, gid, page=0: [{"url": "https://sgdb/logo.png"}])

    result = steamgriddb.bulk_fill_all_missing(db_session, user)
    assert result["filled"] == 4
    # The SGDB game is resolved ONCE per entry, not once per type.
    assert calls["search"] == 1
    for at in ("cover_v", "cover_h", "hero", "logo"):
        assert db_session.query(models.UserArtwork).filter_by(entry_id=entry.id, artwork_type=at).count() == 1
    assert all(result["per_type"][t]["filled"] == 1 for t in steamgriddb.IMAGE_TYPES)


def test_search_games_encodes_colon_titles(monkeypatch):
    """Titles whose pre-colon segment parses as a URL scheme (no space) used to
    be mangled: 'BioShock: The Collection' searched for ' The Collection'. The
    whole title must be percent-encoded into one path segment instead."""
    from backend import steamgriddb

    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(steamgriddb.httpx, "get", fake_get)
    steamgriddb.search_games("key", "BioShock: The Collection")
    # The full title survives, encoded — not truncated to " The Collection".
    assert captured["url"].endswith("/search/autocomplete/BioShock%3A%20The%20Collection")


def test_bulk_fill_all_missing_scopes_to_sources(db_session, monkeypatch):
    """sources={'psn'} must fill only PSN-source entries and leave a Steam
    entry (also missing art) untouched — the post-import auto-fill scope."""
    from backend import steamgriddb

    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.flush()

    psn_game = models.Game(title="Bloodborne")
    steam_game = models.Game(title="Half-Life 2")
    db_session.add_all([psn_game, steam_game])
    db_session.flush()
    psn_rel = models.GameRelease(game_id=psn_game.id, platform="PS4", source="psn", external_id="CUSA00900")
    steam_rel = models.GameRelease(game_id=steam_game.id, platform="Steam", source="steam", external_id="220")
    db_session.add_all([psn_rel, steam_rel])
    db_session.flush()
    psn_entry = models.UserLibraryEntry(user_id=user.id, release_id=psn_rel.id, import_source="psn_import")
    steam_entry = models.UserLibraryEntry(user_id=user.id, release_id=steam_rel.id, import_source="steam_import")
    db_session.add_all([psn_entry, steam_entry])
    db_session.commit()

    searched = []
    monkeypatch.setattr(steamgriddb, "search_games", lambda k, q: searched.append(q) or [{"id": 1, "name": q}])
    monkeypatch.setattr(steamgriddb, "get_grids_for_game", lambda k, gid, o, page=0: [{"url": f"https://sgdb/{o}.png"}])
    monkeypatch.setattr(steamgriddb, "get_heroes_for_game", lambda k, gid, page=0: [{"url": "https://sgdb/hero.png"}])
    monkeypatch.setattr(steamgriddb, "get_logos_for_game", lambda k, gid, page=0: [{"url": "https://sgdb/logo.png"}])

    result = steamgriddb.bulk_fill_all_missing(db_session, user, sources={"psn"})

    # Only the PSN entry was looked up and filled.
    assert searched == ["Bloodborne"]
    assert result["filled"] == 4
    assert db_session.query(models.UserArtwork).filter_by(entry_id=psn_entry.id).count() == 4
    assert db_session.query(models.UserArtwork).filter_by(entry_id=steam_entry.id).count() == 0


def test_sgdb_fill_all_endpoint_requires_key(client, db_session, monkeypatch):
    # The endpoint create_task()s the runner, which opens its own real
    # SessionLocal — stub it so the test doesn't fire a real fill.
    from backend import integrations, jobs

    jobs.clear_all()  # module-level registry persists across tests (same user id=1)

    async def _noop(job_id, user_id):
        return None

    monkeypatch.setattr(integrations, "_run_sgdb_fill_all_job", _noop)

    token = _signup_and_login(client)
    r = client.post("/integrations/steamgriddb/fill-all")
    assert r.status_code == 422
    assert b"API key" in r.content

    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.steamgriddb_api_key = "k"
    db_session.commit()
    r = client.post("/integrations/steamgriddb/fill-all")
    assert r.status_code == 200
