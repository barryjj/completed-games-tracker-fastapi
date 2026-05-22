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


def test_sync_success(client, db_session):
    token = _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "FAKEKEY",
        "steam_id64": "76561197960287930",
    })

    fake_games = [
        {"appid": 1245620, "name": "Elden Ring", "playtime_forever": 300, "rtime_last_played": 0},
        {"appid": 570, "name": "Dota 2", "playtime_forever": 0, "rtime_last_played": 0},
    ]

    with patch("backend.steam.get_owned_games", return_value=fake_games):
        r = client.post("/integrations/steam/sync")

    assert r.status_code == 200
    assert b"2 added" in r.content or b"added" in r.content

    entries = db_session.query(models.UserLibraryEntry).all()
    assert len(entries) == 2
    titles = {e.release.game.title for e in entries}
    assert titles == {"Elden Ring", "Dota 2"}


def test_sync_all_imports_games_and_dlc(client, db_session):
    """Full sync mocks all three Steam endpoints — runs in ms, no real network."""
    _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "FAKEKEY",
        "steam_id64": "76561197960287930",
        "steam_session_id": "fake-session",
        "steam_login_secure": "fake-login",
    })

    fake_games = [
        {"appid": 100, "name": "Game One", "playtime_forever": 0, "rtime_last_played": 0},
        {"appid": 200, "name": "Game Two", "playtime_forever": 60, "rtime_last_played": 0},
    ]
    # rgOwnedApps includes the 2 games above plus 2 DLC IDs not in fake_games
    fake_userdata = MagicMock()
    fake_userdata.json.return_value = {"rgOwnedApps": [100, 200, 300, 400]}
    fake_userdata.raise_for_status.return_value = None

    fake_app_names = {100: "Game One", 200: "Game Two", 300: "Game One - DLC", 400: "Game Two - DLC"}

    with patch("backend.steam.get_owned_games", return_value=fake_games), \
         patch("backend.steam.httpx.get", return_value=fake_userdata), \
         patch("backend.steam.get_app_list", return_value=fake_app_names):
        r = client.post("/integrations/steam/sync-all")

    assert r.status_code == 200
    # 2 games + 2 DLC = 4 library entries
    assert db_session.query(models.UserLibraryEntry).count() == 4
    dlc_count = db_session.query(models.Game).filter_by(is_dlc=True).count()
    assert dlc_count == 2


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
    _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "FAKEKEY", "steam_id64": "76561197960287930",
    })
    fake_games = [{"appid": 100, "name": "G", "playtime_forever": 0, "rtime_last_played": 0}]
    with patch("backend.steam.get_owned_games", return_value=fake_games):
        client.post("/integrations/steam/sync")

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


def test_sync_updates_playtime_on_resync(client, db_session):
    token = _signup_and_login(client)
    client.post("/integrations/steam/credentials", data={
        "steam_api_key": "FAKEKEY",
        "steam_id64": "76561197960287930",
    })

    game_v1 = [{"appid": 1245620, "name": "Elden Ring", "playtime_forever": 100, "rtime_last_played": 0}]
    game_v2 = [{"appid": 1245620, "name": "Elden Ring", "playtime_forever": 250, "rtime_last_played": 0}]

    with patch("backend.steam.get_owned_games", return_value=game_v1):
        client.post("/integrations/steam/sync")
    with patch("backend.steam.get_owned_games", return_value=game_v2):
        client.post("/integrations/steam/sync")

    entries = db_session.query(models.UserLibraryEntry).all()
    assert len(entries) == 1
    db_session.refresh(entries[0])
    assert entries[0].playtime_minutes == 250
