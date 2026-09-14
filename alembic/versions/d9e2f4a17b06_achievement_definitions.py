"""Achievement definitions per release; user_achievements reshaped to reference them

user_achievements existed from the first migration with the definition folded
into every row -- name, description, icon, and a JSON blob for tier and rarity
-- and was never written to. That shape duplicates the definition per user and
per entry, and the schema is multi-user by design.

achievement_definitions holds what the GAME defines, once per release, shared
by everyone who owns it. user_achievements keeps only what a user has done:
earned, when, and progress where the source reports it. It is keyed by the
library entry, which already is (user, release) and already cascades.

Recreated rather than altered: the table was empty, so there was nothing to
carry, and SQLite cannot drop columns in place anyway (#136).

Revision ID: d9e2f4a17b06
Revises: c8a1d4b60f77
"""

import sqlalchemy as sa
from alembic import op

revision = "d9e2f4a17b06"
down_revision = "c8a1d4b60f77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "achievement_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("game_releases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("set_id", sa.String(), nullable=True),
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
        sa.UniqueConstraint("release_id", "external_id", name="uq_achievement_release_external"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_achievement_definitions_id", "achievement_definitions", ["id"])
    op.create_index("ix_achievement_definitions_release_id", "achievement_definitions", ["release_id"])
    op.create_index("ix_achievement_definitions_set_id", "achievement_definitions", ["set_id"])

    op.drop_index("ix_user_achievements_id", table_name="user_achievements")
    op.drop_table("user_achievements")
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


def downgrade() -> None:
    op.drop_index("ix_user_achievements_definition_id", table_name="user_achievements")
    op.drop_index("ix_user_achievements_library_entry_id", table_name="user_achievements")
    op.drop_index("ix_user_achievements_id", table_name="user_achievements")
    op.drop_table("user_achievements")
    # The original single-table shape, as the first migration created it.
    op.create_table(
        "user_achievements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("library_entry_id", sa.Integer(), sa.ForeignKey("user_library.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon_url", sa.String(), nullable=True),
        sa.Column("unlocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.UniqueConstraint("library_entry_id", "external_id", name="uq_achievement_entry_external"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_user_achievements_id", "user_achievements", ["id"])

    op.drop_index("ix_achievement_definitions_set_id", table_name="achievement_definitions")
    op.drop_index("ix_achievement_definitions_release_id", table_name="achievement_definitions")
    op.drop_index("ix_achievement_definitions_id", table_name="achievement_definitions")
    op.drop_table("achievement_definitions")
