"""Sync match review — detect and manage potential duplicates between manual
library entries and synced platform games.

Detection pass
--------------
For each manual UserLibraryEntry owned by the user, compare its title against
every synced game on the same platform.  A confidence score (0.0–1.0) is
computed from a small set of normalised-title heuristics.  Pairs above the
minimum threshold are written to sync_match_candidates (upserted — re-running
is safe; already-reviewed pairs are not touched).

Scoring tiers (approximate guidance shown in the UI):
  >= 0.90  High    — exact or near-exact normalised match
  >= 0.70  Medium  — strong token overlap or subtitle difference
  >= 0.40  Low     — partial overlap, worth a look

Pairs below MIN_SCORE (0.40) are not queued — too noisy to be useful.
"""

from __future__ import annotations

import datetime
import html
import re
import unicodedata
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from backend import models

MIN_SCORE: float = 0.40

# ──────────────────────────────────────────────────────────────────────────────
# Title normalisation
# ──────────────────────────────────────────────────────────────────────────────

_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NONALNUM_RE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s{2,}")
# Strip common subtitle patterns: ": Subtitle", "- Subtitle", "(Subtitle)"
_SUBTITLE_RE = re.compile(r"\s*[:\-–]\s+.+$")
_PAREN_SUFFIX = re.compile(r"\s*\([^)]+\)\s*$")


def _normalise(title: str) -> str:
    """Lowercase, unescape HTML entities, strip punctuation and articles."""
    t = html.unescape(title)
    # Unicode normalise to strip accents etc.
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = t.lower()
    t = _NONALNUM_RE.sub(" ", t)
    t = _MULTI_SPACE.sub(" ", t).strip()
    t = _ARTICLE_RE.sub("", t).strip()
    return t


def _without_subtitle(title: str) -> str:
    t = _SUBTITLE_RE.sub("", title)
    t = _PAREN_SUFFIX.sub("", t)
    return _normalise(t)


def _token_set(s: str) -> set[str]:
    return set(s.split())


# ──────────────────────────────────────────────────────────────────────────────
# Confidence scoring
# ──────────────────────────────────────────────────────────────────────────────


def _score(manual_title: str, synced_title: str) -> float:
    """Return a 0.0–1.0 confidence score for the pair."""
    a = _normalise(manual_title)
    b = _normalise(synced_title)

    if not a or not b:
        return 0.0

    # Exact normalised match
    if a == b:
        return 1.0

    # One is a prefix/suffix of the other (handles edition suffixes like "GOTY Edition")
    if a.startswith(b) or b.startswith(a):
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))
        return 0.85 + 0.10 * (shorter / longer)

    # Subtitle-stripped match
    a_short = _without_subtitle(manual_title)
    b_short = _without_subtitle(synced_title)
    if a_short and b_short and a_short == b_short:
        return 0.88

    # SequenceMatcher ratio
    seq = SequenceMatcher(None, a, b).ratio()

    # Token overlap bonus
    ta, tb = _token_set(a), _token_set(b)
    if ta and tb:
        overlap = len(ta & tb) / max(len(ta), len(tb))
    else:
        overlap = 0.0

    score = seq * 0.6 + overlap * 0.4
    return round(min(score, 0.99), 4)


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


def scan_for_matches(db: Session, user: models.User) -> dict:
    """Compare manual entries against all synced games on the same platform.

    Returns counts: {"candidates_added": N, "candidates_updated": N, "pairs_checked": N}
    """
    # Load all manual library entries for this user (not hidden, not already merged)
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

    # Load all synced releases for this user (steam_import / psn_import)
    synced_entries = (
        db.query(models.UserLibraryEntry)
        .join(models.GameRelease)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.UserLibraryEntry.import_source.in_(["steam_import", "psn_import"]),
        )
        .all()
    )

    # Build a lookup of existing candidates so we don't re-evaluate reviewed pairs
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

        for synced in synced_entries:
            # Only compare same platform (by platform_id if both linked, else by
            # normalised platform string)
            if manual_platform_id and synced.release.platform_id:
                if manual_platform_id != synced.release.platform_id:
                    continue
            else:
                if _normalise(manual.release.platform) != _normalise(synced.release.platform):
                    continue

            synced_title = synced.release.game.display_name or synced.release.game.title
            external_id = synced.release.external_id or str(synced.release.id)
            platform_source = synced.release.source  # "steam" | "psn"

            pairs_checked += 1
            score = _score(manual_title, synced_title)

            if score < MIN_SCORE:
                continue

            key = (manual.id, platform_source, external_id)
            existing = existing_candidates.get(key)

            if existing is None:
                candidate = models.SyncMatchCandidate(
                    manual_entry_id=manual.id,
                    platform_source=platform_source,
                    external_id=external_id,
                    synced_title=synced_title,
                    match_score=score,
                    status="pending",
                )
                db.add(candidate)
                added += 1
            elif existing.status == "pending":
                # Update score in case titles changed
                existing.match_score = score
                existing.synced_title = synced_title
                updated += 1
            # If already merged or kept_separate — leave it alone

    db.commit()
    return {"candidates_added": added, "candidates_updated": updated, "pairs_checked": pairs_checked}


# ──────────────────────────────────────────────────────────────────────────────
# Merge action
# ──────────────────────────────────────────────────────────────────────────────


def merge_candidate(db: Session, candidate: models.SyncMatchCandidate, user: models.User) -> bool:
    """Merge the synced game into the manual entry.

    - Finds the synced GameRelease via (platform_source, external_id)
    - Copies external_id, raw_data, source, artwork to the manual release
    - Deletes the synced UserLibraryEntry (manual one survives with its completions)
    - Marks candidate merged
    Returns True on success, False if the synced release can't be found.
    """
    synced_release = db.query(models.GameRelease).filter_by(source=candidate.platform_source, external_id=candidate.external_id).first()
    if synced_release is None:
        return False

    manual_entry = candidate.manual_entry
    manual_release = manual_entry.release

    # Copy platform data onto the manual release
    manual_release.external_id = synced_release.external_id
    manual_release.source = synced_release.source
    manual_release.raw_data = synced_release.raw_data
    manual_release.metadata_fetched_at = synced_release.metadata_fetched_at
    if synced_release.platform_id and not manual_release.platform_id:
        manual_release.platform_id = synced_release.platform_id

    # Re-home artwork from synced release to manual release
    for artwork in list(synced_release.artwork):
        # Skip if manual release already has artwork of this type+source
        already = any(a.artwork_type == artwork.artwork_type and a.source == artwork.source for a in manual_release.artwork)
        if not already:
            artwork.release_id = manual_release.id

    # Update the manual library entry import source
    manual_entry.import_source = f"{candidate.platform_source}_import"

    # Remove the synced UserLibraryEntry (manual entry keeps its completions)
    synced_entry = db.query(models.UserLibraryEntry).filter_by(user_id=user.id, release_id=synced_release.id).first()
    if synced_entry:
        db.delete(synced_entry)

    # If the synced release now has no entries, clean it up too
    db.flush()
    remaining = db.query(models.UserLibraryEntry).filter_by(release_id=synced_release.id).count()
    if remaining == 0 and synced_release.id != manual_release.id:
        db.delete(synced_release)

    # Mark candidate
    candidate.status = "merged"
    candidate.reviewed_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return True


def dismiss_candidate(db: Session, candidate: models.SyncMatchCandidate, note: str | None = None) -> None:
    """Mark a candidate as kept_separate."""
    candidate.status = "kept_separate"
    candidate.note = note
    candidate.reviewed_at = datetime.datetime.now(datetime.UTC)
    db.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Queue helpers
# ──────────────────────────────────────────────────────────────────────────────


def pending_count(db: Session, user: models.User) -> int:
    return (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.SyncMatchCandidate.status == "pending",
        )
        .count()
    )


def get_candidates(db: Session, user: models.User, include_skipped: bool = False):
    """Return candidates for the review page, newest first."""
    q = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(models.UserLibraryEntry.user_id == user.id)
    )
    if not include_skipped:
        q = q.filter(models.SyncMatchCandidate.status == "pending")
    else:
        q = q.filter(models.SyncMatchCandidate.status.in_(["pending", "kept_separate"]))
    return q.order_by(models.SyncMatchCandidate.match_score.desc()).all()
