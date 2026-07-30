"""psn review candidates: hero + logo art

Revision ID: e7b2a4c19d63
Revises: d1c4b7e93a25
Create Date: 2026-07-30

The review cards showed a horizontal grid (460x215) in a hero box shaped 96/22
(~4.36:1), so cover-cropping sliced roughly half the image away vertically.

Every other card in the app — import review, the library detail pane — shows a
real SGDB hero (~1920x620) with the logo overlaid. These rows have no library
entry to borrow that from, so they cache their own, the same way thumbnail_url
already works. The grid stays: it's the right shape for the list view's thumb.
"""

import sqlalchemy as sa
from alembic import op

revision = "e7b2a4c19d63"
down_revision = "d1c4b7e93a25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("psn_review_candidates", sa.Column("hero_url", sa.String(), nullable=True))
    op.add_column("psn_review_candidates", sa.Column("logo_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("psn_review_candidates", "logo_url")
    op.drop_column("psn_review_candidates", "hero_url")
