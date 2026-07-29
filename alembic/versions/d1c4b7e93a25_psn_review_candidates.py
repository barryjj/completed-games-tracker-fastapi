"""psn review candidates table

Revision ID: d1c4b7e93a25
Revises: c5e2a91d7f30
Create Date: 2026-07-28

Moves PSN's review queues out of the crawl snapshot and into the database,
alongside import_candidates (#157).

The JSON snapshot was a proof of concept. It existed because the first PSN PR
promised zero library writes, so a crawl needed staging outside the DB. One-click
sync retired that promise, but the file stayed in the read path — where it could
drift from the library it described: it survived a DB restore still holding
decisions about entries that had been rolled away, and its already_imported count
was frozen at crawl time rather than read live.

No data migration. Review rows are derived from a crawl and the next sync
rebuilds them; nothing here is user-authored that a re-sync can't reproduce.
Any decisions recorded in an existing snapshot's entry_decisions are not carried
over — they were only ever "don't ask me again" markers, and the games they
covered are already in the library.
"""

import sqlalchemy as sa
from alembic import op

revision = "d1c4b7e93a25"
down_revision = "c5e2a91d7f30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "psn_review_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="cross_play"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("chosen_platforms", sa.JSON(), nullable=True),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # A re-sync updates the row for a game rather than duplicating it.
        sa.UniqueConstraint("user_id", "external_id", name="uq_psn_review_user_external"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_psn_review_candidates_id", "psn_review_candidates", ["id"])
    op.create_index("ix_psn_review_candidates_external_id", "psn_review_candidates", ["external_id"])
    op.create_index("ix_psn_review_candidates_kind", "psn_review_candidates", ["kind"])
    op.create_index("ix_psn_review_candidates_status", "psn_review_candidates", ["status"])
    # Crawl telemetry moves out of the dump file too, so the PSN page's report
    # reads from the database like everything else.
    op.add_column("users", sa.Column("psn_last_sync_report", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("psn_last_synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "psn_last_synced_at")
    op.drop_column("users", "psn_last_sync_report")
    op.drop_index("ix_psn_review_candidates_status", table_name="psn_review_candidates")
    op.drop_index("ix_psn_review_candidates_kind", table_name="psn_review_candidates")
    op.drop_index("ix_psn_review_candidates_external_id", table_name="psn_review_candidates")
    op.drop_index("ix_psn_review_candidates_id", table_name="psn_review_candidates")
    op.drop_table("psn_review_candidates")
