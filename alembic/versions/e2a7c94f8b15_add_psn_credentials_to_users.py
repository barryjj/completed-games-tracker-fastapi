"""add psn credentials to users

Revision ID: e2a7c94f8b15
Revises: d9f6ab3e5c74
Create Date: 2026-07-16

PSN capture framework (Tauri step 3): the NPSSO token (captured via the
desktop WebView or pasted manually), when it was captured, and the user's
Online ID (needed later to resolve accountId for the library crawl).
Plaintext columns, matching the existing Steam credential pattern.
"""

import sqlalchemy as sa
from alembic import op

revision = "e2a7c94f8b15"
down_revision = "d9f6ab3e5c74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("psn_npsso", sa.String(), nullable=True))
    op.add_column("users", sa.Column("psn_npsso_captured_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("psn_online_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "psn_online_id")
    op.drop_column("users", "psn_npsso_captured_at")
    op.drop_column("users", "psn_npsso")
