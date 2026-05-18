import pytest
from unittest.mock import patch
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
