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


TIERS = ("platinum", "gold", "silver", "bronze")
# Sony's progress weights each trophy by points and leaves the platinum out:
# ASTRO BOT at 53 of 63 is 87% in the PlayStation app, not 84%.
_PSN_POINTS = {"platinum": 0, "gold": 90, "silver": 30, "bronze": 15}
_STRIP = 10  # icons per strip in the pane box before "+N"


def _psn_progress(items: list[dict]) -> int:
    total = sum(_PSN_POINTS.get(i["tier"] or "", 0) for i in items)
    earned = sum(_PSN_POINTS.get(i["tier"] or "", 0) for i in items if i["earned"])
    return earned * 100 // total if total else 0


def _percent(source: str, items: list[dict]) -> int:
    if source == "psn":
        return _psn_progress(items)
    earned = sum(1 for i in items if i["earned"])
    return round(earned * 100 / len(items)) if items else 0


def _tiers(items: list[dict]) -> list[dict]:
    return [
        {
            "tier": t,
            "earned": sum(1 for i in items if i["tier"] == t and i["earned"]),
            "total": sum(1 for i in items if i["tier"] == t),
        }
        for t in TIERS
    ]


def set_view(db: Session, user_id: int, source: str, set_id: str) -> dict | None:
    """One set as the detail pane shows it (#210): the summary, PSN's tiers
    and per-group split, the rarest earned, the pane's two icon strips, and
    every item for the full list.

    Built from the definitions and this account's unlock rows. A set known
    only by its summary (Steam's batch progress, no rows yet) shows the
    counts and nothing else. None when the account has neither."""
    summary = db.query(models.UserAchievementSet).filter_by(user_id=user_id, source=source, set_id=set_id).first()
    defs = (
        db.query(models.AchievementDefinition)
        .filter_by(source=source, set_id=set_id)
        .order_by(models.AchievementDefinition.sort_order, models.AchievementDefinition.id)
        .all()
    )
    mine = {}
    if defs:
        mine = {
            a.definition_id: a
            for a in db.query(models.UserAchievement).filter(
                models.UserAchievement.user_id == user_id,
                models.UserAchievement.definition_id.in_([d.id for d in defs]),
            )
        }
    noun = "Trophies" if source == "psn" else "Achievements"
    if not mine:
        if summary is None or not summary.total:
            return None
        tiers = None
        if source == "psn" and summary.tiers:
            tiers = [{"tier": t, **(summary.tiers.get(t) or {"earned": 0, "total": 0})} for t in TIERS]
        return {
            "source": source,
            "set_id": set_id,
            "noun": noun,
            "earned": summary.earned,
            "total": summary.total,
            "percent": summary.progress if summary.progress is not None else round(summary.earned * 100 / summary.total),
            "complete": summary.earned >= summary.total,
            "tiers": tiers,
            "groups": [],
            "rarest": None,
            "unlocked": [],
            "unlocked_more": 0,
            "locked": [],
            "locked_more": 0,
            "items": [],
        }

    items = []
    for d in defs:
        a = mine.get(d.id)
        earned = bool(a and a.earned)
        # A hidden one you have not earned stays hidden, as on the platforms.
        secret = d.hidden and not earned
        items.append(
            {
                "name": ("Hidden trophy" if source == "psn" else "Hidden achievement") if secret else d.name,
                "description": None if secret else d.description,
                "icon_url": (d.icon_locked_url or d.icon_url) if not earned else d.icon_url,
                # PSN ships one icon per trophy; a locked one is greyed in CSS.
                "grey": not earned and not d.icon_locked_url,
                "earned": earned,
                "earned_at": a.earned_at if earned else None,
                "rarity_pct": d.rarity_pct,
                "tier": d.tier,
                "group_id": d.group_id or "default",
                "group_name": d.group_name,
            }
        )

    earned_items = sorted(
        (i for i in items if i["earned"]),
        key=lambda i: (i["earned_at"] is None, -(i["earned_at"].timestamp() if i["earned_at"] else 0)),
    )
    locked_items = [i for i in items if not i["earned"]]
    rated = [i for i in earned_items if i["rarity_pct"] is not None]
    rarest = min(rated, key=lambda i: i["rarity_pct"]) if rated else (earned_items[0] if earned_items else None)

    groups = []
    if source == "psn":
        by_group: dict[str, list[dict]] = defaultdict(list)
        for i in items:
            by_group[i["group_id"]].append(i)
        if len(by_group) > 1:
            # The base game is Sony's "default" group; add-ons follow in the
            # order Sony lists them.
            order = sorted(by_group, key=lambda g: (g != "default", list(by_group).index(g)))
            for gid in order:
                g_items = by_group[gid]
                groups.append(
                    {
                        "id": gid,
                        "name": "Full game" if gid == "default" else (g_items[0]["group_name"] or gid),
                        "base": gid == "default",
                        "earned": sum(1 for i in g_items if i["earned"]),
                        "total": len(g_items),
                        "percent": _psn_progress(g_items),
                        "tiers": _tiers(g_items),
                    }
                )

    earned_n = len(earned_items)
    return {
        "source": source,
        "set_id": set_id,
        "noun": noun,
        "earned": earned_n,
        "total": len(items),
        "percent": _percent(source, items),
        "complete": earned_n == len(items),
        "tiers": _tiers(items) if source == "psn" else None,
        "groups": groups,
        "rarest": rarest,
        "unlocked": earned_items[:_STRIP],
        "unlocked_more": max(0, earned_n - _STRIP),
        "locked": locked_items[:_STRIP],
        "locked_more": max(0, len(locked_items) - _STRIP),
        "items": items,
    }


def sets_of(release: "models.GameRelease") -> list[tuple[str, str]]:
    """The achievement sets a release shows: a Steam game's own, and a PSN
    set -- a PlayStation release's, or a PC copy's attached to a Steam one."""
    keys = []
    if release.source == "steam" and release.external_id:
        keys.append(("steam", release.external_id))
    item = trophy_item_of(release)
    if item:
        keys.append(("psn", item["npCommunicationId"]))
    return keys


def views_for(db: Session, user_id: int, release: "models.GameRelease") -> list[dict]:
    """set_view for every set the release shows that the account has."""
    return [v for v in (set_view(db, user_id, s, sid) for s, sid in sets_of(release)) if v]


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
