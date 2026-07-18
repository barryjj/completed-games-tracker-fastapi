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
