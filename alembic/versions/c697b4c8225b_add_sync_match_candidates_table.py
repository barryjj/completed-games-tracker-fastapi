"""Add sync_match_candidates table.

Stores potential duplicate matches between manual library entries and
synced platform entries, pending user review.

status values:
  pending       – awaiting review
  merged        – user approved the merge
  kept_separate – user chose to keep entries distinct (hidden by default, reviewable via toggle)
"""

from alembic import op
import sqlalchemy as sa

revision = "c697b4c8225b"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_match_candidates",
        sa.Column("id", sa.Integer, primary_key=True),
        # The manual UserLibraryEntry that may be a duplicate
        sa.Column("manual_entry_id", sa.Integer, sa.ForeignKey("user_library.id", ondelete="CASCADE"), nullable=False, index=True),
        # The platform source and external ID of the synced game
        sa.Column("platform_source", sa.String, nullable=False),   # "steam" | "psn"
        sa.Column("external_id", sa.String, nullable=False),        # appid or psn title id
        sa.Column("synced_title", sa.String, nullable=False),       # title from the sync source
        # 0.0–1.0 confidence score
        sa.Column("match_score", sa.Float, nullable=False),
        # pending | merged | kept_separate
        sa.Column("status", sa.String, nullable=False, default="pending", index=True),
        # optional user note when keeping separate
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        # prevent duplicate candidates for the same manual entry + external game
        sa.UniqueConstraint("manual_entry_id", "platform_source", "external_id", name="uq_match_candidate"),
    )


def downgrade() -> None:
    op.drop_table("sync_match_candidates")
