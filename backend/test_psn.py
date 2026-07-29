"""PSN crawl + snapshot tests (PR 1 of #135 — no library writes yet)."""

from unittest.mock import MagicMock, patch

import pytest

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


def test_sync_requires_credentials(client, db_session):
    token = _signup_and_login(client)
    r = client.post("/integrations/psn/sync")
    assert r.status_code == 422
    assert b"NPSSO" in r.content

    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "x" * 64
    db_session.commit()
    r = client.post("/integrations/psn/sync")
    assert r.status_code == 422
    assert b"Online ID" in r.content


def test_snapshot_report_empty_state(client):
    _signup_and_login(client)
    r = client.get("/integrations/psn/snapshot-report")
    assert r.status_code == 200
    assert b"No sync yet" in r.content


def test_snapshot_report_renders_counts(client, db_session):
    """Crawl telemetry is stored on the user, not read back out of the dump
    file — a restored database and the page can no longer disagree (#157)."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_last_sync_report = {
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
    }
    db_session.commit()

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
    assert b"Sync PSN library" in r.content
    assert b"/integrations/psn/sync" in r.content


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


# ─── avatar (#171) ─────────────────────────────────────────────────────────


def test_largest_avatar_url_picks_biggest():
    urls = {"avatarUrls": [{"size": "m", "avatarUrl": "m.png"}, {"size": "xl", "avatarUrl": "xl.png"}, {"size": "s", "avatarUrl": "s.png"}]}
    assert psn._largest_avatar_url(urls) == "xl.png"
    assert psn._largest_avatar_url({}) is None
    assert psn._largest_avatar_url({"avatarUrls": []}) is None


def test_resolve_profile_extracts_account_and_avatar():
    data = {"profile": {"accountId": "123", "avatarUrls": [{"size": "l", "avatarUrl": "l.png"}]}}
    with patch("backend.psn._bearer_get", return_value=data) as m:
        acct, avatar = psn._resolve_profile("tok", "dude")
    assert (acct, avatar) == ("123", "l.png")
    assert "avatarUrls" in m.call_args.kwargs["params"]["fields"]  # widened field list


def test_refresh_avatar_stores_url_on_user(db_session):
    user = models.User(name="a", username="a", password_hash="x", api_token="t", psn_npsso="n" * 64, psn_online_id="dude")
    db_session.add(user)
    db_session.commit()
    prof = {"profile": {"accountId": "123", "avatarUrls": [{"size": "xl", "avatarUrl": "doom.png"}]}}
    with patch("backend.psn._exchange_npsso", return_value="tok"), patch("backend.psn._bearer_get", return_value=prof):
        assert psn.refresh_avatar(db_session, user) == "doom.png"
    assert user.psn_avatar_url == "doom.png"


def test_test_token_refreshes_avatar(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "n" * 64
    user.psn_online_id = "dude"
    db_session.commit()
    prof = {"profile": {"accountId": "123", "avatarUrls": [{"size": "xl", "avatarUrl": "doom.png"}]}}
    with patch("backend.psn._exchange_npsso", return_value="tok"), patch("backend.psn._bearer_get", return_value=prof):
        r = client.post("/integrations/psn/test-token")
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") == "true"
    db_session.refresh(user)
    assert user.psn_avatar_url == "doom.png"


def test_test_token_requires_online_id(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "n" * 64  # no online id
    db_session.commit()
    r = client.post("/integrations/psn/test-token")
    assert b"Online ID" in r.content


def test_clearing_npsso_clears_avatar(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "n" * 64
    user.psn_online_id = "dude"
    user.psn_avatar_url = "doom.png"
    db_session.commit()
    client.post("/integrations/psn/credentials", data={"psn_online_id": "dude", "psn_npsso": ""})
    db_session.refresh(user)
    assert user.psn_avatar_url is None


def test_psn_avatar_shows_on_card_and_page(client, db_session):
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "n" * 64
    user.psn_online_id = "corrosivefrost"
    user.psn_avatar_url = "https://cdn.example/doom.png"
    db_session.commit()
    assert b"https://cdn.example/doom.png" in client.get("/tools").content
    assert b"https://cdn.example/doom.png" in client.get("/integrations/psn").content


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


def _user(db, name="u"):
    u = models.User(name=name, username=name, password_hash="x", api_token=f"{name}-tok", psn_npsso="n" * 64, psn_online_id="dude")
    db.add(u)
    db.commit()
    return u


def _report_fixture(merged_total=1):
    return {
        "totals": {
            "purchased_fetched": 0,
            "trophy_fetched": merged_total,
            "trophy_reported": merged_total,
            "played_fetched": 0,
            "played_reported": 0,
        },
        "merged_total": merged_total,
        "filtered": {"non_game_purchased": 0, "non_game_titles": 0, "media_apps_played": 0, "non_game_played": 0},
        "membership": {},
        "platforms": {},
        "unresolvable_platforms": [],
        "no_external_id": 0,
        "already_imported": 0,
        "new": merged_total,
        "sample": [],
    }


def _seed_review(db, user, merged, kind="cross_play"):
    """Create review rows the way a sync would, without crawling.

    Review state lives in psn_review_candidates now, not in the crawl dump —
    seeding the file would test nothing the app reads (#157).
    """
    for item in merged:
        psn._upsert_review_candidate(db, user, item, kind)
    db.commit()


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
    _seed_review(db_session, user, merged)

    result = psn.import_merged(db_session, user, merged)
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
    result2 = psn.import_merged(db_session, user, merged)
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
    _seed_review(db_session, user, merged)

    result = psn.import_merged(db_session, user, merged)
    assert result["skipped_pc_dupe"] == 1
    # The pspc Stellar Blade (trophy id) was skipped...
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR37356_00").count() == 0
    # ...but the non-Steam PC game and the native PS5 copy both imported.
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR90001_00").count() == 1
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="PPSA03016_00").count() == 1


def test_played_only_actions(client, db_session):
    """The played-only queue is a tab of the PSN review page, with the same
    per-row-action contract as the cross-play tab (#157)."""
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
    _seed_review(db_session, user, merged, kind="played_only")

    # Suggestions: disc signature -> import; ps_plus + tiny -> skip.
    by_id = {r["external_id"]: r for r in psn.played_only_rows(db_session, user.id)}
    assert by_id["PPSA01442_00"]["suggested"] == "import"
    assert "disc" in by_id["PPSA01442_00"]["reason"]
    assert by_id["CUSA17670_00"]["suggested"] == "skip"

    # The tab renders them, and the cross-play tab does not.
    body = client.get("/tools/psn-review?kind=played_only").content
    assert b"Devil May Cry 5 Series" in body
    assert b"psn-tabs" in body
    assert b"Devil May Cry 5 Series" not in client.get("/tools/psn-review").content

    # Import one — the row retires in place.
    r = client.post("/tools/psn-review/PPSA01442_00/played-only/import")
    assert r.status_code == 200
    assert b"added to your library" in r.content
    assert r.headers["HX-Retarget"] == "#psn-row-PPSA01442_00"
    rel = db_session.query(models.GameRelease).filter_by(source="psn", external_id="PPSA01442_00").one()
    entry = db_session.query(models.UserLibraryEntry).filter_by(release_id=rel.id).one()
    assert entry.playtime_minutes == 1823

    # Skip the other.
    r = client.post("/tools/psn-review/CUSA17670_00/played-only/skip")
    assert r.status_code == 200
    assert b"skipped" in r.content
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="CUSA17670_00").count() == 0

    # Both decided — the queue is empty and stays that way.
    assert [r_["external_id"] for r_ in psn.played_only_rows(db_session, user.id) if not r_["decision"]] == []


def test_attach_played_only_to_existing_entry(client, db_session):
    """The DMC5-SE-on-disc case: 30h of playtime that exists nowhere else, on a
    row whose Sony name doesn't match the game you own. Attach moves the stats
    onto the entry you already have instead of minting a second one."""
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
    _seed_review(db_session, user, merged, kind="played_only")

    # The attach-search picker finds the SE entry.
    r = client.get("/integrations/psn/attach-search", params={"external_id": "PPSA01442_00", "q": "devil"})
    assert b"Devil May Cry 5 Special Edition" in r.content

    r = client.post("/tools/psn-review/PPSA01442_00/played-only/attach", data={"entry_id": target.id})
    assert b"play stats attached" in r.content
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
    _seed_review(db_session, user, merged)
    result = psn.import_merged(db_session, user, merged)
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
    psn.import_merged(
        db_session,
        user,
        [{"titleId": "CUSA_A", "name": "Twin Title", "displayName": "Twin Title", "platform": "PS4", "sources": ["purchased"]}],
    )

    # Second import: a different external_id, same display title + platform
    # (a beta, a cross-region edition, …). It reuses the committed game, whose
    # PS4 slot is taken — must skip, not crash. Other Game still imports.
    merged = [
        {"titleId": "CUSA_B", "name": "Twin Title", "displayName": "Twin Title", "platform": "PS4", "sources": ["purchased"]},
        {"titleId": "CUSA_C", "name": "Other Game", "displayName": "Other Game", "platform": "PS4", "sources": ["purchased"]},
    ]
    result = psn.import_merged(db_session, user, merged)
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
    _seed_review(db_session, user, merged)
    psn.import_merged(db_session, user, merged)
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

    psn.import_merged(
        db_session,
        user,
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


def test_psn_sync_auto_triggers_sgdb_fill_when_key_present(db_session, monkeypatch):
    import asyncio

    from backend import integrations, jobs
    from backend import psn as psn_mod

    jobs.clear_all()
    user = models.User(name="t", username="t", password_hash="x", api_token="tok", steamgriddb_api_key="sgdb-key")
    db_session.add(user)
    db_session.commit()

    monkeypatch.setattr(psn_mod, "sync_library", lambda db, u: _psn_import_result())

    async def _noop(job_id, user_id):
        return None

    monkeypatch.setattr(integrations, "_run_sgdb_fill_all_job", _noop)
    db_session.close = lambda: None

    job = jobs.create(user_id=user.id, kind="psn_sync", label="Library sync")
    with patch("backend.integrations.SessionLocal", return_value=db_session):
        asyncio.run(integrations._run_sync_job(job.id, user.id, "psn_sync"))

    assert any(j.kind == "sgdb_fill_all" for j in jobs.active_jobs_for(user.id))


def test_psn_sync_skips_sgdb_fill_without_key(db_session, monkeypatch):
    import asyncio

    from backend import integrations, jobs
    from backend import psn as psn_mod

    jobs.clear_all()
    user = models.User(name="t", username="t", password_hash="x", api_token="tok")  # no sgdb key
    db_session.add(user)
    db_session.commit()

    monkeypatch.setattr(psn_mod, "sync_library", lambda db, u: _psn_import_result())
    db_session.close = lambda: None

    job = jobs.create(user_id=user.id, kind="psn_sync", label="Library sync")
    with patch("backend.integrations.SessionLocal", return_value=db_session):
        asyncio.run(integrations._run_sync_job(job.id, user.id, "psn_sync"))

    assert not any(j.kind == "sgdb_fill_all" for j in jobs.active_jobs_for(user.id))


# ─── cross-play platform resolution (#163) ─────────────────────────────────


def test_platform_candidates_ranks_and_drops_pspc():
    assert psn.platform_candidates({"platform": "PS3,PSVITA"}) == ["PS3", "PSVITA"]
    assert psn.platform_candidates({"platform": "PSVITA,PS4"}) == ["PS4", "PSVITA"]
    # PSPC is PSN's PC integration, never a PlayStation platform.
    assert psn.platform_candidates({"platform": "PS5,PSPC"}) == ["PS5"]
    assert psn.platform_candidates({"platform": ""}) == []


def test_resolve_platform_single_is_confident():
    p, _, confident = psn.resolve_platform_choice({"platform": "PS4"})
    assert (p, confident) == ("PS4", True)


def test_resolve_platform_uses_substantial_playtime():
    """73h on PS5 beats a 14min PS4 cross-gen touch, and it's confident."""
    item = {"platform": "PS4,PS5", "playByCategory": {"ps5_native_game": 4380, "ps4_game": 14}}
    p, reason, confident = psn.resolve_platform_choice(item)
    assert p == "PS5" and confident is True
    assert "PS5" in reason


def test_resolve_platform_two_substantial_is_a_suggestion():
    """Played both for real — pick the bigger, but make the user confirm."""
    item = {"platform": "PS4,PS5", "playByCategory": {"ps5_native_game": 1700, "ps4_game": 3400}}
    p, _, confident = psn.resolve_platform_choice(item)
    assert p == "PS4" and confident is False


def test_resolve_platform_trivial_playtime_is_not_confident():
    item = {"platform": "PSVITA,PS4", "playByCategory": {"ps4_game": 5}}
    p, reason, confident = psn.resolve_platform_choice(item)
    assert p == "PS4" and confident is False
    assert "5m" in reason


def test_resolve_platform_trophy_only_suggests_oldest():
    """Shinovi Versus: PS3,PSVITA with no play history. The modern purchased
    feed doesn't cover that era, so the handheld copy is the better guess — but
    it stays a suggestion, never applied silently."""
    item = {"platform": "PS3,PSVITA", "sources": ["titles"]}
    p, reason, confident = psn.resolve_platform_choice(item)
    assert p == "PSVITA" and confident is False
    assert "No play history" in reason


def test_platform_for_item_prefers_a_user_decision(db_session):
    _seed_platforms(db_session)
    vita = models.Platform(name="PSVITA", display_name="PlayStation Vita")
    db_session.add(vita)
    db_session.commit()
    item = {"platform": "PS3,PSVITA", "platformDecision": "PS3"}
    assert psn.platform_for_item(db_session, item) == models.resolve_platform_id(db_session, "PS3")


def test_platform_for_item_never_returns_pspc(db_session):
    _seed_platforms(db_session)
    # A PC-only trophy set has no PlayStation platform to resolve to.
    assert psn.platform_for_item(db_session, {"platform": "PSPC"}) is None


def test_merge_tracks_play_minutes_per_category():
    titles = [{"npCommunicationId": "NPWR1_00", "trophyTitleName": "X", "trophyTitlePlatform": "PS4,PS5"}]
    played = [
        {"npCommunicationId": "NPWR1_00", "name": "X", "category": "ps5_native_game", "playDuration": "PT10H"},
        {"npCommunicationId": "NPWR1_00", "name": "X", "category": "ps4_game", "playDuration": "PT30M"},
    ]
    item = psn.merge_library([], titles, played)["merged"][0]
    assert item["playByCategory"] == {"ps5_native_game": 600, "ps4_game": 30}
    assert psn.played_minutes_by_platform(item) == {"PS5": 600, "PS4": 30}


def test_import_review_rows_lists_only_cross_play_games(db_session):
    """Only an ambiguous PLATFORM reaches the review. Anything on a single
    platform is settled — the import creates it without asking."""
    _seed_platforms(db_session)
    user = _user(db_session, "rev")
    merged = [
        {  # cross-play: played on PS5, but the set also covers PS4
            "npCommunicationId": "NPWR_X_00",
            "name": "Cross",
            "displayName": "Cross",
            "platform": "PS4,PS5",
            "playByCategory": {"ps5_native_game": 600},
            "sources": ["titles", "played"],
        },
        {  # single platform, trophy-only — nothing to ask
            "npCommunicationId": "NPWR_ONE_00",
            "name": "TrophyOnly",
            "displayName": "TrophyOnly",
            "platform": "PS4",
            "sources": ["titles"],
        },
        {  # single platform and purchased — likewise settled
            "titleId": "CUSA9999_00",
            "name": "Settled",
            "displayName": "Settled",
            "platform": "PS4",
            "sources": ["purchased"],
        },
    ]
    psn.import_merged(db_session, user, merged)
    rows = psn.import_review_rows(db_session, user.id)

    assert [r["name"] for r in rows] == ["Cross"]
    options = {o["platform"]: o for o in rows[0]["options"]}
    assert set(options) == {"PS4", "PS5"}
    # Both pre-ticked: PS4 is proven by the two settled games the import just
    # created as PS4 entries, PS5 by the logged play time. Defaulting to
    # everything the account can actually run beats defaulting to one guess.
    assert options["PS5"]["selected"] is True
    assert options["PS4"]["selected"] is True


def test_confirm_validates_platforms_against_the_trophy_set(db_session):
    """Cross-buy means several platforms are a legitimate answer — but only
    ones the set actually covers. A stale page posting anything else would
    otherwise mint an entry on a platform Sony never listed."""
    _seed_platforms(db_session)
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita"))
    db_session.commit()
    user = _user(db_session, "val")
    merged = [{"npCommunicationId": "NPWR_A_00", "name": "A", "displayName": "A", "platform": "PS3,PSVITA,PS4", "sources": ["titles"]}]
    _seed_review(db_session, user, merged)

    result = psn.confirm_entry_decision(db_session, user, "NPWR_A_00", ["PS4", "PSVITA"])
    assert sorted(result["platforms"]) == ["PS4", "PSVITA"]
    assert result["created"] == 2

    # A platform the set doesn't cover is dropped rather than trusted.
    _seed_review(db_session, user, [{**merged[0], "npCommunicationId": "NPWR_B_00"}])
    result = psn.confirm_entry_decision(db_session, user, "NPWR_B_00", ["PS5"])
    assert result["platforms"] == []
    assert result["created"] == 0


def test_confirm_creates_one_entry_per_chosen_platform(db_session):
    """Cross-buy: one trophy set becomes a library entry on each platform the
    user picked, created on the click rather than staged for a later import."""
    _seed_platforms(db_session)
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita"))
    db_session.commit()
    user = _user(db_session, "multi")
    merged = [
        {"npCommunicationId": "NPWR_D_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PSVITA,PS4", "sources": ["titles"]}
    ]
    _seed_review(db_session, user, merged)

    result = psn.confirm_entry_decision(db_session, user, "NPWR_D_00", ["PS4", "PSVITA"])
    assert result["created"] == 2
    releases = db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_D_00").all()
    assert {r.platform_id for r in releases} == {
        models.resolve_platform_id(db_session, "PS4"),
        models.resolve_platform_id(db_session, "PSVITA"),
    }


def test_dismissing_a_row_creates_nothing_and_survives_a_resync(db_session):
    """Unticking every platform is a real decision: don't import this one — and
    a later sync must not re-ask, which is what the row's status now carries."""
    _seed_platforms(db_session)
    user = _user(db_session, "skip")
    merged = [{"npCommunicationId": "NPWR_S_00", "name": "Skip", "displayName": "Skip", "platform": "PS3,PS4", "sources": ["titles"]}]
    _seed_review(db_session, user, merged)

    psn.dismiss_entry_decision(db_session, user, "NPWR_S_00")
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_S_00").count() == 0

    psn.import_merged(db_session, user, merged)
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_S_00").count() == 0
    assert psn.import_review_rows(db_session, user.id) == []


def test_psn_review_confirm_creates_the_entries(client, db_session, monkeypatch, tmp_path):
    """The review IS the action — confirming a row creates its library entries
    there and then, the way match review merges on click."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    merged = [
        {
            "npCommunicationId": "NPWR_E_00",
            "name": "Cross",
            "displayName": "Cross",
            "platform": "PS3,PS4",
            "sources": ["titles"],
        }
    ]
    _seed_review(db_session, user, merged)

    r = client.post("/tools/psn-review/NPWR_E_00/confirm", data={"platforms": ["PS3", "PS4"]})
    assert r.status_code == 200
    assert b"added 2" in r.content
    # The response replaces that row in place, in either view.
    assert b'id="psn-row-NPWR_E_00"' in r.content
    assert r.headers["HX-Retarget"] == "#psn-row-NPWR_E_00"

    rels = db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_E_00").all()
    assert {r_.platform_id for r_ in rels} == {
        models.resolve_platform_id(db_session, "PS3"),
        models.resolve_platform_id(db_session, "PS4"),
    }
    # ...and the row drops off the review list, like a merged pair does.
    assert psn.import_review_rows(db_session, user.id) == []


def test_psn_review_dismiss_is_a_real_decision(client, db_session, monkeypatch, tmp_path):
    """Dismiss means don't import this one — recorded, so it stops being work."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    merged = [
        {
            "npCommunicationId": "NPWR_S2_00",
            "name": "Skip Me",
            "displayName": "Skip Me",
            "platform": "PS3,PS4",
            "sources": ["titles"],
        }
    ]
    _seed_review(db_session, user, merged)

    r = client.post("/tools/psn-review/NPWR_S2_00/dismiss")
    assert r.status_code == 200
    assert b"dismissed" in r.content
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_S2_00").count() == 0
    assert psn.import_review_rows(db_session, user.id) == []


def test_psn_review_confirm_rejects_an_unknown_row(client, db_session, monkeypatch, tmp_path):
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    pass  # nothing to seed
    r = client.post("/tools/psn-review/NOPE_00/confirm", data={"platforms": ["PS4"]})
    assert r.status_code == 404


def test_two_trophy_sets_for_one_name_stay_separate():
    """Two trophy sets are two records — a second platform's progress, or an
    outright different game sharing a name (Demon's Souls PS3 vs the PS5
    remake). Both used to match the same purchased row by name, resolve to the
    same key, and the second silently overwrote the first (#163)."""
    purchased = [_purchased("Demon's Souls", "CUSA00881_00", platform="PS4")]
    titles = [
        {"npCommunicationId": "NPWR00881_00", "trophyTitleName": "Demon's Souls", "trophyTitlePlatform": "PS3", "progress": 100},
        {"npCommunicationId": "NPWR20277_00", "trophyTitleName": "Demon's Souls", "trophyTitlePlatform": "PS5", "progress": 42},
    ]
    merged = psn.merge_library(purchased, titles, [])["merged"]
    by_npc = {m.get("npCommunicationId"): m for m in merged}
    assert "NPWR00881_00" in by_npc and "NPWR20277_00" in by_npc
    # Each keeps its own progress — neither is clobbered.
    assert by_npc["NPWR00881_00"]["trophyProgress"] == 100
    assert by_npc["NPWR20277_00"]["trophyProgress"] == 42
    assert by_npc["NPWR00881_00"]["platform"] == "PS3"
    assert by_npc["NPWR20277_00"]["platform"] == "PS5"


def test_played_feed_name_never_overwrites_the_real_title():
    """Sony's played feed reports activity under a concept/collection name:
    Uncharted 4 and The Lost Legacy both come back as 'UNCHARTED: Legacy of
    Thieves Collection' even though their trophy sets and store entries name
    them correctly. Letting that win merged two distinct games into one name
    and broke matching against the user's own records (#163)."""
    purchased = [
        _purchased("Uncharted 4: A Thief's End", "CUSA00341_00", platform="PS4"),
        _purchased("Uncharted: The Lost Legacy", "CUSA07737_00", platform="PS4"),
    ]
    titles = [
        {
            "npCommunicationId": "NPWR07028_00",
            "titleId": "CUSA00341_00",
            "trophyTitleName": "Uncharted 4: A Thief's End",
            "trophyTitlePlatform": "PS4",
            "progress": 84,
        },
        {
            "npCommunicationId": "NPWR13408_00",
            "titleId": "CUSA07737_00",
            "trophyTitleName": "Uncharted: The Lost Legacy",
            "trophyTitlePlatform": "PS4",
            "progress": 100,
        },
    ]
    played = [
        {"titleId": "CUSA00341_00", "name": "UNCHARTED: Legacy of Thieves Collection", "category": "ps4_game", "playDuration": "PT50H"},
        {"titleId": "CUSA07737_00", "name": "UNCHARTED: Legacy of Thieves Collection", "category": "ps4_game", "playDuration": "PT23H"},
    ]
    merged = psn.merge_library(purchased, titles, played)["merged"]
    names = sorted(m["name"] for m in merged)
    assert names == ["Uncharted 4: A Thief's End", "Uncharted: The Lost Legacy"]
    # ...and each keeps its own trophy progress.
    by_name = {m["name"]: m for m in merged}
    assert by_name["Uncharted 4: A Thief's End"]["trophyProgress"] == 84
    assert by_name["Uncharted: The Lost Legacy"]["trophyProgress"] == 100


def test_played_only_rows_still_use_the_played_name():
    """The played name is the fallback, not banned — a played-only row has
    nothing else to go on."""
    played = [{"titleId": "CUSA9_00", "name": "Some Game", "category": "ps4_game", "playDuration": "PT1H"}]
    merged = psn.merge_library([], [], played)["merged"]
    assert merged[0]["name"] == "Some Game"


# ─── shared match normalization (#180) ─────────────────────────────────────


def test_normalize_folds_instead_of_deleting():
    """The old PSN folding stripped non-alphanumerics, so accented and
    non-Latin characters were DELETED: ABZÛ -> 'abz', NINJA GAIDEN Σ2 ->
    'ninjagaiden2'. Every one of these was a real unmatched game."""
    from backend.titles import normalize_for_match as n

    def same(a, b):
        return n(a).replace(" ", "") == n(b).replace(" ", "")

    assert same("ABZÛ*#", "ABZU")  # accent + the sheet's own annotation chars
    assert same("Söldner-X 2", "Soldner-X 2")
    assert same("Ninja Gaiden Sigma 2", "NINJA GAIDEN Σ2")  # Greek, which NFKD leaves alone
    assert same("Soul Calibur V", "SOULCALIBUR Ⅴ")  # U+2164, plus a word break
    assert same("Super Street Fighter IV", "SUPER STREET FIGHTER Ⅳ")
    assert same("Ratchet and Clank", "Ratchet & Clank")
    assert same("Assassin's Creed", "Assassin’s Creed")  # curly apostrophe
    # Trophy-set suffixes Sony appends
    assert same("God of War II", "God of War® II Trophies")
    assert same("Tekken 6", "TEKKEN 6 Trophy Set")
    assert same("Slayaway Camp", "Slayaway Camp trophies")
    # Port/edition markers and disambiguators either side may carry
    assert same("Hitman (2016)", "HITMAN")
    assert same("Resident Evil 4 HD", "resident evil 4")
    assert same("PaRappa the Rapper", "PaRappa The Rapper Remastered")
    # Titles really do ship with embedded newlines — they collapse to spaces
    # rather than surviving into the key. (What's left here is a subtitle
    # difference, which is a matching-strategy problem, not a folding one.)
    assert n("The Legend of Dark Witch\n-Chronicle 2D ACT-") == "the legend of dark witch chronicle 2d act"


def test_normalize_keeps_numbers_distinct():
    """Numbers are identity, not noise — these must never collapse together."""
    from backend.titles import normalize_for_match as n

    assert n("Final Fantasy XII") != n("Final Fantasy XVI")
    assert n("Street Fighter IV") != n("Street Fighter V")
    assert n("Assassin's Creed") != n("Assassin's Creed II")
    assert n("Uncharted 2") != n("Uncharted 3")
    # Roman numerals fold to arabic so both spellings agree
    assert n("God of War II") == n("God of War 2")


def test_normalize_leaves_a_lone_letter_alone():
    """The X in Soldner-X is part of the name. Converting single letters turned
    it into 'soldner 10', which matched nothing."""
    from backend.titles import normalize_for_match as n

    assert "10" not in n("Soldner-X 2")
    assert n("Street Fighter V") == n("STREET FIGHTER V")


def test_normalize_survives_non_latin_titles():
    """A Japanese title must fold to something, not to nothing."""
    from backend.titles import normalize_for_match as n

    assert n("閃乱カグラ SHINOVI VERSUS")
    assert "shinovi versus" in n("閃乱カグラ SHINOVI VERSUS")


def test_trademark_stripped_before_nfkd():
    """Regression: NFKD expands ™ into the letters 'TM', so stripping after it
    left 'Stellar Blade™' as 'stellarbladetm' and broke the Steam-dupe check."""
    from backend.titles import normalize_for_match as n

    assert n("Stellar Blade™") == n("Stellar Blade")


def test_titles_match_tiers():
    """Exact is safe to act on; contained is a suggestion to confirm."""
    from backend.titles import titles_match as m

    # Exact — folding differences only.
    assert m("ABZÛ*#", "ABZU") == "exact"
    assert m("Tekken 6", "TEKKEN 6 Trophy Set") == "exact"
    assert m("Soul Calibur V", "SOULCALIBUR Ⅴ") == "exact"

    # Contained — one side drops a subtitle...
    assert m("Uncharted", "Uncharted: Drake's Fortune") == "contained"
    assert m("Enslaved", "ENSLAVED™: Odyssey to the West™") == "contained"
    # ...or a franchise prefix, in either direction (Sony's is the short one here)
    assert m("The Elder Scrolls V: Skyrim", "Skyrim") == "contained"
    assert m("Stranger's Wrath HD", "Oddworld: Stranger's Wrath HD") == "contained"
    # Sony appends the platform to some titles
    assert m("Grounded", "Grounded PS4 & PS5") == "exact"


def test_titles_match_never_crosses_a_sequel():
    """The whole risk of containment matching. A number in the dropped words
    means a different entry in the series — evidence for #160, where fuzzy
    matching confidently produced every one of these."""
    from backend.titles import titles_match as m

    assert m("Uncharted", "Uncharted 2: Among Thieves") is None
    assert m("Uncharted", "Uncharted 3: Drake's Deception") is None
    assert m("Assassin's Creed", "Assassin's Creed II") is None
    assert m("Final Fantasy XII", "FINAL FANTASY XVI") is None
    assert m("Street Fighter IV", "Street Fighter V") is None
    assert m("God of War II", "God of War") is None
    assert m("Call of Duty: Modern Warfare 2", "Modern Warfare 3") is None
    # A lone short extra word is a different game, not a dropped subtitle.
    assert m("Hitman GO", "HITMAN") is None


def test_titles_match_contained_is_only_a_suggestion():
    """Contained legitimately covers DLC-to-parent, which is NOT the same game.
    Auto-merging would fold separate DLC completions into their base game."""
    from backend.titles import titles_match as m

    assert m("Alan Wake II: Night Springs", "Alan Wake II") == "contained"
    assert m("Nioh 2 - The Tengu's Disciple", "Nioh 2") == "contained"
    assert m("Peggle Nights", "Peggle") == "contained"


def test_purchased_fetch_asks_for_every_platform():
    """The original port asked only for ps4/ps5, so every conclusion about
    'PSN doesn't return PS3/Vita purchases' was really about our own filter
    (#181)."""
    import json as _j

    page = {"data": {"purchasedTitlesRetrieve": {"games": []}}}
    with patch("backend.psn._bearer_get", return_value=page) as mocked:
        psn._fetch_purchased("tok", "acct")
    variables = _j.loads(mocked.call_args.kwargs["params"]["variables"])
    assert "ps3" in variables["platform"]
    assert "ps vita" in variables["platform"]
    assert "psp" in variables["platform"]


def test_purchased_fetch_falls_back_when_sony_rejects_the_wider_list():
    """A rejected platform token must not cost the user their whole crawl."""
    import json as _j

    good = {"data": {"purchasedTitlesRetrieve": {"games": [{"titleId": "CUSA1_00"}]}}}
    calls = []

    def fake(_token, _url, params=None):
        platforms = _j.loads(params["variables"])["platform"]
        calls.append(platforms)
        if "ps3" in platforms:
            return {"errors": [{"message": "unknown platform"}]}
        return good

    with patch("backend.psn._bearer_get", side_effect=fake):
        out = psn._fetch_purchased("tok", "acct")
    assert out == [{"titleId": "CUSA1_00"}]  # fallback result, not an exception
    assert calls[0] != calls[-1]
    assert calls[-1] == ["ps4", "ps5"]


def test_psn_review_page_renders_with_both_layouts(client, db_session, monkeypatch, tmp_path):
    """The cross-play decisions are their own page with a list/card toggle and
    a sticky Save — matching match review and import review, not buried in the
    fetch report."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    merged = [
        {
            "npCommunicationId": "NPWR_R_00",
            "name": "Cross Play Game",
            "displayName": "Cross Play Game",
            "platform": "PS3,PSVITA",
            "sources": ["titles"],
        }
    ]
    _seed_review(db_session, user, merged)

    r = client.get("/tools/psn-review")
    assert r.status_code == 200
    body = r.content
    assert b"Cross Play Game" in body
    # list is the baseline view, with the import-review layout toggle
    assert b"psn-view-toggle" in body
    assert b"psn-review-table" in body
    # a platform checkbox per candidate, in this row's own option group
    assert b'id="psn-opts-NPWR_R_00"' in body
    assert body.count(b'name="platforms"') == 2
    # per-row actions that take effect on click, not a bulk-only save
    assert b"/tools/psn-review/NPWR_R_00/confirm" in body
    assert b"/tools/psn-review/NPWR_R_00/dismiss" in body


def test_psn_review_page_empty_states(client, db_session):
    """Empty state now reads the library and queue, not a file on disk — a
    restored database and the page can't disagree (#157)."""
    _seed_platforms(db_session)
    _signup_and_login(client)
    r = client.get("/tools/psn-review")
    assert b"No PSN sync yet" in r.content


def test_tools_shows_the_psn_review_card(client, db_session, monkeypatch, tmp_path):
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    merged = [
        {
            "npCommunicationId": "NPWR_T_00",
            "name": "Cross",
            "displayName": "Cross",
            "platform": "PS3,PSVITA",
            "sources": ["titles"],
        }
    ]
    _seed_review(db_session, user, merged)
    r = client.get("/tools")
    assert b"PSN review" in r.content
    assert b"/tools/psn-review" in r.content


def test_psn_fetch_report_links_out_instead_of_embedding_the_review(client, db_session):
    """Creating library entries doesn't belong inside a summary of the crawl."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    user.psn_last_sync_report = _report_fixture()
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_L_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PSVITA", "sources": ["titles"]}],
    )
    r = client.get("/integrations/psn/snapshot-report")
    assert b"/tools/psn-review" in r.content  # links out
    assert b'name="platforms"' not in r.content  # no checkboxes embedded


def test_import_holds_ambiguous_games_back_instead_of_guessing(db_session):
    """Import does the unambiguous work and funnels the rest to review. It
    creates entries and cannot un-create them, so a wrong platform would need
    manual cleanup — better to wait for the user."""
    _seed_platforms(db_session)
    user = _user(db_session, "hold")
    merged = [
        {  # unambiguous — imports straight away
            "titleId": "CUSA1111_00",
            "name": "Clear",
            "displayName": "Clear",
            "platform": "PS4",
            "sources": ["purchased"],
        },
        {  # cross-play with no decision — held back as a review row
            "npCommunicationId": "NPWR_H_00",
            "name": "Ambiguous",
            "displayName": "Ambiguous",
            "platform": "PS3,PS4",
            "sources": ["titles"],
        },
    ]
    result = psn.import_merged(db_session, user, merged)
    assert result["added"] == 1
    assert result["needs_review"] == 1
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="CUSA1111_00").count() == 1
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_H_00").count() == 0

    # Confirming creates exactly what was chosen, and a later sync neither
    # re-asks nor duplicates it.
    psn.confirm_entry_decision(db_session, user, "NPWR_H_00", ["PS3"])
    result = psn.import_merged(db_session, user, merged)
    assert result["needs_review"] == 0
    rels = db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_H_00").all()
    assert [r.platform_id for r in rels] == [models.resolve_platform_id(db_session, "PS3")]


def test_two_trophy_sets_for_one_title_are_two_review_rows(db_session, monkeypatch, tmp_path):
    """Sony returns two Crimsonland sets — 90% and 23% — both declaring the
    identical PS3,PSVITA,PS4, and nothing anywhere says which console each
    covers. They're two real progress records wanting two entries, so they get
    asked about separately, labelled by the only thing that tells them apart."""
    merged = [
        {
            "npCommunicationId": "NPWR06670_00",
            "titleId": "CUSA00426_00",
            "name": "Crimsonland",
            "displayName": "Crimsonland",
            "normalizedName": "crimsonland",
            "platform": "PS3,PSVITA,PS4",
            "trophyProgress": 90,
            "trophies": {"bronze": 21},
            "earnedTrophies": {"bronze": 19},
            "playByCategory": {"ps4_game": 56},
            "sources": ["played", "purchased", "titles"],
        },
        {
            "npCommunicationId": "NPWR06085_00",
            "name": "Crimsonland",
            "displayName": "Crimsonland",
            "normalizedName": "crimsonland",
            "platform": "PS3,PSVITA,PS4",
            "trophyProgress": 23,
            "trophies": {"bronze": 21},
            "earnedTrophies": {"bronze": 5},
            "sources": ["titles"],
        },
    ]
    user = _user(db_session)
    _seed_review(db_session, user, merged)
    rows = psn.import_review_rows(db_session, 1)

    assert len(rows) == 2
    assert [r["set_index"] for r in rows] == [1, 2]
    assert all(r["set_count"] == 2 for r in rows)
    # Trophy progress is the discriminator, so it has to be on the row.
    assert sorted(r["trophy_progress"] for r in rows) == [23, 90]
    assert sorted((r["trophy_earned"], r["trophy_defined"]) for r in rows) == [(5, 21), (19, 21)]


def test_playtime_is_not_attributed_across_several_trophy_sets(db_session, monkeypatch, tmp_path):
    """The 56m of ps4_game play merged onto Crimsonland's 90% set by title
    alone, and that set is the Vita one. With more than one set for a title the
    attribution is unknowable, so the row must not quote it as a platform hint."""
    merged = [
        {
            "npCommunicationId": "NPWR06670_00",
            "name": "Crimsonland",
            "displayName": "Crimsonland",
            "normalizedName": "crimsonland",
            "platform": "PS3,PSVITA,PS4",
            "trophyProgress": 90,
            "playByCategory": {"ps4_game": 56},
            "sources": ["played", "purchased", "titles"],
        },
        {
            "npCommunicationId": "NPWR06085_00",
            "name": "Crimsonland",
            "displayName": "Crimsonland",
            "normalizedName": "crimsonland",
            "platform": "PS3,PSVITA,PS4",
            "trophyProgress": 23,
            "sources": ["titles"],
        },
    ]
    user = _user(db_session)
    _seed_review(db_session, user, merged)
    rows = psn.import_review_rows(db_session, 1)

    played_row = next(r for r in rows if r["trophy_progress"] == 90)
    assert played_row["total_minutes"] == 0
    assert all(o["minutes"] == 0 for o in played_row["options"])
    assert "2 trophy sets" in played_row["reason"]
    assert "PS4" not in played_row["reason"]


def test_owned_platforms_narrows_the_default_to_hardware_you_have(db_session):
    """An account whose only evidence is Vita defaults a cross-play set to Vita
    — not to every platform Sony lists on it."""
    _seed_platforms(db_session)
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita"))
    db_session.commit()
    user = _user(db_session, "vita")
    merged = [
        {  # single-platform set: the import places it, proving a Vita
            "npCommunicationId": "NPWR_V_00",
            "name": "Vita Only",
            "displayName": "Vita Only",
            "platform": "PSVITA",
            "sources": ["titles"],
        },
        {  # the ambiguous one
            "npCommunicationId": "NPWR_C_00",
            "name": "Cross",
            "displayName": "Cross",
            "platform": "PS3,PSVITA,PS4",
            "sources": ["titles"],
        },
    ]
    psn.import_merged(db_session, user, merged)

    row = next(r for r in psn.import_review_rows(db_session, user.id) if r["name"] == "Cross")
    assert [o["platform"] for o in row["options"] if o["selected"]] == ["PSVITA"]


def test_psn_review_card_view_has_the_carousel(client, db_session, monkeypatch, tmp_path):
    """Card view is the same stack + sticky arrows + counter as the other
    review queues, not a bespoke layout."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    merged = [
        {
            "npCommunicationId": "NPWR_CARD_00",
            "name": "Cross",
            "displayName": "Cross",
            "platform": "PS3,PSVITA",
            "sources": ["titles"],
        }
    ]
    _seed_review(db_session, user, merged)

    body = client.get("/tools/psn-review?view=card").content
    assert b'id="psn-stack"' in body
    assert b"cgt-match-stage" in body and b"cgt-match-card" in body
    assert b"cgt-match-nav--sticky" in body
    assert b"psnCardNav(-1)" in body and b"psnCardNav(1)" in body
    assert b'id="psn-nav-counter"' in body
    # Per-card actions, same contract as the list rows.
    assert b"/tools/psn-review/NPWR_CARD_00/confirm" in body
    assert b"/tools/psn-review/NPWR_CARD_00/dismiss" in body


def _sgdb_stub(monkeypatch, url="https://sgdb/psn-grid.png"):
    from backend import steamgriddb

    monkeypatch.setattr(steamgriddb, "search_games", lambda k, q: [{"id": 1, "name": q}])
    monkeypatch.setattr(
        steamgriddb,
        "get_grids_for_game",
        lambda k, gid, orientation, page=0: [{"url": url}] if orientation == "h" else [],
    )
    return steamgriddb


def test_review_thumbnail_gaps_only_asks_about_pending_rows(db_session):
    """Art is fetched for rows still awaiting a decision — not for already
    cached ones, and not for ones the user has actioned."""
    _seed_platforms(db_session)
    user = _user(db_session, "gaps")
    merged = [
        {"npCommunicationId": "NPWR_G1_00", "name": "Ask Me", "displayName": "Ask Me", "platform": "PS3,PS4", "sources": ["titles"]},
        {"npCommunicationId": "NPWR_G2_00", "name": "Got Art", "displayName": "Got Art", "platform": "PS3,PS4", "sources": ["titles"]},
        {"npCommunicationId": "NPWR_G4_00", "name": "Decided", "displayName": "Decided", "platform": "PS3,PS4", "sources": ["titles"]},
    ]
    _seed_review(db_session, user, merged)
    psn.save_review_thumbnails(db_session, user.id, {"NPWR_G2_00": "https://sgdb/already.png"})
    psn.confirm_entry_decision(db_session, user, "NPWR_G4_00", ["PS4"])

    assert [g["external_id"] for g in psn.review_thumbnail_gaps(db_session, user.id)] == ["NPWR_G1_00"]


def test_fill_psn_review_thumbnails_caches_onto_the_row(db_session, monkeypatch):
    """Review rows have no library entry to hang art on, so the row itself
    carries it — the same job ImportCandidate.thumbnail_url does."""
    steamgriddb = _sgdb_stub(monkeypatch)
    _seed_platforms(db_session)
    user = _user(db_session, "art")
    user.steamgriddb_api_key = "sgdb-key"
    db_session.commit()
    merged = [{"npCommunicationId": "NPWR_A1_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PS4", "sources": ["titles"]}]
    _seed_review(db_session, user, merged)

    result = steamgriddb.fill_psn_review_thumbnails(db_session, user)
    assert result == {"filled": 1, "no_candidate": 0, "errored": 0}

    # Cached, and used in preference to PSN's square icon on the next render.
    rows = psn.import_review_rows(db_session, user.id)
    assert rows[0]["image"] == "https://sgdb/psn-grid.png"
    # Re-running asks SGDB for nothing — the gap is closed.
    assert psn.review_thumbnail_gaps(db_session, user.id) == []


def test_review_row_prefers_sgdb_art_over_psns_square_icon(db_session):
    """PSN's own image is an icon0.png — the wrong shape for a review card and
    the reason this queue looked nothing like the others."""
    _seed_platforms(db_session)
    user = _user(db_session, "icon")
    merged = [
        {
            "npCommunicationId": "NPWR_I_00",
            "name": "Cross",
            "displayName": "Cross",
            "platform": "PS3,PS4",
            "sources": ["titles"],
            "image": {"url": "https://psn/icon0.png"},
        }
    ]
    _seed_review(db_session, user, merged)
    psn.save_review_thumbnails(db_session, user.id, {"NPWR_I_00": "https://sgdb/grid.png"})
    assert psn.import_review_rows(db_session, user.id)[0]["image"] == "https://sgdb/grid.png"


def test_fill_psn_review_thumbnails_needs_an_sgdb_key(db_session, monkeypatch, tmp_path):
    from backend import steamgriddb

    user = models.User(name="t2", username="t2", password_hash="x", api_token="tok2")
    db_session.add(user)
    db_session.commit()
    with pytest.raises(ValueError, match="SteamGridDB"):
        steamgriddb.fill_psn_review_thumbnails(db_session, user)


# ─── one-click sync (#157) ─────────────────────────────────────────────────


def _stub_crawl(monkeypatch, purchased=None, titles_=None, played=None):
    """Stub the three PSN feeds + auth so a sync can run end to end offline."""
    monkeypatch.setattr(psn, "_exchange_npsso", lambda npsso: "tok")
    monkeypatch.setattr(psn, "_resolve_profile", lambda tok, oid: ("acct-1", None))
    monkeypatch.setattr(psn, "_fetch_purchased", lambda tok, acct: purchased or [])
    monkeypatch.setattr(psn, "_fetch_trophy_titles", lambda tok, acct: (titles_ or [], len(titles_ or [])))
    monkeypatch.setattr(psn, "_fetch_played", lambda tok, acct: (played or [], len(played or [])))


def test_sync_library_crawls_and_creates_entries_in_one_step(db_session, monkeypatch, tmp_path):
    """#157: one click crawls PSN and adds what it can place, the Steam way —
    no separate import run standing between the crawl and the library."""
    _seed_platforms(db_session)
    monkeypatch.setattr(psn, "DATA_DIR", str(tmp_path))
    user = models.User(name="s", username="s", password_hash="x", api_token="stok", psn_npsso="n" * 64, psn_online_id="dude")
    db_session.add(user)
    db_session.commit()
    _stub_crawl(
        monkeypatch,
        purchased=[{"name": "Solo Game", "titleId": "CUSA1111_00", "platform": "PS4", "membership": "NONE"}],
    )

    result = psn.sync_library(db_session, user)

    assert result["added"] == 1
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="CUSA1111_00").count() == 1
    # The crawl's report rides along so one toast can describe both halves.
    assert result["report"]["merged_total"] == 1


def test_sync_holds_ambiguous_games_back_for_review(db_session, monkeypatch, tmp_path):
    """The write is unguarded now, so the safety has to live in the split: a
    cross-play set PSN can't place is still never guessed at."""
    _seed_platforms(db_session)
    monkeypatch.setattr(psn, "DATA_DIR", str(tmp_path))
    user = models.User(name="s2", username="s2", password_hash="x", api_token="stok2", psn_npsso="n" * 64, psn_online_id="dude")
    db_session.add(user)
    db_session.commit()
    _stub_crawl(
        monkeypatch,
        titles_=[
            {
                "npCommunicationId": "NPWR_SY_00",
                "trophyTitleName": "Cross Play",
                "trophyTitlePlatform": "PS3,PSVITA,PS4",
                "definedTrophies": {"bronze": 1},
                "earnedTrophies": {"bronze": 1},
                "progress": 100,
            }
        ],
    )

    result = psn.sync_library(db_session, user)

    assert result["added"] == 0
    assert result["needs_review"] == 1
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_SY_00").count() == 0
    assert [r["name"] for r in psn.import_review_rows(db_session, user.id)] == ["Cross Play"]


def test_resync_keeps_review_decisions_and_cached_art(db_session, monkeypatch, tmp_path):
    """Decisions and cached art live on the review row, so a re-sync keeps them
    for free. Under the old snapshot file a crawl rewrote the whole thing and
    silently discarded both — which one-click made routine (#157)."""
    _seed_platforms(db_session)
    monkeypatch.setattr(psn, "DATA_DIR", str(tmp_path))
    user = _user(db_session, "resync")
    trophy_set = [
        {
            "npCommunicationId": "NPWR_RS_00",
            "trophyTitleName": "Cross Play",
            "trophyTitlePlatform": "PS3,PS4",
            "definedTrophies": {"bronze": 1},
            "earnedTrophies": {"bronze": 1},
            "progress": 100,
        }
    ]
    _stub_crawl(monkeypatch, titles_=trophy_set)
    psn.sync_library(db_session, user)

    # User answers the question, and the art job caches a grid.
    psn.confirm_entry_decision(db_session, user, "NPWR_RS_00", ["PS4"])
    psn.save_review_thumbnails(db_session, user.id, {"NPWR_RS_00": "https://sgdb/kept.png"})
    assert psn.import_review_rows(db_session, user.id) == []

    psn.sync_library(db_session, user)

    cand = db_session.query(models.PsnReviewCandidate).filter_by(user_id=user.id, external_id="NPWR_RS_00").one()
    assert cand.status == "confirmed"
    assert cand.chosen_platforms == ["PS4"]
    assert cand.thumbnail_url == "https://sgdb/kept.png"
    assert psn.import_review_rows(db_session, user.id) == []  # not re-asked
    assert psn.review_thumbnail_gaps(db_session, user.id) == []  # art not re-fetched


def test_sync_toast_reports_every_review_queue(db_session):
    """One job means one toast, so it's the only place the three queues can be
    surfaced. A count silently missing here is work the user never hears about."""
    from backend import integrations

    user = models.User(name="s4", username="s4", password_hash="x", api_token="stok4")
    db_session.add(user)
    db_session.commit()
    msg = integrations._format_sync_result(
        db_session,
        user,
        "psn_sync",
        {
            "added": 12,
            "updated": 3,
            "skipped_no_platform": 0,
            "skipped_no_id": 0,
            "skipped_non_game": 0,
            "skipped_conflict": 0,
            "needs_review": 54,
            "played_only_pending": 7,
            "match_candidates": 9,
        },
    )
    assert "PSN sync complete" in msg
    assert "+12 entries" in msg
    assert "54 cross-play games need a platform" in msg
    assert "7 played-only games" in msg
    assert "9 possible duplicates" in msg


def _run_psn_sync_job(db_session, monkeypatch, user, result):
    """Drive _run_sync_job for a stubbed psn_sync and return the jobs it spawned."""
    import asyncio

    from backend import integrations, jobs
    from backend import psn as psn_mod

    jobs.clear_all()
    monkeypatch.setattr(psn_mod, "sync_library", lambda db, u: result)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(integrations, "_run_sgdb_fill_all_job", _noop)
    db_session.close = lambda: None

    job = jobs.create(user_id=user.id, kind="psn_sync", label="Library sync")
    with patch("backend.integrations.SessionLocal", return_value=db_session):
        asyncio.run(integrations._run_sync_job(job.id, user.id, "psn_sync"))
    return {j.kind for j in jobs.active_jobs_for(user.id)}


def test_sync_chains_store_metadata_when_it_added_entries(db_session, monkeypatch):
    """Store metadata is part of syncing, not a step to remember afterwards."""
    user = _user(db_session, "chain")
    kinds = _run_psn_sync_job(db_session, monkeypatch, user, {**_psn_import_result(), "added": 5})
    assert "psn_store_refresh" in kinds


def test_sync_skips_store_metadata_when_nothing_was_added(db_session, monkeypatch):
    """Nothing new means nothing to enrich — don't spend a rate-limited crawl
    over the store for a no-op sync."""
    user = _user(db_session, "nochain")
    kinds = _run_psn_sync_job(db_session, monkeypatch, user, {**_psn_import_result(), "added": 0})
    assert "psn_store_refresh" not in kinds


def test_tools_card_counts_both_review_queues(client, db_session):
    """Two tabs, one card — a count covering only cross-play would under-report
    the work and send the user to a page with more waiting than it promised."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()

    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_TC_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PS4", "sources": ["titles"]}],
    )
    _seed_review(
        db_session,
        user,
        [
            {
                "titleId": "PPSA_TC_00",
                "name": "Disc Game",
                "displayName": "Disc Game",
                "category": "ps5_native_game",
                "playDuration": "PT20H0M0S",
                "playCount": 30,
                "sources": ["played"],
            }
        ],
        kind="played_only",
    )

    from backend.pages import _psn_pending

    assert _psn_pending(db_session, user) == 2
    assert b"Need a decision" in client.get("/tools").content


def test_review_tabs_carry_the_view_and_reset_the_platform_filter(client, db_session):
    """A platform chosen on the cross-play tab means nothing on played-only and
    would silently empty it."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [
            {
                "titleId": "PPSA_TB_00",
                "name": "Disc Game",
                "displayName": "Disc Game",
                "category": "ps5_native_game",
                "playDuration": "PT20H0M0S",
                "playCount": 30,
                "sources": ["played"],
            }
        ],
        kind="played_only",
    )

    body = client.get("/tools/psn-review?kind=played_only&view=card").content
    assert b'id="psn-stack"' in body
    assert b"cgt-match-nav--sticky" in body
    assert b"/tools/psn-review/PPSA_TB_00/played-only/import" in body
    # The filter form carries the kind so the view toggle doesn't drop the tab.
    assert b'id="psn-kind-field"' in body
    assert b"psnSetKind" in body


def test_enrichment_fires_only_once_the_review_queue_empties(db_session, monkeypatch):
    """Rows confirmed after a sync miss its enrichment pass, so the review runs
    one itself — when the LAST row is decided, not on every click. Clicking
    through 54 rows must not spawn 54 pairs of jobs."""
    from backend import integrations, jobs
    from backend import pages_match_review as pmr

    _seed_platforms(db_session)
    jobs.clear_all()
    user = _user(db_session, "enrich")
    _seed_review(
        db_session,
        user,
        [
            {"npCommunicationId": "NPWR_E1_00", "name": "One", "displayName": "One", "platform": "PS3,PS4", "sources": ["titles"]},
            {"npCommunicationId": "NPWR_E2_00", "name": "Two", "displayName": "Two", "platform": "PS3,PS4", "sources": ["titles"]},
        ],
    )

    fired = []
    monkeypatch.setattr(integrations, "kick_psn_enrichment", lambda u: fired.append(u.id))

    # One row left pending — nothing fires yet.
    psn.confirm_entry_decision(db_session, user, "NPWR_E1_00", ["PS4"])
    pmr._maybe_enrich(db_session, user)
    assert fired == []

    # Last row decided — one pass.
    psn.confirm_entry_decision(db_session, user, "NPWR_E2_00", ["PS4"])
    pmr._maybe_enrich(db_session, user)
    assert fired == [user.id]


def test_review_pending_count_spans_both_queues(db_session):
    _seed_platforms(db_session)
    user = _user(db_session, "count")
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_PC_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PS4", "sources": ["titles"]}],
    )
    _seed_review(
        db_session,
        user,
        [{"titleId": "PPSA_PC_00", "name": "Disc", "displayName": "Disc", "category": "ps5_native_game", "sources": ["played"]}],
        kind="played_only",
    )
    assert psn.review_pending_count(db_session, user.id) == 2

    psn.dismiss_entry_decision(db_session, user, "NPWR_PC_00")
    assert psn.review_pending_count(db_session, user.id) == 1
