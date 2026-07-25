"""clear inferred 'physical' medium

Revision ID: b3f0d27ca514
Revises: a8c4e1f7b309
Create Date: 2026-07-25

a8c4e1f7b309 backfilled `medium='physical'` for PSN entries absent from the
purchased feed. That inference was wrong: absence proves nothing.

- The modern getPurchasedGameList doesn't cover PS3/Vita-era purchases at all,
  yet most of those were digital (Luftrausers, Castle Crashers, Trine and other
  PSN-only titles that never had a physical release were all labelled "Disc").
- Even on PS4/PS5, preinstalled titles (ASTRO's PLAYROOM), classics
  re-releases, and PC copies surfacing through PSN's PC integration are missing
  from that feed.

a8c4e1f7b309 no longer writes those rows, but this revision is needed for
databases that already applied it. Clears only rows the user hasn't set
themselves — medium_user_set rows are their explicit choice and are preserved.
The cleared entries simply show no Format row until resolved in bulk.
"""

from alembic import op

revision = "b3f0d27ca514"
down_revision = "a8c4e1f7b309"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE user_library SET medium = NULL
        WHERE medium = 'physical' AND medium_user_set = 0
          AND release_id IN (SELECT id FROM game_releases WHERE source = 'psn')
        """
    )


def downgrade() -> None:
    # One-way: the discarded values were guesses, not user data.
    pass
