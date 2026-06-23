"""Sync match review — detect and manage potential duplicates between manual
library entries and synced platform games.

Detection pass
--------------
For each manual UserLibraryEntry owned by the user, compare its title against
every synced game on the same platform.  A confidence score (0.0–1.0) is
computed via token-based matching (see _score).  Only the single best-scoring
synced game is kept per manual entry — no spam of franchise-name matches.

Already-reviewed pairs (merged/dismissed) are never re-queued.
Re-running the scan is safe; pending candidates get updated scores.

Scoring tiers:
  >= 0.90  High    — exact or near-exact token match (possibly with trailing subtitle)
  >= 0.70  Medium  — strong overlap, slight spelling differences or short title ambiguity
  >= 0.65  Low     — threshold; below this the match is too noisy to surface

Algorithm summary
-----------------
1. Normalise: lowercase, HTML-unescape, ASCII-fold, & → and, roman numerals → arabic,
   strip non-alphanumeric, collapse whitespace, drop leading articles.
2. Tokenise both titles.
3. Greedy token matching: for each token in the shorter title find the best
   fuzzy-matched token in the longer (SequenceMatcher per token, threshold 0.75).
4. Base score = matched / max(len_a, len_b)  — extra tokens in either direction
   drag the score down, so "Castlevania" vs "Castlevania Lords of Shadow 2"
   scores 1/5 = 0.20 and is dropped.
5. Trailing-number subtitle rule: if every manual token is present in the
   synced title AND the last manual token is a number AND the manual title has
   ≥ 2 tokens, treat the synced title as the same game with a marketing subtitle
   and boost to 0.88.  Handles "Witcher 3" → "Witcher 3: Wild Hunt",
   "Halo 3" → "Halo 3: ODST", "Devil May Cry 4" → "Devil May Cry 4: Special Edition".
   Does NOT fire for single-token titles like "Castlevania".
"""

from __future__ import annotations

import datetime
import html
import re
import unicodedata
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from backend import models

MIN_SCORE: float = 0.65
_TOKEN_FUZZY_THRESHOLD: float = 0.75

# ──────────────────────────────────────────────────────────────────────────────
# Title normalisation
# ──────────────────────────────────────────────────────────────────────────────

_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NONALNUM_RE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s{2,}")

# Roman numeral tokens → arabic.  Whole-word only (applied after tokenising).
_ROMAN_MAP: dict[str, str] = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
    "xi": "11",
    "xii": "12",
    "xiii": "13",
    "xiv": "14",
    "xv": "15",
}


def _normalise_tokens(title: str) -> list[str]:
    """Return a list of normalised tokens for a title."""
    t = html.unescape(title)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = t.lower()
    t = t.replace("&", "and")
    t = _NONALNUM_RE.sub(" ", t)
    t = _MULTI_SPACE.sub(" ", t).strip()
    t = _ARTICLE_RE.sub("", t).strip()
    tokens = t.split()
    # Roman numeral conversion — whole tokens only
    return [_ROMAN_MAP.get(tok, tok) for tok in tokens]


def _tok_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


# ──────────────────────────────────────────────────────────────────────────────
# Confidence scoring
# ──────────────────────────────────────────────────────────────────────────────


def _score(manual_title: str, synced_title: str) -> float:
    """Return a 0.0–1.0 confidence score for the pair.

    See module docstring for full algorithm description.
    """
    a_toks = _normalise_tokens(manual_title)
    b_toks = _normalise_tokens(synced_title)

    if not a_toks or not b_toks:
        return 0.0

    # Exact token-sequence match
    if a_toks == b_toks:
        return 1.0

    max_len = max(len(a_toks), len(b_toks))

    # Greedy token matching — shorter title's tokens matched against longer's
    if len(a_toks) <= len(b_toks):
        shorter, longer = a_toks, b_toks
    else:
        shorter, longer = b_toks, a_toks

    used: set[int] = set()
    matched = 0.0
    for tok in shorter:
        best_sim = 0.0
        best_j = -1
        for j, ltok in enumerate(longer):
            if j in used:
                continue
            sim = _tok_sim(tok, ltok)
            if sim > best_sim:
                best_sim = sim
                best_j = j
        if best_sim >= _TOKEN_FUZZY_THRESHOLD:
            matched += best_sim
            used.add(best_j)

    base_score = matched / max_len

    # ── Trailing-number subtitle rule ──────────────────────────────────────
    # If ALL manual tokens are present in the synced title AND the last manual
    # token is a digit AND the manual title has ≥ 2 tokens, the synced title
    # is almost certainly the same game with a marketing subtitle tacked on.
    # Boost to 0.88 (Medium-high) so it surfaces clearly without claiming
    # a perfect match.
    #
    # Guard: single-token manual titles (e.g. "Castlevania") are excluded so
    # "Castlevania" doesn't boost against "Castlevania: Lords of Shadow 2".
    manual_toks = _normalise_tokens(manual_title)
    if len(manual_toks) >= 2 and manual_toks[-1].isdigit() and base_score < 0.88:
        # Check all manual tokens appear in synced (with fuzzy tolerance)
        synced_toks = _normalise_tokens(synced_title)
        remaining = list(synced_toks)
        all_present = True
        for mt in manual_toks:
            found = False
            for i, st in enumerate(remaining):
                if _tok_sim(mt, st) >= _TOKEN_FUZZY_THRESHOLD:
                    remaining.pop(i)
                    found = True
                    break
            if not found:
                all_present = False
                break
        if all_present:
            base_score = max(base_score, 0.88)

    return round(min(base_score, 0.99), 4)


def confidence_label(score: float) -> str:
    if score >= 0.90:
        return "High"
    if score >= 0.70:
        return "Medium"
    return "Low"


def confidence_css(score: float) -> str:
    """Catppuccin CSS variable name for the confidence badge colour."""
    if score >= 0.90:
        return "var(--ctp-green)"
    if score >= 0.70:
        return "var(--ctp-yellow)"
    return "var(--ctp-peach)"


# ──────────────────────────────────────────────────────────────────────────────
# Detection pass
# ──────────────────────────────────────────────────────────────────────────────


def _build_compat_map(db: Session) -> tuple[dict[int, set[int]], dict[str, set[str]]]:
    """Build platform compatibility maps for matching purposes.

    Steam (the custom distribution platform) is compatible with PC platforms
    because Steam games run on Windows/Linux/Mac. When a user manually adds a
    game as "PC (Microsoft Windows)" and then syncs Steam, we still want to
    find the match — even when the manual entry has no linked platform_id.

    Returns:
        id_compat:   {platform_id → set of compatible platform_ids}
        name_compat: {normalised_platform_name → set of compatible normalised names}
                     Used as a fallback when one side has no linked platform_id.
    """
    pc_names = {"PC (Microsoft Windows)", "Linux", "Mac", "PC"}
    steam_names = {"Steam"}

    pc_ids: set[int] = set()
    steam_ids: set[int] = set()
    all_platforms = db.query(models.Platform).all()

    for p in all_platforms:
        if p.name in pc_names:
            pc_ids.add(p.id)
        if p.name in steam_names:
            steam_ids.add(p.id)

    id_compat: dict[int, set[int]] = {}
    for sid in steam_ids:
        id_compat.setdefault(sid, set()).update(pc_ids)
    for pid in pc_ids:
        id_compat.setdefault(pid, set()).update(steam_ids)

    # Build normalised name → compatible normalised names map so entries with
    # no linked platform_id can still be matched by string.
    pid_to_norm = {p.id: " ".join(_normalise_tokens(p.name)) for p in all_platforms}
    name_compat: dict[str, set[str]] = {}
    for pid, compat_ids in id_compat.items():
        pn = pid_to_norm.get(pid, "")
        for cid in compat_ids:
            cn = pid_to_norm.get(cid, "")
            if pn and cn:
                name_compat.setdefault(pn, set()).add(cn)
                name_compat.setdefault(cn, set()).add(pn)

    return id_compat, name_compat


def _platform_compatible(
    manual_pid: int | None,
    manual_pstr: str,
    synced_pid: int | None,
    synced_pstr: str,
    id_compat: dict[int, set[int]],
    name_compat: dict[str, set[str]],
) -> bool:
    """Return True if the two platform specs are compatible for match purposes."""
    if manual_pid and synced_pid:
        if manual_pid == synced_pid:
            return True
        return synced_pid in id_compat.get(manual_pid, set()) or manual_pid in id_compat.get(synced_pid, set())

    # At least one side lacks a linked platform_id — fall back to normalised
    # string comparison, with compat name map as a secondary check.
    manual_norm = " ".join(_normalise_tokens(manual_pstr))
    synced_norm = " ".join(_normalise_tokens(synced_pstr))
    if manual_norm == synced_norm:
        return True
    return synced_norm in name_compat.get(manual_norm, set()) or manual_norm in name_compat.get(synced_norm, set())


def scan_for_matches(db: Session, user: models.User) -> dict:
    """Compare manual entries against all synced games on the same platform.

    For each manual entry, only the single highest-scoring synced game above
    MIN_SCORE is kept as a candidate — no multiple hits per manual entry.

    Returns counts: {"candidates_added": N, "candidates_updated": N, "pairs_checked": N}
    """
    manual_entries = (
        db.query(models.UserLibraryEntry)
        .join(models.GameRelease)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.UserLibraryEntry.import_source == "manual",
        )
        .all()
    )

    if not manual_entries:
        return {"candidates_added": 0, "candidates_updated": 0, "pairs_checked": 0}

    synced_entries = (
        db.query(models.UserLibraryEntry)
        .join(models.GameRelease)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.UserLibraryEntry.import_source.in_(["steam_import", "psn_import"]),
        )
        .all()
    )

    id_compat, name_compat = _build_compat_map(db)

    # Build lookup of existing candidates keyed by (manual_entry_id, platform_source, external_id)
    existing_candidates: dict[tuple, models.SyncMatchCandidate] = {
        (c.manual_entry_id, c.platform_source, c.external_id): c
        for c in db.query(models.SyncMatchCandidate)
        .filter(models.SyncMatchCandidate.manual_entry_id.in_([e.id for e in manual_entries]))
        .all()
    }

    added = 0
    updated = 0
    pairs_checked = 0

    for manual in manual_entries:
        manual_title = manual.release.game.display_name or manual.release.game.title
        manual_platform_id = manual.release.platform_id

        # Collect all scoring candidates for this manual entry
        scored: list[tuple[float, models.UserLibraryEntry]] = []

        for synced in synced_entries:
            if not _platform_compatible(
                manual_platform_id,
                manual.release.platform,
                synced.release.platform_id,
                synced.release.platform,
                id_compat,
                name_compat,
            ):
                continue

            pairs_checked += 1
            synced_title = synced.release.game.display_name or synced.release.game.title
            score = _score(manual_title, synced_title)

            if score >= MIN_SCORE:
                scored.append((score, synced))

        if not scored:
            continue

        # Sort descending by score
        scored.sort(key=lambda x: x[0], reverse=True)

        # Surface multiple candidates only when there are 2+ at HIGH confidence
        # (>= 0.95) — this catches same-title different-release situations like
        # RE2 (1998) vs RE2 (2019). Otherwise only keep the single best.
        HIGH = 0.95
        high_scored = [(s, e) for s, e in scored if s >= HIGH]

        # When only one candidate reaches HIGH confidence, check whether other
        # candidates above MIN_SCORE are year-disambiguated versions of the same
        # title (e.g. "Resident Evil 2 (1998)" vs "Resident Evil 2").  Strip the
        # trailing (YYYY) and re-score; if that brings them to HIGH, include them
        # so the user can pick the correct release.
        if len(high_scored) == 1:
            best_title = high_scored[0][1].release.game.display_name or high_scored[0][1].release.game.title
            _year_re = re.compile(r"\s*\(\d{4}\)\s*$")
            for s, e in scored:
                if s >= HIGH:
                    continue
                t = e.release.game.display_name or e.release.game.title
                stripped = _year_re.sub("", t).strip()
                if _score(manual_title, stripped) >= HIGH or _score(best_title, stripped) >= HIGH:
                    high_scored.append((s, e))

        candidates_to_write = high_scored if len(high_scored) >= 2 else scored[:1]

        for best_score, best_synced in candidates_to_write:
            synced_title = best_synced.release.game.display_name or best_synced.release.game.title
            external_id = best_synced.release.external_id or str(best_synced.release.id)
            platform_source = best_synced.release.source

            key = (manual.id, platform_source, external_id)
            existing = existing_candidates.get(key)

            if existing is None:
                db.add(
                    models.SyncMatchCandidate(
                        manual_entry_id=manual.id,
                        platform_source=platform_source,
                        external_id=external_id,
                        synced_title=synced_title,
                        match_score=best_score,
                        status="pending",
                    )
                )
                added += 1
            elif existing.status == "pending":
                existing.match_score = best_score
                existing.synced_title = synced_title
                updated += 1
            # merged / dismissed — leave alone

    db.commit()
    return {"candidates_added": added, "candidates_updated": updated, "pairs_checked": pairs_checked}


# ──────────────────────────────────────────────────────────────────────────────
# Merge action
# ──────────────────────────────────────────────────────────────────────────────


def merge_candidate(db: Session, candidate: models.SyncMatchCandidate, user: models.User) -> bool:
    """Promote the synced entry to canonical; migrate user data from manual entry.

    The synced release (Steam/PSN) is the authoritative record — it has the
    external ID, playtime, and will keep receiving updates on future syncs.
    The manual entry was a placeholder. Merge direction:

    1. Move completions from manual UserLibraryEntry → synced UserLibraryEntry.
    3. User artwork is NOT migrated — synced entry has official platform art
       (Steam CDN etc.); user can re-pick SGDB art if desired.
    4. Delete manual UserLibraryEntry, then its GameRelease if orphaned, then
       its Game if orphaned.
    5. Invalidate any other pending candidates pointing at the same synced
       release so they don't surface as stale merge options.
    6. Mark candidate merged.

    Returns True on success, False if the synced release can't be found.
    """
    synced_release = db.query(models.GameRelease).filter_by(source=candidate.platform_source, external_id=candidate.external_id).first()
    if synced_release is None:
        return False

    synced_entry = db.query(models.UserLibraryEntry).filter_by(user_id=user.id, release_id=synced_release.id).first()
    if synced_entry is None:
        return False

    manual_entry = candidate.manual_entry
    manual_release = manual_entry.release
    manual_game = manual_release.game

    # igdb_id is intentionally NOT promoted to the synced game — Steam entries
    # can't have IGDB IDs set through the UI, and copying it here would require
    # clearing it from the manual game first to avoid the unique constraint,
    # which is more complexity than it's worth for no user-visible benefit.

    # 1. Move completions to synced entry via direct SQL to avoid ORM
    # relationship tracking nulling the FK when manual_entry is later deleted.
    db.query(models.Completion).filter_by(library_entry_id=manual_entry.id).update({"library_entry_id": synced_entry.id})

    # User artwork is intentionally NOT migrated — the synced entry has
    # official platform artwork (Steam CDN etc.) which is better than whatever
    # SGDB auto-fetched for the manual placeholder. User can re-pick if wanted.

    # 4. Mark merged and invalidate siblings BEFORE deleting the manual entry.
    # The candidate has ondelete=CASCADE on manual_entry_id — if we delete the
    # entry first the candidate row vanishes at DB level and the status update
    # below becomes a no-op.
    candidate.status = "merged"
    candidate.reviewed_at = datetime.datetime.now(datetime.UTC)

    # 5. Invalidate sibling candidates pointing at the same synced release.
    db.query(models.SyncMatchCandidate).filter(
        models.SyncMatchCandidate.platform_source == candidate.platform_source,
        models.SyncMatchCandidate.external_id == candidate.external_id,
        models.SyncMatchCandidate.id != candidate.id,
        models.SyncMatchCandidate.status == "pending",
    ).update({"status": "dismissed", "note": "Invalidated — sibling candidate was merged."})

    db.flush()

    # 6. Delete manual entry, then cascade if orphaned.
    db.delete(manual_entry)
    db.flush()

    remaining_entries = db.query(models.UserLibraryEntry).filter_by(release_id=manual_release.id).count()
    if remaining_entries == 0:
        db.delete(manual_release)
        db.flush()
        remaining_releases = db.query(models.GameRelease).filter_by(game_id=manual_game.id).count()
        if remaining_releases == 0:
            db.delete(manual_game)
        db.flush()

    db.commit()
    return True


def dismiss_candidate(db: Session, candidate: models.SyncMatchCandidate, note: str | None = None) -> None:
    """Mark a candidate as dismissed."""
    candidate.status = "dismissed"
    candidate.note = note
    candidate.reviewed_at = datetime.datetime.now(datetime.UTC)
    db.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Queue helpers
# ──────────────────────────────────────────────────────────────────────────────


def pending_count(db: Session, user: models.User) -> int:
    return (
        db.query(models.SyncMatchCandidate.manual_entry_id)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.SyncMatchCandidate.status == "pending",
        )
        .distinct()
        .count()
    )


def get_candidates(db: Session, user: models.User, include_skipped: bool = False):
    """Return candidates for the review page, sorted by score descending."""
    q = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(models.UserLibraryEntry.user_id == user.id)
    )
    if not include_skipped:
        q = q.filter(models.SyncMatchCandidate.status == "pending")
    else:
        q = q.filter(models.SyncMatchCandidate.status.in_(["pending", "dismissed"]))
    return q.order_by(models.SyncMatchCandidate.match_score.desc()).all()
