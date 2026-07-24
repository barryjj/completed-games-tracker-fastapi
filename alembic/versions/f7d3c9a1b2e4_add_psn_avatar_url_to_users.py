"""add psn avatar url to users

Revision ID: f7d3c9a1b2e4
Revises: e2a7c94f8b15
Create Date: 2026-07-24

The user's PSN profile avatar (largest size from the profile2 avatarUrls list),
captured alongside accountId resolution. Shown on the PSN integration card,
mirroring steam_avatar_url. Plaintext column, same pattern as the Steam avatar.
"""

import sqlalchemy as sa
from alembic import op

revision = "f7d3c9a1b2e4"
down_revision = "e2a7c94f8b15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("psn_avatar_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "psn_avatar_url")
