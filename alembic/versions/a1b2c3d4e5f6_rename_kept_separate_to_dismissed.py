"""rename kept_separate to dismissed in sync_match_candidates.status

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-06-21

"""

from alembic import op

revision = "g5h6i7j8k9l0"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE sync_match_candidates SET status = 'dismissed' WHERE status = 'kept_separate'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE sync_match_candidates SET status = 'kept_separate' WHERE status = 'dismissed'"
    )
