"""PSN crawl + snapshot tests (PR 1 of #135 — no library writes yet)."""

from unittest.mock import MagicMock, patch

import httpx
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


def test_a_bonus_product_does_not_swallow_the_game_it_came_with():
    """Real shapes from the 2026-08-11 crawl (#180).

    concept.titleIds lists every SKU Sony shipped for a concept — 37 of them for
    ELDEN RING, including CUSA30022_00, which belongs to the "ELDEN RING
    Adventure Guide" pre-order bonus. That guide founded a purchased record, and
    the 140-hour PS5 play record merged into it: the library showed the bonus
    item's name and the real game survived only as an orphaned trophy row.

    The play record is PS5 and the guide is PS4, so platform overlap refuses the
    match and the game keeps its own identity."""
    purchased = [
        _purchased("ELDEN RING Adventure Guide", "CUSA30022_00", platform="PS4", product_id="UP0700-PPSA04610_00-EREPBONUSSET0000")
    ]
    titles = [
        {
            "npCommunicationId": "NPWR25067_00",
            "trophyTitleName": "ELDEN RING™",
            "trophyTitlePlatform": "PS5",
            "progress": 100,
        }
    ]
    played = [
        {
            "titleId": "PPSA04610_00",
            "name": "ELDEN RING",
            "category": "ps5_native_game",
            "playDuration": "PT140H52M31S",
            "concept": {"titleIds": ["CUSA30022_00", "PPSA04610_00", "CUSA18746_00"]},
        }
    ]
    merged = {m["normalizedName"]: m for m in psn.merge_library(purchased, titles, played)["merged"]}

    # The guide is now dropped outright by the BONUSSET marker, so it is not
    # even a record to absorb anything. Belt and braces: the platform guard
    # below is what protects the case where a non-game slips the filter, which
    # is how this one got through for weeks.
    assert "eldenringadventureguide" not in merged, "a pre-order bonus is not a game"

    game = merged["eldenring"]
    assert game["playDuration"] == "PT140H52M31S"
    assert game["npCommunicationId"] == "NPWR25067_00", "the trophy set belongs to the game, not the guide"


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
    mistaken for a PC copy (Spider-Man 2: 28h PS5 + a 37min PC touch)."""
    titles = [{"npCommunicationId": "NPWR700_00", "trophyTitleName": "Cross Play Game", "trophyTitlePlatform": "PS5,PSPC", "progress": 50}]
    played = [
        {"npCommunicationId": "NPWR700_00", "name": "Cross Play Game", "category": "ps5_native_game", "playDuration": "PT28H"},
        {"npCommunicationId": "NPWR700_00", "name": "Cross Play Game", "category": "pspc_game", "playDuration": "PT37M"},
    ]
    item = psn.merge_library([], titles, played)["merged"][0]
    assert item["playCategories"] == ["ps5_native_game", "pspc_game"]
    assert psn.is_pc_copy(item) is False


def test_is_pc_copy_uses_evidence_not_sonys_platform_string():
    """Sony is not consistent here: Stellar Blade came back PSPC-only while the
    Until Dawn remake came back "PS5,PSPC" for the identical situation — a game
    owned on Steam, played on PC, never owned on a PlayStation. Requiring the
    set to be PC-ONLY missed the second one and minted a phantom PS5 entry.

    The test is: mentions PSPC and there is no PlayStation-side evidence — no
    purchase, no native play time. The Steam library is NOT required. Requiring
    it made the answer depend on whether Steam happened to be synced yet, which
    minted a phantom PS5 entry for MARVEL Tokon."""
    titles = [{"npCommunicationId": "NPWR701_00", "trophyTitleName": "PC Only Game", "trophyTitlePlatform": "PS5,PSPC", "progress": 100}]
    played = [{"npCommunicationId": "NPWR701_00", "name": "PC Only Game", "category": "pspc_game", "playDuration": "PT80H"}]
    item = psn.merge_library([], titles, played)["merged"][0]
    assert item["playCategories"] == ["pspc_game"]
    assert psn.is_pc_copy(item) is True

    # The Until Dawn shape: trophy-only, no play record at all, PS5,PSPC.
    # 100% trophies prove it was PLAYED, never on which platform — so trophies
    # alone must not save it from the skip.
    remake = psn.merge_library(
        [],
        [{"npCommunicationId": "NPWR37139_00", "trophyTitleName": "PC Only Game", "trophyTitlePlatform": "PS5,PSPC", "progress": 100}],
        [],
    )["merged"][0]
    assert psn.is_pc_copy(remake) is True

    # A real purchase is PlayStation-side evidence; keep it.
    assert psn.is_pc_copy({**remake, "sources": ["purchased", "titles"]}) is False
    # No PSPC anywhere → never in scope.
    assert psn.is_pc_copy({"platform": "PS5", "sources": ["purchased"]}) is False
    # Conflicting evidence — played on PC AND natively — means a PlayStation
    # copy exists, so this is not merely the PC one.
    both = psn.merge_library(
        [],
        [{"npCommunicationId": "NPWR702_00", "trophyTitleName": "Both Ways", "trophyTitlePlatform": "PS5,PSPC"}],
        [
            {"npCommunicationId": "NPWR702_00", "name": "Both Ways", "category": "pspc_game", "playDuration": "PT10H"},
            {"npCommunicationId": "NPWR702_00", "name": "Both Ways", "category": "ps5_native_game", "playDuration": "PT5H"},
        ],
    )["merged"][0]
    assert psn.is_pc_copy(both) is False


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
    # Stellar Blade imports (it has a titleId). Demon's Souls is trophy-only,
    # so its name came from a trophy set and it waits for review instead (#180).
    assert result["added"] == 1
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

    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR555_00").count() == 0
    held = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR555_00").one()
    assert held.kind == "title_fix" and held.status == "pending"

    # Played-only stayed out.
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="CUSA14394_00").count() == 0

    # Idempotent re-run: no new rows.
    result2 = psn.import_merged(db_session, user, merged)
    assert result2["added"] == 0
    assert result2["updated"] == 1
    assert db_session.query(models.GameRelease).filter_by(source="psn").count() == 1


def test_import_skips_pc_copies_whether_or_not_steam_is_synced(db_session, monkeypatch, tmp_path):
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
    # BOTH pspc-only rows are skipped now. Requiring the game to be in the Steam
    # library made the answer depend on whether Steam happened to be synced yet
    # — which minted a phantom PS5 entry for MARVEL Tokon. With no purchase and
    # no native play there is no PlayStation copy to have an opinion about.
    assert result["skipped_pc_dupe"] == 2
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR37356_00").count() == 0
    # ...the native PS5 copy imported (it has a titleId)...
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="PPSA03016_00").count() == 1
    # The non-Steam PC game is skipped as a PC copy too — no library entry.
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR90001_00").count() == 0


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
    # Trophy-only, so it waits for review instead of importing — but the
    # trophy-set suffix is still stripped, because the queue has to show the
    # name a human would recognise, not "God of War II Trophies" (#180).
    held = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR555_00").one()
    assert held.title == "God of War II"


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


def test_psn_sync_followups_run_in_sequence_store_metadata_first(db_session, monkeypatch):
    """These were three concurrent tasks. SQLite in WAL mode allows one writer,
    each holds its transaction across an HTTP call, and busy_timeout is 10s — so
    a writer queued behind the other two died on "database is locked".

    Order matters too: store metadata retitles, and both artwork passes look
    SGDB up BY TITLE, so running it last fetched covers against whatever name
    PSN happened to return."""
    import asyncio

    from backend import integrations, jobs

    jobs.clear_all()
    user = _user(db_session, "chain2")
    user.steamgriddb_api_key = "sgdb-key"
    db_session.commit()

    ran = []

    async def _fake_sync_job(job_id, user_id, kind):
        ran.append(kind)

    async def _fake_fill(job_id, user_id, sources=None):
        ran.append("sgdb_fill_all")

    monkeypatch.setattr(integrations, "_run_sync_job", _fake_sync_job)
    monkeypatch.setattr(integrations, "_run_sgdb_fill_all_job", _fake_fill)

    asyncio.run(integrations._run_psn_followups(user.id, added=True, needs_review=True, has_sgdb_key=True))
    assert ran == ["psn_store_refresh", "psn_igdb_titles", "psn_igdb_link", "sgdb_fill_all", "psn_review_art"]
    jobs.clear_all()


def test_psn_followups_skip_what_does_not_apply(db_session, monkeypatch):
    """Nothing added means nothing to enrich; no SGDB key means no art passes."""
    import asyncio

    from backend import integrations, jobs

    jobs.clear_all()
    user = _user(db_session, "chain3")
    ran = []

    async def _fake_sync_job(job_id, user_id, kind):
        ran.append(kind)

    async def _fake_fill(job_id, user_id, sources=None):
        ran.append("sgdb_fill_all")

    monkeypatch.setattr(integrations, "_run_sync_job", _fake_sync_job)
    monkeypatch.setattr(integrations, "_run_sgdb_fill_all_job", _fake_fill)

    asyncio.run(integrations._run_psn_followups(user.id, added=False, needs_review=False, has_sgdb_key=False))
    assert ran == []

    ran.clear()
    asyncio.run(integrations._run_psn_followups(user.id, added=True, needs_review=True, has_sgdb_key=False))
    assert ran == ["psn_store_refresh", "psn_igdb_titles", "psn_igdb_link"], "no SGDB key kills the artwork passes, not the IGDB steps"
    jobs.clear_all()


def test_one_failing_followup_does_not_strand_the_rest(db_session, monkeypatch):
    """A chain is only worth serialising if a broken link doesn't stop it."""
    import asyncio

    from backend import integrations, jobs

    jobs.clear_all()
    user = _user(db_session, "chain4")
    ran = []

    async def _fake_sync_job(job_id, user_id, kind):
        if kind == "psn_store_refresh":
            raise RuntimeError("store is down")
        ran.append(kind)

    async def _fake_fill(job_id, user_id, sources=None):
        ran.append("sgdb_fill_all")

    monkeypatch.setattr(integrations, "_run_sync_job", _fake_sync_job)
    monkeypatch.setattr(integrations, "_run_sgdb_fill_all_job", _fake_fill)

    asyncio.run(integrations._run_psn_followups(user.id, added=True, needs_review=True, has_sgdb_key=True))
    assert ran == ["psn_igdb_titles", "psn_igdb_link", "sgdb_fill_all", "psn_review_art"]
    jobs.clear_all()


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


def test_import_review_rows_lists_rows_awaiting_any_decision(db_session):
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
        {  # single platform, but trophy-only: its NAME came from a trophy set,
            # so it is held back for review even though its platform is settled.
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

    # One queue: a cross-play row (platform question) and a trophy-only row
    # (name question) both wait here. "Settled" is store-backed and single
    # platform, so it imported without asking anything.
    assert sorted(r["name"] for r in rows) == ["Cross", "TrophyOnly"]
    cross = next(r for r in rows if r["name"] == "Cross")
    assert next(r for r in rows if r["name"] == "TrophyOnly")["kind_is_title_fix"] is True
    options = {o["platform"]: o for o in cross["options"]}
    assert set(options) == {"PS4", "PS5"}
    # Both pre-ticked: PS4 is proven by the two settled games the import just
    # created as PS4 entries, PS5 by the logged play time. Defaulting to
    # everything the account can actually run beats defaulting to one guess.
    assert options["PS5"]["selected"] is True
    assert options["PS4"]["selected"] is True

    # Shadow mode (#180). The row carries only whether it WOULD have imported
    # unattended — the wordier label lives on review_verdict, which is what the
    # eventual automation gates on, not something the row renders.
    assert cross["verdict_auto"] is False


def test_verdict_needs_both_halves_settled(db_session):
    """ "Would auto-import" means BOTH questions are answered. A row is routinely
    certain about one and not the other — a single-platform trophy set with no
    IGDB match (HITMAN 2 Expansion) is settled on platform and unknown on
    identity — so the badge names the holdout instead of a useless "not sure".

    A pending RENAME is not settled identity: proposing a different name is a
    judgement call by definition, and a wrong id attaches wrong metadata
    quietly."""
    single_platform = {"name": "Solo", "npCommunicationId": "NPWR_V_00", "platform": "PS4", "sources": ["titles"]}

    class _Cand:
        def __init__(self, igdb_id=None, title=None):
            self.proposed_igdb_id, self.proposed_title = igdb_id, title

    # settled platform + exact IGDB match (an id, nothing to rename)
    got = psn.review_verdict(_Cand(igdb_id=42), single_platform)
    assert got["auto"] is True
    assert got["label"] == "Would auto-import"

    # settled platform, no IGDB identity at all
    assert psn.review_verdict(_Cand(), single_platform)["label"] == "Needs you: identity"

    # an id, but a rename is being proposed — a judgement call, not a verdict.
    # NOT an identity question: IGDB knows the game, the name awaits approval.
    renaming = psn.review_verdict(_Cand(igdb_id=42, title="Something Else"), single_platform)
    assert renaming["auto"] is False
    assert renaming["label"] == "Needs you: name"

    # platform unsettled, identity fine
    two_platform = {**single_platform, "platform": "PS4,PS5"}
    assert psn.review_verdict(_Cand(igdb_id=42), two_platform)["label"] == "Needs you: platform"


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
    # Count the rendered checkboxes, not every occurrence of the string — the
    # bulk-select JS also contains an input[name="platforms"] selector.
    assert body.count(b"cgt-psn-plat-check") == 2
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
    monkeypatch.setattr(steamgriddb, "get_heroes_for_game", lambda k, gid, page=0: [{"url": "https://sgdb/psn-hero.png"}])
    monkeypatch.setattr(steamgriddb, "get_logos_for_game", lambda k, gid, page=0: [{"url": "https://sgdb/psn-logo.png"}])
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
    psn.save_review_thumbnails(
        db_session, user.id, {"NPWR_G2_00": {"thumbnail_url": "https://sgdb/already.png", "hero_url": "https://sgdb/h.png"}}
    )
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

    # The list gets the grid; the card gets a hero with the logo over it, which
    # is what every other review card shows.
    rows = psn.import_review_rows(db_session, user.id)
    assert rows[0]["image"] == "https://sgdb/psn-grid.png"
    assert rows[0]["hero"] == "https://sgdb/psn-hero.png"
    assert rows[0]["logo"] == "https://sgdb/psn-logo.png"
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
    psn.save_review_thumbnails(
        db_session, user.id, {"NPWR_I_00": {"thumbnail_url": "https://sgdb/grid.png", "hero_url": "https://sgdb/h.png"}}
    )
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


def test_sync_attaches_igdb_suggestions_without_a_second_button(db_session, monkeypatch, tmp_path):
    """A row that says only "held back for review" is a chore, not a review —
    the user still has to work out what the game actually is.

    So the lookup runs INSIDE the sync (#180): the suggestion is already
    attached the first time the queue is opened, and there is no second job to
    remember to press."""
    _seed_platforms(db_session)
    # The lookup is platform-scoped, so the Vita needs its IGDB id to resolve.
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita", igdb_id=46))
    monkeypatch.setattr(psn, "DATA_DIR", str(tmp_path))
    user = models.User(name="chain", username="chain", password_hash="x", api_token="ctok", psn_npsso="n" * 64, psn_online_id="dude")
    user.twitch_client_id, user.twitch_client_secret = "cid", "sec"
    db_session.add(user)
    db_session.commit()

    _stub_crawl(
        monkeypatch,
        titles_=[
            {"npCommunicationId": "NPWR09999_00", "trophyTitleName": "SHINOVI VERSUS", "trophyTitlePlatform": "PSVITA", "progress": 20}
        ],
    )

    def fake_search(term, platform_ids):
        return [{"id": 77, "name": "Senran Kagura: Shinovi Versus", "platform_ids": platform_ids, "game_type": 0}]

    orig = psn._igdb_search_adapter
    psn._igdb_search_adapter = lambda *a, **k: fake_search
    try:
        result = psn.sync_library(db_session, user)
    finally:
        psn._igdb_search_adapter = orig

    cand = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR09999_00").one()
    assert cand.proposed_title == "Senran Kagura: Shinovi Versus", "the sync itself should have asked IGDB"
    assert cand.proposed_igdb_id == 77
    assert "proposals" in result, "and report what it found alongside the library delta"


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
    psn.save_review_thumbnails(
        db_session, user.id, {"NPWR_RS_00": {"thumbnail_url": "https://sgdb/kept.png", "hero_url": "https://sgdb/h.png"}}
    )
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


def test_tools_psn_card_syncs_from_the_card_like_steam(client, db_session):
    """Parity with the Steam card. Sending the user to a config page to press
    the button they came for is what made this feel two-step (#157)."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()

    body = client.get("/tools").content
    assert b'hx-post="/integrations/psn/sync"' in body
    assert b"Configure" in body


def test_tools_psn_card_offers_setup_not_sync_without_credentials(client, db_session):
    """No credentials, no Sync button — it could only fail."""
    _seed_platforms(db_session)
    _signup_and_login(client)
    body = client.get("/tools").content
    assert b'hx-post="/integrations/psn/sync"' not in body


def test_review_search_and_sort(client, db_session):
    """Same filter affordances as import review: search by title, plus a sort
    whose options differ per queue."""
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
                "npCommunicationId": "NPWR_S1_00",
                "name": "Alpha Game",
                "displayName": "Alpha Game",
                "platform": "PS3,PS4",
                "trophyProgress": 10,
                "sources": ["titles"],
            },
            {
                "npCommunicationId": "NPWR_S2_00",
                "name": "Zeta Game",
                "displayName": "Zeta Game",
                "platform": "PS3,PS4",
                "trophyProgress": 90,
                "sources": ["titles"],
            },
        ],
    )

    # Search narrows to one.
    body = client.get("/tools/psn-review?q=zeta").content
    assert b"Zeta Game" in body and b"Alpha Game" not in body

    # Default sort is title; progress sort puts the 90% row first.
    order = client.get("/tools/psn-review?sort=progress").content
    assert order.index(b"Zeta Game") < order.index(b"Alpha Game")
    order = client.get("/tools/psn-review?sort=name").content
    assert order.index(b"Alpha Game") < order.index(b"Zeta Game")

    # An unknown sort key falls back to title rather than arbitrary order.
    order = client.get("/tools/psn-review?sort=nonsense").content
    assert order.index(b"Alpha Game") < order.index(b"Zeta Game")

    # Sort options are per-queue, and ride along OOB on an HX swap because the
    # select lives outside the swap target.
    hx = client.get("/tools/psn-review?kind=played_only", headers={"HX-Request": "true"}).content
    assert b'hx-swap-oob="true"' in hx
    assert b"Playtime" in hx and b"Trophy progress" not in hx


def test_cross_buy_reference_restricts_defaults_on_both_axes(db_session, monkeypatch, tmp_path):
    """The reference file exists for what Sony's API can't express. Two
    independent axes break the offer-everything default, and either one is
    enough: separate trophy lists (this set covers ONE platform) or separate
    purchases (owning one implies nothing about the rest)."""
    import json as _j

    ref = tmp_path / "psn_cross_buy.json"
    ref.write_text(
        _j.dumps(
            {
                "titles": [
                    {"title": "Split Lists", "shared_trophies": False, "cross_buy": None, "notes": "Separate lists."},
                    {"title": "Paid Twice", "shared_trophies": True, "cross_buy": False, "notes": "Sold separately."},
                    {"title": "Real Cross Buy", "shared_trophies": True, "cross_buy": True, "notes": "One purchase, both."},
                ]
            }
        )
    )
    monkeypatch.setattr(psn, "_CROSS_BUY_PATH", str(ref))
    monkeypatch.setattr(psn, "_cross_buy_cache", None)

    _seed_platforms(db_session)
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita"))
    db_session.commit()
    user = _user(db_session, "xbuy")

    def row(name, **extra):
        return {
            "npCommunicationId": f"NPWR_{abs(hash(name)) % 9999}_00",
            "name": name,
            "displayName": name,
            "normalizedName": psn._normalized_name(name),
            "platform": "PS4,PSVITA",
            "sources": ["titles"],
            **extra,
        }

    _seed_review(db_session, user, [row("Split Lists"), row("Paid Twice"), row("Real Cross Buy")])
    by_name = {r["name"]: r for r in psn.import_review_rows(db_session, user.id)}

    # Only the not-cross-buy row restricts. Separate trophy lists say nothing
    # about OWNERSHIP — Axiom Verge and Bastion have per-platform lists AND
    # cross-buy, so both copies are genuinely owned and both belong.
    paid = by_name["Paid Twice"]
    assert [o["platform"] for o in paid["options"]] == ["PS4", "PSVITA"]
    assert [o["platform"] for o in paid["options"] if o["selected"]] == []
    assert paid["restricted"] is True
    assert "Sold separately" in paid["reason"]

    split = by_name["Split Lists"]
    assert split["restricted"] is False
    assert all(o["selected"] for o in split["options"])

    # A confirmed cross-buy title changes nothing — that's already the default.
    assert all(o["selected"] for o in by_name["Real Cross Buy"]["options"])
    assert by_name["Real Cross Buy"]["restricted"] is False


def test_restricted_row_still_credits_the_platform_you_bought(db_session, monkeypatch, tmp_path):
    """A purchase names the SKU bought — the only per-platform entitlement
    signal left once a trophy set has overwritten the platform string. Without
    it a bought-on-PS4 row would pre-tick nothing at all."""
    import json as _j

    ref = tmp_path / "psn_cross_buy.json"
    ref.write_text(_j.dumps({"titles": [{"title": "Split Lists", "shared_trophies": True, "cross_buy": False, "notes": ""}]}))
    monkeypatch.setattr(psn, "_CROSS_BUY_PATH", str(ref))
    monkeypatch.setattr(psn, "_cross_buy_cache", None)

    _seed_platforms(db_session)
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita"))
    db_session.commit()
    user = _user(db_session, "bought")
    _seed_review(
        db_session,
        user,
        [
            {
                "npCommunicationId": "NPWR_B_00",
                "titleId": "CUSA02052_00",
                "name": "Split Lists",
                "displayName": "Split Lists",
                "normalizedName": psn._normalized_name("Split Lists"),
                "platform": "PS4,PSVITA",
                "sources": ["purchased", "titles"],
            }
        ],
    )
    row = psn.import_review_rows(db_session, user.id)[0]
    assert [o["platform"] for o in row["options"] if o["selected"]] == ["PS4"]


def test_missing_cross_buy_reference_degrades_to_no_exceptions(monkeypatch, tmp_path):
    """A sync must never fail because reference data is absent or malformed."""
    monkeypatch.setattr(psn, "_CROSS_BUY_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(psn, "_cross_buy_cache", None)
    assert psn.cross_buy_exception({"displayName": "Anything"}) is None

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(psn, "_CROSS_BUY_PATH", str(bad))
    monkeypatch.setattr(psn, "_cross_buy_cache", None)
    assert psn.cross_buy_exception({"displayName": "Anything"}) is None


def test_shipped_cross_buy_reference_is_valid_and_ignores_unverified():
    """The file that actually ships parses, and `_unverified` never applies."""
    psn._cross_buy_cache = None
    index = psn._load_cross_buy()
    assert index["by_title"], "shipped reference should carry entries"
    assert psn.cross_buy_exception({"displayName": "Dragon's Crown"})["cross_buy"] is False
    # Parked claims must never RESTRICT. Some are separately confirmed as
    # cross-buy by the bulk list (Sly Cooper), which is fine — that only
    # reinforces the permissive default. What's parked is the unverified
    # *restriction* (its claimed one-way asymmetry), and that must not apply.
    for name in ("Terraria", "Sly Cooper: Thieves in Time", "Volume", "Helldivers"):
        hit = psn.cross_buy_exception({"displayName": name})
        assert hit is None or hit["restricts"] is False, name
    psn._cross_buy_cache = None


def test_curated_exception_beats_the_bulk_cross_buy_list(db_session, monkeypatch, tmp_path):
    """The confirmed-cross-buy list is bulk reference data; a curated entry is a
    deliberate correction. If both match, the correction has to win — otherwise
    a title appearing on both lists would silently lose its restriction."""
    import json as _j

    ref = tmp_path / "psn_cross_buy.json"
    ref.write_text(
        _j.dumps(
            {
                "titles": [{"title": "Contested Game", "shared_trophies": True, "cross_buy": False, "notes": "Sold separately."}],
                "cross_buy_confirmed": {"titles": ["Contested Game", "Plain Game"]},
            }
        )
    )
    monkeypatch.setattr(psn, "_CROSS_BUY_PATH", str(ref))
    monkeypatch.setattr(psn, "_cross_buy_cache", None)

    contested = psn.cross_buy_exception({"displayName": "Contested Game"})
    assert contested["cross_buy"] is False and contested["restricts"] is True
    plain = psn.cross_buy_exception({"displayName": "Plain Game"})
    assert plain["cross_buy"] is True and plain["restricts"] is False
    psn._cross_buy_cache = None


def test_shipped_reference_confirms_the_bulk_list_without_restricting():
    """A confirmed cross-buy title reinforces the default; it must never
    restrict, since restricting is what costs the user an entry they own."""
    psn._cross_buy_cache = None
    hit = psn.cross_buy_exception({"displayName": "Spelunky"})
    assert hit["cross_buy"] is True
    assert hit["restricts"] is False
    # ...while a curated no-cross-buy title still restricts.
    assert psn.cross_buy_exception({"displayName": "Dragon's Crown"})["restricts"] is True
    psn._cross_buy_cache = None


def test_regional_overrides_beat_the_eu_bulk_list_without_catching_siblings():
    """Sony's late PS2-collection Vita ports were cross-buy in EU and not in
    North America — the one place the two source lists genuinely disagree. The
    curated cross_buy=false has to beat the EU bulk confirmation.

    And it must not bleed onto the similarly-named separate games: Thieves in
    Time and Full Frontal Assault were cross-buy in both regions."""
    psn._cross_buy_cache = None
    for name in (
        "The Sly Trilogy",
        "The Sly Collection",
        "The Jak and Daxter Trilogy",
        "Jak and Daxter Collection",
        "The Ratchet & Clank Trilogy",
        "Ratchet & Clank Collection",
    ):
        hit = psn.cross_buy_exception({"displayName": name})
        assert hit is not None, name
        assert hit["cross_buy"] is False and hit["restricts"] is True, name
        assert "REGIONAL" in hit["notes"], name

    for name in ("Sly Cooper: Thieves in Time", "Ratchet & Clank: Full Frontal Assault"):
        hit = psn.cross_buy_exception({"displayName": name})
        assert hit["cross_buy"] is True and hit["restricts"] is False, name
    psn._cross_buy_cache = None


def test_review_page_is_never_cached(client, db_session):
    """Rows are derived live from the candidates and the cross-buy reference, so
    a reload is the refresh — but WKWebView caches header-less GETs and would
    pin a stale queue in a shell with no user-facing reload."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    assert client.get("/tools/psn-review").headers["cache-control"] == "no-store"
    assert client.get("/tools/psn-review", headers={"HX-Request": "true"}).headers["cache-control"] == "no-store"


def test_card_art_is_a_hero_with_a_logo_not_a_cropped_grid(db_session, monkeypatch):
    """The card hero box is shaped 96/22 for SGDB hero art (~1920x620). Feeding
    it a horizontal grid (460x215) cropped half the image away — titles came out
    sliced through the middle. Fetch the right shape rather than reshaping the
    box: hero plus logo overlay, same as import review and the detail pane."""
    steamgriddb = _sgdb_stub(monkeypatch)
    _seed_platforms(db_session)
    user = _user(db_session, "hero")
    user.steamgriddb_api_key = "sgdb-key"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_H1_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PS4", "sources": ["titles"]}],
    )
    steamgriddb.fill_psn_review_thumbnails(db_session, user)

    cand = db_session.query(models.PsnReviewCandidate).filter_by(user_id=user.id, external_id="NPWR_H1_00").one()
    assert cand.hero_url == "https://sgdb/psn-hero.png"
    assert cand.logo_url == "https://sgdb/psn-logo.png"
    assert cand.thumbnail_url == "https://sgdb/psn-grid.png"


def test_rows_cached_before_hero_art_get_topped_up(db_session, monkeypatch):
    """Rows filled when only a grid was fetched have a thumbnail but no hero.
    They must still count as gaps or they'd never get card art at all."""
    steamgriddb = _sgdb_stub(monkeypatch)
    _seed_platforms(db_session)
    user = _user(db_session, "topup")
    user.steamgriddb_api_key = "sgdb-key"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_T1_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PS4", "sources": ["titles"]}],
    )
    # Simulate the old cache shape: grid only.
    psn.save_review_thumbnails(db_session, user.id, {"NPWR_T1_00": {"thumbnail_url": "https://sgdb/old-grid.png"}})
    assert [g["external_id"] for g in psn.review_thumbnail_gaps(db_session, user.id)] == ["NPWR_T1_00"]

    steamgriddb.fill_psn_review_thumbnails(db_session, user)
    assert psn.review_thumbnail_gaps(db_session, user.id) == []


def test_hero_fetch_failure_keeps_the_thumbnail(db_session, monkeypatch):
    """Hero and logo are cosmetic extras — losing them must not cost the
    thumbnail already in hand, or a flaky call blanks the list view too."""
    from backend import steamgriddb

    _seed_platforms(db_session)
    user = _user(db_session, "flaky")
    user.steamgriddb_api_key = "sgdb-key"
    db_session.commit()
    monkeypatch.setattr(steamgriddb, "search_games", lambda k, q: [{"id": 1, "name": q}])
    monkeypatch.setattr(steamgriddb, "get_grids_for_game", lambda k, gid, orientation, page=0: [{"url": "https://sgdb/g.png"}])

    def boom(*a, **k):
        raise RuntimeError("SGDB 503")

    monkeypatch.setattr(steamgriddb, "get_heroes_for_game", boom)
    monkeypatch.setattr(steamgriddb, "get_logos_for_game", boom)

    art = steamgriddb._placeholder_art("sgdb-key", "Anything")
    assert art == {"thumbnail_url": "https://sgdb/g.png"}


def test_bulk_confirm_uses_each_row_s_own_platform_selection(client, db_session):
    """The point of the cross-buy reference is that most rows arrive pre-ticked
    correctly, so bulk confirm carries a per-row platform list rather than just
    ids — otherwise it could only guess what to create."""
    import json as _j

    _seed_platforms(db_session)
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita"))
    db_session.commit()
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [
            {"npCommunicationId": "NPWR_BC1_00", "name": "Both", "displayName": "Both", "platform": "PS4,PSVITA", "sources": ["titles"]},
            {"npCommunicationId": "NPWR_BC2_00", "name": "One", "displayName": "One", "platform": "PS4,PSVITA", "sources": ["titles"]},
            {
                "npCommunicationId": "NPWR_BC3_00",
                "name": "Untouched",
                "displayName": "Untouched",
                "platform": "PS4,PS3",
                "sources": ["titles"],
            },
        ],
    )

    payload = _j.dumps({"NPWR_BC1_00": ["PS4", "PSVITA"], "NPWR_BC2_00": ["PS4"]})
    r = client.post("/tools/psn-review/bulk-confirm", data={"selections": payload})
    assert r.status_code == 200
    assert b"Confirmed 2 games" in r.content
    assert b"added 3 library entries" in r.content

    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_BC1_00").count() == 2
    assert db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_BC2_00").count() == 1
    # An unselected row is untouched and stays in the queue.
    assert [r_["external_id"] for r_ in psn.import_review_rows(db_session, user.id)] == ["NPWR_BC3_00"]


def test_bulk_confirm_drops_platforms_the_trophy_set_does_not_cover(client, db_session):
    """A stale page can post anything, and an entry this can't un-create is the
    expensive mistake — so the per-row validation still applies in bulk."""
    import json as _j

    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_BV_00", "name": "Guard", "displayName": "Guard", "platform": "PS3,PS4", "sources": ["titles"]}],
    )
    client.post("/tools/psn-review/bulk-confirm", data={"selections": _j.dumps({"NPWR_BV_00": ["PS5", "PS4"]})})
    rels = db_session.query(models.GameRelease).filter_by(source="psn", external_id="NPWR_BV_00").all()
    assert [r.platform_id for r in rels] == [models.resolve_platform_id(db_session, "PS4")]


def test_bulk_dismiss_clears_rows_without_creating_anything(client, db_session):
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [
            {"npCommunicationId": "NPWR_BD1_00", "name": "A", "displayName": "A", "platform": "PS3,PS4", "sources": ["titles"]},
            {"npCommunicationId": "NPWR_BD2_00", "name": "B", "displayName": "B", "platform": "PS3,PS4", "sources": ["titles"]},
        ],
    )
    r = client.post("/tools/psn-review/bulk-dismiss", data={"keys": "NPWR_BD1_00,NPWR_BD2_00"})
    assert b"Dismissed 2 games" in r.content
    assert db_session.query(models.GameRelease).filter_by(source="psn").count() == 0
    assert psn.import_review_rows(db_session, user.id) == []


def test_bulk_endpoints_reject_an_empty_payload(client, db_session):
    _seed_platforms(db_session)
    _signup_and_login(client)
    assert client.post("/tools/psn-review/bulk-confirm", data={"selections": ""}).status_code == 422
    assert client.post("/tools/psn-review/bulk-confirm", data={"selections": "{not json"}).status_code == 422
    assert client.post("/tools/psn-review/bulk-dismiss", data={"keys": ""}).status_code == 422


def test_bulk_mode_button_only_where_it_applies(client, db_session):
    """Card view shows one row at a time and played-only rows take three
    different actions — neither has anything to bulk."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_BM_00", "name": "A", "displayName": "A", "platform": "PS3,PS4", "sources": ["titles"]}],
    )
    body = client.get("/tools/psn-review").content
    assert b'id="psn-select-toggle"' in body
    assert b'id="psn-select-toggle" hidden' not in body.replace(b"\n", b" ")
    # Card view hides it.
    assert b"hidden" in client.get("/tools/psn-review?view=card").content


def test_each_service_gets_its_own_progress_indicator(client, db_session):
    """#sync-indicator was one global id and the poller rendered active_jobs[0]
    into it, so concurrent jobs overwrote each other — and on Tools that one
    indicator sat inside the Steam card, captioning PSN work as Steam's."""
    from backend import jobs

    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    jobs.clear_all()
    jobs.update(jobs.create(user_id=user.id, kind="psn_sync", label="Library sync").id, status=jobs.JobStatus.RUNNING)
    jobs.update(jobs.create(user_id=user.id, kind="sgdb_fill_all", label="Artwork fill").id, status=jobs.JobStatus.RUNNING)

    body = client.get("/integrations/jobs/poll").text
    # Split rather than regex: each block contains nested divs, so a non-greedy
    # match to the first </div> stops inside the spinner.
    blocks = {}
    for chunk in body.split('<div id="sync-indicator-')[1:]:
        name, _, rest = chunk.partition('"')
        blocks[name] = rest.split('<div id="sync-indicator')[0]

    # Both run at once and each lands in its own target.
    assert "Library sync in progress" in blocks["psn"]
    assert "Artwork fill in progress" in blocks["artwork"]
    # Steam has nothing running — its target is still emitted (empty), so a
    # finished job's spinner is actively cleared rather than left behind.
    assert "steam" in blocks
    assert "in progress" not in blocks["steam"]
    jobs.clear_all()


def test_job_service_falls_back_rather_than_hiding_a_new_kind():
    """An unmapped kind must land somewhere visible, not vanish."""
    from backend import integrations

    assert integrations.job_service("psn_sync") == "psn"
    assert integrations.job_service("steam_sync_full") == "steam"
    assert integrations.job_service("something_new") == "other"


def test_js_toast_markup_matches_the_server_toast():
    """All toast styling hangs off .toast-success / .toast-danger. The JS
    builder used its own markup with an inline colour and no kind class, so
    every JS-raised toast (cookie refresh, NPSSO refresh) rendered with no
    background — the transparent green one."""
    js = open("frontend/static/js/app.js").read()
    fn = js[js.index("function cgtToast(") : js.index("function _initToast(")]
    assert "'toast toast-' + kind + ' align-items-center show'" in fn
    assert "btn-close-white" not in fn
    assert 'style="color:' not in fn
    # Messages interpolate shell error strings — never parsed as markup.
    assert ".textContent = message" in fn


def test_played_only_rows_carry_the_same_art_as_cross_play(db_session, monkeypatch):
    """Both queues are rows of the same table with the same art columns, and the
    fill job never distinguished them — only the templates did, which left one
    queue looking unfinished next to the other."""
    steamgriddb = _sgdb_stub(monkeypatch)
    _seed_platforms(db_session)
    user = _user(db_session, "poart")
    user.steamgriddb_api_key = "sgdb-key"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"titleId": "PPSA_ART_00", "name": "Disc Game", "displayName": "Disc Game", "category": "ps5_native_game", "sources": ["played"]}],
        kind="played_only",
    )
    steamgriddb.fill_psn_review_thumbnails(db_session, user)

    row = psn.played_only_rows(db_session, user.id)[0]
    assert row["image"] == "https://sgdb/psn-grid.png"
    assert row["hero"] == "https://sgdb/psn-hero.png"
    assert row["logo"] == "https://sgdb/psn-logo.png"


def test_both_review_queues_render_their_art(client, db_session, monkeypatch):
    """Guards the templates, not just the data — the art was already being
    fetched for played-only rows and simply never shown."""
    steamgriddb = _sgdb_stub(monkeypatch)
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id, user.steamgriddb_api_key = "n" * 64, "dude", "sgdb-key"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"titleId": "PPSA_T_00", "name": "Disc Game", "displayName": "Disc Game", "category": "ps5_native_game", "sources": ["played"]}],
        kind="played_only",
    )
    steamgriddb.fill_psn_review_thumbnails(db_session, user)

    rows = client.get("/tools/psn-review?kind=played_only").content
    assert b"cgt-list-row-thumb" in rows
    assert b"https://sgdb/psn-grid.png" in rows

    cards = client.get("/tools/psn-review?kind=played_only&view=card").content
    assert b"cgt-import-hero" in cards
    assert b"https://sgdb/psn-hero.png" in cards
    assert b"cgt-detail-hero__logo" in cards


def test_card_dashes_render_as_dashes_not_entity_text(client, db_session):
    """{{ x or "&mdash;" }} escapes the entity and prints the literal characters
    &mdash; on screen. Jinja autoescaping applies to the fallback too."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        # No category, no service — both fall back to a dash.
        [{"titleId": "PPSA_D_00", "name": "Bare", "displayName": "Bare", "sources": ["played"]}],
        kind="played_only",
    )
    body = client.get("/tools/psn-review?kind=played_only&view=card").text
    assert "&amp;mdash;" not in body, "entity was double-escaped and shows as text"


def test_review_tabs_repaint_on_switch(client, db_session):
    """The tab bar sits outside the swap target, so a tab switch replaced the
    body but left the previous tab highlighted. It rides along out-of-band now,
    which also keeps the counts current as rows are confirmed away."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_TB_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PS4", "sources": ["titles"]}],
    )
    _seed_review(
        db_session,
        user,
        [{"titleId": "PPSA_TB_00", "name": "Disc", "displayName": "Disc", "sources": ["played"]}],
        kind="played_only",
    )

    hx = client.get("/tools/psn-review?kind=played_only", headers={"HX-Request": "true"}).text
    tabs = hx[hx.index('id="psn-tabs"') :]
    tabs = tabs[: tabs.index("</div>")]
    # The attribute follows the id on the next line.
    assert 'hx-swap-oob="true"' in tabs[:120]
    # Played-only is the active one; cross-play is not.
    cross_btn = tabs[tabs.index("psnSetKind('cross_play')") - 200 : tabs.index("Needs review")]
    played_btn = tabs[tabs.index("psnSetKind('played_only')") - 200 : tabs.index("Played-only")]
    assert "active" in played_btn
    assert "active" not in cross_btn


def test_switching_tabs_updates_the_count_and_the_blurb(client, db_session):
    """The pending badge and the description live outside the swap target and
    both describe the CURRENT queue — a switch to Played-only left "54 pending"
    and the cross-play blurb sitting above seven rows."""
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
                "npCommunicationId": f"NPWR_C{i}_00",
                "name": f"Cross {i}",
                "displayName": f"Cross {i}",
                "platform": "PS3,PS4",
                "sources": ["titles"],
            }
            for i in range(3)
        ],
    )
    _seed_review(
        db_session,
        user,
        [{"titleId": "PPSA_PO_00", "name": "Disc", "displayName": "Disc", "sources": ["played"]}],
        kind="played_only",
    )

    hx = client.get("/tools/psn-review?kind=played_only", headers={"HX-Request": "true"}).text
    assert 'id="psn-pending-count" hx-swap-oob="true">1<' in hx.replace("\n", "")
    assert "no purchase or trophy set behind them" in hx
    assert "cross-buy the answer is often more than one" not in hx


def test_row_actions_refresh_the_counts_and_retire_the_row(client, db_session):
    """Deciding a row left the tab badges and pending count showing the number
    you started with until a full swap. And a decided row that sits there
    forever turns the queue into a list of things you already dealt with."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [
            {"npCommunicationId": "NPWR_R1_00", "name": "One", "displayName": "One", "platform": "PS3,PS4", "sources": ["titles"]},
            {"npCommunicationId": "NPWR_R2_00", "name": "Two", "displayName": "Two", "platform": "PS3,PS4", "sources": ["titles"]},
        ],
    )

    r = client.post("/tools/psn-review/NPWR_R1_00/dismiss")
    body = r.content.decode()
    # The row retires itself rather than lingering.
    assert 'data-retire="1"' in body
    # ...and the chrome comes back with one fewer.
    assert 'id="psn-tabs"' in body and 'hx-swap-oob="true"' in body
    assert 'id="psn-pending-count" hx-swap-oob="true">1<' in body.replace("\n", "")


def test_review_rows_wire_up_draft_persistence(client, db_session):
    """Tentative ticks survive leaving the page. Nothing is committed until you
    confirm, so the draft is client-side — but the wiring has to be present on
    every checkbox and every action, which is what silently rots."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_DR_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PS4", "sources": ["titles"]}],
    )

    for view in ("list", "card"):
        body = client.get(f"/tools/psn-review?view={view}").text
        assert body.count("psnRememberDraft('NPWR_DR_00')") == 2, f"{view}: one per platform checkbox"
        assert "psnForgetDraft('NPWR_DR_00')" in body, f"{view}: deciding clears the draft"

    js = client.get("/tools/psn-review").text
    # An empty selection is a real tentative state and must round-trip.
    assert "psnApplyDrafts" in js and "psnWriteDrafts" in js
    # Bulk actions clear what they acted on.
    assert "psnClearActedDrafts" in js


def test_card_stacks_defer_their_images(client, db_session):
    """Every card in a stack is in the DOM and hidden ones use opacity: 0, not
    display: none — so the browser fetched and decoded ALL of them up front, and
    loading="lazy" didn't help because they're absolutely positioned at the same
    spot and all count as in-viewport. 54 PSN cards meant 102 SGDB heroes
    decoded before the first one appeared."""
    import re

    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id, user.steamgriddb_api_key = "n" * 64, "dude", "k"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [
            {
                "npCommunicationId": f"NPWR_HY{i}_00",
                "name": f"Cross {i}",
                "displayName": f"Cross {i}",
                "platform": "PS3,PS4",
                "sources": ["titles"],
            }
            for i in range(4)
        ],
    )
    psn.save_review_thumbnails(
        db_session,
        user.id,
        {
            f"NPWR_HY{i}_00": {"thumbnail_url": "https://sgdb/g.png", "hero_url": "https://sgdb/h.png", "logo_url": "https://sgdb/l.png"}
            for i in range(4)
        },
    )

    card = client.get("/tools/psn-review?view=card").text
    assert len(re.findall(r'<img [^>]*\ssrc="https?://', card)) == 0, "no card image loads up front"
    assert len(re.findall(r'data-src="https?://', card)) == 8, "hero + logo per card, deferred"

    # The list view keeps eager thumbnails — small, and in a normal scrolling
    # list where the browser's own lazy-loading actually works.
    rows = client.get("/tools/psn-review?view=list").text
    assert 'class="cgt-list-row-thumb" src="https://sgdb/g.png"' in rows


def test_every_card_stack_uses_the_shared_window():
    """PSN and import both render stacks; the fix has to cover both or the
    other one keeps the bug — import is the bigger stack at 253 cards."""
    helper = open("frontend/static/js/app.js").read()
    assert "window.cgtHydrateCards" in helper
    for page in ("frontend/templates/psn_review.html", "frontend/templates/import_review.html"):
        assert "cgtHydrateCards(" in open(page).read(), page
    for tpl in (
        "frontend/templates/partials/_psn_review_cards.html",
        "frontend/templates/partials/_psn_played_only_cards.html",
        "frontend/templates/partials/_import_cards.html",
    ):
        body = open(tpl).read()
        assert "data-src=" in body, tpl
        assert 'hero__img" src=' not in body, tpl


def test_stack_pages_do_not_call_deferred_helpers_at_parse_time():
    """app.js is loaded with defer, so it has NOT executed while an inline
    script in the body runs. Calling into it there threw, which aborted
    psnShowCard before any card got --active and left the whole stack hidden —
    a blank card view.

    Two guards: the call site is defensive, and the initial render waits for
    DOMContentLoaded, which fires after deferred scripts."""
    base = open("frontend/templates/base.html").read()
    assert "app.js" in base and "defer" in base, "premise: app.js is deferred"

    for page in ("frontend/templates/psn_review.html", "frontend/templates/import_review.html"):
        body = open(page).read()
        # Guarding alone silently skipped hydration forever — cards kept their
        # data-src and showed no art. It has to RETRY once defer completes.
        assert "cgtHydrateWhenReady(" in body, page
        assert "window.addEventListener('load'" in body, page
        assert "if (window.cgtHydrateCards) cgtHydrateCards(cards," not in body, page

    psn_page = open("frontend/templates/psn_review.html").read()
    assert "DOMContentLoaded" in psn_page
    # The bare top-level call is what blanked the stage.
    assert "\npsnRebuildNav(true);" not in psn_page


def test_card_position_is_remembered_per_queue(client, db_session):
    """Position already survived view and tab switches in memory; this makes it
    survive a reload. Stored per queue — the two stacks are different lists, so
    one slot would have each tab trying to restore the other's card."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_CP_00", "name": "Cross", "displayName": "Cross", "platform": "PS3,PS4", "sources": ["titles"]}],
    )

    page = client.get("/tools/psn-review?view=card").text
    assert "psnRememberCard(" in page
    assert "psnReadCardMemory()[psnCurrentKind()]" in page
    # Restored on first render, not only after a swap.
    head = page[page.index("DOMContentLoaded") :][:400]
    assert "psnReadCardMemory" in head
    assert "psnRebuildNav(false)" in head, "must not reset to the top"


def test_card_shows_the_trophy_tiers_not_a_summed_chip(client, db_session):
    """The feed carries per-tier counts and a summed chip threw them away. This
    is also the shape trophy tracking (#136) needs, so it's a step toward that
    rather than decoration."""
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
                "npCommunicationId": "NPWR_TT_00",
                "name": "Tiered",
                "displayName": "Tiered",
                "platform": "PS3,PS4",
                "trophyProgress": 60,
                "trophies": {"bronze": 31, "silver": 8, "gold": 7, "platinum": 1},
                "earnedTrophies": {"bronze": 20, "silver": 4, "gold": 2, "platinum": 0},
                "sources": ["titles"],
            }
        ],
    )

    row = psn.import_review_rows(db_session, user.id)[0]
    # Sony's order, and tiers the game doesn't have are omitted entirely.
    assert [t["tier"] for t in row["trophy_tiers"]] == ["platinum", "gold", "silver", "bronze"]
    assert row["trophy_tiers"][3] == {"tier": "bronze", "earned": 20, "defined": 31}

    body = client.get("/tools/psn-review?view=card").text
    assert "cgt-trophy-tier--platinum" in body
    assert "20/31" in body
    # Both halves sit side by side, matching the played-only card.
    assert "cgt-psn-card-split" in body


def test_trophyless_row_says_so_rather_than_rendering_an_empty_box(db_session, client):
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"titleId": "CUSA_NT_00", "name": "Bare", "displayName": "Bare", "platform": "PS3,PS4", "sources": ["purchased"]}],
    )
    row = psn.import_review_rows(db_session, user.id)[0]
    assert row["trophy_tiers"] == []
    assert b"No trophy data." in client.get("/tools/psn-review?view=card").content


def test_card_title_is_not_the_faintest_thing_on_the_card(client, db_session):
    """A bare h6 inherits the body colour and read fainter than the small-caps
    block headings under it — on a card whose job is naming one game."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso, user.psn_online_id = "n" * 64, "dude"
    db_session.commit()
    _seed_review(
        db_session,
        user,
        [{"npCommunicationId": "NPWR_TI_00", "name": "Named", "displayName": "Named", "platform": "PS3,PS4", "sources": ["titles"]}],
    )
    assert "cgt-review-card-title" in client.get("/tools/psn-review?view=card").text
    css = open("frontend/static/css/theme.css").read()
    block = css[css.index(".cgt-review-card-title {") :][:200]
    assert "var(--ctp-text)" in block

    # The title also has to start at the same x as the reason beneath it — the
    # old table's label cell kept its padding even when empty.
    cards = open("frontend/templates/partials/_psn_review_cards.html").read()
    assert "cgt-source-table__label" not in cards


def test_sibling_sets_cannot_silently_claim_the_same_platform(db_session):
    """Both Crimsonland sets declare the identical PS3,PSVITA,PS4 — nothing in
    Sony's data tells them apart. Confirming both with overlapping platforms
    made _import_one drop the second as a (game, platform) conflict, so the 90%
    set's data vanished and Vita ended up showing 23%."""
    _seed_platforms(db_session)
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita"))
    db_session.commit()
    user = _user(db_session, "sib")
    merged = [
        {
            "npCommunicationId": f"NPWR_SIB{i}_00",
            "name": "Twinned",
            "displayName": "Twinned",
            "normalizedName": psn._normalized_name("Twinned"),
            "platform": "PS3,PSVITA,PS4",
            "trophyProgress": p,
            "sources": ["titles"],
        }
        for i, p in enumerate((90, 23))
    ]
    _seed_review(db_session, user, merged)

    rows = psn.import_review_rows(db_session, user.id)
    assert [r["set_count"] for r in rows] == [2, 2]
    assert all(o["claimed_by"] is None for r in rows for o in r["options"])

    # Claim Vita on the first set.
    psn.confirm_entry_decision(db_session, user, "NPWR_SIB0_00", ["PSVITA"])

    survivor = psn.import_review_rows(db_session, user.id)[0]
    opts = {o["platform"]: o for o in survivor["options"]}
    # Vita is marked taken and no longer pre-ticked...
    assert opts["PSVITA"]["claimed_by"] == "set 1"
    assert opts["PSVITA"]["selected"] is False
    # ...but is still offered, because Sony gives nothing to prove which set
    # owns it and the user may know better.
    assert "PSVITA" in opts
    # The untaken platforms are what's pre-ticked now.
    assert opts["PS4"]["selected"] is True
    # And "set 1 of 2" keeps saying 2 after one is decided.
    assert survivor["set_count"] == 2


def test_confirm_reports_when_siblings_need_re_rendering(db_session):
    """The sibling card is already on screen offering platforms this row just
    claimed — the response has to say so or it sits there stale."""
    _seed_platforms(db_session)
    user = _user(db_session, "stale")
    merged = [
        {
            "npCommunicationId": f"NPWR_ST{i}_00",
            "name": "Twinned",
            "displayName": "Twinned",
            "normalizedName": psn._normalized_name("Twinned"),
            "platform": "PS3,PS4",
            "sources": ["titles"],
        }
        for i in range(2)
    ]
    _seed_review(db_session, user, merged)
    assert psn.confirm_entry_decision(db_session, user, "NPWR_ST0_00", ["PS4"])["siblings_stale"] is True
    # Nothing left to go stale once the sibling is decided too.
    assert psn.confirm_entry_decision(db_session, user, "NPWR_ST1_00", ["PS3"])["siblings_stale"] is False


def test_attach_search_is_scoped_to_playstation_and_ranked(client, db_session):
    """Searching the whole library returned eight Devil May Cry 5 DLC rows
    alphabetically and pushed the Special Edition past the limit — the entry the
    user wanted was structurally unreachable, so they hit Import and got a
    duplicate game instead."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()

    def add(title, source, platform="PS5"):
        g = models.Game(title=title)
        db_session.add(g)
        db_session.flush()
        rel = models.GameRelease(
            game_id=g.id, platform=platform, platform_id=models.resolve_platform_id(db_session, platform), source=source, external_id=title
        )
        db_session.add(rel)
        db_session.flush()
        db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=rel.id))

    for i in range(9):
        add(f"Devil May Cry 5 - Alt Colors {i}", "steam", "Steam")
    add("Devil May Cry 5 Special Edition", "psn")
    db_session.commit()

    body = client.get("/integrations/psn/attach-search", params={"external_id": "X", "q": "devil"}).text
    assert "Devil May Cry 5 Special Edition" in body
    assert "Alt Colors" not in body, "Steam DLC is never an attach target"


# ─────────────────────────────────────────────────────────────────────────────
# IGDB title proposals (#180)
# ─────────────────────────────────────────────────────────────────────────────


def test_trophy_only_rows_are_the_ones_with_suspect_names():
    """A store-backed row got its name from a store listing and is fine. A row
    whose external_id is an npCommunicationId was named after its trophy SET —
    and the purchased feed can never supply a store record for PS3/Vita (#181),
    so that name is permanent unless something external fixes it."""
    assert psn.is_trophy_only("NPWR03481_00")
    assert psn.is_trophy_only("npwr00791_00")
    assert not psn.is_trophy_only("CUSA12431_00")
    assert not psn.is_trophy_only("PPSA21564_00")
    assert not psn.is_trophy_only(None)


def test_mixed_script_titles_fall_back_to_their_latin_run():
    """IGDB returns ZERO results for "閃乱カグラ SHINOVI VERSUS" and an exact hit
    for "SHINOVI VERSUS" — verified against the live API 2026-08-07. Sending
    the raw title alone would fail the case this feature exists for, and fail
    silently, because 0 results reads as "no proposal" rather than an error."""
    terms = psn.search_terms("閃乱カグラ SHINOVI VERSUS")
    assert terms[0] == "閃乱カグラ SHINOVI VERSUS", "raw title is tried first"
    assert "SHINOVI VERSUS" in terms[1], "latin run is the fallback"

    # A pure-Latin title must not queue a duplicate query.
    assert psn.search_terms("Modern Warfare 2") == ["Modern Warfare 2"]
    assert psn.search_terms("") == []


def test_proposal_requires_platform_overlap_not_agreement():
    """Shinovi Versus' trophy set claims PS3 + Vita; IGDB says Vita alone. The
    phantom PS3 IS the bad data being corrected, so demanding the sets agree
    would reject the very row this feature targets."""
    hits = [{"id": 4242, "name": "Senran Kagura: Shinovi Versus", "platform_ids": [46]}]

    def fake_search(term, platform_ids):
        # Faithful to the live API: the mixed-script string returns nothing,
        # the latin run returns the exact game.
        return hits if term == "SHINOVI VERSUS" else []

    got = psn.build_proposal("閃乱カグラ SHINOVI VERSUS", ["PS3", "PSVITA"], [9, 46], fake_search)
    assert got["proposed_title"] == "Senran Kagura: Shinovi Versus"
    assert got["proposed_igdb_id"] == 4242
    assert got["proposed_platforms"] == [46], "the corrected set, not the trophy set's claim"
    assert got["matched_term"] == "SHINOVI VERSUS", "matched via the latin fallback"


def test_no_proposal_when_nothing_overlaps():
    """Zero overlap means no proposal. A row left raw beats a confident wrong
    rename, which is harder to notice because it looks authoritative."""
    hits = [{"id": 1, "name": "Some Xbox Game", "platform_ids": [12]}]
    assert psn.build_proposal("Whatever", ["PS3"], [9], lambda t, p: hits) is None
    assert psn.build_proposal("Whatever", ["PS3"], [9], lambda t, p: []) is None
    # No resolvable platforms at all -> nothing to corroborate against.
    assert psn.build_proposal("Whatever", [], [], lambda t, p: [{"id": 1, "name": "X", "platform_ids": [9]}]) is None


def test_an_already_correct_name_still_keeps_its_igdb_id():
    """Most rows are already named right, and that is NOT a dead end — the id is
    the whole point of the lookup. Discarding it reported ~279 rows as "not
    identified" when IGDB had matched every one of them exactly.

    No rename is proposed (there is nothing to approve), but the id and IGDB's
    platform list come back so confirm can attach them."""
    hits = [{"id": 7, "name": "Bloodborne", "platform_ids": [48], "game_type": 0}]
    got = psn.build_proposal("Bloodborne™", ["PS4"], [48], lambda t, p: hits)
    assert got is not None, "an exact match is a match, not a miss"
    assert got["exact"] is True
    assert got["proposed_title"] is None, "nothing to rename"
    assert got["proposed_igdb_id"] == 7
    assert got["proposed_platforms"] == [48]


def test_an_edition_collapses_to_the_game_it_repackages():
    """version_parent + game_type main means "same game, different box" (#180).

    Shapes taken from a live IGDB probe on 2026-08-11. The library wants
    "Mass Effect: Andromeda", not the edition words trailing off the end."""
    hit = {
        "id": 9999,
        "name": "Mass Effect: Andromeda - Super Deluxe Edition",
        "platform_ids": [48],
        "game_type": 0,
        "version_parent": {"id": 11, "name": "Mass Effect: Andromeda"},
    }
    got = psn._collapse_edition(hit)
    assert got["id"] == 11, "the parent's id, so metadata attaches to the real game"
    assert got["name"] == "Mass Effect: Andromeda"
    assert got["collapsed_from"] == "Mass Effect: Andromeda - Super Deluxe Edition"


def test_a_distinct_product_is_never_collapsed():
    """Devil May Cry 5: Special Edition is game_type EXPANDED — a different
    product that happens to carry a version_parent, not a repackaging. Ghost of
    Tsushima: Legends links by parent_game only, which is also not an edition.

    Collapsing either would erase a game the user genuinely owns separately."""
    dmc = {
        "id": 5,
        "name": "Devil May Cry 5: Special Edition",
        "platform_ids": [167],
        "game_type": 10,
        "version_parent": {"id": 4, "name": "Devil May Cry 5"},
        "parent_game": {"id": 4, "name": "Devil May Cry 5"},
    }
    assert psn._collapse_edition(dmc)["id"] == 5
    assert psn._collapse_edition(dmc)["name"] == "Devil May Cry 5: Special Edition"

    legends = {
        "id": 6,
        "name": "Ghost of Tsushima: Legends",
        "platform_ids": [48],
        "game_type": 4,
        "parent_game": {"id": 3, "name": "Ghost of Tsushima"},
    }
    assert psn._collapse_edition(legends)["id"] == 6


def test_the_search_adapter_preserves_both_igdb_parent_links():
    """Regression. The adapter reshapes IGDB rows, and it used to drop
    version_parent and parent_game — which disabled _collapse_edition entirely
    in production while every test still passed, because the tests hand-build
    hits with those fields already present.

    Live symptom: editions never collapsed onto their game, and the Telltale
    Batman episode never resolved to its series, so a sparse "Batman" got a
    confident dart-throw rename to "LEGO Batman 3: Beyond Gotham" instead."""
    rows = [
        {
            "id": 26638,
            "name": "Batman: The Telltale Series - Episode 1: Realm of Shadows",
            "platform_ids": [48],
            "game_type": 6,
            "version_parent": None,
            "parent_game": {"id": 14746, "name": "Batman: The Telltale Series"},
        }
    ]
    with patch("backend.igdb.search_games_on_platforms", return_value=rows):
        got = psn._igdb_search_adapter("cid", "sec")("Batman", [48])
    assert got[0]["parent_game"] == {"id": 14746, "name": "Batman: The Telltale Series"}
    assert "version_parent" in got[0], "both links must survive the reshape"
    # and the collapse it feeds actually fires on what the adapter returns
    assert psn._collapse_edition(got[0])["id"] == 14746


def test_an_episode_resolves_to_the_series_it_belongs_to():
    """A concept page names the current SKU, and for an episodic game that is
    episode one. Searched verbatim, IGDB returns ONLY the episode — so without
    this we would propose one chapter as the whole game.

    Resolved via IGDB's own parent_game rather than by stripping "Episode N"
    off the string: the relationship is authoritative, and it cannot misfire on
    a title where "Episode" is genuinely part of the name (Episode Gladiolus,
    Episode Prologue). Shapes verified live — Telltale Batman episodes 1 and 5
    both point at series id 14746."""
    episode = {
        "id": 26638,
        "name": "Batman: The Telltale Series - Episode 1: Realm of Shadows",
        "platform_ids": [48],
        "game_type": 6,
        "parent_game": {"id": 14746, "name": "Batman: The Telltale Series", "game_type": 0},
    }
    got = psn._collapse_edition(episode)
    assert got["id"] == 14746
    assert got["name"] == "Batman: The Telltale Series"
    assert got["game_type"] == 0, "ranks as the main game it resolved to, not as an episode"

    # A title that merely CONTAINS "Episode" is untouched — no parent, no type 6.
    gladiolus = {"id": 900, "name": "Final Fantasy XV: Episode Gladiolus", "platform_ids": [48], "game_type": 1}
    assert psn._collapse_edition(gladiolus)["id"] == 900


def test_the_store_name_rescues_a_sparse_feed_name():
    """Batman. Sony's PS4 trophy set is named just "Batman" — unidentifiable,
    and IGDB on that term is a dart throw. The store page names it properly.

    The store title is offered to IGDB as a second candidate name rather than
    applied as an override, so the match is something IGDB vouched for and the
    id comes with it. This is the case the store retitle was built for (#180)."""

    def fake_search(term, platform_ids):
        # Only the store's name finds anything; "Batman" alone matches nothing
        # this search is willing to stand behind.
        if "telltale" not in term.lower():
            return []
        return [{"id": 501, "name": "Batman: The Telltale Series", "platform_ids": platform_ids, "game_type": 0}]

    got = psn.build_proposal("Batman", ["PS4"], [48], fake_search, store_title="Batman: The Telltale Series")
    assert got is not None, "the store name is a lead, not noise"
    assert got["proposed_igdb_id"] == 501
    assert got["proposed_title"] == "Batman: The Telltale Series", "matching the store name IS a rename for us"


def test_the_feed_name_wins_when_the_store_sku_was_repurposed():
    """Ghost of Tsushima. Sony's store SKU now serves a page titled "Ghost of
    Tsushima: Legends", a separate standalone product — adopting it renamed the
    game to something never bought.

    Both names match IGDB exactly, so ordering cannot decide it. The main game
    beats the standalone expansion, and the feed name survives. Same rule as
    Batman, opposite input, opposite answer — which is the point: no global
    winner between the two sources, IGDB arbitrates per row."""
    hits = {
        "Ghost of Tsushima": {"id": 3, "name": "Ghost of Tsushima", "platform_ids": [48], "game_type": 0},
        "Ghost of Tsushima: Legends": {
            "id": 44,
            "name": "Ghost of Tsushima: Legends",
            "platform_ids": [48],
            "game_type": 4,  # standalone expansion
            "parent_game": {"id": 3, "name": "Ghost of Tsushima"},
        },
    }

    def fake_search(term, platform_ids):
        # The expansion is returned FIRST for every term, so position alone
        # would hand it the win.
        return [hits["Ghost of Tsushima: Legends"], hits["Ghost of Tsushima"]]

    got = psn.build_proposal("Ghost of Tsushima", ["PS4"], [48], fake_search, store_title="Ghost of Tsushima: Legends")
    assert got["proposed_igdb_id"] == 3, "the game, not the standalone expansion"
    assert got["proposed_title"] is None, "our name was already right — nothing to rename"


def test_an_exact_match_prefers_the_main_game_over_a_derivative():
    """Exact matches are collected, not returned on sight (#180).

    IGDB routinely lists several entries under the SAME name — a main game and
    a port, a remaster, a bundle. Returning whichever came back first meant the
    derivative won on ordering alone, and ordering is not identity. It also
    blocks feeding a second source name (the store title) into the same search,
    because then two genuinely different names can each match exactly and the
    spin-off would win by position.

    The derivative is deliberately listed FIRST here — first-hit logic passes
    this test only by accident."""
    hits = [
        {"id": 11, "name": "Ghost of Tsushima", "platform_ids": [48], "game_type": 11},  # port
        {"id": 3, "name": "Ghost of Tsushima", "platform_ids": [48], "game_type": 0},  # main
    ]
    got = psn.build_proposal("Ghost of Tsushima", ["PS4"], [48], lambda t, p: hits)
    assert got["exact"] is True
    assert got["proposed_igdb_id"] == 3, "the main game, not whichever IGDB listed first"


def test_an_edition_hit_can_exact_match_a_plain_trophy_title():
    """The payoff. PSN's trophy set is named "Ghost of Tsushima"; IGDB returns
    the Special Edition. The old `version_parent = null` filter dropped that hit
    before ranking, so the row reported no match at all — the same mechanism
    that made Devil May Cry 5: Special Edition unfindable."""
    hits = [
        {
            "id": 99,
            "name": "Ghost of Tsushima: Special Edition",
            "platform_ids": [48],
            "game_type": 0,
            "version_parent": {"id": 3, "name": "Ghost of Tsushima"},
        }
    ]
    got = psn.build_proposal("Ghost of Tsushima", ["PS4"], [48], lambda t, p: hits)
    assert got is not None, "an edition of the game IS the game"
    assert got["exact"] is True
    assert got["proposed_igdb_id"] == 3, "the parent's id, not the edition's"


def test_igdb_platform_ids_come_from_the_platforms_table(db_session):
    """The app's platform rows already carry IGDB ids, so there is no name
    mapping to do — and a hand-rolled one would drift from the real catalogue.
    Real ids: PS3=9, Vita=46 (confirmed against the live IGDB platform list)."""
    db_session.add_all(
        [
            models.Platform(name="PlayStation 3", display_name="PS3", igdb_id=9),
            models.Platform(name="PlayStation Vita", display_name="PS Vita", igdb_id=46),
        ]
    )
    db_session.commit()

    got = psn.igdb_platform_ids(db_session, ["PS3", "PSVITA"])
    assert sorted(got) == [9, 46], f"expected the platforms table's own igdb ids, got {got}"
    # An unresolvable token contributes nothing rather than guessing.
    assert psn.igdb_platform_ids(db_session, ["NotAPlatform"]) == []


def _prop_user(db, name):
    user = models.User(name=name, username=name, password_hash="x", api_token=f"{name}-tok")
    user.twitch_client_id, user.twitch_client_secret = "cid", "sec"
    db.add(user)
    db.add(models.Platform(name="PlayStation 3", display_name="PS3", igdb_id=9))
    db.flush()
    return user


def _cand(db, user, ext, title, kind="title_fix", status="pending", proposal_status=None, platform="PS3"):
    c = models.PsnReviewCandidate(
        user_id=user.id,
        external_id=ext,
        title=title,
        kind=kind,
        status=status,
        proposal_status=proposal_status,
        raw_data={"platform": platform},
    )
    db.add(c)
    return c


def test_only_overlapping_sets_are_flagged_as_contested(db_session):
    """Two sets for a title only COMPETE when they claim the same platform.

    Big Sky Infinity has a PS3 set and a PSVITA set — each already says which
    console it covers, so there is nothing to choose and nothing to warn about.
    Crimsonland has two sets both declaring PS3,PSVITA,PS4, where nothing says
    which is which and picking wrong silently drops one as a (game, platform)
    conflict.

    Warning on set_count alone cried wolf on every ordinary cross-gen pair."""
    user = _prop_user(db_session, "u-contested")
    for n in ("PS3", "PlayStation 4", "PlayStation Vita"):
        db_session.add(models.Platform(name=n, display_name=n))

    for ext, plat in (("NPWR03639_00", "PS3"), ("NPWR03640_00", "PSVITA")):
        c = _cand(db_session, user, ext, "Big Sky Infinity", kind="title_fix", platform=plat)
        c.raw_data = {"platform": plat, "normalizedName": "bigskyinfinity"}
    for ext in ("NPWR06670_00", "NPWR06085_00"):
        c = _cand(db_session, user, ext, "Crimsonland", kind="title_fix", platform="PS3,PSVITA,PS4")
        c.raw_data = {"platform": "PS3,PSVITA,PS4", "normalizedName": "crimsonland"}
    db_session.commit()

    rows = {r["key"]: r for r in psn.import_review_rows(db_session, user.id)}
    assert rows["NPWR03639_00"]["contested"] is False, "distinct platforms are already determined"
    assert rows["NPWR03640_00"]["contested"] is False
    assert rows["NPWR06670_00"]["contested"] is True, "identical claims genuinely compete"
    assert rows["NPWR06085_00"]["contested"] is True


def test_two_trophy_sets_for_one_title_still_say_so(db_session):
    """Big Sky Infinity has a PS3 set and a Vita set — consecutive npCommIds,
    same 14 trophies, both 3/14, last played a month apart. Two real progress
    records wanting two entries, and without the "Set 1 of 2" badge they read as
    an accidental duplicate.

    The siblings query was scoped to kind == "cross_play", which was fine while
    that was the only kind a multi-set title could land in. Once the sync began
    holding every new title back as title_fix, the badge silently stopped
    appearing — and so did claimed_by, the guard that stops two sets claiming
    the same platform and one of them being dropped as a conflict."""
    user = _prop_user(db_session, "u-sets")
    db_session.add(models.Platform(name="PS3", display_name="PlayStation 3"))
    for ext, plat in (("NPWR03639_00", "PS3"), ("NPWR03640_00", "PSVITA")):
        c = _cand(db_session, user, ext, "Big Sky Infinity", kind="title_fix", platform=plat)
        c.raw_data = {"platform": plat, "normalizedName": "bigskyinfinity"}
    db_session.commit()

    rows = psn.import_review_rows(db_session, user.id)
    assert len(rows) == 2
    assert all(r["set_count"] == 2 for r in rows), "title_fix rows must see their siblings too"
    assert sorted(r["set_index"] for r in rows) == [1, 2]


def test_a_platform_claimed_by_a_decided_set_is_marked_on_its_sibling(db_session):
    """Crimsonland. Two trophy sets for one title, both declaring the identical
    PS3,PSVITA,PS4 — nothing in any feed says which console each covers. Confirm
    one on PS4 and the other must SHOW that PS4 is spoken for, or confirming it
    too drops silently as a (game, platform) conflict. That is how the 90% set's
    data vanished and Vita ended up showing 23%.

    Still overridable: the platform stays tickable, it just says who has it.

    Seeded as title_fix, because the sync holds every new title back that way
    now and the guard was scoped to cross_play until #180."""
    user = _prop_user(db_session, "u-claim")
    for n in ("PS3", "PlayStation 4", "PlayStation Vita"):
        db_session.add(models.Platform(name=n, display_name=n))
    decided = _cand(db_session, user, "NPWR06670_00", "Crimsonland", kind="title_fix", status="confirmed", platform="PS3,PSVITA,PS4")
    decided.raw_data = {"platform": "PS3,PSVITA,PS4", "normalizedName": "crimsonland"}
    decided.chosen_platforms = ["PS4"]
    pending = _cand(db_session, user, "NPWR06085_00", "Crimsonland", kind="title_fix", platform="PS3,PSVITA,PS4")
    pending.raw_data = {"platform": "PS3,PSVITA,PS4", "normalizedName": "crimsonland"}
    db_session.commit()

    row = next(r for r in psn.import_review_rows(db_session, user.id) if r["key"] == "NPWR06085_00")
    opts = {o["platform"]: o for o in row["options"]}
    assert opts["PS4"]["claimed_by"], "PS4 is taken by the other set and must say so"
    assert not opts["PS3"]["claimed_by"] and not opts["PSVITA"]["claimed_by"]


def test_rows_without_a_normalized_name_are_not_siblings(db_session):
    """They used to all bucket under "", so unrelated titles counted as several
    sets for one title — which marks their playtime unattributable and hides it."""
    user = _prop_user(db_session, "u-nosib")
    db_session.add(models.Platform(name="PS4", display_name="PlayStation 4"))
    for ext, title in (("CUSA0AA_00", "Alpha"), ("CUSA0BB_00", "Beta")):
        c = _cand(db_session, user, ext, title, kind="title_fix", platform="PS4")
        c.raw_data = {"platform": "PS4"}  # no normalizedName
    db_session.commit()

    rows = psn.import_review_rows(db_session, user.id)
    assert all(r["set_count"] == 1 for r in rows), "unrelated titles are not each other's sets"


def test_each_kind_of_row_says_why_it_is_actually_there(db_session):
    """The reason line only ever reported PLATFORM reasoning, so a media app
    read "Only one platform on this trophy set" — true, and not why it was in
    front of you. The queue became multi-kind; this text never did (#180).

    An igdb_link row is the one that already exists in the library, so it must
    not read like a pending import."""
    user = _prop_user(db_session, "u-reasons")
    db_session.add(models.Platform(name="PS4", display_name="PlayStation 4"))
    for ext, title, kind in (
        ("CUSA00130_00", "Amazon Prime Video", "media_app"),
        ("CUSA00900_00", "Bloodborne", "title_fix"),
        ("CUSA00901_00", "Already Here", "igdb_link"),
    ):
        c = _cand(db_session, user, ext, title, kind=kind, platform="PS4")
        c.raw_data = {"platform": "PS4"}
    db_session.commit()

    rows = {r["name"]: r["reason"] for r in psn.import_review_rows(db_session, user.id)}
    assert "media app" in rows["Amazon Prime Video"].lower()
    assert "platform" not in rows["Amazon Prime Video"].lower(), "the old bug: platform reasoning on a non-platform question"
    assert "library" in rows["Bloodborne"].lower()
    assert "already in your library" in rows["Already Here"].lower()


def test_the_merge_carries_the_concept_id():
    """Regression, and the second seam of its kind today (#180).

    merge_library builds merged items from a fixed set of keys, and `concept`
    was not one of them — so the concept fallback in _store_title_for could
    never fire. "Batman" had a concept page naming it properly, never reached
    it, and the lookup fell back to the bare word and proposed "Batman: The
    Enemy Within".

    Only the id is carried: concept.titleIds is the whole SKU family (37 for
    ELDEN RING) and nothing downstream reads it off the row."""
    played = [
        {
            "titleId": "CUSA05332_00",
            "name": "Batman",
            "category": "ps4_game",
            "concept": {"id": 221667, "titleIds": ["CUSA05332_00", "CUSA05333_00"]},
        }
    ]
    merged = psn.merge_library([], [], played)["merged"]
    assert merged[0]["conceptId"] == 221667
    assert "titleIds" not in str(merged[0].get("conceptId")), "just the id, not the family"


def test_an_exact_match_that_still_renames_is_a_question(db_session, monkeypatch):
    """ "matched" means IGDB knows the game AND our name is already right.

    An exact match can still produce a rename — Sony's "BattleWorldsKronos"
    matches "Battle Worlds: Kronos" once spacing is ignored — and labelling that
    matched made the modal say "the name is already right" while proposing to
    change it, and the row report an identity problem it did not have."""
    user = _prop_user(db_session, "u-spaceless")
    db_session.add(models.Platform(name="PS4", display_name="PlayStation 4", igdb_id=48))
    cand = _cand(db_session, user, "CUSA04483_00", "BattleWorldsKronos", platform="PS4")
    cand.raw_data = {"platform": "PS4"}
    db_session.commit()

    monkeypatch.setattr(psn.psn_store, "fetch_product", lambda p, locale=None: {"name": ""})

    def fake_search(term, platform_ids):
        return [{"id": 7206, "name": "Battle Worlds: Kronos", "platform_ids": platform_ids, "game_type": 0}]

    orig = psn._igdb_search_adapter
    psn._igdb_search_adapter = lambda *a, **k: fake_search
    try:
        psn.fill_review_proposals(db_session, user, store_sleep=0)
        db_session.commit()
    finally:
        psn._igdb_search_adapter = orig

    row = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA04483_00").one()
    assert row.proposed_title == "Battle Worlds: Kronos", "the spacing fix is a real improvement"
    assert row.proposal_status == "pending", "a rename is a question, not a settled match"


def test_search_terms_space_out_jammed_punctuation():
    """Sony writes "NieR:Automata" with no space after the colon, and IGDB
    returns ZERO results for that string while matching "NieR Automata"
    exactly — the words never get tokenized apart. Without this the row got no
    proposal at all once its store name (a bundle) was correctly rejected."""
    assert "NieR Automata" in psn.search_terms("NieR:Automata")
    # A title already spaced gains nothing and must not be searched twice.
    assert psn.search_terms("Gone Home") == ["Gone Home"]
    assert psn.search_terms("Ghost of Tsushima") == ["Ghost of Tsushima"]


def test_the_edition_that_matched_reaches_the_library(db_session, monkeypatch):
    """When an edition is folded onto its base game, WHICH edition matched is
    kept — the user owns "Game of the Yorha Edition", which bundles content the
    base game does not, and that is a different fact from matching NieR:
    Automata outright.

    Recorded rather than displayed, and it rides raw_data onto the library entry
    at confirm, which is where it is actually wanted (#180)."""
    user = _prop_user(db_session, "u-via")
    db_session.add(models.Platform(name="PS4", display_name="PlayStation 4", igdb_id=48))
    cand = _cand(db_session, user, "CUSA0NIER_00", "NieR Automata", platform="PS4")
    cand.raw_data = {"platform": "PS4", "normalizedName": "nierautomata", "titleId": "CUSA0NIER_00", "name": "NieR Automata"}
    db_session.commit()

    monkeypatch.setattr(psn.psn_store, "fetch_product", lambda p, locale=None: {"name": ""})

    def fake_search(term, platform_ids):
        # the edition, typed as a bundle, carrying its version_parent
        return [
            {
                "id": 55555,
                "name": "Nier: Automata - Game of the Yorha Edition",
                "platform_ids": platform_ids,
                "game_type": 3,
                "version_parent": {"id": 11208, "name": "NieR: Automata"},
            }
        ]

    orig = psn._igdb_search_adapter
    psn._igdb_search_adapter = lambda *a, **k: fake_search
    try:
        psn.fill_review_proposals(db_session, user, store_sleep=0)
        db_session.commit()
    finally:
        psn._igdb_search_adapter = orig

    row = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA0NIER_00").one()
    assert row.proposed_igdb_id == 11208, "folded onto the base game"
    assert row.raw_data["matchedVia"] == {"name": "Nier: Automata - Game of the Yorha Edition", "igdb_id": 55555}

    psn.confirm_entry_decision(db_session, user, "CUSA0NIER_00", ["PS4"])
    db_session.commit()
    rel = db_session.query(models.GameRelease).filter_by(source="psn", external_id="CUSA0NIER_00").one()
    assert rel.raw_data["matchedVia"]["igdb_id"] == 55555, "and it reaches the library entry"


def test_psvr_games_are_searched_on_the_headset_too(db_session):
    """Sony has no VR platform — a PSVR game's trophy set and its purchase both
    report PS4 — but IGDB models PlayStation VR separately.

    Moss is IGDB 37095 on platforms 165/390/163/…, with NO 48. Filtering the
    search to 48 excluded the game from its own lookup, so the row got whatever
    else mentioned "moss": "Where Moss Grows". ASTRO BOT Rescue Mission came
    back unidentified by the same route. The filter is "any of", so widening
    only lets the real game back in."""
    db_session.add(models.Platform(name="PS4", display_name="PlayStation 4", igdb_id=48))
    db_session.add(models.Platform(name="PS5", display_name="PlayStation 5", igdb_id=167))
    db_session.commit()

    assert psn.igdb_platform_ids(db_session, ["PS4"]) == [48, 165], "PS4 carries PlayStation VR"
    assert psn.igdb_platform_ids(db_session, ["PS5"]) == [167, 390], "PS5 carries PSVR2"

    # And the reverse, or accepting a VR game's proposal strikes out the only
    # platform it has and confirming creates nothing.
    assert psn._platform_in_igdb_set(db_session, "PS4", [165]) is True
    assert psn._platform_in_igdb_set(db_session, "PS4", [48]) is True
    assert psn._platform_in_igdb_set(db_session, "PS5", [165]) is False, "a PS4 headset is not a PS5"


def test_artwork_is_refetched_when_the_name_moves_on(db_session):
    """Art found for "Batman" is a different game's cover once the row reads
    "Batman: The Telltale Series" — confidently wrong, which is worse than none.

    The gap query only ever looked for MISSING art, so a renamed row kept its
    old cover forever. It now also refetches when the name it was fetched under
    is no longer the name the row carries."""
    user = _prop_user(db_session, "u-refetch")

    settled = _cand(db_session, user, "CUSA0AAA_00", "Bloodborne", platform="PS4")
    settled.thumbnail_url, settled.hero_url = "grid.png", "hero.png"
    settled.raw_data = {"platform": "PS4", "artForTitle": "Bloodborne"}

    renamed = _cand(db_session, user, "CUSA0BBB_00", "Batman", platform="PS4")
    renamed.thumbnail_url, renamed.hero_url = "grid.png", "hero.png"
    renamed.proposed_title = "Batman: The Telltale Series"
    renamed.raw_data = {"platform": "PS4", "artForTitle": "Batman"}
    db_session.commit()

    gaps = {g["external_id"]: g["title"] for g in psn.review_thumbnail_gaps(db_session, user.id)}
    assert "CUSA0AAA_00" not in gaps, "art still matches its name — nothing to do"
    assert gaps["CUSA0BBB_00"] == "Batman: The Telltale Series", "refetch under the name it has now"


def test_artwork_searches_the_corrected_name(db_session):
    """SGDB is asked for the best name we hold, not Sony's (#180).

    A sparse feed name is precisely what SGDB cannot resolve — "Batman" returns
    nothing usable — and precisely what IGDB has already corrected on the row.
    Searching the raw name meant the rows most in need of art were the ones
    guaranteed never to get any."""
    user = _prop_user(db_session, "u-art")
    sparse = _cand(db_session, user, "CUSA05332_00", "Batman", platform="PS4")
    sparse.proposed_title = "Batman: The Telltale Series"
    sparse.proposed_igdb_id = 14746
    sparse.proposal_status = "pending"
    plain = _cand(db_session, user, "CUSA00900_00", "Bloodborne", platform="PS4")
    plain.proposal_status = "matched"
    db_session.commit()

    gaps = {g["external_id"]: g["title"] for g in psn.review_thumbnail_gaps(db_session, user.id)}
    assert gaps["CUSA05332_00"] == "Batman: The Telltale Series"
    assert gaps["CUSA00900_00"] == "Bloodborne", "with nothing proposed, Sony's name is the best we have"


def test_better_sony_data_reopens_a_settled_row(db_session):
    """The PS+ lifecycle (#180). "Batman" was confirmed under Sony's sparse name
    because nothing could identify it — no purchase meant no productId meant no
    store name. Renewing PS+ puts the purchase back, so the sync can now do
    better and says so, instead of leaving the entry wrong forever and relying
    on the user knowing a relink button exists."""
    user = _prop_user(db_session, "u-reopen")
    user.twitch_client_id, user.twitch_client_secret = "cid", "sec"
    cand = _cand(db_session, user, "CUSA05332_00", "Batman", status="confirmed", platform="PS4")
    cand.raw_data = {"platform": "PS4"}  # no productId, no concept: nothing to go on
    cand.chosen_platforms = ["PS4"]
    db_session.commit()

    # Same data again: nothing new, so nothing is disturbed.
    same = {"name": "Batman", "titleId": "CUSA05332_00", "platform": "PS4", "sources": ["titles"]}
    out = psn.import_merged(db_session, user, [same])
    db_session.commit()
    assert out["reopened"] == 0, "a re-sync with identical data must never resurface a decision"
    assert db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA05332_00").one().status == "confirmed"

    # PS+ renewed: the purchase is back, so a store name is reachable.
    richer = {**same, "productId": "UP1018-CUSA05332_00-BATMANTELLTALE00", "sources": ["purchased", "titles"]}
    out = psn.import_merged(db_session, user, [richer])
    db_session.commit()
    assert out["reopened"] == 1
    row = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA05332_00").one()
    assert row.status == "pending", "back in the queue as a proposed change"
    assert row.proposal_status is None, "and eligible to be looked up again"


def test_a_dismissal_is_never_reopened(db_session):
    """A dismissal is an answer, not a gap. Better data does not entitle the
    sync to re-ask something already refused outright — that is how a queue
    turns into a treadmill."""
    user = _prop_user(db_session, "u-dismissed")
    user.twitch_client_id, user.twitch_client_secret = "cid", "sec"
    cand = _cand(db_session, user, "CUSA00130_00", "Amazon Prime Video", status="dismissed", platform="PS4")
    cand.raw_data = {"platform": "PS4"}
    db_session.commit()

    richer = {
        "name": "Amazon Prime Video",
        "titleId": "CUSA00130_00",
        "platform": "PS4",
        "productId": "UP0000-CUSA00130_00-XXXXXXXXXXXXXXXX",
        "sources": ["purchased"],
    }
    out = psn.import_merged(db_session, user, [richer])
    db_session.commit()
    assert out["reopened"] == 0
    assert db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA00130_00").one().status == "dismissed"


def test_a_resync_does_not_forget_what_you_refused(db_session):
    """The crawl payload is refreshed wholesale on every sync — trophy progress
    and playtime move — but three keys on that blob are NOT crawl data.

    rejectedTitle is the name you already turned down. Losing it meant a resync
    silently re-asked a refused question, so the refusal memory lasted exactly
    until the next sync. storeTitle is the fetch cache; losing it re-fetched
    hundreds of store pages for nothing."""
    user = _prop_user(db_session, "u-carry")
    cand = _cand(db_session, user, "CUSA07000_00", "Thing", platform="PS4")
    cand.raw_data = {
        "platform": "PS4",
        "rejectedTitle": "Wrong Guess",
        "storeTitle": "Thing: Special Edition",
        "proposalVersion": 1,
        "trophyProgress": 10,
    }
    db_session.commit()

    fresh = {"name": "Thing", "titleId": "CUSA07000_00", "platform": "PS4", "trophyProgress": 55, "sources": ["titles"]}
    psn._upsert_review_candidate(db_session, user, fresh, "title_fix")
    db_session.commit()

    raw = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA07000_00").one().raw_data
    assert raw["trophyProgress"] == 55, "crawl data IS refreshed"
    assert raw["rejectedTitle"] == "Wrong Guess", "a refusal must survive a resync"
    assert raw["storeTitle"] == "Thing: Special Edition"
    assert raw["proposalVersion"] == 1


def test_a_refused_name_is_not_proposed_again(db_session, monkeypatch):
    """Reopening must carry refusals forward. Otherwise the next lookup offers
    the name you already turned down, every sync, and a row reappearing stops
    meaning anything changed."""
    user = _prop_user(db_session, "u-refused")
    db_session.add(models.Platform(name="PS4", display_name="PlayStation 4", igdb_id=48))
    cand = _cand(db_session, user, "CUSA09999_00", "Sparse", platform="PS4")
    cand.raw_data = {"platform": "PS4", "rejectedTitle": "Sparse Name Deluxe"}
    db_session.commit()

    monkeypatch.setattr(psn.psn_store, "fetch_product", lambda p, locale=None: {"name": ""})

    def fake_search(term, platform_ids):
        return [{"id": 77, "name": "Sparse Name Deluxe", "platform_ids": platform_ids, "game_type": 0}]

    orig = psn._igdb_search_adapter
    psn._igdb_search_adapter = lambda *a, **k: fake_search
    try:
        psn.fill_review_proposals(db_session, user, store_sleep=0)
        db_session.commit()
    finally:
        psn._igdb_search_adapter = orig

    row = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA09999_00").one()
    assert row.proposal_status == "rejected", "the same refused name must not come back as a question"


def test_the_store_name_is_fetched_during_the_sync_and_cached(db_session, monkeypatch):
    """Batman, end to end. The store name is fetched HERE — during the review
    lookup — rather than by the store-metadata job, because that job works on
    library ENTRIES and this row is not an entry yet. Waiting until after
    import is what forced the correct-then-relink dance across three buttons.

    Cached on the row, so a re-sync over hundreds of rows spends nothing."""
    user = _prop_user(db_session, "u-store")
    db_session.add(models.Platform(name="PS4", display_name="PlayStation 4", igdb_id=48))
    cand = _cand(db_session, user, "CUSA05332_00", "Batman", platform="PS4")
    cand.raw_data = {"platform": "PS4", "productId": "UP1018-CUSA05332_00-BATMANTELLTALE00"}
    db_session.commit()

    fetches = []

    def fake_fetch(product_id, locale=None):
        fetches.append(product_id)
        return {"name": "Batman: The Telltale Series"}

    monkeypatch.setattr(psn.psn_store, "fetch_product", fake_fetch)

    def fake_search(term, platform_ids):
        # Sony's "Batman" is a dart throw; only the store's name identifies it.
        if "telltale" not in term.lower():
            return []
        return [{"id": 501, "name": "Batman: The Telltale Series", "platform_ids": platform_ids, "game_type": 0}]

    orig = psn._igdb_search_adapter
    psn._igdb_search_adapter = lambda *a, **k: fake_search
    try:
        psn.fill_review_proposals(db_session, user, store_sleep=0)
        db_session.commit()
        row = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA05332_00").one()
        assert row.proposed_title == "Batman: The Telltale Series", "the store name identified it"
        assert row.proposed_igdb_id == 501
        assert row.raw_data["storeTitle"] == "Batman: The Telltale Series", "cached on the row"

        # A re-sync must not pay for it again. Reset the row's lookup state so
        # it is eligible, and confirm the cache — not the network — answers.
        row.proposal_status = None
        db_session.commit()
        psn.fill_review_proposals(db_session, user, store_sleep=0)
    finally:
        psn._igdb_search_adapter = orig

    assert fetches == ["UP1018-CUSA05332_00-BATMANTELLTALE00"], "fetched once, then cached"


def test_a_transient_store_failure_is_not_cached_as_no_name(db_session, monkeypatch):
    """A flaky fetch must not permanently decide this game has no store name.
    A 404 is a real answer (delisted) and is cached; anything else retries."""
    user = _prop_user(db_session, "u-flaky")
    cand = _cand(db_session, user, "CUSA07777_00", "Sparse", platform="PS3")
    cand.raw_data = {"platform": "PS3", "productId": "UP0000-CUSA07777_00-XXXXXXXXXXXXXXXX"}
    db_session.commit()

    def boom(product_id, locale=None):
        raise httpx.ConnectError("network went away")

    monkeypatch.setattr(psn.psn_store, "fetch_product", boom)
    assert psn._store_title_for(cand, sleep=0) == ""
    assert "storeTitle" not in (cand.raw_data or {}), "a transient failure must stay retryable"

    monkeypatch.setattr(psn.psn_store, "fetch_product", lambda p, locale=None: (_ for _ in ()).throw(psn.psn_store.ProductNotFound("gone")))
    assert psn._store_title_for(cand, sleep=0) == ""
    assert cand.raw_data["storeTitle"] == "", "delisted IS an answer — cache it"


def test_trophy_only_rows_are_held_back_for_review_not_imported(db_session):
    """A trophy-set name is often wrong and Sony can never improve it — no store
    record exists for these generations (#181), and there's no playtime or
    metadata to fall back on. Importing one silently is how "SF3: Online
    Edition" became a library entry nobody knew was wrong. So it is held back
    and the entry is created on confirm, under the approved name. There is no
    rename path because nothing is written under the bad name first."""
    user = _prop_user(db_session, "u-hold")
    db_session.commit()

    merged = [
        {"name": "SF3: Online Edition", "npCommunicationId": "NPWR01456_00", "platform": "PS3"},
        {"name": "Bloodborne", "titleId": "CUSA00900_00", "platform": "PS4"},
    ]
    db_session.add(models.Platform(name="PlayStation 4", display_name="PS4", igdb_id=48))
    db_session.commit()

    result = psn.import_merged(db_session, user, merged)
    db_session.commit()

    held = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR01456_00").first()
    assert held is not None, "a suspect trophy-set name must not import silently"
    assert held.kind == "title_fix"
    assert result["needs_review"] >= 1

    titles_in_lib = {g.title for g in db_session.query(models.Game).all()}
    assert "SF3: Online Edition" not in titles_in_lib, "held back, so never written under the bad name"
    # This user HAS IGDB credentials, so store-backed titles are vetted too and
    # wait alongside the trophy-only one — Sony's store names go wrong in their
    # own way ("Ghost of Tsushima Legends", edition suffixes, an Elden Ring
    # bonus guide arriving as a game). Without credentials there is nothing to
    # check against and they import directly, which the other import tests cover.
    assert "Bloodborne" not in titles_in_lib
    store_row = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA00900_00").first()
    assert store_row is not None and store_row.kind == "title_fix"


def test_proposal_job_runs_on_candidates_not_the_library(db_session):
    """Phase 1 is candidates only — nothing in the library is touched. Walking
    existing library entries to attach igdb_ids is phase 2 (#161), and a
    button rather than part of sync."""
    user = _prop_user(db_session, "u-cand")
    _cand(db_session, user, "NPWR00001_00", "Shinovi Versus")
    _cand(db_session, user, "CUSA00001_00", "Store Backed Name")  # store name is fine
    db_session.commit()

    seen = []

    def fake_search(term, platform_ids):
        seen.append(term)
        # A realistic correction: the canonical name ADDS a franchise prefix.
        # An unrelated name is now rejected by the similarity gate, which is
        # what stops "ELDEN RING" becoming "Elden Ring Nightreign".
        return [{"id": 1, "name": "Senran Kagura: Shinovi Versus", "platform_ids": platform_ids}]

    import backend.psn as psn_mod

    orig = psn_mod._igdb_search_adapter
    psn_mod._igdb_search_adapter = lambda *a, **k: fake_search
    try:
        out = psn_mod.fill_review_proposals(db_session, user)
    finally:
        psn_mod._igdb_search_adapter = orig

    # Store-backed rows ARE looked up now. The old rule skipped them on the
    # theory that a store name is fine as-is, which the PS4/PS5 import
    # disproved — and with the sync holding those rows back too, skipping them
    # would queue them and then never ask IGDB anything about them (#180).
    assert seen == ["Shinovi Versus", "Store Backed Name"]
    assert out["checked"] == 2 and out["proposed"] == 1
    row = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR00001_00").first()
    assert row.proposed_title == "Senran Kagura: Shinovi Versus"
    assert row.proposal_status == "pending"


def test_proposal_job_records_a_miss_so_it_stays_visible(db_session):
    """SF3: IGDB cannot expand the abbreviation. The row is marked, not
    forgotten — it still shows in the queue so the name can be fixed by hand."""
    user = _prop_user(db_session, "u-miss")
    _cand(db_session, user, "NPWR09999_00", "SF3: Online Edition")
    db_session.commit()

    import backend.psn as psn_mod

    orig = psn_mod._igdb_search_adapter
    psn_mod._igdb_search_adapter = lambda *a, **k: lambda term, pids: []
    try:
        out = psn_mod.fill_review_proposals(db_session, user)
    finally:
        psn_mod._igdb_search_adapter = orig

    assert out["no_match"] == 1 and out["proposed"] == 0
    row = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR09999_00").first()
    assert row.proposal_status == "none", "recorded, so a re-sync doesn't pay for the same miss"
    assert row.proposed_title is None


def test_proposal_job_skips_rows_already_looked_up_or_decided(db_session):
    """Self-gating is what makes this safe to chain off a sync — but it gates on
    "looked up BY THIS MATCHER", not merely "looked up".

    Gating on the latter froze every answer in place: a matcher improvement
    could never reach rows that had already been asked about, and the only way
    to revisit them was hand-written SQL against the candidates table."""
    user = _prop_user(db_session, "u-skip")
    _cand(db_session, user, "NPWR00002_00", "Decided", status="confirmed")
    current = {"platform": "PS3", "proposalVersion": psn._PROPOSAL_VERSION}
    proposed = _cand(db_session, user, "NPWR00003_00", "Proposed", proposal_status="pending")
    proposed.raw_data = dict(current)
    missed = _cand(db_session, user, "NPWR00004_00", "Missed", proposal_status="none")
    missed.raw_data = dict(current)
    db_session.commit()

    import backend.psn as psn_mod

    called = []
    orig = psn_mod._igdb_search_adapter
    psn_mod._igdb_search_adapter = lambda *a, **k: lambda t, p: called.append(t) or []
    try:
        out = psn_mod.fill_review_proposals(db_session, user)
    finally:
        psn_mod._igdb_search_adapter = orig

    assert called == [], f"no IGDB calls should be spent, got {called}"
    assert out["checked"] == 0

    # ...but a row answered by an OLDER matcher is asked again, so an
    # improvement reaches it on the next sync without anyone running SQL.
    stale = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR00003_00").one()
    stale.raw_data = {"platform": "PS3", "proposalVersion": psn._PROPOSAL_VERSION - 1}
    db_session.commit()

    called.clear()
    psn_mod._igdb_search_adapter = lambda *a, **k: lambda t, p: called.append(t) or []
    try:
        out = psn_mod.fill_review_proposals(db_session, user, store_sleep=0)
    finally:
        psn_mod._igdb_search_adapter = orig
    assert out["checked"] == 1, "a stale proposal is revisited"


def test_proposal_job_is_a_no_op_without_igdb_credentials(db_session):
    """No Twitch/IGDB key means no lookup — and it must say so rather than
    silently reporting zero suggestions, which reads as 'nothing to fix'."""
    user = models.User(name="u-nokey", username="u-nokey", password_hash="x", api_token="nokey-tok")
    db_session.add(user)
    db_session.commit()
    out = psn.fill_review_proposals(db_session, user)
    assert out["skipped_no_credentials"] is True
    assert out["checked"] == 0


def test_one_queue_carries_both_kinds_and_their_proposals(db_session):
    """A trophy set can need its name approved, its platforms chosen, or BOTH.
    Splitting those into two queues would mean importing one game required
    visiting two places, with no ordering guarantee — and a row needing both
    would either appear twice or land in whichever queue won."""
    user = _prop_user(db_session, "u-queue")
    db_session.add(models.Platform(name="PlayStation Vita", display_name="PS Vita", igdb_id=46))
    db_session.flush()

    # Needs BOTH: cross-play platforms AND a name fix.
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0BOTH_00",
            title="閃乱カグラ SHINOVI VERSUS",
            kind="cross_play",
            status="pending",
            raw_data={"platform": "PS3,PSVITA", "trophyTitlePlatform": "PS3,PSVITA"},
            proposed_title="Senran Kagura: Shinovi Versus",
            proposed_igdb_id=11536,
            proposed_platforms=[46],
            proposal_status="pending",
        )
    )
    # Needs only a name fix — imported cleanly, single platform.
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0NAME_00",
            title="SF3: Online Edition",
            kind="title_fix",
            status="pending",
            raw_data={"platform": "PS3", "trophyTitlePlatform": "PS3"},
            proposal_status="none",
        )
    )
    db_session.commit()

    rows = {r["external_id"]: r for r in psn.import_review_rows(db_session, user.id)}
    assert set(rows) == {"NPWR0BOTH_00", "NPWR0NAME_00"}, "both kinds share one queue"

    both = rows["NPWR0BOTH_00"]
    assert both["proposed_title"] == "Senran Kagura: Shinovi Versus"
    assert both["proposal_status"] == "pending"
    assert both["proposed_platforms"] == ["PSVITA"], "accepting narrows to what IGDB confirms — the phantom PS3 goes"
    assert both["kind_is_title_fix"] is False, "this row is here for its platforms too"

    unknown = rows["NPWR0NAME_00"]
    assert unknown["proposal_status"] == "none", "an unidentified row still has to be seen"
    assert unknown["proposed_title"] is None
    assert unknown["kind_is_title_fix"] is True, "a name-only row still sits in the same queue"


def test_pre_ticks_are_not_filtered_by_hardware_owned(db_session):
    """One cross-buy purchase puts every version on the account whether or not
    the console was ever in the house — and a PS3 sold years ago doesn't
    un-buy the PS3 copy. So every platform in the set is pre-ticked.

    Narrowing comes from the two things that actually know something: a
    cross-buy exception (sold separately per platform), and an accepted IGDB
    proposal restricting to the platforms the game really shipped on."""
    _seed_platforms(db_session)
    user = _user(db_session, "notick")
    # Nothing at all in the library — under an ownership filter this would be
    # the worst case, with no evidence for any platform.
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR_XBUY_00",
            title="Shovel Knight: Treasure Trove",
            kind="cross_play",
            status="pending",
            raw_data={"platform": "PS3,PSVITA,PS4", "normalizedName": "shovelknighttreasuretrove"},
        )
    )
    db_session.commit()

    row = psn.import_review_rows(db_session, user.id)[0]
    ticked = {o["platform"] for o in row["options"] if o["selected"]}
    assert ticked == {"PS3", "PSVITA", "PS4"}, f"every platform in the set should pre-tick, got {ticked}"


def test_accepting_the_igdb_name_creates_the_entry_under_it(client, db_session):
    """The corrected name is applied AT CREATION — the bad name is never
    written, so there is nothing to rename. Approving the name and choosing
    platforms is ONE decision on ONE row."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    # Most trophy-only rows are PS3/Vita, and a cross-buy set narrowed by IGDB
    # commonly resolves to Vita alone. Seeded here rather than in the shared
    # fixture, which other tests assert exact platform sets against.
    db_session.add(models.Platform(name="PSVITA", display_name="PlayStation Vita"))
    db_session.flush()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR03481_00",
            title="閃乱カグラ SHINOVI VERSUS",
            kind="cross_play",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR03481_00",
                "name": "閃乱カグラ SHINOVI VERSUS",
                "displayName": "閃乱カグラ SHINOVI VERSUS",
                "platform": "PS3,PSVITA",
                "normalizedName": "shinoviversus",
            },
            proposed_title="Senran Kagura: Shinovi Versus",
            proposed_igdb_id=11536,
            proposed_platforms=[46],
            proposal_status="pending",
        )
    )
    db_session.commit()

    r = client.post(
        "/tools/psn-review/NPWR03481_00/confirm", data={"platforms": ["PSVITA"], "use_proposed": "true"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200

    games = {g.title for g in db_session.query(models.Game).all()}
    assert "Senran Kagura: Shinovi Versus" in games
    assert "閃乱カグラ SHINOVI VERSUS" not in games, "the bad name must never be written"
    game = db_session.query(models.Game).filter_by(title="Senran Kagura: Shinovi Versus").one()
    assert game.igdb_id == 11536, "the id is what later unblocks metadata and the match veto"


def test_declining_the_name_keeps_sonys(client, db_session):
    """Unticking the box confirms the platforms under PSN's own name."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR00791_00",
            title="Modern Warfare 2",
            kind="cross_play",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR00791_00",
                "name": "Modern Warfare 2",
                "displayName": "Modern Warfare 2",
                "platform": "PS3",
                "normalizedName": "modernwarfare2",
            },
            proposed_title="Call of Duty: Modern Warfare 2",
            proposed_igdb_id=559,
            proposed_platforms=[9],
            proposal_status="pending",
        )
    )
    db_session.commit()

    client.post("/tools/psn-review/NPWR00791_00/confirm", data={"platforms": ["PS3"]}, headers={"HX-Request": "true"})

    games = {g.title for g in db_session.query(models.Game).all()}
    assert "Modern Warfare 2" in games
    assert "Call of Duty: Modern Warfare 2" not in games


def test_rejecting_a_proposal_discards_the_whole_match(client, db_session):
    """Name and platforms go together. A lookup wrong about the name has no
    claim to be right about the platforms it returned — honouring half of a
    rejected match would map the row to the wrong name AND platform."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0BAD_00",
            title="Some Trophy Set",
            kind="cross_play",
            status="pending",
            raw_data={"platform": "PS3,PSVITA", "normalizedName": "sometrophyset"},
            proposed_title="Wrong Game",
            proposed_igdb_id=1,
            proposed_platforms=[9],
            proposal_status="pending",
        )
    )
    db_session.commit()

    r = client.post("/tools/psn-review/NPWR0BAD_00/reject-name", headers={"HX-Request": "true"})
    assert r.status_code == 200

    cand = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR0BAD_00").one()
    assert cand.proposal_status == "rejected"
    assert cand.proposed_title is None and cand.proposed_igdb_id is None
    assert cand.proposed_platforms is None, "the platform narrowing goes with the name"
    assert cand.status == "pending", "the row stays in the queue with Sony's raw data"
    assert cand.title == "Some Trophy Set"


def test_bulk_confirm_honours_the_igdb_name(client, db_session):
    """Bulk posted platforms only, so bulk-confirming a row that was visibly
    showing the IGDB name created the entry under Sony's — the per-row Confirm
    honoured the suggestion and bulk silently did not."""
    import json as _json

    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR00791_00",
            title="Modern Warfare 2",
            kind="cross_play",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR00791_00",
                "name": "Modern Warfare 2",
                "displayName": "Modern Warfare 2",
                "platform": "PS3",
                "normalizedName": "modernwarfare2",
            },
            proposed_title="Call of Duty: Modern Warfare 2",
            proposed_igdb_id=559,
            proposed_platforms=[9],
            proposal_status="pending",
        )
    )
    db_session.commit()

    payload = _json.dumps({"NPWR00791_00": {"platforms": ["PS3"], "use_proposed": True}})
    r = client.post("/tools/psn-review/bulk-confirm", data={"selections": payload}, headers={"HX-Request": "true"})
    assert r.status_code == 200

    games = {g.title for g in db_session.query(models.Game).all()}
    assert "Call of Duty: Modern Warfare 2" in games, "bulk must honour the accepted name"
    assert "Modern Warfare 2" not in games


def test_bulk_confirm_still_accepts_the_old_payload_shape(client, db_session):
    """A page loaded before the shape changed posts a bare list. It must confirm
    the platforms rather than being silently dropped as malformed."""
    import json as _json

    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR00123_00",
            title="Some Game",
            kind="cross_play",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR00123_00",
                "name": "Some Game",
                "displayName": "Some Game",
                "platform": "PS3",
                "normalizedName": "somegame",
            },
        )
    )
    db_session.commit()

    payload = _json.dumps({"NPWR00123_00": ["PS3"]})
    r = client.post("/tools/psn-review/bulk-confirm", data={"selections": payload}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    cand = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR00123_00").one()
    assert cand.status == "confirmed"


def test_a_typed_name_beats_both_sony_and_igdb(client, db_session):
    """When the trophy-set name AND the IGDB suggestion are both wrong, the fix
    belongs in the review row. Otherwise the only route is accepting a name you
    know is bad to create the entry, then editing it in the library after."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0TYPE_00",
            title="METAL GEAR SOLID 4",
            kind="title_fix",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR0TYPE_00",
                "name": "METAL GEAR SOLID 4",
                "displayName": "METAL GEAR SOLID 4",
                "platform": "PS3",
                "normalizedName": "metalgearsolid4",
            },
            proposed_title="Metal Gear Solid 4 Database",
            proposed_igdb_id=999,
            proposed_platforms=[9],
            proposal_status="pending",
        )
    )
    db_session.commit()

    client.post(
        "/tools/psn-review/NPWR0TYPE_00/confirm",
        data={"platforms": ["PS3"], "use_proposed": "true", "custom_title": "Metal Gear Solid 4: Guns of the Patriots"},
        headers={"HX-Request": "true"},
    )

    games = {g.title for g in db_session.query(models.Game).all()}
    assert "Metal Gear Solid 4: Guns of the Patriots" in games
    assert "Metal Gear Solid 4 Database" not in games, "the typed name must win"
    assert "METAL GEAR SOLID 4" not in games

    game = db_session.query(models.Game).filter_by(title="Metal Gear Solid 4: Guns of the Patriots").one()
    assert game.igdb_id is None, "a typed name overrules the match, so its id must not ride along"


def test_leaving_the_prefilled_name_alone_still_takes_the_igdb_id(client, db_session):
    """The field is prefilled with what would be written anyway, so not touching
    it has to behave exactly like accepting the suggestion — id included."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0KEEP_00",
            title="GTA IV",
            kind="title_fix",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR0KEEP_00",
                "name": "GTA IV",
                "displayName": "GTA IV",
                "platform": "PS3",
                "normalizedName": "gta4",
            },
            proposed_title="Grand Theft Auto IV",
            proposed_igdb_id=731,
            proposed_platforms=[9],
            proposal_status="pending",
        )
    )
    db_session.commit()

    client.post(
        "/tools/psn-review/NPWR0KEEP_00/confirm",
        data={"platforms": ["PS3"], "use_proposed": "true", "custom_title": "Grand Theft Auto IV"},
        headers={"HX-Request": "true"},
    )
    game = db_session.query(models.Game).filter_by(title="Grand Theft Auto IV").one()
    assert game.igdb_id == 731


def test_a_numeral_appended_after_our_title_is_a_different_installment():
    """ "Stealth Inc." must not become "Stealth Inc 2: A Game of Clones". IGDB
    ranks the sequel as a main game and the correct answer as a port, so
    game_type alone picks the wrong one — position of the numeral is what
    separates them.

    A franchise prefix puts its numeral BEFORE our words and is the same game:
    "Skyrim" -> "The Elder Scrolls V: Skyrim" has to survive."""
    assert not psn._is_same_game("Stealth Inc.", "Stealth Inc 2: A Game of Clones")
    assert not psn._is_same_game("Sly", "Sly 2: Band of Thieves")
    # ...but the same numeral repeated is the same game, spelled out further.
    assert psn._is_same_game("Sly 2", "Sly 2: Band of Thieves")
    assert psn._is_same_game("Skyrim", "The Elder Scrolls V: Skyrim")
    assert psn._is_same_game("Modern Warfare 2", "Call of Duty: Modern Warfare 2")
    assert psn._is_same_game("METAL GEAR SOLID 4", "Metal Gear Solid 4: Guns of the Patriots")


def test_game_type_beats_word_counting():
    """IGDB classifies its own entries, which is authoritative where guessing
    from the title was not. "Metal Gear Solid 4 Database" (dlc) added FEWER
    words than "…: Guns of the Patriots" and so won on word count alone."""
    hits = [
        {"id": 1, "name": "Metal Gear Solid 4 Database", "platform_ids": [9], "game_type": 1},
        {"id": 2, "name": "Metal Gear Solid 4: Guns of the Patriots", "platform_ids": [9], "game_type": 0},
    ]
    got = psn.build_proposal("METAL GEAR SOLID 4", ["PS3"], [9], lambda t, p: hits)
    assert got["proposed_title"] == "Metal Gear Solid 4: Guns of the Patriots"
    assert got["proposed_igdb_id"] == 2

    # A bundle is never a better name for one trophy set — this is the row that
    # is a MEMBER of the collection, not the collection.
    only_bundle = [{"id": 3, "name": "Devil May Cry HD Collection", "platform_ids": [9], "game_type": 3}]
    assert psn.build_proposal("Devil May Cry HD", ["PS3"], [9], lambda t, p: only_bundle) is None

    # RANKING, not rejection: neither of these is filtered out, so the only
    # thing separating them is that one is a main game and the other an
    # expanded edition. On word count alone "Ultimate" (1 added word) beats
    # "Fate of Two Worlds" (4) and the wrong release wins.
    mvc = [
        {"id": 4, "name": "Ultimate Marvel vs. Capcom 3", "platform_ids": [9], "game_type": 10},
        {"id": 5, "name": "Marvel vs. Capcom 3: Fate of Two Worlds", "platform_ids": [9], "game_type": 0},
    ]
    got = psn.build_proposal("MARVEL VS. CAPCOM 3", ["PS3"], [9], lambda t, p: mvc)
    assert got["proposed_title"] == "Marvel vs. Capcom 3: Fate of Two Worlds"


def test_edit_modal_renames_and_confirm_uses_it(client, db_session):
    """Editing is a modal like every other review page, not an inline field.
    The name saved there is what the entry gets created under."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0EDIT_00",
            title="METAL GEAR SOLID 4",
            kind="title_fix",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR0EDIT_00",
                "name": "METAL GEAR SOLID 4",
                "displayName": "METAL GEAR SOLID 4",
                "platform": "PS3",
                "normalizedName": "metalgearsolid4",
            },
            proposed_title="Metal Gear Solid 4 Database",
            proposed_igdb_id=999,
            proposed_platforms=[9],
            proposal_status="pending",
        )
    )
    db_session.commit()

    assert client.get("/tools/psn-review/NPWR0EDIT_00/edit", headers={"HX-Request": "true"}).status_code == 200

    client.post(
        "/tools/psn-review/NPWR0EDIT_00/edit", data={"title": "Metal Gear Solid 4: Guns of the Patriots"}, headers={"HX-Request": "true"}
    )
    cand = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR0EDIT_00").one()
    assert cand.proposed_title == "Metal Gear Solid 4: Guns of the Patriots"
    assert cand.proposed_igdb_id is None, "overruling the match drops its id"

    client.post(
        "/tools/psn-review/NPWR0EDIT_00/confirm", data={"platforms": ["PS3"], "use_proposed": "true"}, headers={"HX-Request": "true"}
    )
    games = {g.title for g in db_session.query(models.Game).all()}
    assert "Metal Gear Solid 4: Guns of the Patriots" in games
    assert "Metal Gear Solid 4 Database" not in games


def test_a_matched_row_attaches_its_id_without_renaming(client, db_session):
    """An exact name match is identified, not unidentified. Nothing to approve,
    but the id still lands — that id is the whole point of the lookup."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0EXACT_00",
            title="3D DOT GAME HEROES",
            kind="title_fix",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR0EXACT_00",
                "name": "3D DOT GAME HEROES",
                "displayName": "3D DOT GAME HEROES",
                "platform": "PS3",
                "normalizedName": "3ddotgameheroes",
            },
            proposed_title=None,
            proposed_igdb_id=7265,
            proposed_platforms=[9],
            proposal_status="matched",
        )
    )
    db_session.commit()

    client.post("/tools/psn-review/NPWR0EXACT_00/confirm", data={"platforms": ["PS3"]}, headers={"HX-Request": "true"})
    game = db_session.query(models.Game).filter_by(title="3D DOT GAME HEROES").one()
    assert game.igdb_id == 7265, "a matched row must still attach its id"


def test_picking_an_igdb_result_in_the_modal_corrects_the_match(client, db_session):
    """The user is already making the judgement call while editing, so let them
    fix the MATCH there rather than just the text and have to look it up again
    later. A chosen id wins outright over whatever the automatic lookup found."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0PICK_00",
            title="God of War",
            kind="title_fix",
            status="pending",
            raw_data={
                "npCommunicationId": "NPWR0PICK_00",
                "name": "God of War",
                "displayName": "God of War",
                "platform": "PS3",
                "normalizedName": "godofwar",
            },
            proposed_title="God of War: Ascension",
            proposed_igdb_id=1291,
            proposed_platforms=[9],
            proposal_status="pending",
        )
    )
    db_session.commit()

    # The automatic match was the wrong game; pick the right one.
    client.post("/tools/psn-review/NPWR0PICK_00/edit", data={"title": "God of War", "igdb_id": "6036"}, headers={"HX-Request": "true"})

    cand = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR0PICK_00").one()
    assert cand.proposed_igdb_id == 6036, "a chosen id overrules the automatic match"
    assert cand.proposed_title is None, "the name is unchanged from Sony's, so nothing to rename"

    client.post("/tools/psn-review/NPWR0PICK_00/confirm", data={"platforms": ["PS3"]}, headers={"HX-Request": "true"})
    game = db_session.query(models.Game).filter_by(title="God of War").one()
    assert game.igdb_id == 6036, "the corrected id has to reach the library entry"


def test_media_apps_are_flagged_for_review_not_dropped(db_session):
    """A demo is never a completion, so filtering it silently is safe. "Do you
    want Netflix in your library" is a preference and not ours to decide — and a
    hardcoded list can never be complete, so silently dropping whatever it
    happens to match is the one mistake nobody can notice afterwards."""
    _seed_platforms(db_session)
    user = _user(db_session, "media")
    db_session.commit()

    merged = [
        {"name": "Amazon Prime Video", "titleId": "CUSA00130_00", "platform": "PS4", "sources": ["purchased"]},
        {"name": "Bloodborne", "titleId": "CUSA00900_00", "platform": "PS4", "sources": ["purchased"]},
    ]
    psn.import_merged(db_session, user, merged)
    db_session.commit()

    flagged = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA00130_00").first()
    assert flagged is not None, "a media app must be asked about, not silently dropped"
    assert flagged.kind == "media_app"
    titles_in_lib = {g.title for g in db_session.query(models.Game).all()}
    assert "Amazon Prime Video" not in titles_in_lib
    assert "Bloodborne" in titles_in_lib


def test_media_app_matching_never_eats_a_real_game():
    """Substring matching would take "Zen Pinball 2" (pi-NBA-ll) and "Shadow
    Complex" (com-PLEX). Whole-title comparison, spaceless because
    "PlayStation(TM)Vue" loses its glyph and becomes one word."""
    for name in ("Amazon Prime Video", "Netflix", "HBO GO", "PlayStation™Vue", "Twitch"):
        assert psn.is_media_app({"name": name}), name
    for name in ("Zen Pinball 2", "Shadow Complex Remastered", "NBA Playgrounds", "MARVEL Pinball", "Bloodborne™"):
        assert not psn.is_media_app({"name": name}), name


def test_a_decided_row_can_be_reopened(client, db_session):
    """This queue used to drop decided rows entirely, so a mis-click was
    unrecoverable from the UI — import review keeps a Confirmed tab and
    played-only leaves them inline. Reopening restores the row only; entries a
    confirm created are left alone, since silently deleting library rows from an
    undo is a bigger surprise than leaving them."""
    _seed_platforms(db_session)
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="NPWR0UNDO_00",
            title="Some Game",
            kind="title_fix",
            status="dismissed",
            chosen_platforms=[],
            raw_data={
                "npCommunicationId": "NPWR0UNDO_00",
                "name": "Some Game",
                "displayName": "Some Game",
                "platform": "PS3",
                "normalizedName": "somegame",
            },
        )
    )
    db_session.commit()

    assert [r["key"] for r in psn.decided_rows(db_session, user.id)] == ["NPWR0UNDO_00"]
    assert psn.import_review_rows(db_session, user.id) == []

    r = client.post("/tools/psn-review/NPWR0UNDO_00/reopen", headers={"HX-Request": "true"})
    assert r.status_code == 200

    cand = db_session.query(models.PsnReviewCandidate).filter_by(external_id="NPWR0UNDO_00").one()
    assert cand.status == "pending"
    assert psn.decided_rows(db_session, user.id) == []
    assert [row["key"] for row in psn.import_review_rows(db_session, user.id)] == ["NPWR0UNDO_00"]


def test_a_ps4_purchase_never_merges_into_a_ps3_trophy_set():
    """_platforms_compatible recognised only PS4 and PS5, so PS3 and Vita
    returned None and "no opinion" was read as compatible. A PS4 purchase then
    merged into a PS3 trophy set on name alone.

    Against the real library that mislabelled all 11 PS3/Vita entries,
    overwrote titles ("Gravity Rush Remastered" became "GRAVITY RUSH"), and
    suppressed the cross-play question entirely: Sound Shapes' PS3 set merged
    into its PS4 purchase, so the row looked single-platform and nothing asked.
    """
    purchased = [{"titleId": "CUSA04313_00", "name": "Dead Rising 2", "platform": "PS4"}]
    trophies = [{"npCommunicationId": "NPWR00699_00", "trophyTitleName": "Dead Rising 2", "trophyTitlePlatform": "PS3"}]
    merged = psn.merge_library(purchased, trophies, [])["merged"]

    assert len(merged) == 2, "a PS4 purchase and a PS3 trophy set are two releases"
    by_platform = {m["platform"]: m for m in merged}
    assert set(by_platform) == {"PS4", "PS3"}
    assert by_platform["PS4"]["titleId"] == "CUSA04313_00"
    assert by_platform["PS3"]["npCommunicationId"] == "NPWR00699_00"


def test_a_genuine_cross_buy_set_still_merges():
    """The guard is OVERLAP, not equality — a trophy set covering PS3, Vita and
    PS4 shares a platform with the PS4 purchase and is genuinely one item."""
    purchased = [{"titleId": "CUSA00001_00", "name": "Cross Buy Game", "platform": "PS4"}]
    trophies = [{"npCommunicationId": "NPWR00001_00", "trophyTitleName": "Cross Buy Game", "trophyTitlePlatform": "PS3,PSVITA,PS4"}]
    merged = psn.merge_library(purchased, trophies, [])["merged"]
    assert len(merged) == 1, "one purchase covering several platforms is one item"
    assert set(merged[0]["sources"]) == {"purchased", "titles"}


def test_unknown_platform_still_merges():
    """A played row carries a category, not a platform. Unknown on either side
    has to keep meaning "no opinion" or those stop merging entirely."""
    purchased = [{"titleId": "CUSA00002_00", "name": "Some Game", "platform": "PS4"}]
    played = [{"titleId": "CUSA00002_00", "name": "Some Game", "category": "ps4_game", "playDuration": "PT1H"}]
    merged = psn.merge_library(purchased, [], played)["merged"]
    assert len(merged) == 1


def test_a_completed_trophy_set_is_never_absorbed_by_a_play_record():
    """Limbo: a 100% Vita trophy set and a PS4 play session with no trophies.
    They used to merge into one row wearing the Vita label and the PS4 id,
    which is two different things pretending to be one. The trophy set — the
    thing carrying the achievement — has to survive on its own."""
    trophies = [
        {
            "npCommunicationId": "NPWR04612_00",
            "trophyTitleName": "Limbo",
            "trophyTitlePlatform": "PSVITA",
            "progress": 100,
            "earnedTrophies": {"bronze": 9, "silver": 3, "gold": 1, "platinum": 0},
        }
    ]
    played = [{"titleId": "CUSA01664_00", "name": "LIMBO", "category": "ps4_game", "playDuration": "PT2H"}]
    merged = psn.merge_library([], trophies, played)["merged"]

    vita = [m for m in merged if m.get("npCommunicationId") == "NPWR04612_00"]
    assert len(vita) == 1, "the completed trophy set must remain its own row"
    assert vita[0]["trophyProgress"] == 100
    assert vita[0]["platform"] == "PSVITA"
    assert psn.is_played_only(vita[0]) is False, "it has a trophy set, so it is not played-only"


def _linkable(db, user, ext, title, platform="PS4"):
    g = models.Game(title=title, display_name=title)
    db.add(g)
    db.flush()
    rel = models.GameRelease(
        game_id=g.id,
        source="psn",
        external_id=ext,
        platform=platform,
        platform_id=models.resolve_platform_id(db, platform),
        raw_data={"platform": platform},
    )
    db.add(rel)
    db.flush()
    db.add(models.UserLibraryEntry(user_id=user.id, release_id=rel.id, import_source="psn_import"))
    db.flush()
    return g


def test_phase2_attaches_a_confident_match_and_queues_an_ambiguous_one(db_session):
    """Store-backed entries import directly, so their name is fine and there is
    nothing to rename — but without an id there is no metadata (#161) and the
    match veto stays inert.

    An exact name match attaches silently. Anything else is a judgement call, so
    it goes to the queue rather than guessing: a wrong id attaches wrong
    metadata, which is quiet and only visible if you go looking."""
    _seed_platforms(db_session)
    user = _prop_user(db_session, "link")
    # _seed_platforms already made PS4 — give it the real IGDB id (48).
    db_session.query(models.Platform).filter_by(name="PS4").update({"igdb_id": 48})
    db_session.flush()
    sure = _linkable(db_session, user, "CUSA00001_00", "Bloodborne")
    unsure = _linkable(db_session, user, "CUSA00002_00", "Some Ambiguous Game")
    db_session.commit()

    def fake_search(term, ids):
        if term == "Bloodborne":
            return [{"id": 7, "name": "Bloodborne", "platform_ids": ids, "game_type": 0}]
        return [
            {"id": 11, "name": "Some Ambiguous Game: Director's Cut", "platform_ids": ids, "game_type": 0},
            {"id": 12, "name": "Some Ambiguous Game Remake", "platform_ids": ids, "game_type": 0},
        ]

    import backend.psn as psn_mod

    orig = psn_mod._igdb_search_adapter
    psn_mod._igdb_search_adapter = lambda *a, **k: fake_search
    try:
        out = psn_mod.link_igdb_ids(db_session, user)
    finally:
        psn_mod._igdb_search_adapter = orig

    assert out["linked"] == 1 and out["queued"] == 1, out
    db_session.refresh(sure)
    assert sure.igdb_id == 7, "an exact match attaches without asking"
    db_session.refresh(unsure)
    assert unsure.igdb_id is None, "an ambiguous one must not guess"

    cand = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA00002_00").one()
    assert cand.kind == "igdb_link"
    assert cand.proposed_igdb_id == 11


def test_confirming_an_igdb_link_row_attaches_rather_than_creating(db_session):
    """These entries are ALREADY in the library. Confirming attaches the id to
    the existing entry — creating would duplicate it."""
    _seed_platforms(db_session)
    user = _prop_user(db_session, "attach")
    # _seed_platforms already made PS4 — give it the real IGDB id (48).
    db_session.query(models.Platform).filter_by(name="PS4").update({"igdb_id": 48})
    db_session.flush()
    game = _linkable(db_session, user, "CUSA00003_00", "Some Game")
    db_session.add(
        models.PsnReviewCandidate(
            user_id=user.id,
            external_id="CUSA00003_00",
            title="Some Game",
            kind="igdb_link",
            status="pending",
            proposed_igdb_id=42,
            proposed_platforms=[48],
            proposal_status="pending",
            raw_data={"platform": "PS4"},
        )
    )
    db_session.commit()
    before = db_session.query(models.GameRelease).filter_by(source="psn").count()

    psn.confirm_entry_decision(db_session, user, "CUSA00003_00", [])
    db_session.refresh(game)

    assert game.igdb_id == 42
    assert db_session.query(models.GameRelease).filter_by(source="psn").count() == before, "must not create a release"
    cand = db_session.query(models.PsnReviewCandidate).filter_by(external_id="CUSA00003_00").one()
    assert cand.status == "confirmed"


def test_store_retitle_defers_to_an_igdb_id():
    """Sony repurposes store listings: the base Ghost of Tsushima SKU now serves
    a page titled "Ghost of Tsushima: Legends", a separate standalone product.
    An id means something already verified the name against the platform, so the
    store must not overwrite it."""
    src = open("backend/psn_store.py").read()
    block = src[src.index("def _apply_title(") : src.index("def apply_metadata(")]
    assert "if game.igdb_id:" in block, "the retitle must defer when an id exists"
    assert "return False" in block[block.index("if game.igdb_id:") :][:80]


def test_a_review_row_renders_its_new_shape(client, db_session):
    """The row said the same thing three times — a "Needs you: …" verdict, a
    generic reason line, and a rename badge — while showing raw minutes and a
    redundant "100% 30/30" (#180).

    Now: each name carries its source, so the strikethrough explains itself; one
    short badge says why the row is here; playtime goes through the same filter
    as the Steam library; trophies get a glyph, a count and a bar.

    Rendered for real, because none of that is reachable from unit tests — a
    missing filter or a bad include only fails at request time."""
    token = _signup_and_login(client)
    user = db_session.query(models.User).filter_by(api_token=token).first()
    user.psn_npsso = "n" * 64
    db_session.add(models.Platform(name="PS4", display_name="PlayStation 4"))
    cand = _cand(db_session, user, "CUSA0RENDER_00", "Atelier Totori The Adventurer of Arland", platform="PS4")
    cand.raw_data = {
        "platform": "PS4",
        "trophyProgress": 62,
        "playByCategory": {"ps4_game": 690},
        "trophies": {"bronze": 30, "silver": 8},
        "earnedTrophies": {"bronze": 20, "silver": 4},
    }
    cand.proposed_title = "Atelier Totori Plus: The Adventurer of Arland"
    cand.proposed_igdb_id = 999
    cand.proposal_status = "pending"
    db_session.commit()

    # A row IGDB vouched for WITHOUT needing a rename. Before, it rendered
    # identically to one IGDB could not identify at all — nothing on the row
    # said a match had been found.
    matched = _cand(db_session, user, "CUSA0MATCH_00", "Assassin's Creed II", platform="PS4")
    matched.raw_data = {"platform": "PS4", "trophies": {"bronze": 51}, "earnedTrophies": {"bronze": 51}}
    matched.proposed_igdb_id = 4321
    matched.proposal_status = "matched"
    db_session.commit()

    body = client.get("/tools/psn-review", headers={"HX-Request": "true"}).text

    assert "Matched to IGDB 4321" in body, "a match with nothing to rename must still say so"
    assert "Atelier Totori Plus: The Adventurer of Arland" in body
    assert "IGDB" in body and "PlayStation" in body, "each name says where it came from"
    assert "Edit to correct the match" not in body, "no reasoning line — it was true of every row"
    assert "tag-platform-playstation" in body and "tag-igdb" in body, "sources are coloured chips"
    assert "Name corrected" not in body, "reason badges are gone — the strikethrough says it"
    assert "11.5 hours" in body, "playtime uses the shared filter, beside the title where width is free"
    assert "690m" not in body
    assert "24/38" in body and "cgt-trophy-bar" in body
