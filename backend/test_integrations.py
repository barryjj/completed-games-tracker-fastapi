from unittest.mock import MagicMock, patch

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
    r = client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "TESTAPIKEY123",
            "steam_id64": "76561197960287930",
        },
    )
    assert r.status_code == 200
    assert b"saved" in r.content.lower()

    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.refresh(user)
    assert user.steam_api_key == "TESTAPIKEY123"
    assert user.steam_id64 == "76561197960287930"


def test_save_steam_credentials_clears_on_empty(client, db_session):
    token = _signup_and_login(client)
    client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "KEY",
            "steam_id64": "123",
        },
    )
    client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "",
            "steam_id64": "",
        },
    )
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
    client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "FAKEKEY",
            "steam_id64": "76561197960287930",
        },
    )

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
    client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "FAKEKEY",
            "steam_id64": "76561197960287930",
        },
    )
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

    _signup_and_login(client)
    client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "K",
            "steam_id64": "1",
        },
    )
    user = db_session.query(models.User).first()
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


def test_enrichment_status_returns_counts(client, db_session):
    _signup_and_login(client)
    client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "K",
            "steam_id64": "1",
        },
    )
    r = client.get("/integrations/steam/enrichment-status")
    assert r.status_code == 200
    # Empty library — both numbers are zero
    assert b"0" in r.content


def test_enrichment_refresh_nulls_timestamps(client, db_session):
    """The bug we just fixed: this endpoint used to 500 on a join+update."""
    from backend import steam

    _signup_and_login(client)
    client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "FAKEKEY",
            "steam_id64": "76561197960287930",
        },
    )
    user = db_session.query(models.User).first()
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

    _signup_and_login(client)
    client.post(
        "/integrations/steam/credentials",
        data={
            "steam_api_key": "FAKEKEY",
            "steam_id64": "76561197960287930",
        },
    )
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


# ─── Heuristic / user-override tests ─────────────────────────────────────────


def test_clean_title_strips_trademark_symbols():
    from backend.steam import _clean_title

    assert _clean_title("ELDEN RING™") == "Elden Ring"
    assert _clean_title("Halo®: Combat Evolved") == "Halo: Combat Evolved"


def test_clean_title_all_caps_normalizes():
    from backend.steam import _clean_title

    assert _clean_title("ELDEN RING") == "Elden Ring"
    assert _clean_title("RESIDENT EVIL 4") == "Resident Evil 4"
    assert _clean_title("DEAD CELLS") == "Dead Cells"


def test_clean_title_preserves_roman_numerals_and_acronyms():
    from backend.steam import _clean_title

    assert _clean_title("GRAND THEFT AUTO V") == "Grand Theft Auto V"
    assert _clean_title("DARK SOULS III") == "Dark Souls III"
    assert _clean_title("FINAL FANTASY VII REMAKE") == "Final Fantasy VII Remake"
    assert _clean_title("FTL: FASTER THAN LIGHT") == "FTL: Faster Than Light"
    assert _clean_title("CALL OF DUTY: BLACK OPS VIII") == "Call Of Duty: Black Ops VIII"


def test_clean_title_handles_apostrophes():
    from backend.steam import _clean_title

    assert _clean_title("ASSASSIN'S CREED II") == "Assassin's Creed II"


def test_clean_title_leaves_short_or_single_word_alone():
    from backend.steam import _clean_title

    assert _clean_title("DOOM") == "DOOM"
    assert _clean_title("FTL") == "FTL"
    assert _clean_title("GTA V") == "GTA V"  # too short for the heuristic to trigger


def test_clean_title_is_idempotent():
    from backend.steam import _clean_title

    # Running on already-cleaned title should be a no-op.
    assert _clean_title("Elden Ring") == "Elden Ring"
    assert _clean_title("Dark Souls III") == "Dark Souls III"


def test_should_auto_hide_only_fires_for_dlc():
    """Auto-hide is gated on is_dlc=True. A game can never be auto-hidden by
    the heuristic — even if its title accidentally contains a pattern word."""
    from backend.steam import _should_auto_hide

    # is_dlc=True + matching title → hide
    assert _should_auto_hide("Elden Ring - Soundtrack", None, is_dlc=True) is True
    assert _should_auto_hide("Game OST", None, is_dlc=True) is True
    assert _should_auto_hide("Game Artbook", None, is_dlc=True) is True
    assert _should_auto_hide("Cosmetic Pack", None, is_dlc=True) is True
    assert _should_auto_hide("Wallpaper Set", None, is_dlc=True) is True
    # New DLC patterns from the user's screenshots
    assert _should_auto_hide("TEKKEN 8 - Season 1 Character Pass", None, is_dlc=True) is True
    assert _should_auto_hide("TEKKEN 8 - Season 2 Character & Stage Pass", None, is_dlc=True) is True
    assert _should_auto_hide("Street Fighter 6 - Year 1 Ultimate Pass", None, is_dlc=True) is True
    assert _should_auto_hide("Mortal Kombat 11 Klassic Skin Pack", None, is_dlc=True) is True
    assert _should_auto_hide("Mortal Kombat 11 Cinematic Pack", None, is_dlc=True) is True
    assert _should_auto_hide("MK11 Ultimate Add-On Bundle", None, is_dlc=True) is True
    assert _should_auto_hide("Blaster Master Zero 2 - DLC Playable Character: Copen", None, is_dlc=True) is True
    assert _should_auto_hide("TEKKEN 8 - Avatar Skin: Tetsujin", None, is_dlc=True) is True
    assert _should_auto_hide("Game - Digital Deluxe Edition Upgrade", None, is_dlc=True) is True
    # appdetails type=music → hide regardless of title (still requires is_dlc)
    assert _should_auto_hide("Anything", {"type": "music"}, is_dlc=True) is True

    # is_dlc=False → NEVER hide, even if title matches a pattern
    assert _should_auto_hide("Elden Ring - Soundtrack", None, is_dlc=False) is False
    assert _should_auto_hide("Cosmetic Pack", None, is_dlc=False) is False
    # type=music without is_dlc still doesn't hide — sync's rgOwnedApps
    # subtraction lands actual music products in is_dlc=True anyway.
    assert _should_auto_hide("Some Soundtrack", {"type": "music"}, is_dlc=False) is False

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
