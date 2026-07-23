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
