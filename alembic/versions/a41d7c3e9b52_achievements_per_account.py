"""Achievements per account: unlocks keyed by user, plus per-set summaries

user_achievements was keyed by library entry, so a set with no entry
recorded nothing (most of a PSN library sits in review; a PC copy whose
Steam game is not synced has no entry at all), and a set shared by two
entries -- PS5 and Steam copies, cross-buy PS4/Vita, a rebuy -- needed a
row per entry for one set of trophies. Trophies belong to the account.

- user_achievements is re-keyed (user_id, definition_id). Existing rows
  take user_id from their entry; a shared set's duplicate rows collapse to
  one, earned if either copy was.
- user_achievement_sets is new: the account's summary of every set, which
  totals read. Backfilled from what the database already knows -- each
  PSN trophy item on a release, a PC-copy link or a review row (any
  status), and each Steam game's batch progress or its rows. The next PSN
  sync fills in sets the database never held (#222).

Downgrade maps each row back onto the entries that own its set and drops
rows with no owner, which the old shape cannot hold.

Revision ID: a41d7c3e9b52
Revises: e3b7c9d02a41
"""

import datetime
import json

import sqlalchemy as sa
from alembic import op

revision = "a41d7c3e9b52"
down_revision = "e3b7c9d02a41"
branch_labels = None
depends_on = None

_TIERS = ("platinum", "gold", "silver", "bronze")
_PC_SET_KEY = "psn_trophy_set"
_PASS_KEYS = (
    "npCommunicationId",
    "npServiceName",
    "trophySetVersion",
    "trophyLastUpdated",
    "platform",
    "name",
    "trophies",
    "earnedTrophies",
    "trophyProgress",
    "trophyIconUrl",
)


def _unlock_columns() -> list:
    return [
        sa.Column("earned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("earned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress_value", sa.Integer(), nullable=True),
        sa.Column("progress_target", sa.Integer(), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _definition_fk() -> sa.Column:
    return sa.Column("definition_id", sa.Integer(), sa.ForeignKey("achievement_definitions.id", ondelete="CASCADE"), nullable=False)


def _drop_unlock_indexes(key: str) -> None:
    op.drop_index("ix_user_achievements_id", table_name="user_achievements")
    op.drop_index(f"ix_user_achievements_{key}", table_name="user_achievements")
    op.drop_index("ix_user_achievements_definition_id", table_name="user_achievements")


def _create_unlock_indexes(key: str) -> None:
    op.create_index("ix_user_achievements_id", "user_achievements", ["id"])
    op.create_index(f"ix_user_achievements_{key}", "user_achievements", [key])
    op.create_index("ix_user_achievements_definition_id", "user_achievements", ["definition_id"])


def _loads(value):
    if value is None or isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _stamp(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _psn_item(raw: dict | None) -> dict | None:
    raw = raw or {}
    if raw.get("npCommunicationId"):
        return raw
    attached = raw.get(_PC_SET_KEY)
    return attached if isinstance(attached, dict) and attached.get("npCommunicationId") else None


def _backfill_sets(bind) -> None:
    sets = sa.table(
        "user_achievement_sets",
        sa.column("user_id", sa.Integer),
        sa.column("source", sa.String),
        sa.column("set_id", sa.String),
        sa.column("title", sa.String),
        sa.column("platform", sa.String),
        sa.column("earned", sa.Integer),
        sa.column("total", sa.Integer),
        sa.column("tiers", sa.JSON),
        sa.column("progress", sa.Integer),
        sa.column("last_earned_at", sa.DateTime(timezone=True)),
        sa.column("raw_data", sa.JSON),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.datetime.now(datetime.UTC)
    out: dict[tuple, dict] = {}

    # PSN: every trophy item the database holds -- library releases, PC-copy
    # links on Steam releases, and review rows of any status. The account
    # earned those trophies whatever was decided about the row.
    psn_items = bind.execute(
        sa.text(
            "SELECT ul.user_id, gr.raw_data FROM user_library ul JOIN game_releases gr ON gr.id = ul.release_id "
            "WHERE gr.source = 'psn' OR json_extract(gr.raw_data, '$.psn_trophy_set') IS NOT NULL"
        )
    ).fetchall()
    psn_items += bind.execute(sa.text("SELECT user_id, raw_data FROM psn_review_candidates")).fetchall()
    for user_id, raw in psn_items:
        item = _psn_item(_loads(raw))
        if not item or not isinstance(item.get("earnedTrophies"), dict):
            continue
        key = (user_id, "psn", item["npCommunicationId"])
        if key in out:
            continue
        defined, got = item.get("trophies") or {}, item.get("earnedTrophies") or {}
        tiers = {t: {"earned": _int(got.get(t)), "total": _int(defined.get(t))} for t in _TIERS}
        out[key] = {
            "title": item.get("name") or item.get("displayName"),
            "platform": item.get("platform"),
            "earned": sum(v["earned"] for v in tiers.values()),
            "total": sum(v["total"] for v in tiers.values()),
            "tiers": tiers,
            "progress": _int(item.get("trophyProgress")) if item.get("trophyProgress") is not None else None,
            "last_earned_at": _stamp(item.get("trophyLastUpdated")),
            "raw_data": {k: item.get(k) for k in _PASS_KEYS},
        }

    # Steam: the batch progress where there is one (it covers games marked
    # private, which have counts and no rows), else the rows themselves.
    counted = {
        (u, s): (_int(e), _int(t))
        for u, s, e, t in bind.execute(
            sa.text(
                "SELECT ua.user_id, ad.set_id, SUM(ua.earned), COUNT(*) FROM user_achievements ua "
                "JOIN achievement_definitions ad ON ad.id = ua.definition_id WHERE ad.source = 'steam' "
                "GROUP BY ua.user_id, ad.set_id"
            )
        )
    }
    steam = bind.execute(
        sa.text(
            "SELECT ul.user_id, gr.external_id, COALESCE(g.display_name, g.title), gr.raw_data "
            "FROM user_library ul JOIN game_releases gr ON gr.id = ul.release_id JOIN games g ON g.id = gr.game_id "
            "WHERE gr.source = 'steam' AND (json_extract(gr.raw_data, '$.achievement_progress') IS NOT NULL "
            "OR json_extract(gr.raw_data, '$.achievement_schema.total') > 0)"
        )
    ).fetchall()
    for user_id, appid, title, raw in steam:
        key = (user_id, "steam", appid)
        if key in out:
            continue
        progress = (_loads(raw) or {}).get("achievement_progress")
        if isinstance(progress, dict) and _int(progress.get("total")):
            earned, total = _int(progress.get("unlocked")), _int(progress.get("total"))
        elif (user_id, appid) in counted:
            earned, total = counted[(user_id, appid)]
        else:
            continue
        out[key] = {
            "title": title,
            "platform": "Steam",
            "earned": earned,
            "total": total,
            "tiers": None,
            "progress": round(earned / total * 100) if total else None,
            "last_earned_at": None,
            "raw_data": None,
        }

    rows = [{"user_id": u, "source": src, "set_id": sid, "updated_at": now, **fields} for (u, src, sid), fields in out.items()]
    for start in range(0, len(rows), 500):
        bind.execute(sets.insert(), rows[start : start + 500])


def upgrade() -> None:
    op.create_table(
        "user_achievement_sets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("set_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("platform", sa.String(), nullable=True),
        sa.Column("earned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tiers", sa.JSON(), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("last_earned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "source", "set_id", name="uq_user_achievement_set"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_user_achievement_sets_id", "user_achievement_sets", ["id"])
    op.create_index("ix_user_achievement_sets_user_id", "user_achievement_sets", ["user_id"])

    # Rename the old table out of the way (indexes first -- their names are
    # reused), create the new one under the real name, copy, drop.
    _drop_unlock_indexes("library_entry_id")
    op.rename_table("user_achievements", "user_achievements_by_entry")
    op.create_table(
        "user_achievements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        _definition_fk(),
        *_unlock_columns(),
        sa.UniqueConstraint("user_id", "definition_id", name="uq_user_achievement_user_definition"),
        sqlite_autoincrement=True,
    )
    _create_unlock_indexes("user_id")
    # One row per (user, achievement). A shared set had a row per entry;
    # earned if any copy was, the latest fetch as the freshness stamp.
    op.execute(
        "INSERT INTO user_achievements "
        "(user_id, definition_id, earned, earned_at, progress_value, progress_target, raw_data, updated_at) "
        "SELECT ul.user_id, old.definition_id, MAX(old.earned), MAX(old.earned_at), MAX(old.progress_value), "
        "MAX(old.progress_target), MAX(old.raw_data), MAX(old.updated_at) "
        "FROM user_achievements_by_entry old JOIN user_library ul ON ul.id = old.library_entry_id "
        "GROUP BY ul.user_id, old.definition_id"
    )
    op.drop_table("user_achievements_by_entry")
    _backfill_sets(op.get_bind())


def downgrade() -> None:
    _drop_unlock_indexes("user_id")
    op.rename_table("user_achievements", "user_achievements_by_user")
    op.create_table(
        "user_achievements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("library_entry_id", sa.Integer(), sa.ForeignKey("user_library.id", ondelete="CASCADE"), nullable=False),
        _definition_fk(),
        *_unlock_columns(),
        sa.UniqueConstraint("library_entry_id", "definition_id", name="uq_user_achievement_entry_definition"),
        sqlite_autoincrement=True,
    )
    _create_unlock_indexes("library_entry_id")
    # Back onto every entry that owns the set: a Steam release whose appid is
    # the set, or a release whose PSN trophy item (own or PC-copy link) is.
    op.execute(
        "INSERT OR IGNORE INTO user_achievements "
        "(library_entry_id, definition_id, earned, earned_at, progress_value, progress_target, raw_data, updated_at) "
        "SELECT ul.id, u.definition_id, u.earned, u.earned_at, u.progress_value, u.progress_target, u.raw_data, u.updated_at "
        "FROM user_achievements_by_user u "
        "JOIN achievement_definitions ad ON ad.id = u.definition_id "
        "JOIN user_library ul ON ul.user_id = u.user_id "
        "JOIN game_releases gr ON gr.id = ul.release_id "
        "WHERE (ad.source = 'steam' AND gr.source = 'steam' AND gr.external_id = ad.set_id) "
        "OR (ad.source = 'psn' AND (json_extract(gr.raw_data, '$.npCommunicationId') = ad.set_id "
        "OR json_extract(gr.raw_data, '$.psn_trophy_set.npCommunicationId') = ad.set_id))"
    )
    op.drop_table("user_achievements_by_user")
    op.drop_index("ix_user_achievement_sets_user_id", table_name="user_achievement_sets")
    op.drop_index("ix_user_achievement_sets_id", table_name="user_achievement_sets")
    op.drop_table("user_achievement_sets")
