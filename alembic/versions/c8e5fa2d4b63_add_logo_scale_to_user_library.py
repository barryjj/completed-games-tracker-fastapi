"""add logo_scale to user_library

Revision ID: c8e5fa2d4b63
Revises: b7d4e91c3a52
Create Date: 2026-07-08

Hero-logo size preset ('small' | 'large' | 'xlarge'); NULL = default.
"""

import sqlalchemy as sa
from alembic import op

revision = "c8e5fa2d4b63"
down_revision = "b7d4e91c3a52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_library", sa.Column("logo_scale", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_library", "logo_scale")
