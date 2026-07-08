"""add logo_position to user_library

Revision ID: b7d4e91c3a52
Revises: cfccdfb8c419
Create Date: 2026-07-08

Per-entry hero-logo placement in the detail pane: preset anchor name or
'hidden'; NULL = default bottom-left.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d4e91c3a52"
down_revision = "cfccdfb8c419"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_library", sa.Column("logo_position", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_library", "logo_position")
