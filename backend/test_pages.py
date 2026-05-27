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
    assert b"Other completions of this game" in r.content
    assert f"/completions/{c1.id}/detail".encode() in r.content
    assert f"/completions/{c3.id}/detail".encode() in r.content
    # c2 itself shouldn't be in the others list
    assert f"/completions/{c2.id}/detail".encode() not in r.content


def test_completion_detail_single_completion_no_others_section(client, db_session):
    import datetime as _dt

    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user, title="One Shot")
    comp = models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=_dt.date(2026, 1, 1))
    db_session.add(comp)
    db_session.commit()

    r = client.get(f"/completions/{comp.id}/detail")
    assert b"Other completions of this game" not in r.content


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


def test_detail_pane_provides_parent_cover_fallback_for_dlc(client, db_session):
    """When a DLC's pane is rendered, fallback_header_url points at the parent
    game's Steam header. The template uses this as the cover img onerror swap."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    # Parent game with a Steam release + header artwork
    parent = models.Game(title="Elden Ring Nightreign")
    db_session.add(parent)
    db_session.flush()
    parent_rel = models.GameRelease(game_id=parent.id, platform="Steam", source="steam", external_id="3000000")
    db_session.add(parent_rel)
    db_session.flush()
    parent_art = models.GameArtwork(
        release_id=parent_rel.id,
        artwork_type="header",
        source="steam",
        url="https://cdn.akamai.steamstatic.com/steam/apps/3000000/header.jpg",
    )
    db_session.add(parent_art)

    # DLC linked to that parent, plus its own (broken) header
    dlc = models.Game(title="The Forsaken Hollows", is_dlc=True, parent_id=parent.id)
    db_session.add(dlc)
    db_session.flush()
    dlc_rel = models.GameRelease(game_id=dlc.id, platform="Steam", source="steam", external_id="3000001")
    db_session.add(dlc_rel)
    db_session.flush()
    db_session.add(
        models.GameArtwork(
            release_id=dlc_rel.id,
            artwork_type="header",
            source="steam",
            url="https://cdn.akamai.steamstatic.com/steam/apps/3000001/header.jpg",
        )
    )
    dlc_entry = models.UserLibraryEntry(user_id=user.id, release_id=dlc_rel.id, import_source="steam_import")
    db_session.add(dlc_entry)
    db_session.commit()

    r = client.get(f"/library/entries/{dlc_entry.id}/detail")
    assert r.status_code == 200
    # Own header rendered as src
    assert b"3000001/header.jpg" in r.content
    # Parent's header surfaced as data-fallback so onerror can swap to it
    assert b"3000000/header.jpg" in r.content
    assert b"data-fallback=" in r.content


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
    assert b"cgt-library-card" not in r.content


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
            artwork_type="cover",
            source="steam",
            url="https://example.com/100/library_600x900.jpg",
        )
    )
    db_session.add(
        models.GameArtwork(
            release_id=release.id,
            artwork_type="header",
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


def test_cover_url_override_h_takes_precedence_in_detail_pane(client, db_session):
    """When cover_url_override_h is set, the detail pane uses it as the header."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    game = models.Game(title="Cover Override Test")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="Steam", source="steam", external_id="900")
    db_session.add(release)
    db_session.flush()
    db_session.add(
        models.GameArtwork(
            release_id=release.id,
            artwork_type="header",
            source="steam",
            url="https://cdn.example.com/steam-header.jpg",
        )
    )
    entry = models.UserLibraryEntry(
        user_id=user.id,
        release_id=release.id,
        import_source="steam_import",
        cover_url_override_h="https://sgdb.example.com/custom-header.jpg",
    )
    db_session.add(entry)
    db_session.commit()

    r = client.get(f"/library/entries/{entry.id}/detail")
    assert r.status_code == 200
    assert b"sgdb.example.com/custom-header.jpg" in r.content
    assert b"cdn.example.com/steam-header.jpg" not in r.content


def test_grid_cover_url_v_override_wins(client, db_session):
    """In grid_v view, cover_url_override_v wins over GameArtwork."""
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
            artwork_type="cover",
            source="steam",
            url="https://cdn.example.com/steam-600x900.jpg",
        )
    )
    entry = models.UserLibraryEntry(
        user_id=user.id,
        release_id=release.id,
        import_source="steam_import",
        cover_url_override_v="https://sgdb.example.com/custom-600x900.jpg",
    )
    db_session.add(entry)
    db_session.commit()

    r = client.get("/library?view_mode=grid_v")
    assert b"sgdb.example.com/custom-600x900.jpg" in r.content
    assert b"cdn.example.com/steam-600x900.jpg" not in r.content


def test_clear_cover_override_v(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    entry.cover_url_override_v = "https://example.com/custom.jpg"
    entry.cover_url_override_h = "https://example.com/custom_h.jpg"
    db_session.commit()

    r = client.post(f"/library/entries/{entry.id}/clear-cover-override", data={"orientation": "v"})
    assert r.status_code == 200
    db_session.refresh(entry)
    assert entry.cover_url_override_v is None
    # _h untouched
    assert entry.cover_url_override_h == "https://example.com/custom_h.jpg"


def test_clear_cover_override_h(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    entry.cover_url_override_v = "https://example.com/custom.jpg"
    entry.cover_url_override_h = "https://example.com/custom_h.jpg"
    db_session.commit()

    r = client.post(f"/library/entries/{entry.id}/clear-cover-override", data={"orientation": "h"})
    assert r.status_code == 200
    db_session.refresh(entry)
    assert entry.cover_url_override_h is None
    assert entry.cover_url_override_v == "https://example.com/custom.jpg"


def test_clear_cover_override_rejects_bad_orientation(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    entry = _add_game(db_session, user)
    r = client.post(f"/library/entries/{entry.id}/clear-cover-override", data={"orientation": "x"})
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
