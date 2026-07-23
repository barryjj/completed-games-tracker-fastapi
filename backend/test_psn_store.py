"""PS Store product-page parsing (#168).

The fixture is built in-code rather than committing a ~700 KB real page, but it
reproduces the exact nesting the live store uses — including the decoy
`apolloState` stub — so a store-side change to that shape fails here loudly
instead of silently yielding empty metadata.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend import psn_store

PRODUCT_ID = "UP4040-PPSA01949_00-CONTROLUEPS50000"

_REAL_RECORD = {
    "__typename": "Product",
    "id": PRODUCT_ID,
    "name": "CONTROL Ultimate Edition",
    "publisherName": "REMEDY ENTERTAINMENT LTD.",
    "releaseDate": "2021-02-02T05:00:00Z",
    "localizedGenres": [{"__typename": "LocalizedGenre", "value": "Action"}],
    "platforms": ["PS4", "PS5"],
    "starRating": {"__typename": "StarRating", "averageRating": 4.39, "totalRatingsCount": 47038},
    "storeDisplayClassification": "FULL_GAME",
    "spokenLanguages": ["en", "fr", "es", "de"],
}


def _store_page(product_id=PRODUCT_ID, locale="en-us", record=None, include_ld=True):
    """Rebuild the live page shape: __NEXT_DATA__ -> batarangs.<widget>.text
    (an HTML fragment *string*) -> <script type="application/json"> -> Apollo
    cache keyed Product:{id}:{locale}."""
    inner = json.dumps({"cache": {f"Product:{product_id}:{locale}": record or _REAL_RECORD}})
    widget_text = f'<script id="env:abc" type="application/json">{inner}</script><div class="psw">markup</div>'
    next_data = {
        "props": {
            # Decoy: the top-level apolloState Product is a stub in the real page.
            "apolloState": {f"Product:{product_id}:{locale}": {"__typename": "Product", "id": product_id, "name": "stub"}},
            "pageProps": {"batarangs": {"info": {"text": widget_text}}},
        }
    }
    ld = json.dumps(
        {
            "@type": "Product",
            "name": "CONTROL Ultimate Edition",
            "description": "Control Ultimate Edition contains the main game and all Expansions.",
            "image": "https://image.api.playstation.com/vulcan/img/rnd/x.png",
        }
    )
    ld_tag = f'<script id="mfe-jsonld-tags" type="application/ld+json">{ld}</script>' if include_ld else ""
    # Next.js escapes "<" as < inside __NEXT_DATA__ so embedded markup can't
    # break out of the script tag. Mirror that — without it the fixture's inner
    # </script> would truncate the blob, which the live page never does.
    payload = json.dumps(next_data).replace("<", "\\u003c")
    return f'<html><head><script id="__NEXT_DATA__" type="application/json">{payload}</script>{ld_tag}</head><body></body></html>'


def test_parse_product_reaches_the_nested_record():
    raw = psn_store.parse_product(_store_page(), PRODUCT_ID)
    assert raw["publisherName"] == "REMEDY ENTERTAINMENT LTD."
    assert raw["releaseDate"] == "2021-02-02T05:00:00Z"
    assert raw["platforms"] == ["PS4", "PS5"]
    assert raw["starRating"]["averageRating"] == 4.39
    # JSON-LD supplies description/image.
    assert raw["description"].startswith("Control Ultimate Edition")
    assert raw["image"].endswith(".png")


def test_parse_product_survives_a_page_with_no_payload():
    """A store redesign / bot wall must be a miss, not a crash."""
    assert psn_store.parse_product("<html><body>nothing here</body></html>", PRODUCT_ID) == {}


def test_parse_product_ignores_a_different_products_record():
    """Only the requested productId is harvested. Page-level JSON-LD still
    contributes name/description, but none of the other product's fields leak."""
    page = _store_page(product_id="UP0000-OTHER_00-SOMETHINGELSE0")
    raw = psn_store.parse_product(page, PRODUCT_ID)
    assert "publisherName" not in raw
    assert "starRating" not in raw
    assert "platforms" not in raw


def test_normalize_flattens_to_the_stored_shape():
    meta = psn_store.normalize(psn_store.parse_product(_store_page(), PRODUCT_ID), PRODUCT_ID)
    assert meta["name"] == "CONTROL Ultimate Edition"
    assert meta["publisher"] == "REMEDY ENTERTAINMENT LTD."
    assert meta["genres"] == ["Action"]
    assert meta["platforms"] == ["PS4", "PS5"]
    assert meta["rating"] == 4.39
    assert meta["rating_count"] == 47038
    assert meta["classification"] == "FULL_GAME"
    assert meta["product_id"] == PRODUCT_ID
    assert meta["fetched_at"]


def test_normalize_tolerates_a_sparse_record():
    meta = psn_store.normalize({"name": "Bare"}, PRODUCT_ID)
    assert meta["name"] == "Bare"
    assert meta["genres"] == [] and meta["platforms"] == []
    assert meta["rating"] is None and meta["publisher"] is None


def test_fetch_html_raises_product_not_found_on_404():
    resp = MagicMock(status_code=404)
    with patch("backend.psn_store.httpx.get", return_value=resp):
        with pytest.raises(psn_store.ProductNotFound):
            psn_store.fetch_html(PRODUCT_ID)


def test_fetch_html_propagates_transient_errors():
    resp = MagicMock(status_code=503)
    resp.raise_for_status.side_effect = httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)
    with patch("backend.psn_store.httpx.get", return_value=resp):
        with pytest.raises(httpx.HTTPStatusError):
            psn_store.fetch_html(PRODUCT_ID)


def test_product_id_for_only_returns_psn_product_ids():
    psn = MagicMock(source="psn", raw_data={"productId": PRODUCT_ID})
    trophy_only = MagicMock(source="psn", raw_data={"npCommunicationId": "NPWR555_00"})
    steam = MagicMock(source="steam", raw_data={"productId": "nope"})
    assert psn_store.product_id_for(psn) == PRODUCT_ID
    assert psn_store.product_id_for(trophy_only) is None
    assert psn_store.product_id_for(steam) is None


# ─── Persistence (DB) ──────────────────────────────────────────────────────

from backend import models  # noqa: E402

_META = {"name": "Batman: The Telltale Series", "publisher": "Telltale", "genres": ["Adventure"], "rating": 3.5}


def _psn_release(db, title, product_id="UP0-CUSA05332_00-X", display_name_user_set=False, extra_source=None, user=None):
    game = models.Game(title=title, display_name_user_set=display_name_user_set)
    db.add(game)
    db.flush()
    rel = models.GameRelease(game_id=game.id, platform="PS4", source="psn", external_id=product_id, raw_data={"productId": product_id})
    db.add(rel)
    if extra_source:
        db.add(models.GameRelease(game_id=game.id, platform="Steam", source=extra_source, external_id=product_id + "-steam"))
    db.flush()
    if user is not None:
        db.add(models.UserLibraryEntry(user_id=user.id, release_id=rel.id, import_source="psn_import"))
        db.flush()
    return rel


def test_apply_metadata_stores_blob_and_adopts_title(db_session):
    rel = _psn_release(db_session, "Batman")
    retitled = psn_store.apply_metadata(db_session, rel, _META)
    assert retitled is True
    assert rel.game.title == "Batman: The Telltale Series"
    assert rel.raw_data["store"]["publisher"] == "Telltale"
    assert rel.metadata_fetched_at is not None


def test_apply_metadata_never_overwrites_a_user_set_title(db_session):
    rel = _psn_release(db_session, "My Batman", display_name_user_set=True)
    assert psn_store.apply_metadata(db_session, rel, _META) is False
    assert rel.game.title == "My Batman"
    # ...but the store blob is still stored for the detail pane.
    assert rel.raw_data["store"]["name"] == "Batman: The Telltale Series"


def test_apply_metadata_does_not_retitle_a_steam_shared_game(db_session):
    """A game with a Steam release too keeps its (good) title; only the blob lands."""
    rel = _psn_release(db_session, "Control Ultimate Edition", extra_source="steam")
    assert psn_store.apply_metadata(db_session, rel, {"name": "CONTROL Ultimate Edition"}) is False
    assert rel.game.title == "Control Ultimate Edition"
    assert rel.raw_data["store"]["name"] == "CONTROL Ultimate Edition"


def test_refresh_release_no_product_id(db_session):
    game = models.Game(title="Old Vita Game")
    db_session.add(game)
    db_session.flush()
    rel = models.GameRelease(
        game_id=game.id, platform="PSVITA", source="psn", external_id="NPWR555_00", raw_data={"npCommunicationId": "NPWR555_00"}
    )
    db_session.add(rel)
    db_session.flush()
    assert psn_store.refresh_release(db_session, rel) == "no_product"


def test_refresh_release_marks_not_found(db_session):
    rel = _psn_release(db_session, "Delisted Game")
    with patch("backend.psn_store.fetch_product", side_effect=psn_store.ProductNotFound("gone")):
        assert psn_store.refresh_release(db_session, rel) == "not_found"
    assert rel.raw_data["store"]["not_found"] is True
    assert rel.metadata_fetched_at is not None


def test_refresh_release_retitles_on_success(db_session):
    rel = _psn_release(db_session, "Batman")
    with patch("backend.psn_store.fetch_product", return_value=_META):
        assert psn_store.refresh_release(db_session, rel) == "retitled"
    assert rel.game.title == "Batman: The Telltale Series"


def test_store_is_stale(db_session):
    rel = _psn_release(db_session, "Batman")
    assert psn_store.store_is_stale(rel) is True  # productId present, no store record yet
    psn_store.apply_metadata(db_session, rel, _META)
    assert psn_store.store_is_stale(rel) is False  # just fetched
    # A trophy-only release (no productId) is never stale — nothing to fetch.
    g = models.Game(title="Old Vita")
    db_session.add(g)
    db_session.flush()
    tro = models.GameRelease(game_id=g.id, platform="PSVITA", source="psn", external_id="NPWR1", raw_data={"npCommunicationId": "NPWR1"})
    assert psn_store.store_is_stale(tro) is False


def _user(db, n):
    u = models.User(name=n, username=n, password_hash="x", api_token=n)
    db.add(u)
    db.commit()
    return u


def test_refresh_all_walks_only_owned_psn_releases_that_need_it(db_session):
    user = _user(db_session, "walker")
    _psn_release(db_session, "Batman", product_id="UP0-CUSA05332_00-X", user=user)
    _psn_release(db_session, "Some Game", product_id="UP0-CUSA111_00-Y", user=user)
    # trophy-only (no productId), owned
    g = models.Game(title="Old Vita")
    db_session.add(g)
    db_session.flush()
    tro = models.GameRelease(game_id=g.id, platform="PSVITA", source="psn", external_id="NPWR1", raw_data={"npCommunicationId": "NPWR1"})
    db_session.add(tro)
    db_session.flush()
    db_session.add(models.UserLibraryEntry(user_id=user.id, release_id=tro.id, import_source="psn_import"))
    db_session.commit()

    def fake(pid, locale="en-us"):
        return {"name": "Batman: The Telltale Series"} if "CUSA05332" in pid else {"name": "Some Game"}

    with patch("backend.psn_store.fetch_product", side_effect=fake):
        counts = psn_store.refresh_all_store_metadata(db_session, user, sleep=0)
    assert counts["retitled"] == 1  # Batman → fuller title
    assert counts["updated"] == 1  # Some Game: blob stored, title unchanged
    assert counts["no_product"] == 1  # trophy-only Vita entry


def test_refresh_all_skips_fresh_and_counts_transient_errors(db_session):
    user = _user(db_session, "walker2")
    fresh = _psn_release(db_session, "Fresh", product_id="UP0-CUSA1_00-A", user=user)
    psn_store.apply_metadata(db_session, fresh, {"name": "Fresh"})  # mark fetched now
    _psn_release(db_session, "Boom", product_id="UP0-CUSA2_00-B", user=user)
    db_session.commit()

    with patch("backend.psn_store.fetch_product", side_effect=httpx.ConnectError("down")):
        counts = psn_store.refresh_all_store_metadata(db_session, user, sleep=0)
    assert counts["skipped"] == 1  # fresh one, not re-fetched
    assert counts["errored"] == 1  # boom, transient error swallowed
