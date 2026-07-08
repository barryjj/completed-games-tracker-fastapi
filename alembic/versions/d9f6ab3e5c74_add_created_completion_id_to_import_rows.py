"""add created_completion_id to import_rows

Revision ID: d9f6ab3e5c74
Revises: c8e5fa2d4b63
Create Date: 2026-07-08

Links each import row to the Completion its confirm created, so the
Reopen action can delete exactly those. Nullable; rows confirmed before
this column existed stay NULL (Reopen matches by entry+date+sort_order).
"""

import sqlalchemy as sa
from alembic import op

revision = "d9f6ab3e5c74"
down_revision = "c8e5fa2d4b63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_rows", sa.Column("created_completion_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("import_rows", "created_completion_id")
