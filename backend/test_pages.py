import datetime
import pytest
from backend import models


def _signup_and_login(client, username="testuser", password="testpass"):
    """Helper: create account and return an authenticated client session."""
    client.post("/signup", data={"username": username, "password": password, "password_confirm": password})
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    token = r.cookies["session"]
    client.cookies.set("session", token)
    return token


def _add_game(db, user, title="Elden Ring", platform="Steam"):
    game = models.Game(title=title)
    db.add(game)
    db.flush()
    release = models.GameRelease(game_id=game.id, platform=platform, source="manual")
    db.add(release)
    db.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="manual")
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


# --- login / logout ---

def test_login_page_loads(client):
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 200
    assert b"Sign In" in r.content


def test_login_valid_credentials(client):
    client.post("/signup", data={"username": "u1", "password": "pw", "password_confirm": "pw"})
    r = client.post("/login", data={"username": "u1", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/library"
    assert "session" in r.cookies


def test_login_invalid_credentials(client):
    r = client.post("/login", data={"username": "nobody", "password": "bad"}, follow_redirects=False)
    assert r.status_code == 401
    assert b"Invalid" in r.content


def test_logout_clears_cookie(client):
    _signup_and_login(client)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# --- auth redirects ---

def test_library_requires_auth(client):
    r = client.get("/library", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_completions_requires_auth(client):
    r = client.get("/completions", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_root_redirects_to_library(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/library"


# --- library page ---

def test_library_page_loads(client):
    _signup_and_login(client)
    r = client.get("/library")
    assert r.status_code == 200
    assert b"Library" in r.content


def test_add_game_to_library(client):
    _signup_and_login(client)
    r = client.post("/library/games", data={"title": "Elden Ring", "platform": "Steam"})
    assert r.status_code == 200
    assert b"Elden Ring" in r.content
    assert b"Steam" in r.content


def test_add_game_appears_in_library(client):
    _signup_and_login(client)
    client.post("/library/games", data={"title": "Hollow Knight", "platform": "Switch"})
    r = client.get("/library")
    assert b"Hollow Knight" in r.content
    assert b"Switch" in r.content


def test_add_dlc_type(client):
    _signup_and_login(client)
    r = client.post("/library/games", data={"title": "Shadow of the Erdtree", "platform": "Steam", "is_dlc": "true"})
    assert r.status_code == 200
    assert b"DLC" in r.content


def test_collection_auto_detected_by_title(client):
    _signup_and_login(client)
    r = client.post("/library/games", data={"title": "Castlevania Anniversary Collection", "platform": "Steam"})
    assert r.status_code == 200
    assert b"Collection" in r.content


# --- completions page ---

def test_completions_page_loads(client):
    _signup_and_login(client)
    r = client.get("/completions")
    assert r.status_code == 200
    assert b"Completions" in r.content


def test_log_completion(client, db_session):
    token = _signup_and_login(client)
    user = models.User.__new__(models.User)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)

    r = client.post("/completions/log", data={
        "library_entry_id": entry.id,
        "completed_at": "2026-01-15",
        "playthroughs": "1",
        "notes": "Platinum",
    })
    assert r.status_code == 200
    assert b"Elden Ring" in r.content
    assert b"Platinum" in r.content


def test_log_completion_appears_in_list(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Astro Bot", platform="PS5")

    client.post("/completions/log", data={
        "library_entry_id": entry.id,
        "completed_at": "2026-01-04",
        "playthroughs": "1",
        "notes": "Platinum + DLC",
    })
    r = client.get("/completions")
    assert b"Astro Bot" in r.content
    assert b"PS5" in r.content
    assert b"Platinum + DLC" in r.content
