"""PS Store product-page metadata — the PSN equivalent of Steam's appdetails.

The three PSN API feeds (purchased / trophy / played) and the
`tmdb.np.dl.playstation.net` JSON endpoint all return sparse names ("Batman",
"DIRT5") and carry no metadata. The **store product page**, keyed on the
`productId` we already store on the release, carries the real marketing title
plus publisher, release date, genres, platforms, rating and description (#168).

Extraction is structured JSON — no DOM-text scraping — but it is double-nested,
which is the non-obvious part:

    <script id="__NEXT_DATA__">                     -> JSON
      props.pageProps.batarangs.<widget>.text       -> an HTML fragment *string*
        <script type="application/json">            -> JSON
          Apollo cache keyed "Product:{productId}"  <- the actual record

`props.apolloState`'s own `Product` entity is a stub (id + name only) and is a
decoy — the real record lives inside the batarangs widget blobs. Because the
widget layout differs per page, we harvest depth-agnostically: merge every dict
whose key starts with `Product:{productId}`, wherever it turns up.
"""

from __future__ import annotations

import datetime
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://store.playstation.com"
_DEFAULT_LOCALE = "en-us"

# The store rejects/redirects unknown clients; present as a normal browser.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_SCRIPT_JSON_RE = re.compile(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S)
_LD_JSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class ProductNotFound(ValueError):
    """The store has no page for this productId — delisted or region-locked."""


def product_url(product_id: str, locale: str = _DEFAULT_LOCALE) -> str:
    return f"{_BASE}/{locale}/product/{product_id}"


def fetch_html(product_id: str, locale: str = _DEFAULT_LOCALE, timeout: int = 20) -> str:
    """GET the product page. Raises ProductNotFound on 404 (delisted titles are
    common in a long PSN history); other HTTP errors propagate as transient."""
    resp = httpx.get(product_url(product_id, locale), headers=_HEADERS, timeout=timeout, follow_redirects=True)
    if resp.status_code == 404:
        raise ProductNotFound(f"No store page for {product_id}")
    resp.raise_for_status()
    return resp.text


def _harvest_product(obj, product_id: str, out: dict) -> None:
    """Recursively merge every `Product:{product_id}*` dict found anywhere."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.startswith(f"Product:{product_id}") and isinstance(value, dict):
                for field, val in value.items():
                    if val not in (None, [], {}):
                        out.setdefault(field, val)
            _harvest_product(value, product_id, out)
    elif isinstance(obj, list):
        for value in obj:
            _harvest_product(value, product_id, out)


def parse_product(html: str, product_id: str) -> dict:
    """Pull the merged raw product record out of a store page. Returns {} when
    the page has no recognizable payload (store redesign, bot wall, etc.) —
    callers treat that as a miss rather than crashing the job."""
    raw: dict = {}
    match = _NEXT_DATA_RE.search(html)
    if match:
        try:
            next_data = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.warning("PS Store: __NEXT_DATA__ for %s did not parse", product_id)
            next_data = None
        if next_data:
            batarangs = next_data.get("props", {}).get("pageProps", {}).get("batarangs") or {}
            for widget in batarangs.values():
                text = widget.get("text") if isinstance(widget, dict) else None
                if not isinstance(text, str):
                    continue
                for blob in _SCRIPT_JSON_RE.findall(text):
                    try:
                        _harvest_product(json.loads(blob), product_id, raw)
                    except json.JSONDecodeError:
                        continue

    # schema.org block carries the cleanest name/description/image.
    ld = _LD_JSON_RE.search(html)
    if ld:
        try:
            doc = json.loads(ld.group(1))
        except json.JSONDecodeError:
            doc = {}
        for field in ("name", "description", "image"):
            if doc.get(field):
                raw.setdefault(field, doc[field])
    return raw


def normalize(raw: dict, product_id: str) -> dict:
    """Raw store record -> the flat shape we persist and render. Mirrors how the
    Steam appdetails blob is reduced for the detail pane."""
    genres = [g.get("value") for g in (raw.get("localizedGenres") or []) if isinstance(g, dict) and g.get("value")]
    star = raw.get("starRating") or {}
    return {
        "product_id": product_id,
        "name": raw.get("name"),
        "publisher": raw.get("publisherName"),
        "release_date": raw.get("releaseDate"),
        "genres": genres,
        "platforms": raw.get("platforms") or [],
        "rating": star.get("averageRating"),
        "rating_count": star.get("totalRatingsCount"),
        "classification": raw.get("storeDisplayClassification"),
        "languages": raw.get("spokenLanguages") or [],
        "description": raw.get("description"),
        "image": raw.get("image"),
        "fetched_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def fetch_product(product_id: str, locale: str = _DEFAULT_LOCALE) -> dict:
    """Fetch + parse + normalize one product. Raises ProductNotFound for 404s."""
    return normalize(parse_product(fetch_html(product_id, locale), product_id), product_id)


def product_id_for(release) -> str | None:
    """The store productId recorded on a PSN release, if any. Only ~70% of PSN
    releases have one — trophy-only (old Vita/PS3) entries never do."""
    if release.source != "psn":
        return None
    return ((release.raw_data or {}).get("productId")) or None


# ─── Persistence ───────────────────────────────────────────────────────────


def _is_psn_only(db, game) -> bool:
    """True when every release of this game is source='psn'.

    A Game row can carry both a Steam and a PSN release (they get matched into
    one game — Control Ultimate Edition does exactly this). In that case the
    title already came from Steam and is good; adopting the PSN store's casing
    over it would be a pointless churn, so titles are only rewritten for
    PSN-exclusive games."""
    from . import models

    other = db.query(models.GameRelease).filter(models.GameRelease.game_id == game.id, models.GameRelease.source != "psn").first()
    return other is None


def apply_metadata(db, release, meta: dict) -> bool:
    """Persist a fetched store record onto the release, and adopt the store's
    title when ours is sparse. Returns True when the title was rewritten.

    The title is only taken when the user hasn't renamed the game and the game
    is PSN-exclusive — never clobber a hand-edited or Steam-sourced title."""
    raw = dict(release.raw_data or {})
    raw["store"] = meta
    release.raw_data = raw  # reassign: SQLAlchemy won't see in-place JSON edits
    release.metadata_fetched_at = datetime.datetime.now(datetime.UTC)

    store_name = (meta.get("name") or "").strip()
    game = release.game
    if not store_name or game is None:
        return False
    if game.display_name_user_set or not _is_psn_only(db, game):
        return False
    if store_name == game.title:
        return False
    game.title = store_name
    game.display_name = None  # display_title falls back to the (now correct) title
    return True


def refresh_release(db, release) -> str:
    """Fetch + apply store metadata for one release. Returns an outcome string:
    'updated' | 'retitled' | 'no_product' | 'not_found'. Transient HTTP errors
    propagate so the caller can leave it unfetched and retry later."""
    product_id = product_id_for(release)
    if not product_id:
        return "no_product"
    try:
        meta = fetch_product(product_id)
    except ProductNotFound:
        # Delisted/region-locked: record the attempt so the staleness check
        # doesn't re-fetch it on every pane open.
        raw = dict(release.raw_data or {})
        raw["store"] = {"product_id": product_id, "not_found": True, "fetched_at": datetime.datetime.now(datetime.UTC).isoformat()}
        release.raw_data = raw
        release.metadata_fetched_at = datetime.datetime.now(datetime.UTC)
        return "not_found"
    return "retitled" if apply_metadata(db, release, meta) else "updated"
