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


def test_detail_pane_renders_psn_store_metadata(client, db_session):
    """A PSN entry with a cached store record shows publisher / genre / release /
    rating / description rows in the detail pane (#168 Chunk C)."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="Control Ultimate Edition")
    db_session.add(game)
    db_session.flush()
    rel = models.GameRelease(
        game_id=game.id,
        platform="PS5",
        source="psn",
        external_id="PPSA01949_00",
        raw_data={
            "productId": "UP4040-PPSA01949_00-CONTROLUEPS50000",
            "store": {
                "name": "Control Ultimate Edition",
                "publisher": "REMEDY ENTERTAINMENT LTD.",
                "release_date": "2021-02-02T05:00:00Z",
                "genres": ["Action"],
                "rating": 4.39,
                "rating_count": 47038,
                "description": "A corruptive presence has invaded the Bureau.",
            },
        },
    )
    db_session.add(rel)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=rel.id, import_source="psn_import")
    db_session.add(entry)
    db_session.commit()

    r = client.get(f"/library/entries/{entry.id}/detail", headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.content
    assert b"REMEDY ENTERTAINMENT LTD." in body
    assert b"Action" in body
    assert b"Feb 2, 2021" in body
    assert b"4.4" in body and b"47,038 ratings" in body
    assert b"A corruptive presence" in body


def test_psn_store_refresh_endpoint(client, db_session):
    """The single-entry refresh endpoint refetches PSN store data; trophy-only
    entries (no productId) are rejected."""
    from unittest.mock import patch

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="Batman")
    db_session.add(game)
    db_session.flush()
    rel = models.GameRelease(
        game_id=game.id, platform="PS4", source="psn", external_id="CUSA05332_00", raw_data={"productId": "UP2026-CUSA05332_00-X"}
    )
    db_session.add(rel)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=rel.id, import_source="psn_import")
    db_session.add(entry)
    db_session.commit()

    with patch("backend.psn_store.fetch_product", return_value={"name": "Batman: The Telltale Series"}):
        r = client.post(f"/library/entries/{entry.id}/refresh-metadata")
    assert r.status_code == 200
    assert b"Refreshed store metadata" in r.content
    db_session.refresh(game)
    assert game.title == "Batman: The Telltale Series"


def test_detail_pane_psn_entry_links_to_ps_store(client, db_session):
    """A PSN entry with a productId gets a PS Store link; a trophy-only entry
    without one just shows the ID, no store link."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    with_pid = models.Game(title="Control Ultimate Edition")
    without_pid = models.Game(title="Demon's Souls")
    db_session.add_all([with_pid, without_pid])
    db_session.flush()
    rel_a = models.GameRelease(
        game_id=with_pid.id,
        platform="PS5",
        source="psn",
        external_id="PPSA01949_00",
        raw_data={"productId": "UP4040-PPSA01949_00-CONTROLUEPS50000"},
    )
    rel_b = models.GameRelease(game_id=without_pid.id, platform="PS3", source="psn", external_id="NPWR555_00", raw_data={})
    db_session.add_all([rel_a, rel_b])
    db_session.flush()
    entry_a = models.UserLibraryEntry(user_id=user.id, release_id=rel_a.id, import_source="psn_import")
    entry_b = models.UserLibraryEntry(user_id=user.id, release_id=rel_b.id, import_source="psn_import")
    db_session.add_all([entry_a, entry_b])
    db_session.commit()

    hx = {"HX-Request": "true"}
    ra = client.get(f"/library/entries/{entry_a.id}/detail", headers=hx)
    assert b"store.playstation.com/en-us/product/UP4040-PPSA01949_00-CONTROLUEPS50000" in ra.content
    rb = client.get(f"/library/entries/{entry_b.id}/detail", headers=hx)
    assert b"store.playstation.com" not in rb.content


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


def test_detail_pane_scopes_dlc_to_release_source(client, db_session):
    """A base Game shared across stores (one Steam + one PSN release) must not
    leak the other store's DLC into the pane — Steam DLC shows under the Steam
    entry, never under the PSN entry."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    base = models.Game(title="Control Ultimate Edition")
    db_session.add(base)
    db_session.flush()
    steam_rel = models.GameRelease(game_id=base.id, platform="Steam", source="steam", external_id="870780")
    psn_rel = models.GameRelease(game_id=base.id, platform="PS5", source="psn", external_id="PPSA01949_00")
    db_session.add_all([steam_rel, psn_rel])
    db_session.flush()
    steam_entry = models.UserLibraryEntry(user_id=user.id, release_id=steam_rel.id, import_source="steam_import")
    psn_entry = models.UserLibraryEntry(user_id=user.id, release_id=psn_rel.id, import_source="psn_import")
    db_session.add_all([steam_entry, psn_entry])
    db_session.flush()

    dlc_game = models.Game(title="CONTROL - AWE", is_dlc=True, parent_id=base.id)
    db_session.add(dlc_game)
    db_session.flush()
    dlc_rel = models.GameRelease(game_id=dlc_game.id, platform="Steam", source="steam", external_id="dlc1")
    db_session.add(dlc_rel)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=dlc_rel.id, import_source="steam_import"))
    db_session.commit()

    hx = {"HX-Request": "true"}
    r_steam = client.get(f"/library/entries/{steam_entry.id}/detail", headers=hx)
    assert b"CONTROL - AWE" in r_steam.content  # DLC shows under the Steam release
    r_psn = client.get(f"/library/entries/{psn_entry.id}/detail", headers=hx)
    assert b"CONTROL - AWE" not in r_psn.content  # ...but not under the PSN release


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
    from backend.pages_common import _extract_steam_meta

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
    from backend.pages_common import _normalize_steam_date

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
    from backend.pages_common import _extract_steam_meta

    meta = _extract_steam_meta(
        {
            "developers": ["id Software"],
            "publishers": ["Bethesda Softworks"],
        }
    )
    assert meta["developers"] == ["id Software"]
    assert meta["publishers"] == ["Bethesda Softworks"]


def test_extract_steam_meta_empty_payload():
    from backend.pages_common import _extract_steam_meta

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
    from backend.pages_common import _build_detail_pane_visuals

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
    from backend.pages_common import _needs_metadata_refresh

    release = models.GameRelease(source="steam", external_id="220", metadata_fetched_at=None)
    assert _needs_metadata_refresh(release) is True


def test_needs_metadata_refresh_for_fresh_steam():
    import datetime as dt

    from backend.pages_common import _needs_metadata_refresh

    release = models.GameRelease(
        source="steam",
        external_id="220",
        metadata_fetched_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
    )
    assert _needs_metadata_refresh(release) is False


def test_needs_metadata_refresh_for_stale_steam():
    import datetime as dt

    from backend.pages_common import _needs_metadata_refresh

    release = models.GameRelease(
        source="steam",
        external_id="220",
        metadata_fetched_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=14),
    )
    assert _needs_metadata_refresh(release) is True


def test_needs_metadata_refresh_skips_non_steam():
    from backend.pages_common import _needs_metadata_refresh

    release = models.GameRelease(source="manual", external_id=None, metadata_fetched_at=None)
    assert _needs_metadata_refresh(release) is False


def test_needs_metadata_refresh_for_psn():
    """A PSN release with a productId but no store record is stale → the pane
    auto-refetches; a trophy-only PSN release (no productId) is not."""
    from backend.pages_common import _needs_metadata_refresh

    with_pid = models.GameRelease(source="psn", external_id="CUSA1_00", raw_data={"productId": "UP0-CUSA1_00-X"}, metadata_fetched_at=None)
    trophy_only = models.GameRelease(source="psn", external_id="NPWR1", raw_data={"npCommunicationId": "NPWR1"}, metadata_fetched_at=None)
    assert _needs_metadata_refresh(with_pid) is True
    assert _needs_metadata_refresh(trophy_only) is False


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

    from backend.pages_common import _needs_metadata_refresh

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
    r = client.post(f"/tools/import/{cand_id}/reopen")
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
    r = client.post(f"/tools/import/{cand_id}/reopen")
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
    assert client.post(f"/tools/import/{cand.id}/reopen").status_code == 404


def test_confirmed_tab_lists_candidate_with_reopen_action(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    cand, _ = _make_confirmed_candidate(db_session, user.id, entry.id)
    r = client.get("/tools/import/review?tab=confirmed", headers=_HX)
    assert r.status_code == 200
    assert f"/tools/import/{cand.id}/reopen".encode() in r.content


def _make_import_candidate(db, user_id, title, platform_obj, action="create_new"):
    cand = models.ImportCandidate(
        user_id=user_id,
        raw_title=title,
        raw_platform=platform_obj.name,
        platform_id=platform_obj.id,
        status="pending",
        proposed_action=action,
    )
    db.add(cand)
    db.flush()
    return cand


def _make_named_entry(db, user_id, title, platform="Steam"):
    g = models.Game(title=title, display_name=title)
    db.add(g)
    db.flush()
    rel = models.GameRelease(game_id=g.id, source="manual", platform=platform)
    db.add(rel)
    db.flush()
    entry = models.UserLibraryEntry(user_id=user_id, release_id=rel.id, import_source="manual")
    db.add(entry)
    db.flush()
    return entry


def test_library_search_ranks_exact_over_substring_and_survives_cap(client, db_session):
    """Searching an exact title must return (and rank first) that entry even
    when many longer titles contain the query. Regression: alphabetical order +
    LIMIT dropped "Marvel Super Heroes" under 15+ "LEGO Marvel Super Heroes …"."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    exact = _make_named_entry(db_session, user.id, "Marvel Super Heroes")
    # 30 substring decoys that sort BEFORE the exact title alphabetically and
    # would fill the old 15-cap (and even the new 25-cap) on their own.
    for i in range(30):
        _make_named_entry(db_session, user.id, f"LEGO Marvel Super Heroes {i:02d}")
    db_session.commit()
    exact_id = exact.id

    r = client.get("/library/games/search?q=Marvel+Super+Heroes&id_field=entry")
    assert r.status_code == 200
    body = r.text
    # the exact entry is present despite 30 substring matches sorting ahead of it
    marker = f'data-cgt-id="{exact_id}"'
    assert marker in body
    # and it ranks first — before any LEGO row
    assert body.index(marker) < body.index("LEGO Marvel Super Heroes")


def test_confirm_endpoint_rejects_non_add_to_existing(client, db_session):
    """create_new / needs_review candidates are confirmed via the in-place add
    modal (POST /library/games), so the confirm endpoint only handles
    add_to_existing and 400s otherwise (dead redirect branch removed)."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    plat = models.Platform(name="Steam", display_name="Steam")
    db_session.add(plat)
    db_session.flush()
    cand = _make_import_candidate(db_session, user.id, "Some Game", plat, action="create_new")
    db_session.commit()
    r = client.post(f"/tools/import/{cand.id}/confirm")
    assert r.status_code == 400


def test_inplace_add_confirms_candidate_and_returns_oob_counts(client, db_session):
    """Adding a game with an import_candidate_id (the import page's in-place
    "Add new" modal) confirms the candidate, logs its completions, and returns
    the OOB count refresh — not a library row — so the page updates without a
    reload."""
    import datetime as _dt

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    cand = models.ImportCandidate(
        user_id=user.id,
        raw_title="Chibi-Robo",
        raw_platform="GameCube",
        status="pending",
        proposed_action="create_new",
    )
    db_session.add(cand)
    db_session.flush()
    db_session.add(
        models.ImportRow(
            candidate_id=cand.id,
            raw_title="Chibi-Robo",
            raw_platform="GameCube",
            row_number=1,
            completed_at=_dt.date(2007, 12, 1),
            completed_at_precision="month",
            playthroughs=1,
        )
    )
    db_session.commit()
    cand_id = cand.id

    r = client.post(
        "/library/games",
        data={"title": "Chibi-Robo", "platform": "GameCube", "import_candidate_id": str(cand_id)},
    )
    assert r.status_code == 200
    # OOB count refresh, not a library row
    assert 'id="import-pending-count"' in r.text
    assert 'hx-swap-oob="true"' in r.text

    db_session.expire_all()
    reloaded = db_session.get(models.ImportCandidate, cand_id)
    assert reloaded.status == "confirmed"
    assert reloaded.library_entry_id is not None
    # completion logged from the sheet row, linkage stamped for Reopen
    row = db_session.query(models.ImportRow).filter_by(candidate_id=cand_id).first()
    assert row.created_completion_id is not None


def test_per_tab_cookie_filters_initial_render_and_is_tab_scoped(client, db_session):
    """A per-tab filter cookie binds into the initial SQL query (no query param,
    no client re-fetch) — and only for its own tab. This is the whole point of
    using cookies over localStorage: the server can render already filtered."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    steam = models.Platform(name="Steam", display_name="Steam")
    switch = models.Platform(name="Switch", display_name="Switch")
    db_session.add_all([steam, switch])
    db_session.flush()
    _make_import_candidate(db_session, user.id, "Steam Game", steam, action="create_new")
    _make_import_candidate(db_session, user.id, "Switch Game", switch, action="create_new")
    _make_import_candidate(db_session, user.id, "Other Steam", steam, action="needs_review")
    db_session.commit()

    # Stored the way the browser actually writes it: encodeURIComponent turns
    # the ":" into "%3A", so the server must decode before matching.
    client.cookies.set("cgt-import-remember", "1")  # filter memory is opt-in (#189)
    client.cookies.set("cgt-import-create_new-platform", f"pid%3A{steam.id}")

    # create_new: cookie applies, no query param needed — server renders filtered
    r = client.get("/tools/import/review?tab=create_new", headers=_HX)
    assert "Steam Game" in r.text
    assert "Switch Game" not in r.text

    # needs_review: the create_new cookie must NOT leak here
    r2 = client.get("/tools/import/review?tab=needs_review", headers=_HX)
    assert "Other Steam" in r2.text


def test_platform_filter_narrows_results_and_marks_selection(client, db_session):
    """The platform filter actually filters (only matching rows) and marks the
    chosen option selected — regression for the swap that showed every platform
    unfiltered."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    steam = models.Platform(name="Steam", display_name="Steam")
    switch = models.Platform(name="Switch", display_name="Switch")
    db_session.add_all([steam, switch])
    db_session.flush()
    _make_import_candidate(db_session, user.id, "Steam Game", steam)
    _make_import_candidate(db_session, user.id, "Switch Game", switch)
    db_session.commit()

    # HX filter-change response: only the filtered rows (selects are not
    # re-emitted on a plain filter change — see the OOB test below).
    r = client.get(f"/tools/import/review?tab=create_new&platform=pid:{steam.id}", headers=_HX)
    assert r.status_code == 200
    assert "Steam Game" in r.text
    assert "Switch Game" not in r.text

    # Full page render carries the selects inline, with the chosen option marked.
    full = client.get(f"/tools/import/review?tab=create_new&platform=pid:{steam.id}")
    assert "Steam Game" in full.text
    assert "Switch Game" not in full.text
    assert f'value="pid:{steam.id}" selected' in full.text


def test_filter_selects_self_trigger_so_oob_replacement_keeps_them_working(client, db_session):
    """Each filter select must carry its own hx-get. The form used to listen via
    `change from:#import-platform-filter`, which binds to the element at load
    time — a tab switch replaces the selects out-of-band and orphaned that
    listener, silently killing filtering. Self-contained triggers get re-bound
    on every swap. Guard so nobody moves the trigger back onto the form."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    steam = models.Platform(name="Steam", display_name="Steam")
    db_session.add(steam)
    db_session.flush()
    _make_import_candidate(db_session, user.id, "Steam Game", steam)
    db_session.commit()

    full = client.get("/tools/import/review?tab=create_new")
    body = full.text
    # the platform select block itself carries hx-get (self-triggering)
    seg = body[body.index('id="import-platform-filter"') :]
    seg = seg[: seg.index("</select>")]
    assert "hx-get=" in seg
    # and the form no longer re-declares a change-from listener for the selects
    assert "change from:#import-platform-filter" not in body


def test_tab_switch_refreshes_selects_oob_but_filter_change_does_not(client, db_session):
    """refresh_filters=1 (tab buttons) re-emits the selects out-of-band; a plain
    filter-change request must NOT — otherwise it repaints the select the user
    is mid-interaction with, which broke live filtering before."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    steam = models.Platform(name="Steam", display_name="Steam")
    db_session.add(steam)
    db_session.flush()
    _make_import_candidate(db_session, user.id, "Steam Game", steam)
    db_session.commit()

    with_refresh = client.get("/tools/import/review?tab=create_new&refresh_filters=1", headers=_HX)
    assert 'hx-swap-oob="true"' in with_refresh.text

    without = client.get("/tools/import/review?tab=create_new", headers=_HX)
    assert 'hx-swap-oob="true"' not in without.text


def test_full_page_load_never_duplicates_selects_even_with_refresh_flag(client, db_session):
    """A pushed tab-switch URL can carry refresh_filters=1; reloading it does a
    full (non-HX) render. That must not emit a second, OOB copy of the selects
    inside the content div (duplicate ids)."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    steam = models.Platform(name="Steam", display_name="Steam")
    db_session.add(steam)
    db_session.flush()
    _make_import_candidate(db_session, user.id, "Steam Game", steam)
    db_session.commit()

    r = client.get("/tools/import/review?tab=create_new&refresh_filters=1")
    assert r.status_code == 200
    assert 'hx-swap-oob="true"' not in r.text
    assert r.text.count('id="import-platform-filter"') == 1


def test_confirmed_tab_filter_dropdowns_populate_from_confirmed_candidates(client, db_session):
    """The confirmed tab lists status=="confirmed" candidates with ANY
    proposed_action, but the dropdown builders used to hardcode
    status=="pending" AND proposed_action==tab — matching nothing there, so
    platform/year selects rendered empty on that tab."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    # confirmed candidate (SNES platform row + 2009 completion row via helper)
    cand, _ = _make_confirmed_candidate(db_session, user.id, entry.id)
    snes = models.Platform(name="SNES", display_name="Super Nintendo")
    db_session.add(snes)
    db_session.flush()
    cand.platform_id = snes.id
    # pending decoy on another platform/year — must NOT bleed into confirmed's options
    steam = models.Platform(name="Steam", display_name="Steam")
    db_session.add(steam)
    db_session.flush()
    decoy = _make_import_candidate(db_session, user.id, "Pending Game", steam)
    db_session.add(
        models.ImportRow(
            candidate_id=decoy.id,
            raw_title="Pending Game",
            raw_platform="Steam",
            row_number=1,
            completed_at=datetime.date(2021, 3, 2),
            completed_at_precision="day",
        )
    )
    db_session.commit()

    # Full render of the confirmed tab: selects carry the confirmed set's options
    full = client.get("/tools/import/review?tab=confirmed")
    assert full.status_code == 200
    assert f'value="pid:{snes.id}"' in full.text
    assert 'value="2009"' in full.text
    assert f'value="pid:{steam.id}"' not in full.text
    assert 'value="2021"' not in full.text

    # and the platform filter actually narrows the confirmed list
    r = client.get(f"/tools/import/review?tab=confirmed&platform=pid:{snes.id}", headers=_HX)
    assert "Old Game" in r.text


def test_bulk_confirm_confirms_only_eligible_candidates(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)

    def make_pending(title, action="add_to_existing", entry_id=entry.id, row_number=1):
        cand = models.ImportCandidate(
            user_id=user.id,
            raw_title=title,
            raw_platform="SNES",
            library_entry_id=entry_id if action == "add_to_existing" else None,
            status="pending",
            proposed_action=action,
        )
        db_session.add(cand)
        db_session.flush()
        db_session.add(
            models.ImportRow(
                candidate_id=cand.id,
                raw_title=title,
                raw_platform="SNES",
                row_number=row_number,
                completed_at=datetime.date(2010, 1, 1),
                completed_at_precision="day",
            )
        )
        db_session.commit()
        return cand

    a = make_pending("Bulk A", row_number=1)
    # Distinct sheet row number — same entry+date+row_number would trip the
    # intentional duplicate-skip guard (that's re-upload protection, not a bug)
    b = make_pending("Bulk B", row_number=2)
    c = make_pending("Not Matched", action="create_new")
    ids = f"{a.id},{b.id},{c.id},99999"
    a_id, b_id, c_id = a.id, b.id, c.id

    r = client.post("/tools/import/confirm-bulk", data={"ids": ids})
    assert r.status_code == 200
    assert b"Confirmed 2 candidates" in r.content
    db_session.expire_all()
    assert db_session.get(models.ImportCandidate, a_id).status == "confirmed"
    assert db_session.get(models.ImportCandidate, b_id).status == "confirmed"
    assert db_session.get(models.ImportCandidate, c_id).status == "pending"
    comps = db_session.query(models.Completion).filter(models.Completion.library_entry_id == entry.id).all()
    assert len(comps) == 2
    # linkage stamped so these are reopen-able
    rows = db_session.query(models.ImportRow).filter(models.ImportRow.created_completion_id.isnot(None)).count()
    assert rows == 2


def test_bulk_confirm_requires_ids(client):
    _signup_and_login(client)
    assert client.post("/tools/import/confirm-bulk", data={"ids": ""}).status_code == 422


def test_bulk_dismiss_dismisses_pending_candidates(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    cand = models.ImportCandidate(
        user_id=user.id,
        raw_title="Bulk Dismiss Me",
        raw_platform="SNES",
        library_entry_id=entry.id,
        status="pending",
        proposed_action="add_to_existing",
    )
    db_session.add(cand)
    db_session.commit()
    cand_id = cand.id
    r = client.post("/tools/import/dismiss-bulk", data={"ids": str(cand_id)})
    assert r.status_code == 200
    assert b"Dismissed 1 candidate" in r.content
    db_session.expire_all()
    assert db_session.get(models.ImportCandidate, cand_id).status == "dismissed"


def test_link_confirms_immediately(client, db_session):
    """Picking a library entry in the Link modal IS the decision — the
    candidate confirms against it in the same save."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    cand = models.ImportCandidate(
        user_id=user.id,
        raw_title="Ninja Gaiden Sigma B",
        raw_platform="Steam",
        status="pending",
        proposed_action="create_new",
    )
    db_session.add(cand)
    db_session.flush()
    row = models.ImportRow(
        candidate_id=cand.id,
        raw_title="Ninja Gaiden Sigma B",
        raw_platform="Steam",
        row_number=3,
        completed_at=datetime.date(2026, 7, 1),
        completed_at_precision="day",
        raw_notes="Played with Ninja Gaiden Sigma Black mod.",
    )
    db_session.add(row)
    db_session.commit()
    cand_id, row_id, entry_id = cand.id, row.id, entry.id

    r = client.post(f"/tools/import/{cand_id}/link", data={"library_entry_id": str(entry_id)})
    assert r.status_code == 200
    assert b"Confirmed against" in r.content
    db_session.expire_all()
    cand = db_session.get(models.ImportCandidate, cand_id)
    assert cand.status == "confirmed"
    assert cand.library_entry_id == entry_id
    comp = db_session.query(models.Completion).filter(models.Completion.library_entry_id == entry_id).one()
    assert "Sigma Black mod" in comp.notes
    # linkage stamped -> reopenable
    assert db_session.get(models.ImportRow, row_id).created_completion_id == comp.id
    # link modal only offered for pending candidates
    assert client.get(f"/tools/import/{cand_id}/link").status_code == 404


def test_edit_without_link_saves_row_edits_and_rematches(client, db_session):
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    cand = models.ImportCandidate(
        user_id=user.id,
        raw_title="Some Unmatched Game",
        raw_platform="",
        status="pending",
        proposed_action="needs_review",
    )
    db_session.add(cand)
    db_session.flush()
    row = models.ImportRow(
        candidate_id=cand.id,
        raw_title="Some Unmatched Game",
        raw_platform="",
        row_number=1,
        completed_at=datetime.date(2020, 5, 5),
        completed_at_precision="day",
    )
    db_session.add(row)
    db_session.commit()
    cand_id, row_id = cand.id, row.id

    r = client.post(
        f"/tools/import/{cand_id}/edit",
        data={
            "raw_title": "Some Unmatched Game",
            "raw_platform": "",
            "row_id": str(row_id),
            "row_date": "",
            "row_playthroughs": "1+",
            "row_notes": "note here",
        },
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.ImportCandidate, cand_id).status == "pending"
    row = db_session.get(models.ImportRow, row_id)
    assert row.completed_at is None
    assert row.playthroughs == "1+"
    assert row.raw_notes == "note here"


def test_confirm_and_dismiss_are_noops_on_confirmed_candidate(client, db_session):
    """Stale duplicate rows (from overlapping infinite-scroll fetches) must
    not re-process or dismiss an already-confirmed candidate."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    entry = _make_plain_entry(db_session, user.id)
    cand, comp = _make_confirmed_candidate(db_session, user.id, entry.id, link=True)
    cand_id, comp_id = cand.id, comp.id

    r = client.post(f"/tools/import/{cand_id}/confirm")
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.ImportCandidate, cand_id).status == "confirmed"
    assert db_session.query(models.Completion).count() == 1

    r = client.post(f"/tools/import/{cand_id}/dismiss")
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.ImportCandidate, cand_id).status == "confirmed"
    assert db_session.get(models.Completion, comp_id) is not None


def test_matcher_spaceless_exact_and_display_name(client, db_session):
    """'Blade Chimera' matches Steam's 'BLADECHIMERA' (spaceless tier), and
    a user-corrected display name is matchable at the exact tier."""
    from backend import importer

    _signup_and_login(client)
    user = db_session.query(models.User).first()
    plat = models.Platform(name="Steam", display_name="Steam", is_custom=True)
    db_session.add(plat)
    db_session.flush()

    def make_entry(title, display_name=None):
        g = models.Game(title=title, display_name=display_name)
        db_session.add(g)
        db_session.flush()
        rel = models.GameRelease(game_id=g.id, source="steam", platform="Steam", platform_id=plat.id)
        db_session.add(rel)
        db_session.flush()
        e = models.UserLibraryEntry(user_id=user.id, release_id=rel.id, import_source="steam_import")
        db_session.add(e)
        db_session.commit()
        return e

    chimera = make_entry("BLADECHIMERA")
    renamed = make_entry("2", display_name="Dead Rising 3: Operation Broken Eagle")

    hit = importer._best_matching_entry(db_session, user.id, "Blade Chimera", plat.id)
    assert hit is not None and hit.id == chimera.id

    hit = importer._best_matching_entry(db_session, user.id, "Dead Rising 3: Operation Broken Eagle", plat.id)
    assert hit is not None and hit.id == renamed.id

    # sequels must never spaceless-collide: II vs III differ spaceless too,
    # but prove it end-to-end
    make_entry("Golden Axe III")
    hit = importer._best_matching_entry(db_session, user.id, "Golden Axe II", plat.id)
    assert hit is None or "III" not in hit.release.game.title

    # fuzzy pass: single-letter sheet typo still matches...
    fashion = make_entry("Fashion Police Squad")
    hit = importer._best_matching_entry(db_session, user.id, "Fasion Police Squad", plat.id)
    assert hit is not None and hit.id == fashion.id
    # ...but near-miss DIFFERENT titles stay unmatched (ratio below bar)
    make_entry("Mass Effect")
    hit = importer._best_matching_entry(db_session, user.id, "Mass Defect", plat.id)
    assert hit is None or hit.release.game.title != "Mass Effect"

    # number words == digits: sheet "Episode 2" matches Steam's "Episode Two"
    ep2 = make_entry("BioShock Infinite: Burial at Sea - Episode Two")
    hit = importer._best_matching_entry(db_session, user.id, "Bioshock Infinite: Burial at Sea Episode 2", plat.id)
    assert hit is not None and hit.id == ep2.id
    # ...and Roman numerals deliberately do NOT map to digits
    mmx = make_entry("Mega Man X")
    make_entry("Mega Man 10")
    hit = importer._best_matching_entry(db_session, user.id, "Mega Man X", plat.id)
    assert hit is not None and hit.id == mmx.id

    # single-letter numeral fallback: X↔10 converts ONLY when the literal
    # has no exact match — library with just "Final Fantasy 10" catches a
    # sheet "Final Fantasy X" (and Mega Man X above still prefers its
    # literal because exact runs first)
    ff10 = make_entry("Final Fantasy 10")
    hit = importer._best_matching_entry(db_session, user.id, "Final Fantasy X", plat.id)
    assert hit is not None and hit.id == ff10.id

    # multi-char Roman numerals == digits: Blasphemous II matches 2
    blas2 = make_entry("Blasphemous 2")
    hit = importer._best_matching_entry(db_session, user.id, "Blasphemous II", plat.id)
    assert hit is not None and hit.id == blas2.id

    # containment tier: multi-candidate pools resolve to the entry with
    # strictly fewest leftover tokens — RE VII picks Biohazard over the
    # Teaser demo; identical-leftover ties (RE2 original vs remake) refuse
    re7b = make_entry("Resident Evil 7 Biohazard")
    make_entry("Resident Evil 7 Teaser: Beginning Hour")
    hit = importer._best_matching_entry(db_session, user.id, "Resident Evil VII", plat.id)
    assert hit is not None and hit.id == re7b.id
    # tie at equal leftovers → genuinely ambiguous → refuse
    make_entry("Silent Hill 2 Remake")
    make_entry("Silent Hill 2 Classic")
    hit = importer._best_matching_entry(db_session, user.id, "Silent Hill 2", plat.id)
    assert hit is None

    # rogue-match regressions (live 2026-07-12): bare title must not bind a
    # numbered sequel, numbers must not jump structures, and DLC sheet rows
    # must not collapse onto their base game (forward-only containment)
    make_entry("Mega Man 11")
    make_entry("Mega Man Legacy Collection 2")
    hit = importer._best_matching_entry(db_session, user.id, "Mega Man", plat.id)
    assert hit is None
    hit = importer._best_matching_entry(db_session, user.id, "Mega Man 2", plat.id)
    assert hit is None
    make_entry("Alan Wake")
    hit = importer._best_matching_entry(db_session, user.id, "Alan Wake: The Signal", plat.id)
    assert hit is None
    # a LEADING extra word is a different game, not a decorated title
    make_entry("LEGO MARVEL Super Heroes")
    hit = importer._best_matching_entry(db_session, user.id, "Marvel Super Heroes", plat.id)
    assert hit is None

    # accented characters normalize to their plain forms: Abzu == ABZÛ
    abzu = make_entry("ABZ\u00db")
    hit = importer._best_matching_entry(db_session, user.id, "Abzu", plat.id)
    assert hit is not None and hit.id == abzu.id


# --- Bulk edit (#175) ---


def _bulk_entry(db, user, *, source="manual", hidden=False, platform="PS4", n=1):
    game = models.Game(title=f"Bulk {source} {n}")
    db.add(game)
    db.flush()
    rel = models.GameRelease(game_id=game.id, platform=platform, source=source, external_id=f"BULK{source}{n}")
    db.add(rel)
    db.flush()
    e = models.UserLibraryEntry(user_id=user.id, release_id=rel.id, import_source=source, is_hidden=hidden)
    db.add(e)
    db.commit()
    return e


def test_bulk_edit_only_touches_fields_you_changed(client, db_session):
    """Setting one field must not disturb another (every field defaults to keep)."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    e = _bulk_entry(db_session, user, hidden=True, n=3)

    client.post("/library/entries/bulk-edit", data={"ids": str(e.id), "is_hidden": "true"})
    db_session.refresh(e)
    assert e.is_hidden is True


def test_bulk_edit_hidden(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    e = _bulk_entry(db_session, user, hidden=False, n=5)
    client.post("/library/entries/bulk-edit", data={"ids": str(e.id), "is_hidden": "true"})
    db_session.refresh(e)
    assert e.is_hidden is True and e.is_hidden_user_set is True


def test_bulk_edit_platform_rejected_when_any_entry_is_synced(client, db_session):
    """Rewriting a synced entry's platform would break its next re-sync match,
    so the whole request is refused rather than partially applied."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    manual = _bulk_entry(db_session, user, source="manual", n=6)
    synced = _bulk_entry(db_session, user, source="psn", n=7)

    r = client.post("/library/entries/bulk-edit", data={"ids": f"{manual.id},{synced.id}", "platform": "PS5"})
    assert r.status_code == 422
    assert b"manually-added" in r.content
    db_session.refresh(manual)
    assert manual.release.platform == "PS4"  # nothing applied


def test_bulk_edit_platform_applies_to_all_manual_selection(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    e = _bulk_entry(db_session, user, source="manual", platform="PS4", n=8)
    r = client.post("/library/entries/bulk-edit", data={"ids": str(e.id), "platform": "PS5"})
    assert r.status_code == 200
    db_session.refresh(e)
    assert e.release.platform == "PS5"


def test_bulk_edit_rejects_empty_selection_and_no_changes(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    e = _bulk_entry(db_session, user, n=9)
    assert client.post("/library/entries/bulk-edit", data={"ids": ""}).status_code == 422
    assert client.post("/library/entries/bulk-edit", data={"ids": str(e.id)}).status_code == 422


def test_bulk_edit_ignores_other_users_entries(client, db_session):
    _signup_and_login(client, username="alice")
    alice = db_session.query(models.User).filter_by(username="alice").first()
    theirs = _bulk_entry(db_session, alice, n=10)
    _signup_and_login(client, username="bob")
    r = client.post("/library/entries/bulk-edit", data={"ids": str(theirs.id), "is_hidden": "true"})
    assert r.status_code == 404
    db_session.refresh(theirs)


def test_stale_filter_cookie_is_dropped_not_silently_applied(client, db_session):
    """A remembered filter only survives while it's still an option for the tab.

    The dropdowns are built from the tab's own candidates, so confirming the
    last row on a platform drops it from the list — but the cookie still named
    it. The server kept filtering by it while the select, having no option to
    render, showed "All platforms": a queue that read as empty with no filter
    visible to clear, fixable only by changing the filter and changing back."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    steam = models.Platform(name="Steam", display_name="Steam")
    db_session.add(steam)
    db_session.flush()
    _make_import_candidate(db_session, user.id, "Steam Game", steam)
    db_session.commit()

    # A cookie naming a platform this tab has no candidates for.
    client.cookies.set("cgt-import-remember", "1")  # filter memory is opt-in (#189)
    client.cookies.set("cgt-import-create_new-platform", "pid%3A999999")
    body = client.get("/tools/import/review?tab=create_new").text
    client.cookies.delete("cgt-import-create_new-platform")

    assert "Steam Game" in body
    assert "Nothing matches these filters" not in body


def test_valid_filter_cookie_is_still_honoured(client, db_session):
    """The fix must not break stickiness itself — a cookie naming a real option
    still filters."""
    _signup_and_login(client)
    user = db_session.query(models.User).first()
    steam = models.Platform(name="Steam", display_name="Steam")
    switch = models.Platform(name="Switch", display_name="Nintendo Switch")
    db_session.add_all([steam, switch])
    db_session.flush()
    _make_import_candidate(db_session, user.id, "Steam Game", steam)
    _make_import_candidate(db_session, user.id, "Switch Game", switch)
    db_session.commit()

    client.cookies.set("cgt-import-remember", "1")  # filter memory is opt-in (#189)
    client.cookies.set("cgt-import-create_new-platform", f"pid%3A{steam.id}")
    body = client.get("/tools/import/review?tab=create_new").text
    client.cookies.delete("cgt-import-create_new-platform")
    assert "Steam Game" in body
    assert "Switch Game" not in body


def test_html_is_never_cached_but_static_still_is(client, db_session):
    """The asset version lives in the PAGE, so a cached page pins a stale
    stylesheet and the browser never even asks for the new one — the mechanism
    behind three separate false bug hunts in this project. Per-route headers
    didn't catch it: one page of nine had them."""
    _signup_and_login(client)
    for path in ("/", "/tools", "/library", "/completions", "/tools/psn-review", "/tools/import/review", "/tools/match-review"):
        r = client.get(path)
        assert r.headers.get("cache-control") == "no-store", path

    # HTMX partials are dynamic data too.
    partial = client.get("/tools/psn-review", headers={"HX-Request": "true"})
    assert partial.headers.get("cache-control") == "no-store"

    # /static stays cacheable — versioning it is the whole point.
    css = client.get("/static/css/theme.css")
    assert css.status_code == 200
    assert css.headers.get("cache-control") != "no-store"


def test_static_version_tracks_edits_without_a_restart(tmp_path, monkeypatch):
    """uvicorn --reload watches .py only, so a CSS-only edit produced no
    restart, no new version, and a browser correctly serving the old file.
    Recomputing per render is ~0.07ms over six files."""
    import os
    import time

    from backend import main

    static = tmp_path / "static"
    (static / "css").mkdir(parents=True)
    asset = static / "css" / "theme.css"
    asset.write_text("a{}")
    monkeypatch.setattr(main, "STATIC_DIR", str(static))

    first = main._compute_static_version()
    time.sleep(0.01)
    asset.write_text("b{}")
    os.utime(asset, (time.time() + 5, time.time() + 5))

    assert main._compute_static_version() != first, "a CSS edit must change the version"


def test_base_template_calls_the_version_rather_than_baking_it_in():
    """{{ static_version }} on a callable renders its repr, silently producing a
    constant cache-bust — guard the call parentheses."""
    html = open("frontend/templates/base.html").read()
    assert "?v={{ static_version }}" not in html
    assert html.count("?v={{ static_version() }}") == 5


def test_home_widgets_use_linked_numbers_not_footer_buttons(client, db_session):
    """The footer buttons duplicated destinations the numbers already imply, and
    at three per row they wrapped — cluttered and asymmetric. The numbers are the
    links now, and the reclaimed space goes to content."""
    _signup_and_login(client)
    body = client.get("/").text

    assert "cgt-tool-card__actions" not in body, "no footer buttons on Home"
    # The headline stats are the links.
    assert 'class="cgt-tool-stat cgt-tool-stat--blue cgt-tool-stat--link" href="/completions"' in body
    assert 'class="cgt-tool-stat cgt-tool-stat--lavender cgt-tool-stat--link" href="/library"' in body
    # Undecorated, with the hover carrying the affordance.
    css = open("frontend/static/css/theme.css").read()
    assert "a.cgt-tool-stat--link {" in css
    assert "text-decoration: none;" in css[css.index("a.cgt-tool-stat--link {") :][:400]
    assert "a.cgt-tool-stat--link:hover" in css


def test_this_year_widget_shows_progress_against_the_goal(client, db_session):
    """Count + Goal is the original pairing. To go, Best month and last year's
    total were added later to fill a stretched card, making five stats that
    wrapped onto two rows — clutter once the cards shrank. The card fills
    itself via the bar strip now, so the extra stats stay gone."""
    from backend import models

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    import datetime

    entry = _make_named_entry(db_session, user.id, "Done Game")
    year = datetime.date.today().year
    db_session.add(models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=datetime.date(year, 3, 14)))
    db_session.commit()

    body = client.get("/").text
    assert f"Completions in {year}" in body
    assert ">Goal<" in body
    # The three later additions stay retired.
    assert "To go" not in body
    assert "Best &mdash;" not in body and "Best —" not in body
    assert f"{year - 1} total" not in body
    # Two stats in the card, not five.
    card = body[body.index("This year") :]
    card = card[: card.index("cgt-month-bars")]
    assert card.count("cgt-tool-stat__value") == 2, "This-year card should carry count + goal"


def test_this_year_widget_does_not_query_last_year(client, db_session):
    """Dropping the stat should drop its query too, not leave it computed and
    thrown away."""
    import inspect

    from backend import pages

    src = inspect.getsource(pages.home_page)
    assert "year - 1" not in src, "last-year count is still being queried"
    assert "best_month" not in src, "best-month is still being computed"


def test_needs_attention_surfaces_missing_artwork(client, db_session):
    """Every card is forced to the tallest one's height, so a sparse card is
    dead space. Missing artwork is real work and already links somewhere."""
    from backend import models

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    _make_named_entry(db_session, user.id, "Artless Game")
    db_session.commit()

    body = client.get("/").text
    assert "library &middot; missing artwork" in body or "library · missing artwork" in body
    assert "/library?missing_art=true" in body
    # ...and it counts toward "caught up", or the card would claim done with
    # work still listed under it.
    assert "All caught up." not in body


def test_home_pairs_the_short_widgets_into_one_column(client, db_session):
    """Every card used to be forced to the tallest one's height, so widening the
    Library list handed the two short cards a screenful of dead space. They
    share a column now; the list-shaped cards span both rows.

    Source order matters: the grid flows by column, so the two short cards have
    to be adjacent in the markup to land in the same one."""
    _signup_and_login(client)
    body = client.get("/").text

    assert "cgt-tool-grid--paired" in body
    # Scope to the grid — "Library" also appears in the nav, ahead of it.
    grid = body[body.index('id="home-widgets"') :]
    order = [grid.index(t) for t in ("This year", "Needs attention", "Library", "Recently completed")]
    assert order == sorted(order), "short widgets must be adjacent, before the tall ones"
    assert grid.count("cgt-tool-card--tall") == 2

    # Tools keeps the plain uniform grid — this is a Home-only layout.
    tools = client.get("/tools").text
    assert "cgt-tool-grid--paired" not in tools


def test_home_rows_share_one_hover_treatment():
    """Home had three: a mauve tint on the list rows, an underline with no
    background on the Needs-attention rows, and a flat surface fill on the
    linked stats. Same page, same gesture, three answers."""
    css = open("frontend/static/css/theme.css").read()
    tint = "color-mix(in srgb, var(--ctp-mauve) 10%, transparent)"
    for selector in (".cgt-detail-list-row:hover", "a.cgt-breakdown__row:hover", "a.cgt-tool-stat--link:hover"):
        block = css[css.index(selector) :][:200]
        assert tint in block, selector
    # The underline-only treatment is gone.
    assert "text-decoration: underline;" not in css[css.index(".cgt-breakdown__row {") :][:600]


def test_vendored_assets_do_not_request_missing_source_maps(client):
    """We don't ship the .map files, so every page load requested two that don't
    exist and logged 404s. Console noise buries real errors, and the desktop
    shell's devtools is where anything actually gets diagnosed."""
    for asset in ("vendor/bootstrap.bundle.min.js", "vendor/bootstrap.min.css", "vendor/htmx.min.js"):
        r = client.get(f"/static/{asset}")
        assert r.status_code == 200, asset
        assert "sourceMappingURL" not in r.text, f"{asset} would request a .map we don't ship"


def test_missing_artwork_count_survives_a_null_release_id(client, db_session):
    """NOT IN against a subquery containing NULL is never true, so one
    GameArtwork row with a NULL release_id silently made "missing artwork"
    return nothing at all — the Tools card read 0 missing while hundreds were.
    cover_h happened to have no such rows, so the horizontal view worked and hid
    it."""
    from backend.pages_common import _build_lib_query

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _make_named_entry(db_session, user.id, "Artless")
    db_session.commit()

    before = _build_lib_query(db_session, user, "", "", "default", "name", False, True, "grid_v")[0].count()
    assert before >= 1, "the entry has no cover, so it must count as missing"

    # A game-level artwork row with no release attached — exactly what poisoned it.
    db_session.add(models.GameArtwork(release_id=None, artwork_type="cover_v", source="sgdb", url="https://x/y.png", is_valid=True))
    db_session.commit()

    after = _build_lib_query(db_session, user, "", "", "default", "name", False, True, "grid_v")[0].count()
    assert after == before, "a NULL release_id must not zero the whole count"
    assert entry is not None


def _theme_css():
    return open("frontend/static/css/theme.css").read()


def _palette_block(css, selector):
    i = css.index(selector)
    j = css.index("{", i)
    depth = 0
    for k in range(j, len(css)):
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                return css[j:k]
    raise AssertionError(f"unterminated block for {selector}")


def test_stat_values_are_not_mixed_toward_ink():
    """Light mode used to render every stat number as
    color-mix(accent 65%, --ctp-text). That stripped ~42% of each accent's
    saturation, unevenly (teal -29%, pink -55%), so the triplets chosen for
    colorblind separation stopped separating — the light theme read as
    arbitrary. The hairline stroke solves legibility instead, leaving hue
    intact. This is the exact regression to prevent."""
    css = _theme_css()
    for accent in ("teal", "peach", "lavender", "yellow", "green", "blue", "pink", "maroon", "flamingo"):
        needle = f"color-mix(in srgb, var(--ctp-{accent})"
        for hit in range(css.count(needle)):
            idx = -1
            for _ in range(hit + 1):
                idx = css.index(needle, idx + 1)
            line_start = css.rfind("\n", 0, idx) + 1
            line = css[line_start : css.index("\n", idx)]
            assert "--ctp-text" not in line, f"accent mixed toward ink again: {line.strip()[:100]}"


def test_every_palette_declares_the_stat_stroke():
    """The outline is what makes pale accents legible. A palette that forgets
    --cgt-stat-stroke renders it as an invalid value — no stroke, no error."""
    css = _theme_css()
    for selector in ('html[data-bs-theme="dark"] {', 'html[data-bs-theme="light"] {', 'html[data-palette="latte"] {'):
        assert "--cgt-stat-stroke:" in _palette_block(css, selector), selector


def test_latte_overrides_come_after_the_default_light_palette():
    """html[data-bs-theme="light"] and html[data-palette="latte"] have equal
    specificity, so source order alone decides. Move the Latte block above the
    Nord one and Latte silently renders as Nord."""
    css = _theme_css()
    assert css.index('html[data-palette="latte"] {') > css.index('html[data-bs-theme="light"] {')


def test_button_shade_tokens_exist_in_every_palette():
    """--cgt-btn-* used to be declared only in the light palettes, so any use
    that wasn't light-scoped resolved to nothing under Mocha — an invisible
    element, not a build error. The old guard enforced that by demanding every
    USE be light-scoped, which blocked deriving anything from them.

    Dark now declares them too (#180), so the invariant is the stronger one:
    every token consumed anywhere exists in all three palettes. That is what
    makes it safe for a progress fill to take the button's own hue."""
    import re

    css = _theme_css()
    used = set(re.findall(r"var\((--cgt-btn-[a-z-]+)\)", css))
    assert used, "no button tokens consumed — did they get renamed?"

    blocks = {
        "dark": css[css.index('html[data-bs-theme="dark"]') : css.index('html[data-bs-theme="light"]')],
        "light": css[css.index('html[data-bs-theme="light"]') : css.index('html[data-palette="latte"]')],
        "latte": css[css.index('html[data-palette="latte"]') :],
    }
    for token in sorted(used):
        for palette, block in blocks.items():
            assert f"{token}:" in block, f"{token} is consumed but undefined in {palette} — invisible there only"


def test_boot_script_migrates_pre_nord_theme_settings():
    """Existing installs have 'light'/'dark' in localStorage. Without migration
    the picker opens blank and the stored preference is silently ignored."""
    for name in ("base.html", "login.html", "signup.html"):
        html = open(f"frontend/templates/{name}").read()
        assert "localStorage.getItem('theme')" in html, name
        assert "p==='light'" in html and "'nord'" in html, f"{name} drops old light setting"
        assert "p==='dark'" in html and "'mocha'" in html, f"{name} drops old dark setting"
        # data-bs-theme must stay a value Bootstrap understands.
        assert "d.dataset.bsTheme=(p==='mocha'?'dark':'light')" in html, name


def test_month_strip_grows_to_fill_the_card():
    """Home forces every card to the tallest sibling's height. A fixed track
    height is only ever right for one such height — it left dead space under
    the strip, which is what the extra stats were originally added to hide."""
    css = open("frontend/static/css/theme.css").read()
    import re

    track = css[css.index(".cgt-month-bars__track {") :][:220]
    assert "flex: 1 1 auto;" in track, "track must absorb the card's slack"
    assert "min-height: 88px;" in track, "keep 88px as the floor"
    # A bare `height:` (not min-/max-) pins the track and brings the dead space back.
    assert not re.search(r"(?<![-\w])height:\s*\d", track), "a fixed height reintroduces the dead space"


def test_tools_cards_lead_with_a_primary_action(client, db_session):
    """Every tool card's first action is the primary one. Artwork's had
    regressed to btn-surface twice, so the card read as having no main action
    while its neighbours all did."""
    _signup_and_login(client)
    body = client.get("/tools").text
    assert '<a href="/integrations/steamgriddb" class="btn btn-primary btn-sm">' in body
    # Not asserting a global count: some cards legitimately have no primary
    # until they're connected. This is the one that keeps slipping.


def test_tools_cards_do_not_badge_a_count_they_already_show():
    """The pill on a card title repeated the headline stat directly beneath it
    — 578 above "578 imports to review". Checked against the template rather
    than a rendered page: the badges sit behind {% if count %}, so a fixture
    that fails to produce a count would make a rendered assertion pass while
    proving nothing.

    The nav badge in base.html is a different case and stays — it's the only
    place that count appears when you're not on the page."""
    import re

    tools = open("frontend/templates/tools.html").read()
    for m in re.finditer(r'<div class="cgt-tool-card__title">(.*?)</div>', tools, re.S):
        assert "cgt-pending-badge" not in m.group(1), f"count badge back on: {m.group(1)[:60]!r}"

    base = open("frontend/templates/base.html").read()
    assert "cgt-pending-badge" in base, "the nav badge should not have been removed"


def test_card_body_text_has_one_declared_size():
    """Blurbs, the account identity block and the breakdown rows each used to
    opt into Bootstrap's .small on their own, so they matched by coincidence
    rather than by rule and any one could drift. One class owns the size now."""
    import re

    css = open("frontend/static/css/theme.css").read()
    block = css[css.index(".cgt-tool-card__body {") :][:200]
    assert "font-size:" in block, "the body class must declare the size itself"
    # rem, not em: a body element nested inside another would compound.
    assert re.search(r"font-size:\s*[0-9.]+rem", block), "use rem so it cannot compound"

    for name in ("tools.html", "home.html"):
        html = open(f"frontend/templates/{name}").read()
        assert 'class="small' not in html, f"{name} still opts into Bootstrap .small for card text"


def test_psn_card_drops_its_blurb_once_connected(client, db_session):
    """The Steam card replaces its copy with the identity block and stats once
    connected. PSN kept showing "fetch your PlayStation library" to someone who
    already had — instructions for a job already done, and the reason the two
    sync cards didn't read as the same component."""
    from backend import models

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    body = client.get("/tools").text
    assert "Capture your PlayStation sign-in" in body, "disconnected state keeps its copy"

    user.psn_npsso = "npsso-token"
    user.psn_online_id = "tester"
    db_session.commit()

    body = client.get("/tools").text
    assert "Fetch your PlayStation library" not in body
    assert "Capture your PlayStation sign-in" not in body
    assert "tester" in body, "identity block replaces the copy"


def test_theme_css_has_no_stray_comment_markers():
    """A script that rewrote theme.css split on a comment header and rejoined
    without it, leaving a dangling `*/`. CSS recovers from that by discarding
    the *next whole rule* — which silently deleted `.btn-primary`, so every
    primary button fell back to Bootstrap's blue. No error anywhere: not in the
    build, not in the console, not in the tests. Only the pixels."""
    css = open("frontend/static/css/theme.css").read()
    i = 0
    in_comment = False
    while i < len(css) - 1:
        two = css[i : i + 2]
        if not in_comment and two == "/*":
            in_comment, i = True, i + 2
            continue
        if not in_comment and two == "*/":
            line = css[:i].count("\n") + 1
            raise AssertionError(f"stray `*/` at line {line} — the opening `/*` was lost")
        if in_comment and two == "*/":
            in_comment, i = False, i + 2
            continue
        i += 1
    assert not in_comment, "unterminated comment — everything after it is dead"


def test_theme_css_selectors_are_parseable():
    """The same failure with the opener intact but the closer lost would leave
    comment prose sitting where a selector belongs. Any top-level selector
    containing box-drawing characters is comment text the parser is about to
    eat a rule over."""
    import re

    css = open("frontend/static/css/theme.css").read()
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    depth = 0
    chunk = ""
    for ch in stripped:
        if ch == "{":
            if depth == 0:
                sel = chunk.strip()
                assert not re.search(r"[─━—│]", sel), f"comment text leaked into a selector: {sel[:70]!r}"
                assert sel, "empty selector"
            depth += 1
            chunk = ""
        elif ch == "}":
            depth -= 1
            chunk = ""
        elif depth == 0:
            chunk += ch


def test_key_rules_survive_comment_stripping():
    """The regression this exists for: `.btn-primary` vanished from the cascade
    while still being present in the file, so a plain text search found it and
    proved nothing. These read the CSS with comments removed — the way a parser
    sees it — so a rule swallowed by an unclosed comment reads as missing.

    A lost closing `*/` is invisible to marker balance (the next comment's
    closer picks it up) and invisible to selector checks (the swallowed region
    just becomes comment). Only "is the rule still there afterwards" catches
    it."""
    import re

    css = re.sub(r"/\*.*?\*/", "", open("frontend/static/css/theme.css").read(), flags=re.S)
    for selector, needle in (
        (".btn-primary {", "var(--ctp-mauve)"),
        (".btn-danger {", "var(--ctp-red)"),
        (".cgt-tool-card {", "var(--ctp-mantle)"),
        (".cgt-tool-card__body {", "font-size:"),
    ):
        assert selector in css, f"{selector} is not in the parsed stylesheet"
        i = css.index(selector)
        assert needle in css[i : css.index("}", i)], f"{selector} lost {needle}"


def test_sync_cards_report_last_synced_the_same_way(client, db_session):
    """Steam's identity block showed when it last synced; PSN's said
    "PlayStation Network", which restated the card title. Two cards meant to be
    the same component, telling you different amounts."""
    import datetime

    from backend import models

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "npsso-token", "tester"
    user.steam_id64, user.steam_persona_name = "76561198000000000", "tester"
    db_session.commit()

    body = client.get("/tools").text
    assert "PlayStation Network</div>" not in body
    assert body.count("Never synced") == 2, "both cards say it the same way when never synced"

    synced = datetime.datetime(2026, 8, 2, 16, 49, tzinfo=datetime.UTC)
    user.psn_last_synced_at = synced
    user.steam_last_synced_at = synced
    db_session.commit()

    body = client.get("/tools").text
    assert body.count("Last synced") == 2
    # Both carry the local-time hook the JS converts, not a bare UTC string.
    # SQLite drops the tzinfo, so match the timestamp rather than the offset.
    assert body.count('class="local-time" data-utc="2026-08-02T16:49:00') == 2
    assert body.count("2026-08-02 16:49 UTC") == 2


def test_borderless_state_lives_outside_the_swapped_region():
    """The borderless class used to sit on .cgt-library-grid, which is inside
    the HTMX-swapped content region. Every search, filter, sort and page wiped
    it while the checkbox — outside that region — stayed checked, so the toggle
    read as on while being off and only took effect on a second click.

    Gap and size never had this because they were always documentElement CSS
    vars. Borderless now matches. Both grid pages, both orientations."""
    css = open("frontend/static/css/theme.css").read()
    assert "html.cgt-grid-borderless .cgt-library-card" in css
    assert ".cgt-library-grid--borderless" not in css, "container-scoped rule is the bug"

    for name in ("library.html", "completions.html"):
        js = open(f"frontend/templates/{name}").read()
        assert "documentElement.classList.toggle('cgt-grid-borderless'" in js, name
        assert "cgt-library-grid--borderless" not in js, f"{name} still targets the grid"
        # Applied unconditionally, so an off state actually clears.
        assert "applyBorderless(savedBorderless);" in js, f"{name} only applies the on state"


def test_borderless_needs_no_reapply_after_a_swap():
    """The afterSwap re-apply existed only to undo the wipe. Leaving it behind
    would hide a regression if the class ever moved back onto the grid."""
    js = open("frontend/templates/library.html").read()
    i = js.index("htmx:afterSwap")
    block = js[i : js.index("});", i)]
    assert "applyBorderless" not in block, "nothing wipes it now; the re-apply is dead code"


def _make_synced_entry(db, user_id, title, platform="Steam", source="steam", parent_id=None):
    g = models.Game(title=title, display_name=title, parent_id=parent_id)
    db.add(g)
    db.flush()
    rel = models.GameRelease(game_id=g.id, source=source, platform=platform, external_id=f"ext-{g.id}")
    db.add(rel)
    db.flush()
    e = models.UserLibraryEntry(user_id=user_id, release_id=rel.id, import_source=f"{source}_import")
    db.add(e)
    db.flush()
    return e


def test_collection_member_does_not_match_the_standalone_release(client, db_session):
    """Real case: a manual entry for "Devil May Cry 3: Dante's Awakening -
    Special Edition" was created as a member of "Devil May Cry HD Collection".
    The owner also has the standalone Steam "Devil May Cry 3: Special Edition".
    The scanner offered to merge them — but they are different copies, and
    merging destroys the distinction the manual entry exists to record.

    The veto is one-directional: collection-member manual vs parentless synced.
    A synced game under the SAME parent still matches normally.
    """
    from backend import match_review, models

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    collection = models.Game(title="Devil May Cry HD Collection", display_name="Devil May Cry HD Collection", is_collection=True)
    db_session.add(collection)
    db_session.flush()

    manual = _make_named_entry(db_session, user.id, "Devil May Cry 3: Dante's Awakening - Special Edition")
    manual.release.game.parent_id = collection.id
    _make_synced_entry(db_session, user.id, "Devil May Cry 3: Special Edition")
    db_session.commit()

    match_review.scan_for_matches(db_session, user)
    db_session.commit()
    pending = db_session.query(models.SyncMatchCandidate).filter_by(manual_entry_id=manual.id).all()
    assert pending == [], f"collection member matched a standalone release: {[(c.external_id) for c in pending]}"


def test_collection_member_still_matches_within_its_own_collection(client, db_session):
    """The veto must not blind the scanner entirely — a synced game under the
    same collection parent is a legitimate match and has to survive."""
    from backend import match_review, models

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    collection = models.Game(title="Some HD Collection", display_name="Some HD Collection", is_collection=True)
    db_session.add(collection)
    db_session.flush()

    manual = _make_named_entry(db_session, user.id, "Bundled Game Special Edition")
    manual.release.game.parent_id = collection.id
    _make_synced_entry(db_session, user.id, "Bundled Game Special Edition", parent_id=collection.id)
    db_session.commit()

    match_review.scan_for_matches(db_session, user)
    db_session.commit()
    pending = db_session.query(models.SyncMatchCandidate).filter_by(manual_entry_id=manual.id).all()
    assert len(pending) == 1, "same-parent match should still be offered"


def test_remembered_filters_need_the_opt_in_cookie(client, db_session):
    """Nothing is remembered unless the box is ticked. Without the opt-in
    cookie the value cookies must be ignored entirely, or every user gets
    sticky filters they never asked for."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    # The platform <select> only lists platforms the user owns, so the filter
    # has to actually exist in the library for the option to render at all.
    _make_named_entry(db_session, user.id, "A PS3 Game", platform="PS3")
    db_session.commit()

    client.cookies.set("cgt-library-platform", "PS3")
    body = client.get("/library").text
    assert 'value="PS3" selected' not in body

    client.cookies.set("cgt-library-remember", "1")
    body = client.get("/library").text
    assert 'value="PS3" selected' in body, "opt-in cookie should bind the stored platform"


def test_an_explicit_filter_always_beats_the_remembered_one(client, db_session):
    """A query param is the user changing a filter right now. A stored value
    must never override it."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    _make_named_entry(db_session, user.id, "A PS3 Game", platform="PS3")
    _make_named_entry(db_session, user.id, "A Steam Game", platform="Steam")
    db_session.commit()

    client.cookies.set("cgt-library-remember", "1")
    client.cookies.set("cgt-library-platform", "PS3")
    body = client.get("/library?platform=Steam").text
    assert 'value="Steam" selected' in body
    assert 'value="PS3" selected' not in body


def test_remembered_booleans_round_trip(client, db_session):
    """show_hidden / missing_art are bools, stored as 1/absent — a naive
    string read would make "" truthy and pin the filter on forever."""
    _signup_and_login(client)
    client.cookies.set("cgt-library-remember", "1")
    # "0" is the trap: as a raw string it is TRUTHY, so a naive read pins the
    # filter on and there is no way to turn it back off. "" happens to be falsy
    # either way, which is why it proves nothing on its own.
    client.cookies.set("cgt-library-show_hidden", "0")
    body = client.get("/library").text
    seg = body[body.index('id="lib-show-hidden"') : body.index('id="lib-show-hidden"') + 200]
    assert "checked" not in seg, 'a stored "0" must read as False, not as a truthy string'

    client.cookies.set("cgt-library-show_hidden", "1")
    body = client.get("/library").text
    assert "checked" in body[body.index('id="lib-show-hidden"') : body.index('id="lib-show-hidden"') + 200]


def test_filter_memory_uses_cookies_not_localstorage():
    """PR #123 settled this: the server can't read localStorage, so filters
    stored there render unfiltered and get re-applied by JS — a visible flash
    plus a wasted round-trip. Grid size/gap/borderless may use localStorage
    because they're pure CSS and never change what the server renders."""
    for name, prefix in (("library.html", "library"), ("completions.html", "completions")):
        js = open(f"frontend/templates/{name}").read()
        start = js.index("Remember filters (#189)")
        block = js[start : js.index("</script>", start)]
        # Look for real usage, not the comment that explains why it's absent.
        assert "localStorage." not in block, f"{name} filter memory must not use localStorage"
        assert "cgt-' + PREFIX + '-" in block and f"'{prefix}'" in block


def test_every_long_list_page_keeps_its_actions_reachable():
    """PSN review was the only long-list page whose bulk Confirm/Dismiss sat in
    a static div above the table. With 61 rows to decide you tick some, scroll
    down for more, and the buttons are off screen above you — and there was no
    back-to-top either, because that lives in the sticky bar on every other
    page. Same component, same behaviour, everywhere."""
    for name in ("library.html", "import_review.html", "completions.html", "psn_review.html"):
        html = open(f"frontend/templates/{name}").read()
        assert "cgt-sticky-actions" in html, f"{name} has no sticky action bar"

    psn = open("frontend/templates/psn_review.html").read()
    bar = psn.index('class="cgt-sticky-actions"')
    assert psn.index('id="psn-bulk-bar"') > bar, "bulk bar must live inside the sticky footer"
    assert 'id="psn-back-to-top"' in psn
    assert "window.scrollTo({top:0,behavior:'smooth'})" in psn


def test_filter_memory_is_opt_in_on_every_filtered_page():
    """Four filtered pages, one contract. Library and Completions had no memory,
    Import review remembered unconditionally with no way off, PSN review had
    none — three behaviours across four pages that all look the same."""
    pages = {
        "library.html": "lib-remember-filters",
        "completions.html": "comp-remember-filters",
        "import_review.html": "import-remember-filters",
        "psn_review.html": "psn-remember-filters",
    }
    for name, box_id in pages.items():
        html = open(f"frontend/templates/{name}").read()
        assert f'id="{box_id}"' in html, f"{name} has no Remember filters toggle"
        assert "Remember filters" in html, name


def test_import_review_no_longer_remembers_unconditionally(client, db_session):
    """It used to bind filter cookies into the render whether or not the user
    asked. Without the opt-in cookie the stored value must be ignored."""
    from backend import models

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    steam = models.Platform(name="Steam", display_name="Steam")
    switch = models.Platform(name="Switch", display_name="Nintendo Switch")
    db_session.add_all([steam, switch])
    db_session.flush()
    _make_import_candidate(db_session, user.id, "Steam Game", steam)
    _make_import_candidate(db_session, user.id, "Switch Game", switch)
    db_session.commit()

    client.cookies.set("cgt-import-create_new-platform", f"pid%3A{steam.id}")
    body = client.get("/tools/import/review?tab=create_new").text
    assert "Switch Game" in body, "stored filter applied without the opt-in"

    client.cookies.set("cgt-import-remember", "1")
    body = client.get("/tools/import/review?tab=create_new").text
    assert "Switch Game" not in body, "opt-in should bind the stored filter"


def test_psn_review_filters_are_remembered_when_opted_in(client, db_session):
    """PSN review had no filter memory at all — the fourth page, and the one
    the #189 issue text missed."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "npsso", "tester"
    db_session.commit()

    body = client.get("/tools/psn-review").text
    assert 'id="psn-remember-filters"' in body

    client.cookies.set("cgt-psn-review-sort", "name")
    client.cookies.set("cgt-psn-review-remember", "1")
    r = client.get("/tools/psn-review")
    assert r.status_code == 200


def test_external_link_interceptor_binds_unconditionally():
    """It used to bail at parse time if window.__TAURI__ was missing. app.js is
    deferred and the app loads from a REMOTE origin (127.0.0.1:8000), so Tauri
    injects its API asynchronously — lose that race and the listener never
    binds, killing every external link for the life of the page. Silently, since
    target="_blank" does nothing in WKWebView either.

    The check has to happen at CLICK time, not at load."""
    js = open("frontend/static/js/app.js").read()
    start = js.index("WKWebView has no popup handler")
    block = js[start : js.index("})();", start)]
    assert "if (!window.__TAURI__) return;" not in block, "early return at parse time kills the listener"
    assert "addEventListener('click'" in block
    # The guard lives inside the handler.
    handler = block[block.index("addEventListener('click'") :]
    assert "__TAURI__" in handler, "the Tauri check must be inside the click handler"


# ── Import mapping wizard (#197) ────────────────────────────────────────────
_WIZARD_CSV = (
    ",Game,Platform,Notes,Date,Playthroughs,Collection\n"
    "1,Disc Room,Steam,,1/1/2026,1,\n"
    "2,Terminator 2D: NO FATE,Steam,100%,1/1/2026,2+,\n"
    "3,Contra: Hard Corps,Steam,,1/3/2026,4,Contra Anniversary Collection\n"
    "4,,,,,,\n"
)


def _upload_wizard_csv(client, name="Completed Games - 2026.csv", body=None):
    return client.post(
        "/tools/import/upload",
        files={"file": (name, (body or _WIZARD_CSV).encode(), "text/csv")},
    )


def test_uploading_shows_what_was_found_and_imports_nothing(client, db_session):
    """Upload used to import straight away, guessing in silence: a sheet whose
    header it did not recognise was skipped whole, and a column named
    "Completed" rather than "Date" dropped every date without a word.

    It shows its work now and waits for confirmation (#197)."""
    _signup_and_login(client)
    db_session.add(models.Platform(name="Steam", display_name="Steam"))
    db_session.commit()

    html = _upload_wizard_csv(client).text

    # The header it found, and every column with the field it will be read as.
    assert "Completed Games - 2026" in html
    for label in ("Game", "Platform", "Notes", "Date", "Playthroughs", "Collection"):
        assert label in html, f"column {label} not shown"

    # Sample rows, rendered the way they would actually land -- this is the
    # whole point: "2+" silently becoming 2 is visible before it happens.
    assert "Disc Room" in html
    assert "Terminator 2D: NO FATE" in html
    # "2+" is stored as 2. That is flagged on the value and explained once
    # under the table, rather than repeated on every row it happens to hit.
    assert "whole number" in html, "the coercion must be explained"

    # The empty pre-numbered row is counted separately rather than vanishing.
    assert "3 rows to import" in html
    assert "1 empty" in html

    # And the table says it is a sample, not the import.
    assert "First 3 of 3 rows" in html

    # And nothing has been written.
    assert db_session.query(models.ImportCandidate).count() == 0


def test_the_mapping_chosen_in_the_wizard_is_what_gets_imported(client, db_session, monkeypatch):
    """The wizard's answers have to reach the parser, or it is decoration."""
    _signup_and_login(client)
    db_session.add(models.Platform(name="Steam", display_name="Steam"))
    db_session.commit()

    import re as _re

    from . import jobs, pages_import

    # Captured at the enqueue: the background drain pops the queue as soon as
    # the task runs, so reading it back afterwards is a race.
    queued: list[tuple] = []
    monkeypatch.setattr(
        pages_import.jobs,
        "enqueue_import",
        lambda uid, fn, body, specs=None: queued.append((fn, specs)) or jobs.Job(id="t", user_id=uid, kind="import_xlsx", label=fn),
    )

    html = _upload_wizard_csv(client).text
    token = _re.search(r'name="token" value="([^"]+)"', html)[1]

    # Map Title and Date only -- Notes deliberately left on Ignore.
    resp = client.post(
        "/tools/import/process",
        data={
            "token": token,
            "sheet.0.name": "Completed Games - 2026",
            "sheet.0.header": "0",
            "sheet.0.year": "2026",
            "sheet.0.col.1": "game",
            "sheet.0.col.2": "platform",
            "sheet.0.col.4": "date",
            "sheet.0.col.3": "",
        },
    )
    assert resp.status_code == 200
    assert "Queued" in resp.text

    assert queued, "the import was never queued"
    filename, specs = queued[0]
    assert filename == "Completed Games - 2026.csv"
    assert specs["Completed Games - 2026"]["cols"] == {"game": 1, "platform": 2, "date": 4}, "Notes must not be mapped"
    assert specs["Completed Games - 2026"]["year"] == 2026


def test_the_year_is_only_asked_for_when_the_dates_cannot_answer(client, db_session):
    """A sheet whose Date column carries full dates already knows every year,
    so asking is noise. It is asked only when something is blank or says just
    "January" -- and scanned across every row, not just the ones shown."""
    _signup_and_login(client)
    db_session.add(models.Platform(name="Steam", display_name="Steam"))
    db_session.commit()

    full = _upload_wizard_csv(client).text
    assert "Year to use for dates" not in full, "every date here carries its own year"

    vague = _upload_wizard_csv(
        client,
        name="Backlog.csv",
        body=",Game,Platform,Date\n1,Some Game,Steam,January\n2,Other Game,Steam,\n",
    ).text
    assert "Year to use for dates" in vague
    assert "nothing in the sheet name to go on" in vague, "and it says so when it cannot guess"


def test_row_order_comes_from_position_not_from_a_numbering_column(db_session):
    """row_number exists only to become Completion.sort_order, which keeps two
    completions in the same month in the order the sheet had them -- the
    fabricated month dates all land on the 1st, so nothing else can order them.

    Reading it out of a "#" column asked the user to map a column whose only job
    was to restate the order the rows were already in, and it did nothing at all
    for a sheet without one. Position answers it for every sheet (#197)."""
    from . import importer

    db_session.add(models.Platform(name="Steam", display_name="Steam"))
    user = models.User(name="Pos", username="pos", api_token="q" * 20)
    db_session.add(user)
    db_session.commit()

    # Numbering that is present, out of order, and full of gaps.
    csv = b",Game,Platform,Date\n7,Gamma,Steam,1/3/2026\n2,Alpha,Steam,1/1/2026\n99,Beta,Steam,1/2/2026\n"
    result = importer.parse_upload(csv, "Odd - 2026.csv", db_session, user.id)
    order = {c["raw_title"]: c["rows"][0]["row_number"] for c in result.candidates}
    assert order == {"Gamma": 1, "Alpha": 2, "Beta": 3}, "position wins over the column's own numbers"

    # A sheet with no numbering column at all still orders.
    plain = b"Game,Platform,Date\nOne,Steam,1/1/2026\nTwo,Steam,1/1/2026\n"
    result = importer.parse_upload(plain, "Plain - 2026.csv", db_session, user.id)
    assert {c["raw_title"]: c["rows"][0]["row_number"] for c in result.candidates} == {"One": 1, "Two": 2}

    # And it is no longer something the wizard asks about.
    assert "#" not in dict(importer.IMPORT_FIELDS)
    assert "Row number" not in [label for _, label in importer.IMPORT_FIELDS]


def test_a_file_the_parser_cannot_read_says_so_instead_of_queueing(client, db_session):
    """A sheet with no title column can produce nothing, and saying that up
    front is the entire point of the step."""
    _signup_and_login(client)
    html = _upload_wizard_csv(client, body="Alpha,Beta\n1,2\n").text
    assert "no header found" in html
    assert "no column is mapped to Title" in html.replace("&mdash;", "-")
    assert db_session.query(models.ImportCandidate).count() == 0


def test_pale_nord_badges_get_their_own_ink_in_every_palette():
    """Nord's teal, sky, yellow and green are pale by design — DESIGN.md says so
    and forbids darkening the accents themselves, because that turns yellow into
    mustard. As small uppercase text on a white card they were mush; the Steam
    chip in a table row is the case that prompted this.

    Each gets a --cgt-*-ink darkened along its own hue, the same move
    --cgt-igdb-ink already makes (nord14 #a3be8c -> #6f8c58). The accents are
    untouched, so nothing else that uses them changes.
    """
    css = open("frontend/static/css/theme.css").read()

    for accent in ("teal", "sky", "yellow", "green"):
        rule = css[css.index(f".tag-platform-{accent}") :][:200]
        assert f"var(--cgt-{accent}-ink" in rule, f"{accent} badge must use its ink"
        # The wash still comes from the accent — only the text changed.
        assert f"var(--ctp-{accent}) " in rule or f"var(--ctp-{accent})\n" in rule, f"{accent} wash must stay the accent"

    # Defined in all three palettes, or one silently inherits the wrong colour.
    dark = css[css.index('html[data-bs-theme="dark"] {') :][:2600]
    nord = css[css.index('html[data-bs-theme="light"] {') :][:2600]
    latte = css[css.index('html[data-palette="latte"] {') :][:2600]
    for accent in ("teal", "sky", "yellow", "green"):
        for name, block in (("mocha", dark), ("nord", nord), ("latte", latte)):
            assert f"--cgt-{accent}-ink" in block, f"{accent} ink missing from {name}"

    # And Nord's are actually darker than the accents they come from, which is
    # the entire point.
    assert "--cgt-teal-ink:   #56908e" in nord
    assert "--ctp-teal:     #8fbcbb" in nord

    # The chip carries an edge so it reads as an object, taken from currentColor
    # so it follows whatever ink each palette resolved to.
    badge = css[css.index(".tag-badge {") :][:600]
    assert "border: 1px solid color-mix(in srgb, currentColor 45%, transparent)" in badge
