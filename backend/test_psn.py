"""PSN crawl + snapshot tests (PR 1 of #135 — no library writes yet)."""

from unittest.mock import MagicMock, patch

from backend import models, psn


def _purchased(name, title_id, platform="PS5", membership="NONE", product_id=None):
    return {
        "name": name,
        "titleId": title_id,
        "productId": product_id or f"UP0000-{title_id}-{name[:10].upper().replace(' ', '')}",
        "platform": platform,
        "isActive": True,
        "membership": membership,
    }


# ─── merge_library ─────────────────────────────────────────────────────────


def test_merge_joins_by_title_id_and_by_normalized_name():
    purchased = [_purchased("Stellar Blade", "PPSA01234_00")]
    titles = [
        # id-joins via titleId
        {
            "npCommunicationId": "NPWR001_00",
            "titleId": "PPSA01234_00",
            "trophyTitleName": "Stellar Blade",
            "trophyTitlePlatform": "PS5",
            "progress": 100,
        },
        # no shared id — name+platform join (Roman numeral normalization)
        {"npCommunicationId": "NPWR002_00", "trophyTitleName": "Final Fantasy VII", "trophyTitlePlatform": "PS4", "progress": 10},
    ]
    purchased.append(_purchased("FINAL FANTASY 7", "CUSA00552_00", platform="PS4"))
    played = [
        {"titleId": "PPSA01234_00", "name": "Stellar Blade", "category": "ps5_native_game", "playCount": 3, "playDuration": "PT30H"},
    ]
    result = psn.merge_library(purchased, titles, played)
    merged = {m["normalizedName"]: m for m in result["merged"]}
    assert len(result["merged"]) == 2
    sb = merged["stellarblade"]
    assert sb["npCommunicationId"] == "NPWR001_00"
    assert sb["playCount"] == 3
    assert set(sb["sources"]) == {"purchased", "titles", "played"}
    ff = merged["finalfantasy7"]
    assert ff["titleId"] == "CUSA00552_00"
    assert ff["npCommunicationId"] == "NPWR002_00"


def test_merge_name_join_respects_platform_guard():
    purchased = [_purchased("Resident Evil 4", "PPSA100_00", platform="PS5")]
    titles = [{"npCommunicationId": "NPWR900_00", "trophyTitleName": "Resident Evil 4", "trophyTitlePlatform": "PS4", "progress": 50}]
    result = psn.merge_library(purchased, titles, [])
    # PS4 trophy set must NOT merge onto the PS5 purchase — separate items.
    assert len(result["merged"]) == 2


def test_merge_filters_demos_and_media_apps():
    purchased = [
        _purchased("Real Game", "PPSA200_00"),
        _purchased("Cool Game Demo", "PPSA201_00"),
        _purchased("Sneaky", "PPSA202_00", product_id="UP0000-PPSA202_00-COOLDEMO3"),
    ]
    played = [
        {"titleId": "PPSA300_00", "name": "SONY PICTURES CORE", "category": "ps5_web_based_media_app", "playCount": 2},
        {"titleId": "PPSA200_00", "name": "Real Game", "category": "ps5_native_game", "playCount": 1},
    ]
    result = psn.merge_library(purchased, [], played)
    names = [m["name"] for m in result["merged"]]
    assert names == ["Real Game"]
    assert result["filtered"]["non_game_purchased"] == 2
    assert result["filtered"]["media_apps_played"] == 1


def test_merge_trophy_only_history_survives():
    """PS3/Vita-era games exist only in the trophy list — they must come
    through as their own items (the match-review overlap set)."""
    titles = [{"npCommunicationId": "NPWR555_00", "trophyTitleName": "Demon's Souls", "trophyTitlePlatform": "PS3", "progress": 40}]
    result = psn.merge_library([], titles, [])
    assert len(result["merged"]) == 1
    item = result["merged"][0]
    assert item["npCommunicationId"] == "NPWR555_00"
    assert psn.external_id_for(item) == "NPWR555_00"


def test_merge_accumulates_all_play_categories():
    """A game played both natively and on PC keeps BOTH categories, so it isn't
    mistaken for a PC-only copy (Spider-Man 2: 28h PS5 + a 37min PC touch)."""
    titles = [{"npCommunicationId": "NPWR700_00", "trophyTitleName": "Cross Play Game", "trophyTitlePlatform": "PS5,PSPC", "progress": 50}]
    played = [
        {"npCommunicationId": "NPWR700_00", "name": "Cross Play Game", "category": "ps5_native_game", "playDuration": "PT28H"},
        {"npCommunicationId": "NPWR700_00", "name": "Cross Play Game", "category": "pspc_game", "playDuration": "PT37M"},
    ]
    item = psn.merge_library([], titles, played)["merged"][0]
    assert item["playCategories"] == ["ps5_native_game", "pspc_game"]
    assert psn.is_pc_only(item) is False


def test_is_pc_only_true_for_pspc_only():
    """Only PC play evidence, no native PlayStation record → PC/Steam copy."""
    titles = [{"npCommunicationId": "NPWR701_00", "trophyTitleName": "PC Only Game", "trophyTitlePlatform": "PS5,PSPC", "progress": 100}]
    played = [{"npCommunicationId": "NPWR701_00", "name": "PC Only Game", "category": "pspc_game", "playDuration": "PT80H"}]
    item = psn.merge_library([], titles, played)["merged"][0]
    assert item["playCategories"] == ["pspc_game"]
    assert psn.is_pc_only(item) is True
    # No play evidence at all is never PC-only.
    assert psn.is_pc_only({"platform": "PS5", "sources": ["purchased"]}) is False


# ─── auth + pagination ─────────────────────────────────────────────────────


def test_exchange_npsso_expired_raises_typed_error():
    resp = MagicMock()
    resp.headers = {"location": "https://ca.account.sony.com/error"}
    with patch("backend.psn.httpx.get", return_value=resp):
        try:
            psn._exchange_npsso("dead-token")
            raise AssertionError("expected PsnNpssoExpiredError")
        except psn.PsnNpssoExpiredError:
            pass


def test_fetch_played_follows_next_offset():
    pages = [
        {"titles": [{"titleId": f"T{i}"} for i in range(10)], "totalItemCount": 12, "nextOffset": 10},
        {"titles": [{"titleId": "T10"}, {"titleId": "T11"}], "totalItemCount": 12, "nextOffset": None},
    ]
    with patch("backend.psn._bearer_get", side_effect=pages):
        titles, total = psn._fetch_played("tok", "acct")
    assert len(titles) == 12
    assert total == 12


def test_exchange_npsso_posts_the_extracted_code():
    """Regression: Sony's redirect is '…redirect/?code=…' — JS URLSearchParams
    strips the leading '?', Python's parsers don't. The original port looked
    up 'code' but had parsed '?code', POSTed an empty code, and Sony 400'd."""
    authorize = MagicMock()
    authorize.headers = {"location": "com.scee.psxandroid.scecompcall://redirect/?code=v3.SECRETCODE&cid=abc123"}
    token = MagicMock()
    token.json.return_value = {"access_token": "jwt-token-here"}
    token.raise_for_status.return_value = None
    with patch("backend.psn.httpx.get", return_value=authorize), patch("backend.psn.httpx.post", return_value=token) as mocked_post:
        result = psn._exchange_npsso("valid-npsso")
    assert result == "jwt-token-here"
    assert mocked_post.call_args.kwargs["data"]["code"] == "v3.SECRETCODE"


def test_fetch_trophy_titles_pages_until_reported_total():
    pages = [
        {"trophyTitles": [{"npCommunicationId": f"N{i}"} for i in range(100)], "totalItemCount": 150},
        {"trophyTitles": [{"npCommunicationId": f"N{i}"} for i in range(100, 150)], "totalItemCount": 150},
    ]
    with patch("backend.psn._bearer_get", side_effect=pages):
        titles, total = psn._fetch_trophy_titles("tok", "acct")
    assert len(titles) == 150
    assert total == 150


# ─── endpoints ─────────────────────────────────────────────────────────────


def _signup_and_login(client, username="testuser", password="testpass"):
    client.post("/signup", data={"username": username, "password": password, "password_confirm": password})
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    client.cookies.set("session", r.cookies["session"])
    return r.cookies["session"]


def test_fetch_library_requires_credentials(client, db_session):
    token = _signup_and_login(client)
    r = client.post("/integrations/psn/fetch-library")
    assert r.status_code == 422
    assert b"NPSSO" in r.content

    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "x" * 64
    db_session.commit()
    r = client.post("/integrations/psn/fetch-library")
    assert r.status_code == 422
    assert b"Online ID" in r.content


def test_snapshot_report_empty_state(client):
    _signup_and_login(client)
    with patch("backend.psn.load_snapshot", return_value=None):
        r = client.get("/integrations/psn/snapshot-report")
    assert r.status_code == 200
    assert b"No snapshot yet" in r.content


def test_snapshot_report_renders_counts(client):
    _signup_and_login(client)
    snap = {
        "fetched_at": "2026-07-18T01:00:00+00:00",
        "report": {
            "totals": {
                "purchased_fetched": 713,
                "trophy_fetched": 150,
                "trophy_reported": 150,
                "played_fetched": 147,
                "played_reported": 147,
            },
            "merged_total": 731,
            "filtered": {"non_game_purchased": 5, "non_game_titles": 1, "media_apps_played": 3, "non_game_played": 0},
            "membership": {"NONE": 650, "PS_PLUS": 63},
            "platforms": {"PS5": 400, "PS4": 300, "PS3": 31},
            "unresolvable_platforms": [],
            "no_external_id": 0,
            "already_imported": 0,
            "new": 731,
            "sample": [{"name": "Stellar Blade", "platform": "PS5", "sources": ["purchased", "titles"]}],
        },
    }
    with patch("backend.psn.load_snapshot", return_value=snap):
        r = client.get("/integrations/psn/snapshot-report")
    assert r.status_code == 200
    # WKWebView (the desktop shell) heuristically caches header-less GETs —
    # a stale cached report is invisible-bug territory in a no-reload WebView.
    assert r.headers["cache-control"] == "no-store"
    assert b"731" in r.content
    assert b"PS_PLUS" in r.content
    assert b"Stellar Blade" in r.content
    assert b"147" in r.content


def test_psn_page_shows_fetch_button_when_token_saved(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "x" * 64
    user.psn_online_id = "tester"
    db_session.commit()
    r = client.get("/integrations/psn")
    assert b"Fetch Library" in r.content
    assert b"/integrations/psn/fetch-library" in r.content


def test_psn_store_metadata_button_present_and_kicks_off_without_credentials(client, db_session):
    """The store-metadata job scrapes public pages, so it needs no NPSSO — the
    button works even before PSN credentials are saved."""
    _signup_and_login(client)
    page = client.get("/integrations/psn")
    assert b"/integrations/psn/refresh-store-metadata" in page.content
    assert b"Store Metadata" in page.content
    # No credential gate: kicks off a job (or 409s if one is already running) —
    # never the 422 the credentialed endpoints return.
    r = client.post("/integrations/psn/refresh-store-metadata")
    assert r.status_code != 422


# ─── import (PR 2) ─────────────────────────────────────────────────────────


def _seed_platforms(db):
    rows = [
        models.Platform(name="PS5", display_name="PlayStation 5"),
        models.Platform(name="PS4", display_name="PlayStation 4"),
        models.Platform(name="PS3", display_name="PlayStation 3"),
    ]
    db.add_all(rows)
    db.commit()
    return {r.name: r for r in rows}


def _write_snapshot(monkeypatch, tmp_path, user_id, merged, report=None):
    monkeypatch.setattr(psn, "DATA_DIR", str(tmp_path))
    snap = {
        "fetched_at": "2026-07-18T15:00:00+00:00",
        "report": report
        or {
            "totals": {"purchased_fetched": 1, "trophy_fetched": 1, "trophy_reported": 1, "played_fetched": 1, "played_reported": 1},
            "merged_total": len(merged),
            "filtered": {"non_game_purchased": 0, "non_game_titles": 0, "media_apps_played": 0, "non_game_played": 0},
            "membership": {},
            "platforms": {},
            "unresolvable_platforms": [],
            "no_external_id": 0,
            "already_imported": 0,
            "new": len(merged),
            "sample": [],
        },
        "merged": merged,
        "raw": {"purchased": [], "trophy_titles": [], "played": []},
    }
    import json as _json
    import os as _os

    _os.makedirs(str(tmp_path), exist_ok=True)
    with open(psn.snapshot_path(user_id), "w") as f:
        _json.dump(snap, f)
    return snap


def test_duration_to_minutes():
    assert psn.duration_to_minutes("PT30H23M7S") == 1823
    assert psn.duration_to_minutes("PT2M16S") == 2
    assert psn.duration_to_minutes("PT7S") == 0
    assert psn.duration_to_minutes(None) is None
    assert psn.duration_to_minutes("garbage") is None


def test_import_snapshot_creates_rows_and_chains_scan(db_session, monkeypatch, tmp_path):
    _seed_platforms(db_session)
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")
    db_session.add(user)
    db_session.commit()

    # A manual entry that overlaps a PSN title — must surface via the chained scan.
    manual_game = models.Game(title="Stellar Blade")
    db_session.add(manual_game)
    db_session.flush()
    ps5_id = models.resolve_platform_id(db_session, "PS5")
    manual_release = models.GameRelease(game_id=manual_game.id, platform="PS5", platform_id=ps5_id, source="manual")
    db_session.add(manual_release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=manual_release.id, import_source="manual"))
    db_session.commit()

    merged = [
        {  # purchased + trophy + played, PS_PLUS
            "titleId": "PPSA01234_00",
            "npCommunicationId": "NPWR001_00",
            "name": "Stellar Blade",
            "displayName": "Stellar Blade",
            "normalizedName": "stellarblade",
            "platform": "PS5",
            "membership": "PS_PLUS",
            "playDuration": "PT10H30M",
            "lastPlayed": "2026-07-01T12:00:00.000000Z",
            "sources": ["purchased", "titles", "played"],
            "trophies": {"bronze": 1},
        },
        {  # trophy-only PS3 history
            "npCommunicationId": "NPWR555_00",
            "name": "Demon's Souls",
            "displayName": "Demon's Souls",
            "normalizedName": "demonssouls",
            "platform": "PS3",
            "sources": ["titles"],
        },
        {  # played-only — must NOT import
            "titleId": "CUSA14394_00",
            "name": "RESIDENT EVIL 2",
            "displayName": "RESIDENT EVIL 2",
            "category": "ps4_game",
            "service": "none_purchased",
            "playDuration": "PT1M9S",
            "sources": ["played"],
        },
        {  # unresolvable platform — skipped + counted
            "titleId": "WEIRD01_00",
            "name": "Mystery Thing",
            "displayName": "Mystery Thing",
            "platform": "WEIRDPLAT",
            "sources": ["purchased"],
        },
    ]
    _write_snapshot(monkeypatch, tmp_path, user.id, merged)

    result = psn.import_snapshot(db_session, user)
    assert result["added"] == 2
    assert result["played_only_pending"] == 1
    assert result["skipped_no_platform"] == 1
    assert result["match_candidates"] >= 1  # Stellar Blade overlap queued

    sb = db_session.query(models.GameRelease).filter_by(source="psn", external_id="PPSA01234_00").one()
    assert sb.platform_id == ps5_id
    assert sb.raw_data["membership"] == "PS_PLUS"
    entry = db_session.query(models.UserLibraryEntry).filter_by(release_id=sb.id).one()
    assert entry.import_source == "psn_import"
    assert entry.playtime_minutes == 630
    assert entry.last_played_at is not None
    # No artwork rows by design — SGDB is the art source.
    assert db_session.query(models.GameArtwork).filter_by(release_id=sb.id).count() == 0

    ds = db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR555_00").one()
    assert ds.platform_id == models.resolve_platform_id(db_session, "PS3")

    # Played-only stayed out.
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="CUSA14394_00").count() == 0

    # Idempotent re-run: no new rows.
    result2 = psn.import_snapshot(db_session, user)
    assert result2["added"] == 0
    assert result2["updated"] == 2
    assert db_session.query(models.GameRelease).filter_by(source="psn").count() == 2


def test_import_skips_pc_only_game_already_in_steam(db_session, monkeypatch, tmp_path):
    """A pspc (PC) game that's already a Steam entry is the same copy surfacing
    through PSN's PC integration — skip it instead of minting a phantom PS entry.
    A pspc game NOT in Steam still imports; a natively-played game always does."""
    _seed_platforms(db_session)
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")
    db_session.add(user)
    db_session.commit()

    # Existing Steam entry — note the trademark glyph, which normalizes away.
    steam_game = models.Game(title="Stellar Blade™")
    db_session.add(steam_game)
    db_session.flush()
    steam_release = models.GameRelease(game_id=steam_game.id, platform="Steam", source="steam", external_id="3489700")
    db_session.add(steam_release)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=steam_release.id, import_source="steam_import"))
    db_session.commit()

    merged = [
        {  # pspc-only + has a Steam entry → skipped
            "npCommunicationId": "NPWR37356_00",
            "name": "Stellar Blade",
            "displayName": "Stellar Blade",
            "normalizedName": "stellarblade",
            "platform": "PS5,PSPC",
            "playCategories": ["pspc_game"],
            "category": "pspc_game",
            "sources": ["titles", "played"],
        },
        {  # pspc-only but NOT in Steam → still imports (as its resolved platform)
            "npCommunicationId": "NPWR90001_00",
            "name": "Some PC Game",
            "displayName": "Some PC Game",
            "normalizedName": "somepcgame",
            "platform": "PS5,PSPC",
            "playCategories": ["pspc_game"],
            "category": "pspc_game",
            "sources": ["titles", "played"],
        },
        {  # native PS5 play → imports even though a Steam entry exists
            "titleId": "PPSA03016_00",
            "name": "Stellar Blade",
            "displayName": "Stellar Blade",
            "normalizedName": "stellarblade",
            "platform": "PS5",
            "playCategories": ["ps5_native_game"],
            "category": "ps5_native_game",
            "sources": ["purchased", "titles", "played"],
        },
    ]
    _write_snapshot(monkeypatch, tmp_path, user.id, merged)

    result = psn.import_snapshot(db_session, user)
    assert result["skipped_pc_dupe"] == 1
    # The pspc Stellar Blade (trophy id) was skipped...
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR37356_00").count() == 0
    # ...but the non-Steam PC game and the native PS5 copy both imported.
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR90001_00").count() == 1
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="PPSA03016_00").count() == 1


def test_played_only_actions(client, db_session, monkeypatch, tmp_path):
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    merged = [
        {
            "titleId": "PPSA01442_00",
            "name": "Devil May Cry 5 Series",
            "displayName": "Devil May Cry 5 Series",
            "category": "ps5_native_game",
            "service": "other",
            "playDuration": "PT30H23M7S",
            "playCount": 40,
            "firstPlayed": "2021-12-12T08:29:58.930000Z",
            "lastPlayed": "2024-04-04T04:05:51.680000Z",
            "sources": ["played"],
        },
        {
            "titleId": "CUSA17670_00",
            "name": "Moving Out",
            "displayName": "Moving Out",
            "category": "ps4_game",
            "service": "ps_plus",
            "playDuration": "PT2M16S",
            "playCount": 0,
            "sources": ["played"],
        },
    ]
    _write_snapshot(monkeypatch, tmp_path, user.id, merged)

    # Suggestions: disc signature -> import; ps_plus + tiny -> skip.
    rows = psn.played_only_rows(db_session, user.id)
    by_id = {r["external_id"]: r for r in rows}
    assert by_id["PPSA01442_00"]["suggested"] == "import"
    assert "disc" in by_id["PPSA01442_00"]["reason"]
    assert by_id["CUSA17670_00"]["suggested"] == "skip"

    # Report page shows the review section.
    r = client.get("/integrations/psn/snapshot-report")
    assert b"Played-only activity" in r.content
    assert b"Devil May Cry 5 Series" in r.content

    # Import one.
    r = client.post("/integrations/psn/played-only/PPSA01442_00/import")
    assert r.status_code == 200
    assert b"Imported Devil May Cry 5 Series" in r.content
    rel = db_session.query(models.GameRelease).filter_by(source="psn", external_id="PPSA01442_00").one()
    entry = db_session.query(models.UserLibraryEntry).filter_by(release_id=rel.id).one()
    assert entry.playtime_minutes == 1823

    # Skip the other.
    r = client.post("/integrations/psn/played-only/CUSA17670_00/skip")
    assert b"Skipped" in r.content
    rows = psn.played_only_rows(db_session, user.id)
    by_id = {r["external_id"]: r for r in rows}
    assert by_id["PPSA01442_00"]["decision"]["action"] == "imported"
    assert by_id["CUSA17670_00"]["decision"]["action"] == "skipped"


def test_attach_played_only_to_existing_entry(client, db_session, monkeypatch, tmp_path):
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    game = models.Game(title="Devil May Cry 5 Special Edition")
    db_session.add(game)
    db_session.flush()
    rel = models.GameRelease(
        game_id=game.id, platform="PS5", platform_id=models.resolve_platform_id(db_session, "PS5"), source="psn", external_id="NPWR21064_00"
    )
    db_session.add(rel)
    db_session.flush()
    target = models.UserLibraryEntry(user_id=user.id, release_id=rel.id, import_source="psn_import")
    db_session.add(target)
    db_session.commit()

    merged = [
        {
            "titleId": "PPSA01442_00",
            "name": "Devil May Cry 5 Series",
            "displayName": "Devil May Cry 5 Series",
            "category": "ps5_native_game",
            "service": "other",
            "playDuration": "PT30H23M7S",
            "playCount": 40,
            "lastPlayed": "2024-04-04T04:05:51.680000Z",
            "sources": ["played"],
        }
    ]
    _write_snapshot(monkeypatch, tmp_path, user.id, merged)

    # The attach-search picker finds the SE entry.
    r = client.get("/integrations/psn/attach-search", params={"external_id": "PPSA01442_00", "q": "devil"})
    assert b"Devil May Cry 5 Special Edition" in r.content

    r = client.post("/integrations/psn/played-only/PPSA01442_00/attach", data={"entry_id": target.id})
    assert b"Play stats attached to Devil May Cry 5 Special Edition" in r.content
    db_session.refresh(target)
    assert target.playtime_minutes == 1823
    assert target.last_played_at is not None
    db_session.refresh(rel)
    assert rel.raw_data["psn_played"]["titleId"] == "PPSA01442_00"
    # No new release was created for the played row.
    assert db_session.query(models.GameRelease).filter_by(external_id="PPSA01442_00").count() == 0


def test_psn_token_endpoint_preserves_online_id(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_online_id = "corrosivefrost"
    user.psn_npsso = "old" * 21 + "x"
    db_session.commit()

    r = client.post("/integrations/psn/token", data={"psn_npsso": "n" * 64})
    assert r.status_code == 200
    db_session.refresh(user)
    assert user.psn_npsso == "n" * 64
    assert user.psn_online_id == "corrosivefrost"
    assert user.psn_npsso_captured_at is not None

    r = client.post("/integrations/psn/token", data={"psn_npsso": "  "})
    assert r.status_code == 422


# ─── beta filter + import resilience (fix/psn-beta-filter) ─────────────────


def test_is_non_game_catches_beta_mid_string_id():
    """Sony buries BETA mid-id (Diablo IV beta entitlementId ends 'PS4000').
    Regression: an earlier port anchored BETA to end-of-string and let it in."""
    diablo_beta = {
        "name": "Diablo IV",
        "titleId": "CUSA30374_00",
        "entitlementId": "UP0002-CUSA30374_00-RENEGDBETAPS4000",
        "platform": "PS4",
    }
    assert psn.is_non_game(diablo_beta) is True
    # DEMO stays end-anchored; a real game with DEMO mid-id is NOT filtered.
    assert psn.is_non_game({"name": "Demolition Derby", "productId": "UP0000-X-DEMOLITION"}) is False
    # DEMO / DEMO<n> at the end still caught.
    assert psn.is_non_game({"name": "X", "productId": "UP0000-X-COOLDEMO"}) is True
    assert psn.is_non_game({"name": "X", "productId": "UP0000-X-COOLDEMO3"}) is True


def test_import_skips_non_game_in_snapshot(db_session, monkeypatch, tmp_path):
    """A snapshot built before the filter fix can still hold a beta; the import
    must skip it rather than create a row."""
    _seed_platforms(db_session)
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")
    db_session.add(user)
    db_session.commit()
    merged = [
        {"titleId": "CUSA1_00", "name": "Real Game", "displayName": "Real Game", "platform": "PS4", "sources": ["purchased"]},
        {
            "titleId": "CUSA30374_00",
            "name": "Diablo IV",
            "displayName": "Diablo IV",
            "entitlementId": "UP0002-CUSA30374_00-RENEGDBETAPS4000",
            "platform": "PS4",
            "sources": ["purchased"],
        },
    ]
    _write_snapshot(monkeypatch, tmp_path, user.id, merged)
    result = psn.import_snapshot(db_session, user)
    assert result["added"] == 1
    assert result["skipped_non_game"] == 1
    assert db_session.query(models.GameRelease).filter_by(external_id="CUSA30374_00").count() == 0


def test_import_survives_game_platform_collision(db_session, monkeypatch, tmp_path):
    """A second item sharing display title + platform with an ALREADY-IMPORTED
    game (the real crash: the Diablo IV beta reused the committed Diablo IV
    game, which already held a PS4 release) must be skipped, not abort the
    whole import via UNIQUE(game_id, platform)."""
    _seed_platforms(db_session)
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")
    db_session.add(user)
    db_session.commit()

    # First import commits the real Twin Title PS4 entry.
    _write_snapshot(
        monkeypatch,
        tmp_path,
        user.id,
        [{"titleId": "CUSA_A", "name": "Twin Title", "displayName": "Twin Title", "platform": "PS4", "sources": ["purchased"]}],
    )
    psn.import_snapshot(db_session, user)

    # Second import: a different external_id, same display title + platform
    # (a beta, a cross-region edition, …). It reuses the committed game, whose
    # PS4 slot is taken — must skip, not crash. Other Game still imports.
    _write_snapshot(
        monkeypatch,
        tmp_path,
        user.id,
        [
            {"titleId": "CUSA_B", "name": "Twin Title", "displayName": "Twin Title", "platform": "PS4", "sources": ["purchased"]},
            {"titleId": "CUSA_C", "name": "Other Game", "displayName": "Other Game", "platform": "PS4", "sources": ["purchased"]},
        ],
    )
    result = psn.import_snapshot(db_session, user)
    assert result["skipped_conflict"] == 1
    assert result["added"] == 1  # Other Game landed; the run didn't roll back
    assert db_session.query(models.GameRelease).filter_by(external_id="CUSA_B").count() == 0
    assert db_session.query(models.GameRelease).filter_by(external_id="CUSA_C").count() == 1


# ─── trophy-set suffix stripping (display title) ───────────────────────────


def test_strip_trophy_suffix():
    assert psn._strip_trophy_suffix("God of War II Trophies") == "God of War II"
    assert psn._strip_trophy_suffix("TEKKEN 6 Trophy Set") == "TEKKEN 6"
    assert psn._strip_trophy_suffix("Novastrike Trophies") == "Novastrike"
    # Bare ' Trophy' and a trailing period ('Trophy pack.') are stripped too.
    assert psn._strip_trophy_suffix("BlazBlue Continuum Shift Trophy") == "BlazBlue Continuum Shift"
    assert psn._strip_trophy_suffix("STREET FIGHTER IV Trophy pack.") == "STREET FIGHTER IV"
    # Don't over-strip a title where 'trophy' isn't the trailing tag.
    assert psn._strip_trophy_suffix("Trophy Hunter") == "Trophy Hunter"
    assert psn._strip_trophy_suffix("Resident Evil 4") == "Resident Evil 4"
    assert psn._display_name("God of War II Trophies™") == "God of War II"


def test_import_strips_trophy_suffix_from_existing_snapshot(db_session, monkeypatch, tmp_path):
    """A snapshot whose displayName still carries 'Trophies' (pre-fix) imports
    the clean title without a re-fetch."""
    _seed_platforms(db_session)
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")
    db_session.add(user)
    db_session.commit()
    merged = [
        {
            "npCommunicationId": "NPWR555_00",
            "name": "God of War II Trophies",
            "displayName": "God of War II Trophies",
            "platform": "PS3",
            "sources": ["titles"],
        }
    ]
    _write_snapshot(monkeypatch, tmp_path, user.id, merged)
    psn.import_snapshot(db_session, user)
    rel = db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR555_00").one()
    assert rel.game.display_title == "God of War II"


def test_reimport_recleans_stale_title(db_session, monkeypatch, tmp_path):
    """An entry imported before the trophy-strip fix (Game.title still ends in
    'Trophies') gets its title cleaned on a plain re-import — library search
    matches Game.title, so this stops it surfacing by the trophy artifact."""
    _seed_platforms(db_session)
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")
    db_session.add(user)
    db_session.commit()
    ps3 = models.resolve_platform_id(db_session, "PS3")
    game = models.Game(title="Killzone 2 Trophies")
    db_session.add(game)
    db_session.flush()
    rel = models.GameRelease(game_id=game.id, platform="PlayStation 3", platform_id=ps3, source="psn", external_id="NPWR12345_00")
    db_session.add(rel)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=rel.id, import_source="psn_import"))
    db_session.commit()

    _write_snapshot(
        monkeypatch,
        tmp_path,
        user.id,
        [
            {
                "npCommunicationId": "NPWR12345_00",
                "name": "Killzone 2 Trophies",
                "displayName": "Killzone 2 Trophies",
                "platform": "PS3",
                "sources": ["titles"],
            }
        ],
    )
    psn.import_snapshot(db_session, user)
    db_session.refresh(game)
    assert game.title == "Killzone 2"
    assert game.display_name is None


def _psn_import_result():
    return {
        "added": 1,
        "updated": 0,
        "skipped_no_platform": 0,
        "skipped_no_id": 0,
        "skipped_non_game": 0,
        "skipped_conflict": 0,
        "played_only_pending": 0,
        "match_candidates": 0,
    }


def test_psn_import_auto_triggers_sgdb_fill_when_key_present(db_session, monkeypatch):
    import asyncio

    from backend import integrations, jobs
    from backend import psn as psn_mod

    jobs.clear_all()
    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.commit()

    monkeypatch.setattr(psn_mod, "import_snapshot", lambda db, u: _psn_import_result())

    async def _noop(job_id, user_id):
        return None

    monkeypatch.setattr(integrations, "_run_sgdb_fill_all_job", _noop)
    db_session.close = lambda: None

    job = jobs.create(user_id=user.id, kind="psn_import", label="Import")
    with patch("backend.integrations.SessionLocal", return_value=db_session):
        asyncio.run(integrations._run_sync_job(job.id, user.id, "psn_import"))

    assert any(j.kind == "sgdb_fill_all" for j in jobs.active_jobs_for(user.id))


def test_psn_import_skips_sgdb_fill_without_key(db_session, monkeypatch):
    import asyncio

    from backend import integrations, jobs
    from backend import psn as psn_mod

    jobs.clear_all()
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")  # no sgdb key
    db_session.add(user)
    db_session.commit()

    monkeypatch.setattr(psn_mod, "import_snapshot", lambda db, u: _psn_import_result())
    db_session.close = lambda: None

    job = jobs.create(user_id=user.id, kind="psn_import", label="Import")
    with patch("backend.integrations.SessionLocal", return_value=db_session):
        asyncio.run(integrations._run_sync_job(job.id, user.id, "psn_import"))

    assert not any(j.kind == "sgdb_fill_all" for j in jobs.active_jobs_for(user.id))
