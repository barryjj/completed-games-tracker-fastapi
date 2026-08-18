"""psn review candidates: IGDB title proposal

Revision ID: a3f8c2d5e701
Revises: e7b2a4c19d63
Create Date: 2026-08-07

PSN names a trophy-only entry after its trophy SET, and those names are
frequently wrong — localized (閃乱カグラ SHINOVI VERSUS), abbreviated (GTA IV),
or missing their franchise prefix (Modern Warfare 2). 279 of 996 PSN releases
are trophy-only, concentrated on PS3 (177) and Vita (89), and the purchased feed
can never supply store records for those generations (#181), so the bad name is
permanent unless something external fixes it.

A bad title costs three things at once: SGDB can't find artwork, spreadsheet
completions can't match, and the entry carries no igdb_id.

These columns hold an IGDB *proposal* on the candidate rather than applying it
to the game. That is what makes rejection lossless — reject and the row falls
back to the raw trophy name with its full original platform options, with
nothing to undo.

igdb_platforms is the Sony-only intersection of IGDB's platform list, which is
also the corrected platform set: Shinovi Versus' trophy set claims PS3 + Vita,
IGDB says Vita, and the phantom PS3 disappearing IS the fix.

Nullable throughout — a row with no proposal is the normal state, not an error.

Refs #180.
"""

import sqlalchemy as sa
from alembic import op

revision = "a3f8c2d5e701"
down_revision = "e7b2a4c19d63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("psn_review_candidates", sa.Column("proposed_title", sa.String(), nullable=True))
    op.add_column("psn_review_candidates", sa.Column("proposed_igdb_id", sa.Integer(), nullable=True))
    op.add_column("psn_review_candidates", sa.Column("proposed_platforms", sa.JSON(), nullable=True))
    op.add_column("psn_review_candidates", sa.Column("proposal_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("psn_review_candidates", "proposal_status")
    op.drop_column("psn_review_candidates", "proposed_platforms")
    op.drop_column("psn_review_candidates", "proposed_igdb_id")
    op.drop_column("psn_review_candidates", "proposed_title")
