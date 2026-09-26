"""Achievements that belong to an account, and which games show them (#222).

Unlocks are recorded per user (UserAchievement) and per-set summaries per
user (UserAchievementSet). Neither says which library entry a set belongs
to: a set can have several owners (a PS5 copy and a Steam copy of one game,
cross-buy PS4/Vita releases, a later rebuy) or none yet (a PC copy whose
Steam game has not been synced, a game still in review). Ownership is
derived from what the releases already carry, by owners() below.
"""

import datetime
from collections import defaultdict

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from . import models

# Where a PC copy's trophy set lives: on the STEAM release it was earned on,
# under this key, shaped like a psn release's own raw_data. You earn PSN
# trophies -- platinum included -- for playing a game's Steam copy through
# PSN's PC integration, so the set belongs to that game even though no
# PlayStation entry exists. This link is what makes the Steam entry an owner.
PC_SET_KEY = "psn_trophy_set"


def trophy_item_in(raw: dict | None) -> dict | None:
    """The PSN trophy-set item inside a release's raw_data, whichever way it
    is carried: a PSN release IS its item, a Steam release holds a PC copy's
    set under PC_SET_KEY. None when there is no set."""
    raw = raw or {}
    if raw.get("npCommunicationId"):
        return raw
    attached = raw.get(PC_SET_KEY)
    return attached if isinstance(attached, dict) and attached.get("npCommunicationId") else None


def trophy_item_of(release: "models.GameRelease") -> dict | None:
    return trophy_item_in(release.raw_data)


def owners(db: Session, user_id: int, keys: set[tuple[str, str]]) -> dict[tuple[str, str], list[models.UserLibraryEntry]]:
    """{(source, set_id): [entries that show it]} for the given sets.

    Steam: the release whose appid is the set. PSN: the release whose trophy
    item is the set -- its own (a PSN release) or an attached PC copy's (a
    Steam release). A set with no owner is absent from the result.
    """
    out: dict[tuple[str, str], list[models.UserLibraryEntry]] = defaultdict(list)
    ule, gr = models.UserLibraryEntry, models.GameRelease
    base = (
        db.query(ule)
        .join(gr, ule.release_id == gr.id)
        .filter(ule.user_id == user_id)
        .options(contains_eager(ule.release).joinedload(gr.game))
    )
    steam_ids = {sid for src, sid in keys if src == "steam"}
    if steam_ids:
        for entry in base.filter(gr.source == "steam", gr.external_id.in_(steam_ids)).all():
            out[("steam", entry.release.external_id)].append(entry)
    psn_ids = {sid for src, sid in keys if src == "psn"}
    if psn_ids:
        rows = base.filter(
            or_(
                func.json_extract(gr.raw_data, "$.npCommunicationId").in_(psn_ids),
                func.json_extract(gr.raw_data, f"$.{PC_SET_KEY}.npCommunicationId").in_(psn_ids),
            )
        ).all()
        for entry in rows:
            item = trophy_item_of(entry.release)
            if item:
                out[("psn", item["npCommunicationId"])].append(entry)
    return out


def record_set(db: Session, user_id: int, source: str, set_id: str, **fields) -> models.UserAchievementSet:
    """Upsert the account's summary of one set. Does not commit."""
    row = db.query(models.UserAchievementSet).filter_by(user_id=user_id, source=source, set_id=set_id).first()
    if row is None:
        row = models.UserAchievementSet(user_id=user_id, source=source, set_id=set_id)
        db.add(row)
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = datetime.datetime.now(datetime.UTC)
    return row


def upsert_unlocks(db: Session, user_id: int, rows: dict[int, dict]) -> None:
    """Write {definition_id: fields} for one user. One row per achievement
    per account, however many copies of the game there are. Does not commit."""
    if not rows:
        return
    existing = {
        a.definition_id: a
        for a in db.query(models.UserAchievement)
        .filter(models.UserAchievement.user_id == user_id, models.UserAchievement.definition_id.in_(list(rows)))
        .all()
    }
    now = datetime.datetime.now(datetime.UTC)
    for definition_id, fields in rows.items():
        row = existing.get(definition_id)
        if row is None:
            db.add(models.UserAchievement(user_id=user_id, definition_id=definition_id, updated_at=now, **fields))
        else:
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = now


def unlocks_fetched_at(db: Session, user_id: int, source: str, set_id: str) -> datetime.datetime | None:
    """When this account's unlocks for a set were last written; None when
    never. The passes compare it against the source's last-earned stamp."""
    latest = (
        db.query(func.max(models.UserAchievement.updated_at))
        .join(models.AchievementDefinition, models.UserAchievement.definition_id == models.AchievementDefinition.id)
        .filter(
            models.UserAchievement.user_id == user_id,
            models.AchievementDefinition.source == source,
            models.AchievementDefinition.set_id == set_id,
        )
        .scalar()
    )
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=datetime.UTC)
    return latest


def recent_unlocks(db: Session, user_id: int, limit: int = 8) -> list[dict]:
    """The account's latest unlocks, newest first, each with the game it
    belongs to: an owning entry to link to when there is one, else the set's
    own title -- a set with no entry yet still earned the trophy."""
    ua = models.UserAchievement
    rows = (
        db.query(ua)
        .filter(ua.user_id == user_id, ua.earned == True, ua.earned_at.isnot(None))  # noqa: E712
        .options(joinedload(ua.definition))
        .order_by(ua.earned_at.desc(), ua.id.desc())
        .limit(limit)
        .all()
    )
    keys = {(a.definition.source, a.definition.set_id) for a in rows}
    owned = owners(db, user_id, keys)
    titles = {
        (s.source, s.set_id): s.title
        for s in db.query(models.UserAchievementSet).filter(
            models.UserAchievementSet.user_id == user_id,
            models.UserAchievementSet.set_id.in_({sid for _, sid in keys}),
        )
    }
    out = []
    for a in rows:
        key = (a.definition.source, a.definition.set_id)
        entry = (owned.get(key) or [None])[0]
        out.append(
            {
                "name": a.definition.name,
                "icon_url": a.definition.icon_url,
                "earned_at": a.earned_at,
                "entry_id": entry.id if entry else None,
                "game_title": entry.release.game.display_title if entry else titles.get(key) or "",
            }
        )
    return out
