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
import time

import httpx
from sqlalchemy import func

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


# ─── Title cleaning ─────────────────────────────────────────────────────────
# Store names carry cruft the API feeds don't: trademark glyphs, trailing
# platform tags ("PS4 & PS5"), and edition/bundle suffixes ("Digital Deluxe
# Edition", "- Cross-Gen Bundle", "- Season Pass"). Strip them so an adopted
# title reads like the game, not a store listing. Deliberately conservative:
# "Ultimate Edition", "Complete Edition", "Ultra Deluxe" and "The Collection"
# are real product names and are left alone.

_TM_RE = re.compile(r"\(TM\)|™|®", re.IGNORECASE)
_PLATFORM_TAG_RE = re.compile(r"\s*(?:[-–]\s*)?PS4(?:\s*&\s*PS5)?\s*$|\s*(?:[-–]\s*)?PS5\s*$", re.IGNORECASE)
_EDITION_RE = re.compile(
    r"\s*(?:[-–]\s*)?"
    r"(?:(?:Digital\s+)?(?:Deluxe|Standard|Premium)\s+Edition"
    r"|Cross-Gen(?:\s+Deluxe)?\s+Bundle"
    r"|Complete\s+Bundle"
    r"|Season\s+Pass)"
    r"\s*$",
    re.IGNORECASE,
)


def _clean_store_title(name: str | None) -> str:
    """Strip store-listing cruft (trademark glyphs, platform tags, edition /
    bundle suffixes) from a title. Loops because a name can carry several at
    once ('EA SPORTS FC 26 Standard Edition PS4 & PS5')."""
    if not name:
        return ""
    s = _TM_RE.sub("", str(name))
    prev = None
    while prev != s:
        prev = s
        s = _PLATFORM_TAG_RE.sub("", s)
        s = _EDITION_RE.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -–")


def _bundle_titleids(db, product_id: str) -> set[str]:
    """The distinct titleIds (release external_ids) that share this productId."""
    from . import models

    if not product_id:
        return set()
    rows = (
        db.query(models.GameRelease.external_id)
        .filter(
            models.GameRelease.source == "psn",
            func.json_extract(models.GameRelease.raw_data, "$.productId") == product_id,
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows if r[0]}


def _is_multigame_bundle(titleids: set[str]) -> bool:
    """True when a productId is shared across genuinely different games (a
    multi-game bundle like Bleed Complete Bundle), NOT a cross-gen pair of the
    same game (one CUSA/PS4 + one PPSA/PS5, which is fine to adopt)."""
    tids = {t for t in titleids if t}
    if len(tids) <= 1:
        return False
    gens = {("PS5" if t.startswith("PPSA") else "PS4" if t.startswith("CUSA") else "?") for t in tids}
    if len(tids) == 2 and gens == {"PS4", "PS5"}:
        return False  # cross-gen: same game, two platform SKUs
    return True


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


def _apply_title(db, release) -> bool:
    """Set the game's title from the cached store record, cleaned. Returns True
    when the title changed. Idempotent — safe to re-run for repair.

    Guards, in order:
      - never touch a user-renamed title or a game that also has a Steam release
      - for a multi-game-bundle productId (shared across genuinely different
        games), do NOT adopt the bundle's store name — the entry is an
        individual game, so keep/restore its original PSN import name (which
        survives in raw_data). This both prevents and repairs the case where
        e.g. Bleed and Bleed 2 both got titled "Bleed Complete Bundle".
      - otherwise adopt the cleaned store name.
    """
    game = release.game
    if game is None or game.display_name_user_set or not _is_psn_only(db, game):
        return False
    raw = release.raw_data or {}
    store = raw.get("store") or {}
    product_id = product_id_for(release)
    if _is_multigame_bundle(_bundle_titleids(db, product_id)):
        correct = _clean_store_title(raw.get("displayName") or raw.get("name"))
    else:
        correct = _clean_store_title(store.get("name"))
    if not correct or correct == game.title:
        return False
    game.title = correct
    game.display_name = None  # display_title falls back to the (now correct) title
    return True


def apply_metadata(db, release, meta: dict) -> bool:
    """Persist a fetched store record onto the release and (re)derive the title.
    Returns True when the title was rewritten."""
    raw = dict(release.raw_data or {})
    raw["store"] = meta
    release.raw_data = raw  # reassign: SQLAlchemy won't see in-place JSON edits
    release.metadata_fetched_at = datetime.datetime.now(datetime.UTC)
    return _apply_title(db, release)


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


# Store metadata is very stable (a game's publisher/genre/release date don't
# change), so re-check far less often than Steam appdetails.
_STORE_STALE_DAYS = 30


def store_is_stale(release) -> bool:
    """True when a PSN release has a productId but no store record, or one older
    than the staleness window. Drives both the batch job's skip logic and the
    detail-pane auto-refetch."""
    if not product_id_for(release):
        return False
    if not (release.raw_data or {}).get("store"):
        return True
    fetched = release.metadata_fetched_at
    if fetched is None:
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=datetime.UTC)
    return (datetime.datetime.now(datetime.UTC) - fetched).days >= _STORE_STALE_DAYS


def refresh_all_store_metadata(db, user, sleep: float = 1.0, progress_callback=None) -> dict:
    """Walk this user's PSN releases and fetch store metadata for the ones that
    need it (have a productId, and are missing/stale). Rate-limited and
    committed per release so partial progress survives an interruption and the
    DB isn't held in one long write transaction.

    Returns counts by outcome. Transient HTTP errors on a single release are
    swallowed (counted, that release left unfetched) so one flaky page doesn't
    abort the whole run."""
    from . import models

    releases = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry, models.UserLibraryEntry.release_id == models.GameRelease.id)
        .filter(models.UserLibraryEntry.user_id == user.id, models.GameRelease.source == "psn")
        .distinct()
        .all()
    )
    counts = {"updated": 0, "retitled": 0, "not_found": 0, "skipped": 0, "no_product": 0, "errored": 0}
    total = len(releases)
    for i, release in enumerate(releases):
        if progress_callback:
            progress_callback(i, total, release.game.title if release.game else "")
        if not product_id_for(release):
            counts["no_product"] += 1
            continue
        if store_is_stale(release):
            try:
                counts[refresh_release(db, release)] += 1
                db.commit()
            except Exception as e:  # transient network/HTTP — skip this one, keep going
                logger.warning("PS Store refresh failed for %s: %s", product_id_for(release), e)
                db.rollback()
                counts["errored"] += 1
            if sleep:
                time.sleep(sleep)
            continue
        # Not stale: re-derive the title from the cached store record (no fetch).
        # This repairs titles polluted by an earlier, pre-cleaning run — ™®/
        # edition cruft and bundle-name clobbering — on the next job run.
        store = (release.raw_data or {}).get("store") or {}
        if store and not store.get("not_found") and _apply_title(db, release):
            counts["retitled"] += 1
            db.commit()
        else:
            counts["skipped"] += 1
    return counts
