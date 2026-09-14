"""Achievement definitions keyed by set, not release

The previous revision keyed definitions by release. A trophy set is the
identity, not the SKU: two SKUs and two cross-buy releases share one list,
and a PSN review candidate -- no release yet -- still has a set worth
reading. Keyed by (source, set_id) instead; release_id goes.

Recreated rather than altered: the rows on file were fetched a few minutes
before this and are re-fetched in seconds. user_achievements is recreated
only because it references the table being dropped -- its shape is unchanged
(#136).

Revision ID: e3b7c9d02a41
Revises: d9e2f4a17b06
"""

import sqlalchemy as sa
from alembic import op

revision = "e3b7c9d02a41"
down_revision = "d9e2f4a17b06"
branch_labels = None
depends_on = None


def _drop_both() -> None:
    op.drop_index("ix_user_achievements_definition_id", table_name="user_achievements")
    op.drop_index("ix_user_achievements_library_entry_id", table_name="user_achievements")
    op.drop_index("ix_user_achievements_id", table_name="user_achievements")
    op.drop_table("user_achievements")
    op.drop_index("ix_achievement_definitions_set_id", table_name="achievement_definitions")
    op.drop_index("ix_achievement_definitions_id", table_name="achievement_definitions")
    op.drop_table("achievement_definitions")


def _create_user_achievements() -> None:
    op.create_table(
        "user_achievements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("library_entry_id", sa.Integer(), sa.ForeignKey("user_library.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "definition_id", sa.Integer(), sa.ForeignKey("achievement_definitions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("earned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("earned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress_value", sa.Integer(), nullable=True),
        sa.Column("progress_target", sa.Integer(), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("library_entry_id", "definition_id", name="uq_user_achievement_entry_definition"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_user_achievements_id", "user_achievements", ["id"])
    op.create_index("ix_user_achievements_library_entry_id", "user_achievements", ["library_entry_id"])
    op.create_index("ix_user_achievements_definition_id", "user_achievements", ["definition_id"])


def _definition_columns() -> list:
    return [
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tier", sa.String(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.String(), nullable=True),
        sa.Column("group_name", sa.String(), nullable=True),
        sa.Column("icon_url", sa.String(), nullable=True),
        sa.Column("icon_locked_url", sa.String(), nullable=True),
        sa.Column("rarity_pct", sa.Float(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    _drop_both()
    op.create_table(
        "achievement_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("set_id", sa.String(), nullable=False),
        *_definition_columns(),
        sa.UniqueConstraint("source", "set_id", "external_id", name="uq_achievement_set_external"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_achievement_definitions_id", "achievement_definitions", ["id"])
    op.create_index("ix_achievement_definitions_set_id", "achievement_definitions", ["set_id"])
    _create_user_achievements()


def downgrade() -> None:
    _drop_both()
    op.create_table(
        "achievement_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("game_releases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("set_id", sa.String(), nullable=True),
        *_definition_columns(),
        sa.UniqueConstraint("release_id", "external_id", name="uq_achievement_release_external"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_achievement_definitions_id", "achievement_definitions", ["id"])
    op.create_index("ix_achievement_definitions_release_id", "achievement_definitions", ["release_id"])
    op.create_index("ix_achievement_definitions_set_id", "achievement_definitions", ["set_id"])
    _create_user_achievements()
