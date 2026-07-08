import datetime

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
    assert r.headers["location"] == "/"
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


def test_root_requires_auth(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_home_page_loads(client):
    _signup_and_login(client)
    r = client.get("/")
    assert r.status_code == 200
    assert b"This year" in r.content
    assert b"Recently completed" in r.content
    assert b"Needs attention" in r.content


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


def test_completions_grid_view_renders(client):
    _signup_and_login(client)
    r = client.get("/completions?view_mode=grid_v")
    assert r.status_code == 200
    # Grid class on the container — list view uses a <table> instead.
    assert b"cgt-library-grid--grid_v" in r.content


def test_completions_invalid_view_mode_falls_back_to_list(client):
    _signup_and_login(client)
    r = client.get("/completions?view_mode=diagonal")
    assert r.status_code == 200
    # Falls back to list — no grid container.
    assert b"cgt-library-grid--grid" not in r.content


def test_completions_year_default_hides_past_year_completion(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Old Game From The Past")
    db_session.add(models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=datetime.date(2020, 5, 1)))
    db_session.commit()

    r = client.get("/completions")
    assert b"Old Game From The Past" not in r.content


def test_completions_all_time_shows_past_year_completion(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Old Game From The Past")
    db_session.add(models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=datetime.date(2020, 5, 1)))
    db_session.commit()

    r = client.get("/completions?all_time=true")
    assert b"Old Game From The Past" in r.content


def test_log_completion(client, db_session):
    token = _signup_and_login(client)
    user = models.User.__new__(models.User)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)

    r = client.post(
        "/completions/log",
        data={
            "library_entry_id": entry.id,
            "completed_at": "2026-01-15",
            "playthroughs": "1",
            "notes": "Platinum",
        },
    )
    assert r.status_code == 200
    assert b"Elden Ring" in r.content
    assert b"Platinum" in r.content


def test_log_completion_appears_in_list(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Astro Bot", platform="PS5")

    client.post(
        "/completions/log",
        data={
            "library_entry_id": entry.id,
            "completed_at": "2026-01-04",
            "playthroughs": "1",
            "notes": "Platinum + DLC",
        },
    )
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
    return client.post(
        "/completions/log",
        data={
            "library_entry_id": entry_id,
            "completed_at": date,
            "playthroughs": "1",
            "notes": "test note",
        },
    )


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
    r = client.post(
        "/library/games",
        data={
            "title": "Custom Title",
            "platform": "Switch",
            "is_dlc": "false",
            "is_collection": "false",
        },
    )
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
    client.post(
        "/library/games",
        data={
            "title": "Resident Evil Village",
            "display_name": "Resident Evil 8",
            "platform": "Steam",
        },
    )
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

    r = client.patch(
        f"/library/entries/{entry.id}",
        data={
            # Title submission is ignored for imported (non-manual) entries.
            # See test_edit_title_ignored_for_imported_entry for that behavior.
            "title": "ELDEN RING",
            "display_name": "ER",
            "is_dlc": "false",
            "is_collection": "false",
        },
    )
    assert r.status_code == 200
    db_session.refresh(game)
    # Title untouched (imported), display_name updated.
    assert game.title == "ELDEN RING"
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

    r = client.post(
        "/completions/log",
        data={
            "completion_id": completion.id,
            "library_entry_id": entry.id,
            "completed_at": "2026-06-01",
            "playthroughs": "2",
            "notes": "updated note",
        },
    )
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
    assert b"offcanvas-body" in r.content
    assert b"cgt-pane-nav" in r.content


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
        user_id=user.id,
        library_entry_id=entry.id,
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


def test_extract_steam_meta_happy_path():
    """_extract_steam_meta returns clean display fields from a full payload."""
    from backend.pages import _extract_steam_meta

    appdetails = {
        "genres": [{"id": "1", "description": "Action"}, {"id": "25", "description": "Adventure"}],
        "categories": [
            {"id": 2, "description": "Single-player"},
            {"id": 22, "description": "Steam Achievements"},  # should be filtered out
            {"id": 29, "description": "Steam Trading Cards"},  # should be filtered out
            {"id": 28, "description": "Full controller support"},
        ],
        "developers": ["Hello Games"],
        "publishers": ["Hello Games"],
        "release_date": {"coming_soon": False, "date": "Aug 12, 2016"},
        "metacritic": {"score": 71, "url": "https://www.metacritic.com/game/no-mans-sky/"},
        "website": "https://www.no-mans-sky.com",
    }
    meta = _extract_steam_meta(appdetails)

    assert meta["genres"] == ["Action", "Adventure"]
    assert meta["features"] == ["Single-player", "Full controller support"]
    assert meta["developers"] == ["Hello Games"]
    assert meta["publishers"] == ["Hello Games"]
    assert meta["released"] == "August 12, 2016"  # normalized to full month name
    assert meta["metacritic_score"] == 71
    assert meta["metacritic_url"] == "https://www.metacritic.com/game/no-mans-sky/"
    assert meta["website"] == "https://www.no-mans-sky.com"


def test_normalize_steam_date():
    """_normalize_steam_date handles all known Steam date format variants."""
    from backend.pages import _normalize_steam_date

    # Standard US format — normalized to full month name
    assert _normalize_steam_date("Aug 12, 2016") == "August 12, 2016"
    # Day-first with comma (UK/EU locale)
    assert _normalize_steam_date("28 May, 2026") == "May 28, 2026"
    # Day-first no comma
    assert _normalize_steam_date("28 May 2026") == "May 28, 2026"
    # Full month name day-first
    assert _normalize_steam_date("28 August, 2016") == "August 28, 2016"
    # Month + year only
    assert _normalize_steam_date("Aug 2016") == "August 2016"
    # Unparseable — pass through as-is
    assert _normalize_steam_date("Q2 2024") == "Q2 2024"
    assert _normalize_steam_date("Coming soon") == "Coming soon"
    assert _normalize_steam_date("") == ""


def test_extract_steam_meta_publisher_shown_when_different():
    from backend.pages import _extract_steam_meta

    meta = _extract_steam_meta(
        {
            "developers": ["id Software"],
            "publishers": ["Bethesda Softworks"],
        }
    )
    assert meta["developers"] == ["id Software"]
    assert meta["publishers"] == ["Bethesda Softworks"]


def test_extract_steam_meta_empty_payload():
    from backend.pages import _extract_steam_meta

    meta = _extract_steam_meta({})
    assert meta["genres"] == []
    assert meta["features"] == []
    assert meta["developers"] == []
    assert meta["publishers"] == []
    assert meta["released"] == ""
    assert meta["metacritic_score"] is None
    assert meta["website"] is None


def test_detail_pane_shows_steam_meta_fields(client, db_session):
    """Detail pane renders genre, developer, release date, and metacritic from appdetails."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="DOOM Eternal")
    entry.release.raw_data = {
        "appdetails": {
            "genres": [{"id": "1", "description": "Action"}],
            "categories": [{"id": 2, "description": "Single-player"}],
            "developers": ["id Software"],
            "publishers": ["Bethesda Softworks"],
            "release_date": {"coming_soon": False, "date": "Mar 20, 2020"},
            "metacritic": {"score": 88, "url": "https://www.metacritic.com/game/doom-eternal/"},
            "website": "https://bethesda.net/en/game/doom-eternal",
        }
    }
    db_session.commit()

    r = client.get(f"/library/entries/{entry.id}/detail")
    assert b"Action" in r.content
    assert b"id Software" in r.content
    assert b"Bethesda Softworks" in r.content
    assert b"March 20, 2020" in r.content
    assert b"88" in r.content
    assert b"Great" in r.content
    assert b"Single-player" in r.content


# ─── Completion detail pane ─────────────────────────────────────────────────


def test_completion_detail_returns_content(client, db_session):
    import datetime as _dt

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Hades")
    completion = models.Completion(
        user_id=user.id,
        library_entry_id=entry.id,
        completed_at=_dt.date(2026, 3, 14),
        playthroughs="3+",
        notes="Cleared on the third escape",
    )
    db_session.add(completion)
    db_session.commit()

    r = client.get(f"/completions/{completion.id}/detail")
    assert r.status_code == 200
    assert b"Hades" in r.content
    assert b"Cleared on the third escape" in r.content
    assert b"3+" in r.content
    # Cross-link back to the library entry pane
    assert f"/library?detail={entry.id}".encode() in r.content


def test_completion_detail_404_for_other_users(client, db_session):
    import datetime as _dt

    _signup_and_login(client, username="alice")
    alice = db_session.query(models.User).filter_by(username="alice").first()
    entry = _add_game(db_session, alice, title="Alice Game")
    comp = models.Completion(
        user_id=alice.id,
        library_entry_id=entry.id,
        completed_at=_dt.date(2026, 1, 1),
    )
    db_session.add(comp)
    db_session.commit()

    _signup_and_login(client, username="bob")
    r = client.get(f"/completions/{comp.id}/detail")
    assert r.status_code == 404


def test_completion_detail_shows_sibling_completions(client, db_session):
    """If the user has logged the same game multiple times, the pane lists
    the OTHER completions (excludes the one currently displayed)."""
    import datetime as _dt

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Spelunky 2")

    c1 = models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=_dt.date(2026, 1, 5))
    c2 = models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=_dt.date(2026, 2, 14))
    c3 = models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=_dt.date(2026, 3, 22))
    db_session.add_all([c1, c2, c3])
    db_session.commit()

    # Viewing c2's detail should list c1 and c3 in "other completions"
    r = client.get(f"/completions/{c2.id}/detail")
    assert r.status_code == 200
    assert b"Other completions" in r.content
    assert f"/completions/{c1.id}/detail".encode() in r.content
    assert f"/completions/{c3.id}/detail".encode() in r.content
    # c2 itself shouldn't be in the sibling list (hx-get links in the <ul>)
    sibling_section = b"Other completions"
    sibling_start = r.content.find(sibling_section)
    assert sibling_start != -1
    sibling_html = r.content[sibling_start:]
    # The sibling list links use hx-get; c2's URL should not appear there
    assert f'hx-get="/completions/{c2.id}/detail"'.encode() not in sibling_html


def test_completion_detail_single_completion_no_others_section(client, db_session):
    import datetime as _dt

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="One Shot")
    comp = models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=_dt.date(2026, 1, 1))
    db_session.add(comp)
    db_session.commit()

    r = client.get(f"/completions/{comp.id}/detail")
    assert b"Other completions" not in r.content


def test_edit_title_ignored_for_imported_entry(client, db_session):
    """Title is read-only for entries with any non-manual release. The server
    drops the incoming title; everything else (display_name, flags) saves."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="ELDEN RING")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="1245620")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    r = client.patch(
        f"/library/entries/{entry.id}",
        data={
            "title": "Hacked Title",  # imported game — server should ignore this
            "display_name": "Elden Ring",  # but this saves normally
            "is_dlc": "false",
            "is_collection": "false",
        },
    )
    assert r.status_code == 200
    db_session.refresh(game)
    # Title untouched — sync's canonical name preserved
    assert game.title == "ELDEN RING"
    # display_name still updates
    assert game.display_name == "Elden Ring"
    assert game.display_name_user_set is True


def test_edit_title_saves_for_fully_manual_entry(client, db_session):
    """A game whose every release is source='manual' lets the user rename the
    title field freely."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    # _add_game creates a release with source='manual' by default
    entry = _add_game(db_session, user, title="Original Title", platform="Switch")

    r = client.patch(
        f"/library/entries/{entry.id}",
        data={
            "title": "Renamed Title",
            "display_name": "Renamed Display",
            "is_dlc": "false",
            "is_collection": "false",
        },
    )
    assert r.status_code == 200
    db_session.refresh(entry.release.game)
    assert entry.release.game.title == "Renamed Title"
    assert entry.release.game.display_name == "Renamed Display"


def test_detail_pane_provides_parent_hero_fallback_for_dlc(client, db_session):
    """When a DLC's pane is rendered, the hero block uses the DLC's own hero
    art with the parent's hero as data-fallback for cgtCoverFallback() to
    swap on 404. Same for logo (constructed from each appid)."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    # Parent game with hero artwork.
    parent = models.Game(title="Elden Ring Nightreign")
    db_session.add(parent)
    db_session.flush()
    parent_rel = models.GameRelease(game_id=parent.id, platform="Steam", source="steam", external_id="3000000")
    db_session.add(parent_rel)
    db_session.flush()
    db_session.add(
        models.GameArtwork(
            release_id=parent_rel.id,
            artwork_type="hero",
            source="steam",
            url="https://cdn.akamai.steamstatic.com/steam/apps/3000000/library_hero.jpg",
        )
    )

    # DLC linked to that parent, plus its own (broken) hero.
    dlc = models.Game(title="The Forsaken Hollows", is_dlc=True, parent_id=parent.id)
    db_session.add(dlc)
    db_session.flush()
    dlc_rel = models.GameRelease(game_id=dlc.id, platform="Steam", source="steam", external_id="3000001")
    db_session.add(dlc_rel)
    db_session.flush()
    db_session.add(
        models.GameArtwork(
            release_id=dlc_rel.id,
            artwork_type="hero",
            source="steam",
            url="https://cdn.akamai.steamstatic.com/steam/apps/3000001/library_hero.jpg",
        )
    )
    dlc_entry = models.UserLibraryEntry(user_id=user.id, release_id=dlc_rel.id, import_source="steam_import")
    db_session.add(dlc_entry)
    db_session.commit()

    r = client.get(f"/library/entries/{dlc_entry.id}/detail")
    assert r.status_code == 200
    # DLC's own hero rendered as src.
    assert b"3000001/library_hero.jpg" in r.content
    # Parent's hero surfaced as data-fallback so onerror can swap to it.
    assert b"3000000/library_hero.jpg" in r.content
    # Logo URLs constructed for both DLC and parent appids.
    assert b"3000001/logo.png" in r.content
    assert b"3000000/logo.png" in r.content
    # Breadcrumb shows the parent's title in the header.
    assert b"Elden Ring Nightreign" in r.content


def test_detail_pane_omits_fallback_when_no_parent(client, db_session):
    """Standalone games (no parent_id) get an empty data-fallback — there's
    nothing meaningful to fall back to."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Doom Eternal")

    r = client.get(f"/library/entries/{entry.id}/detail")
    assert r.status_code == 200
    # The fallback attribute is rendered as empty since fallback_header_url is None
    # (also no cover at all for manual entries by default — just checking the
    # attribute machinery is sane)
    if b"data-fallback" in r.content:
        assert b'data-fallback=""' in r.content


def test_refresh_metadata_demotes_misclassified_dlc(client, db_session):
    """Single-entry refresh re-runs the same post-fetch logic as the worker —
    appdetails type=game on an entry currently is_dlc=True → demote."""
    from unittest.mock import patch

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="1 Screen Platformer", is_dlc=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="791180")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    with patch("backend.steam._fetch_appdetails", return_value={"type": "game", "short_description": "A platformer."}):
        r = client.post(f"/library/entries/{entry.id}/refresh-metadata")
    assert r.status_code == 200
    assert b"Refreshed metadata" in r.content

    db_session.refresh(game)
    db_session.refresh(release)
    assert game.is_dlc is False
    assert release.raw_data["appdetails"]["type"] == "game"
    assert release.metadata_fetched_at is not None


def test_refresh_metadata_respects_user_set_flag(client, db_session):
    from unittest.mock import patch

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="Manual override", is_dlc=True, is_dlc_user_set=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="500")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    with patch("backend.steam._fetch_appdetails", return_value={"type": "game"}):
        r = client.post(f"/library/entries/{entry.id}/refresh-metadata")
    assert r.status_code == 200

    db_session.refresh(game)
    assert game.is_dlc is True


def test_refresh_metadata_rejects_non_steam_entry(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Pen and Paper RPG")  # source="manual"

    r = client.post(f"/library/entries/{entry.id}/refresh-metadata")
    assert r.status_code == 400
    assert b"only available for Steam" in r.content


def test_refresh_metadata_handles_rate_limit_gracefully(client, db_session):
    from unittest.mock import patch

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="Some Game", is_dlc=True)
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="100")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.commit()

    with patch("backend.steam._fetch_appdetails", side_effect=Exception("Client error '429 Too Many Requests' for url …")):
        r = client.post(f"/library/entries/{entry.id}/refresh-metadata")
    assert r.status_code == 429
    assert b"rate-limiting" in r.content

    db_session.refresh(release)
    assert release.metadata_fetched_at is None


def test_library_grid_view_renders_cards(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="Elden Ring", platform="Steam")

    r = client.get("/library?view_mode=grid_v")
    assert r.status_code == 200
    assert b"cgt-library-grid--grid_v" in r.content
    assert b"cgt-library-card" in r.content
    # List view markup should NOT appear
    assert b"table-striped" not in r.content


def test_library_horizontal_grid_view_uses_grid_h_class(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    _add_game(db_session, user, title="Game", platform="Steam")

    r = client.get("/library?view_mode=grid_h")
    assert r.status_code == 200
    assert b"cgt-library-grid--grid_h" in r.content


def test_library_invalid_view_mode_falls_back_to_list(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    _add_game(db_session, user, title="Game", platform="Steam")

    r = client.get("/library?view_mode=garbage")
    assert r.status_code == 200
    # List markup wins. Checking for the table + absence of cards rather than
    # absence of the string "cgt-library-grid" — that substring appears in JS
    # comments and localStorage keys regardless of view mode.
    assert b"table-striped" in r.content
    assert b'class="cgt-library-grid' not in r.content
    # Use the rendered class= attribute form so JS querySelector strings like
    # '.cgt-library-card__cover' don't trigger a false positive.
    assert b'class="cgt-library-card' not in r.content


def test_grid_vertical_uses_library_cover_url(client, db_session):
    """In grid_v mode the card pulls the vertical library_600x900 art only;
    no header.jpg fallback (cross-orientation borrowing looks bad)."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="Cover Test")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="100")
    db_session.add(release)
    db_session.flush()
    # Add BOTH artworks; the card should only show the vertical one
    db_session.add(
        models.GameArtwork(
            release_id=release.id,
            artwork_type="cover_v",
            source="steam",
            url="https://example.com/100/library_600x900.jpg",
        )
    )
    db_session.add(
        models.GameArtwork(
            release_id=release.id,
            artwork_type="cover_h",
            source="steam",
            url="https://example.com/100/header.jpg",
        )
    )
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import"))
    db_session.commit()

    r = client.get("/library?view_mode=grid_v")
    assert b"100/library_600x900.jpg" in r.content
    assert b"100/header.jpg" not in r.content

    r = client.get("/library?view_mode=grid_h")
    assert b"100/header.jpg" in r.content
    assert b"100/library_600x900.jpg" not in r.content


def test_card_without_matching_artwork_gets_placeholder(client, db_session):
    """Manual entry (no Steam artwork) in grid view renders the placeholder
    class instead of trying to use a header from another orientation."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    _add_game(db_session, user, title="Manual Game", platform="Switch")

    r = client.get("/library?view_mode=grid_v")
    assert b"cgt-library-card--no-art" in r.content


def test_user_artwork_h_wins_over_game_artwork(db_session):
    """UserArtwork cover_h beats a valid GameArtwork cover_h row in the
    detail-pane visuals dict — user explicit pick always wins."""
    from backend.models import User
    from backend.pages import _build_detail_pane_visuals

    user = User(name="t", username="t", password_hash="x", api_token="tok-cov")
    db_session.add(user)
    db_session.flush()
    game = models.Game(title="Cover Override Test")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="900")
    db_session.add(release)
    db_session.flush()
    db_session.add(
        models.GameArtwork(
            release_id=release.id,
            artwork_type="cover_h",
            source="steam",
            url="https://cdn.example.com/steam-header.jpg",
        )
    )
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.flush()
    db_session.add(
        models.UserArtwork(
            user_id=user.id,
            entry_id=entry.id,
            artwork_type="cover_h",
            source="sgdb",
            url="https://sgdb.example.com/custom-header.jpg",
        )
    )
    db_session.commit()
    db_session.refresh(entry)

    visuals = _build_detail_pane_visuals(db_session, entry, game, release)
    assert visuals["header_url"] == "https://sgdb.example.com/custom-header.jpg"


def test_grid_cover_url_v_user_artwork_wins(client, db_session):
    """In grid_v view, UserArtwork cover_v wins over GameArtwork cover_v."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="Vertical Override Test")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="901")
    db_session.add(release)
    db_session.flush()
    db_session.add(
        models.GameArtwork(
            release_id=release.id,
            artwork_type="cover_v",
            source="steam",
            url="https://cdn.example.com/steam-600x900.jpg",
        )
    )
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.flush()
    db_session.add(
        models.UserArtwork(
            user_id=user.id,
            entry_id=entry.id,
            artwork_type="cover_v",
            source="sgdb",
            url="https://sgdb.example.com/custom-600x900.jpg",
        )
    )
    db_session.commit()

    r = client.get("/library?view_mode=grid_v")
    assert b"sgdb.example.com/custom-600x900.jpg" in r.content
    assert b"cdn.example.com/steam-600x900.jpg" not in r.content


def test_clear_cover_override_v(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    db_session.add(
        models.UserArtwork(user_id=user.id, entry_id=entry.id, artwork_type="cover_v", source="sgdb", url="https://example.com/custom.jpg")
    )
    db_session.add(
        models.UserArtwork(
            user_id=user.id, entry_id=entry.id, artwork_type="cover_h", source="sgdb", url="https://example.com/custom_h.jpg"
        )
    )
    db_session.commit()

    r = client.post(f"/library/entries/{entry.id}/clear-cover-override", data={"image_type": "v"})
    assert r.status_code == 200
    remaining = db_session.query(models.UserArtwork).filter_by(entry_id=entry.id).all()
    remaining_types = {ua.artwork_type for ua in remaining}
    # cover_v cleared, cover_h untouched
    assert "cover_v" not in remaining_types
    assert "cover_h" in remaining_types


def test_clear_cover_override_h(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    db_session.add(
        models.UserArtwork(user_id=user.id, entry_id=entry.id, artwork_type="cover_v", source="sgdb", url="https://example.com/custom.jpg")
    )
    db_session.add(
        models.UserArtwork(
            user_id=user.id, entry_id=entry.id, artwork_type="cover_h", source="sgdb", url="https://example.com/custom_h.jpg"
        )
    )
    db_session.commit()

    r = client.post(f"/library/entries/{entry.id}/clear-cover-override", data={"image_type": "h"})
    assert r.status_code == 200
    remaining = db_session.query(models.UserArtwork).filter_by(entry_id=entry.id).all()
    remaining_types = {ua.artwork_type for ua in remaining}
    # cover_h cleared, cover_v untouched
    assert "cover_h" not in remaining_types
    assert "cover_v" in remaining_types


def test_clear_cover_override_rejects_bad_orientation(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    r = client.post(f"/library/entries/{entry.id}/clear-cover-override", data={"image_type": "x"})
    assert r.status_code == 400


# --- metadata staleness helper / detail-pane auto-refresh ---


def test_needs_metadata_refresh_for_never_fetched_steam():
    from backend.pages import _needs_metadata_refresh

    release = models.GameRelease(source="steam", external_id="220", metadata_fetched_at=None)
    assert _needs_metadata_refresh(release) is True


def test_needs_metadata_refresh_for_fresh_steam():
    import datetime as dt

    from backend.pages import _needs_metadata_refresh

    release = models.GameRelease(
        source="steam",
        external_id="220",
        metadata_fetched_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
    )
    assert _needs_metadata_refresh(release) is False


def test_needs_metadata_refresh_for_stale_steam():
    import datetime as dt

    from backend.pages import _needs_metadata_refresh

    release = models.GameRelease(
        source="steam",
        external_id="220",
        metadata_fetched_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=14),
    )
    assert _needs_metadata_refresh(release) is True


def test_needs_metadata_refresh_skips_non_steam():
    from backend.pages import _needs_metadata_refresh

    release = models.GameRelease(source="manual", external_id=None, metadata_fetched_at=None)
    assert _needs_metadata_refresh(release) is False


# --- view_mode resolution from cookie ---


def test_library_view_mode_falls_back_to_cookie(client):
    """When no ?view_mode= in the URL, the page should pick up the cookie
    set by the toggle JS — fixes the brief 'list flashes before grid' lag."""
    _signup_and_login(client)
    client.cookies.set("cgt-library-view-mode", "grid_v")
    r = client.get("/library")
    assert r.status_code == 200
    assert b"cgt-library-grid--grid_v" in r.content


def test_library_query_param_beats_cookie(client):
    """Explicit ?view_mode= in URL takes precedence over the cookie."""
    _signup_and_login(client)
    client.cookies.set("cgt-library-view-mode", "grid_v")
    r = client.get("/library?view_mode=list")
    assert r.status_code == 200
    assert b"cgt-library-grid--grid" not in r.content


def test_completions_view_mode_falls_back_to_cookie(client):
    _signup_and_login(client)
    client.cookies.set("cgt-completions-view-mode", "grid_h")
    r = client.get("/completions")
    assert r.status_code == 200
    assert b"cgt-library-grid--grid_h" in r.content


def test_view_mode_junk_cookie_falls_back_to_list(client):
    _signup_and_login(client)
    client.cookies.set("cgt-library-view-mode", "diagonal")
    r = client.get("/library")
    assert r.status_code == 200
    assert b"cgt-library-grid--grid" not in r.content


# --- _needs_metadata_refresh tolerates naive datetimes ---


def test_needs_metadata_refresh_handles_naive_datetime():
    """SQLite stores DateTime as offset-naive; we need to handle that without
    crashing. (Bug: previously the detail-pane endpoint 500'd with
    'can't subtract offset-naive and offset-aware datetimes', blanking the
    pane for any entry that had been enriched.)"""
    import datetime as dt

    from backend.pages import _needs_metadata_refresh

    # Naive datetime from 14 days ago — should be considered stale, no crash.
    release = models.GameRelease(
        source="steam",
        external_id="220",
        metadata_fetched_at=dt.datetime.utcnow() - dt.timedelta(days=14),  # naive
    )
    assert _needs_metadata_refresh(release) is True

    # Naive datetime from 1 day ago — fresh, no crash.
    release.metadata_fetched_at = dt.datetime.utcnow() - dt.timedelta(days=1)
    assert _needs_metadata_refresh(release) is False


# --- "App {appid}" placeholder title backfill from appdetails ---


def test_enrich_replaces_appid_placeholder_title(db_session):
    """When sync stamped a title as 'App 12345' (appid wasn't in catalog
    cache), enrichment should overwrite it with the real name from
    appdetails. Without this, DLCs whose appid was added after our last
    catalog refresh stay forever as 'App 12345' in the UI."""
    from unittest.mock import patch

    from backend import steam

    game = models.Game(title="App 3515610")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="3515610")
    db_session.add(release)
    db_session.commit()

    fake_details = {"type": "dlc", "name": "ELDEN RING NIGHTREIGN - Deluxe Upgrade Pack"}
    with patch("backend.steam._fetch_appdetails", return_value=fake_details):
        steam.enrich_next_batch(db_session, batch_size=10)

    db_session.refresh(game)
    assert game.title == "ELDEN RING NIGHTREIGN - Deluxe Upgrade Pack"


def test_enrich_does_not_overwrite_real_title(db_session):
    """If the title isn't the 'App {appid}' placeholder, leave it alone —
    don't stomp something the user / sync got from a legitimate source."""
    from unittest.mock import patch

    from backend import steam

    game = models.Game(title="Original Title")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="100")
    db_session.add(release)
    db_session.commit()

    with patch("backend.steam._fetch_appdetails", return_value={"type": "game", "name": "Different Name"}):
        steam.enrich_next_batch(db_session, batch_size=10)

    db_session.refresh(game)
    assert game.title == "Original Title"


# --- Recently played sort ---


def test_library_recently_played_sort(client, db_session):
    """sort=recently_played orders by last_played_at desc, nulls last."""
    import datetime

    _signup_and_login(client)
    user = db_session.query(models.User).first()

    def _add(title, last_played):
        game = models.Game(title=title)
        db_session.add(game)
        db_session.flush()
        release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id=str(game.id))
        db_session.add(release)
        db_session.flush()
        entry = models.UserLibraryEntry(
            user_id=user.id,
            release_id=release.id,
            import_source="steam_import",
            last_played_at=last_played,
        )
        db_session.add(entry)

    now = datetime.datetime.now(datetime.UTC)
    _add("Older Game", now - datetime.timedelta(days=10))
    _add("Newer Game", now - datetime.timedelta(days=1))
    _add("Never Played", None)
    db_session.commit()

    r = client.get("/library?sort=recently_played&view=all")
    assert r.status_code == 200
    body = r.text
    # Newer should appear before Older; Never Played should come last
    pos_newer = body.index("Newer Game")
    pos_older = body.index("Older Game")
    pos_never = body.index("Never Played")
    assert pos_newer < pos_older < pos_never


# --- Missing artwork filter ---


def _add_steam_entry(db, user, title, appid, has_cover=True, has_header=True):
    """Helper: add a Steam entry optionally with GameArtwork rows."""
    game = models.Game(title=title)
    db.add(game)
    db.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id=str(appid))
    db.add(release)
    db.flush()
    if has_cover:
        db.add(models.GameArtwork(release_id=release.id, artwork_type="cover_v", source="steam", url=f"http://cdn/{appid}/cover.jpg"))
    if has_header:
        db.add(models.GameArtwork(release_id=release.id, artwork_type="cover_h", source="steam", url=f"http://cdn/{appid}/header.jpg"))
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db.add(entry)
    db.commit()
    return entry


_HX = {"HX-Request": "true"}  # triggers HTMX partial path — skips modal dropdowns


def test_missing_art_filter_grid_v(client, db_session):
    """missing_art=true in grid_v shows only entries missing a vertical cover."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()

    _add_steam_entry(db_session, user, "Has Cover", 101, has_cover=True)
    _add_steam_entry(db_session, user, "No Cover", 102, has_cover=False)

    r = client.get("/library?missing_art=true&view_mode=grid_v&view=all", headers=_HX)
    assert r.status_code == 200
    assert b"No Cover" in r.content
    assert b"Has Cover" not in r.content


def test_missing_art_filter_grid_h(client, db_session):
    """missing_art=true in grid_h shows only entries missing a header."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()

    _add_steam_entry(db_session, user, "Has Header", 201, has_header=True)
    _add_steam_entry(db_session, user, "No Header", 202, has_header=False)

    r = client.get("/library?missing_art=true&view_mode=grid_h&view=all", headers=_HX)
    assert r.status_code == 200
    assert b"No Header" in r.content
    assert b"Has Header" not in r.content


def test_missing_art_user_artwork_satisfies_filter(client, db_session):
    """An entry with a UserArtwork pick is NOT shown as missing art."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()

    game = models.Game(title="Override Game")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="301")
    db_session.add(release)
    db_session.flush()
    # No GameArtwork row, but has a UserArtwork pick
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source="steam_import")
    db_session.add(entry)
    db_session.flush()
    db_session.add(
        models.UserArtwork(
            user_id=user.id,
            entry_id=entry.id,
            artwork_type="cover_v",
            source="sgdb",
            url="https://example.com/override.jpg",
        )
    )
    db_session.commit()

    r = client.get("/library?missing_art=true&view_mode=grid_v&view=all", headers=_HX)
    assert r.status_code == 200
    assert b"Override Game" not in r.content


# --- hero logo position ---


def _make_entry_with_hero_and_logo(db, user_id):
    """Manual entry with hero + logo artwork so the detail pane renders both."""
    g = models.Game(title="Logo Game")
    db.add(g)
    db.flush()
    rel = models.GameRelease(game_id=g.id, source="manual", platform="PC")
    db.add(rel)
    db.flush()
    db.add(models.GameArtwork(release_id=rel.id, artwork_type="hero", source="sgdb", url="https://example.com/hero.jpg", is_valid=True))
    entry = models.UserLibraryEntry(user_id=user_id, release_id=rel.id, import_source="manual")
    db.add(entry)
    db.flush()
    db.add(models.UserArtwork(user_id=user_id, entry_id=entry.id, artwork_type="logo", source="sgdb", url="https://example.com/logo.png"))
    db.commit()
    return entry


def test_set_logo_position_persists(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_entry_with_hero_and_logo(db_session, user.id)
    r = client.post(f"/library/entries/{entry.id}/logo-position", data={"position": "top-right"})
    assert r.status_code == 204
    db_session.expire_all()
    assert db_session.get(models.UserLibraryEntry, entry.id).logo_position == "top-right"
    # detail pane renders the anchor class
    r = client.get(f"/library/entries/{entry.id}/detail", headers=_HX)
    assert b"cgt-detail-hero__logo--top-right" in r.content
    # clearing (empty value) returns to default — no modifier class
    r = client.post(f"/library/entries/{entry.id}/logo-position", data={"position": ""})
    assert r.status_code == 204
    db_session.expire_all()
    assert db_session.get(models.UserLibraryEntry, entry.id).logo_position is None


def test_logo_position_hidden_removes_logo(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_entry_with_hero_and_logo(db_session, user.id)
    r = client.get(f"/library/entries/{entry.id}/detail", headers=_HX)
    assert b"cgt-detail-hero__logo" in r.content
    client.post(f"/library/entries/{entry.id}/logo-position", data={"position": "hidden"})
    r = client.get(f"/library/entries/{entry.id}/detail", headers=_HX)
    assert b"cgt-detail-hero__logo" not in r.content


def test_logo_position_rejects_unknown_value(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_entry_with_hero_and_logo(db_session, user.id)
    r = client.post(f"/library/entries/{entry.id}/logo-position", data={"position": "upside-down"})
    assert r.status_code == 422
    db_session.expire_all()
    assert db_session.get(models.UserLibraryEntry, entry.id).logo_position is None


def test_set_logo_scale_persists_and_renders(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_entry_with_hero_and_logo(db_session, user.id)
    r = client.post(f"/library/entries/{entry.id}/logo-scale", data={"scale": "xlarge"})
    assert r.status_code == 204
    db_session.expire_all()
    assert db_session.get(models.UserLibraryEntry, entry.id).logo_scale == "xlarge"
    r = client.get(f"/library/entries/{entry.id}/detail", headers=_HX)
    assert b"cgt-detail-hero__logo--scale-xlarge" in r.content
    # invalid value rejected, empty clears
    assert client.post(f"/library/entries/{entry.id}/logo-scale", data={"scale": "gigantic"}).status_code == 422
    assert client.post(f"/library/entries/{entry.id}/logo-scale", data={"scale": ""}).status_code == 204
    db_session.expire_all()
    assert db_session.get(models.UserLibraryEntry, entry.id).logo_scale is None


# --- import candidate reopen ---


def _make_confirmed_candidate(db, user_id, entry_id, link=True):
    """Confirmed add_to_existing candidate with one row + the completion its
    confirm would have created. link=False simulates a pre-linkage confirm."""
    cand = models.ImportCandidate(
        user_id=user_id,
        raw_title="Old Game",
        raw_platform="SNES",
        library_entry_id=entry_id,
        status="confirmed",
        proposed_action="add_to_existing",
    )
    db.add(cand)
    db.flush()
    comp = models.Completion(
        user_id=user_id,
        library_entry_id=entry_id,
        completed_at=datetime.date(2009, 6, 1),
        completed_at_precision="month",
        sort_order=7,
    )
    db.add(comp)
    db.flush()
    row = models.ImportRow(
        candidate_id=cand.id,
        raw_title="Old Game",
        raw_platform="SNES",
        row_number=7,
        completed_at=datetime.date(2009, 6, 1),
        completed_at_precision="month",
        created_completion_id=comp.id if link else None,
    )
    db.add(row)
    db.commit()
    return cand, comp


def _make_plain_entry(db, user_id):
    g = models.Game(title="Old Game")
    db.add(g)
    db.flush()
    rel = models.GameRelease(game_id=g.id, source="manual", platform="SNES")
    db.add(rel)
    db.flush()
    entry = models.UserLibraryEntry(user_id=user_id, release_id=rel.id, import_source="manual")
    db.add(entry)
    db.commit()
    return entry


def test_reopen_deletes_linked_completion_and_flips_pending(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    cand, comp = _make_confirmed_candidate(db_session, user.id, entry.id, link=True)
    comp_id, cand_id = comp.id, cand.id
    r = client.post(f"/library/import/{cand_id}/reopen")
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.Completion, comp_id) is None
    reopened = db_session.get(models.ImportCandidate, cand_id)
    assert reopened.status == "pending"
    assert reopened.reviewed_at is None


def test_reopen_legacy_candidate_matches_by_row_fields(client, db_session):
    """Rows confirmed before created_completion_id existed still reopen —
    the completion is found by entry + date + sheet-row sort_order."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    cand, comp = _make_confirmed_candidate(db_session, user.id, entry.id, link=False)
    comp_id, cand_id = comp.id, cand.id
    r = client.post(f"/library/import/{cand_id}/reopen")
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.Completion, comp_id) is None
    assert db_session.get(models.ImportCandidate, cand_id).status == "pending"


def test_reopen_rejects_pending_candidate(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    cand, _ = _make_confirmed_candidate(db_session, user.id, entry.id)
    cand.status = "pending"
    db_session.commit()
    assert client.post(f"/library/import/{cand.id}/reopen").status_code == 404


def test_confirmed_tab_lists_candidate_with_reopen_action(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    cand, _ = _make_confirmed_candidate(db_session, user.id, entry.id)
    r = client.get("/library/import/review?tab=confirmed", headers=_HX)
    assert r.status_code == 200
    assert f"/library/import/{cand.id}/reopen".encode() in r.content
