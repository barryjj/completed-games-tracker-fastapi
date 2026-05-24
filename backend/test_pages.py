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


def test_add_collection_type(client):
    _signup_and_login(client)
    r = client.post("/library/games", data={"title": "Castlevania Anniversary Collection", "platform": "Steam", "is_collection": "true"})
    assert r.status_code == 200
    assert b'tag-collection">Collection' in r.content


def test_collection_not_auto_detected_server_side(client):
    _signup_and_login(client)
    r = client.post("/library/games", data={"title": "Castlevania Anniversary Collection", "platform": "Steam"})
    assert r.status_code == 200
    # is_collection only set when checkbox is explicitly submitted
    assert b'tag-collection">Collection' not in r.content


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


# --- completion game search ---

def test_completion_search_returns_match(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    _add_game(db_session, user, title="Elden Ring", platform="Steam")
    _add_game(db_session, user, title="Hollow Knight", platform="Switch")

    r = client.get("/completions/games/search?q=elden")
    assert r.status_code == 200
    assert b"Elden Ring" in r.content
    assert b"Hollow Knight" not in r.content


def test_completion_search_empty_query_returns_empty(client):
    _signup_and_login(client)
    r = client.get("/completions/games/search?q=")
    assert r.status_code == 200
    assert b"list-group-item" not in r.content


def test_completion_search_no_match(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    _add_game(db_session, user, title="Elden Ring", platform="Steam")

    r = client.get("/completions/games/search?q=xyzzy")
    assert r.status_code == 200
    assert b"Elden Ring" not in r.content


def test_completion_search_requires_auth(client):
    r = client.get("/completions/games/search?q=test", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


# --- completion edit / delete ---

def _log_completion(client, entry_id, date="2026-01-15"):
    return client.post("/completions/log", data={
        "library_entry_id": entry_id,
        "completed_at": date,
        "playthroughs": "1",
        "notes": "test note",
    })


def test_delete_completion(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    r = _log_completion(client, entry.id)
    completion_id = db_session.query(models.Completion).filter_by(user_id=user.id).first().id

    r = client.delete(f"/completions/{completion_id}")
    assert r.status_code == 200
    assert db_session.query(models.Completion).filter_by(id=completion_id).first() is None


def test_delete_completion_other_user(client, db_session):
    token = _signup_and_login(client, username="u1")
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    _log_completion(client, entry.id)
    completion_id = db_session.query(models.Completion).filter_by(user_id=user.id).first().id

    _signup_and_login(client, username="u2")
    r = client.delete(f"/completions/{completion_id}")
    assert r.status_code == 200
    assert db_session.query(models.Completion).filter_by(id=completion_id).first() is not None


def test_manual_add_marks_all_fields_user_set(client, db_session):
    """Manually added games shouldn't be touched by any heuristic — every
    user-set flag flips to True on create."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    r = client.post("/library/games", data={
        "title": "Custom Title", "platform": "Switch",
        "is_dlc": "false", "is_collection": "false",
    })
    assert r.status_code == 200
    game = db_session.query(models.Game).filter_by(title="Custom Title").first()
    assert game is not None
    assert game.display_name == "Custom Title"
    assert game.display_name_user_set is True
    assert game.is_dlc_user_set is True
    assert game.is_collection_user_set is True
    assert game.parent_id_user_set is True


def test_manual_add_separate_display_name(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    client.post("/library/games", data={
        "title": "Resident Evil Village",
        "display_name": "Resident Evil 8",
        "platform": "Steam",
    })
    game = db_session.query(models.Game).filter_by(title="Resident Evil Village").first()
    assert game.display_name == "Resident Evil 8"
    assert game.display_name_user_set is True


def test_edit_entry_sets_user_overrides(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    # Seed a Steam entry where user_set flags are all False (simulating an
    # auto-imported game we haven't edited yet)
    game = models.Game(title="ELDEN RING")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="100")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)

    r = client.patch(f"/library/entries/{entry.id}", data={
        "title": "Elden Ring",
        "display_name": "ER",
        "is_dlc": "false",
        "is_collection": "false",
    })
    assert r.status_code == 200
    db_session.refresh(game)
    assert game.title == "Elden Ring"
    assert game.display_name == "ER"
    assert game.display_name_user_set is True
    assert game.is_dlc_user_set is True
    assert game.is_collection_user_set is True
    assert game.parent_id_user_set is True


def test_library_hides_is_hidden_entries_by_default(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    visible = _add_game(db_session, user, title="Visible Game")
    hidden = _add_game(db_session, user, title="Hidden Soundtrack")
    hidden.is_hidden = True
    db_session.commit()

    r = client.get("/library")
    assert b"Visible Game" in r.content
    assert b"Hidden Soundtrack" not in r.content

    # show_hidden=true reveals hidden entries
    r2 = client.get("/library?show_hidden=true")
    assert b"Visible Game" in r2.content
    assert b"Hidden Soundtrack" in r2.content


def test_hide_endpoint_sets_user_flag(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="X")
    r = client.post(f"/library/entries/{entry.id}/hide")
    assert r.status_code == 200
    db_session.refresh(entry)
    assert entry.is_hidden is True
    assert entry.is_hidden_user_set is True


def test_unhide_endpoint_locks_against_heuristic(client, db_session):
    """Unhiding sets is_hidden_user_set=True so the auto-hide heuristic
    won't re-hide it on the next enrichment pass."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Game OST")
    entry.is_hidden = True  # pretend the heuristic auto-hid it
    db_session.commit()

    r = client.post(f"/library/entries/{entry.id}/unhide")
    assert r.status_code == 200
    db_session.refresh(entry)
    assert entry.is_hidden is False
    assert entry.is_hidden_user_set is True


def test_update_completion(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    _log_completion(client, entry.id)
    completion = db_session.query(models.Completion).filter_by(user_id=user.id).first()

    r = client.post("/completions/log", data={
        "completion_id": completion.id,
        "library_entry_id": entry.id,
        "completed_at": "2026-06-01",
        "playthroughs": "2",
        "notes": "updated note",
    })
    assert r.status_code == 200
    assert b"updated note" in r.content
    assert r.headers.get("hx-retarget") == f"#completion-{completion.id}"
    db_session.refresh(completion)
    assert completion.notes == "updated note"
    assert str(completion.completed_at) == "2026-06-01"


# ─── Library detail pane ─────────────────────────────────────────────────────

def test_detail_pane_returns_content_for_owned_entry(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Elden Ring", platform="Steam")

    r = client.get(f"/library/entries/{entry.id}/detail")
    assert r.status_code == 200
    assert b"Elden Ring" in r.content
    assert b"offcanvas-header" in r.content
    assert b"offcanvas-body" in r.content


def test_detail_pane_404_for_other_users_entry(client, db_session):
    _signup_and_login(client, username="alice")
    alice = db_session.query(models.User).filter_by(username="alice").first()
    entry = _add_game(db_session, alice, title="Alice's Game")

    _signup_and_login(client, username="bob")
    r = client.get(f"/library/entries/{entry.id}/detail")
    assert r.status_code == 404


def test_detail_pane_shows_child_dlc_for_parent_game(client, db_session):
    """A base game's detail pane lists its DLC children with HTMX links."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    parent = _add_game(db_session, user, title="Elden Ring", platform="Steam")
    # Manually create a DLC linked to the parent
    dlc_game = models.Game(title="Shadow of the Erdtree", is_dlc=True, parent_id=parent.release.game.id)
    db_session.add(dlc_game)
    db_session.flush()
    dlc_release = models.GameRelease(game_id=dlc_game.id, platform="Steam", source="manual")
    db_session.add(dlc_release)
    db_session.flush()
    dlc_entry = models.UserLibraryEntry(user_id=user.id, release_id=dlc_release.id, import_source="manual")
    db_session.add(dlc_entry)
    db_session.commit()

    r = client.get(f"/library/entries/{parent.id}/detail")
    assert r.status_code == 200
    assert b"Shadow of the Erdtree" in r.content
    # The child link should hit the same detail endpoint
    assert f"/library/entries/{dlc_entry.id}/detail".encode() in r.content


def test_detail_pane_shows_completion_history(client, db_session):
    import datetime as _dt
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Hades")

    comp = models.Completion(
        user_id=user.id, library_entry_id=entry.id,
        completed_at=_dt.date(2026, 1, 15),
        notes="Beat the final boss on the 12th run",
    )
    db_session.add(comp)
    db_session.commit()

    r = client.get(f"/library/entries/{entry.id}/detail")
    assert r.status_code == 200
    assert b"Completions" in r.content
    assert b"12th run" in r.content


def test_detail_pane_shows_description_from_appdetails(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Hades")
    entry.release.raw_data = {"appdetails": {"short_description": "A roguelike from Supergiant Games."}}
    db_session.commit()

    r = client.get(f"/library/entries/{entry.id}/detail")
    assert b"A roguelike from Supergiant Games." in r.content
