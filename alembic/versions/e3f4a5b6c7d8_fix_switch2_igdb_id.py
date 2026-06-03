"""Fix Nintendo Switch 2 IGDB ID and link existing releases.

The initial migration used igdb_id=162, which is actually "Oculus VR".
The correct IGDB ID for Nintendo Switch 2 is 508.

Also links any existing game_releases whose platform string is
'Nintendo Switch 2' to the corrected platforms row.
"""

from alembic import op
import sqlalchemy as sa

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # Fix the igdb_id: 162 (Oculus VR) → 508 (Nintendo Switch 2).
    conn.execute(
        sa.text("UPDATE platforms SET igdb_id = 508 WHERE igdb_id = 162 AND name = 'Nintendo Switch 2'")
    )

    # Link any existing game_releases with the raw platform string to the row.
    row = conn.execute(
        sa.text("SELECT id FROM platforms WHERE name = 'Nintendo Switch 2'")
    ).fetchone()
    if row:
        platform_id = row[0]
        conn.execute(
            sa.text(
                "UPDATE game_releases SET platform_id = :pid "
                "WHERE platform IN ('Nintendo Switch 2', 'Switch 2') AND platform_id IS NULL"
            ),
            {"pid": platform_id},
        )


def downgrade():
    conn = op.get_bind()
    # Revert igdb_id back to the (incorrect) original value.
    conn.execute(
        sa.text("UPDATE platforms SET igdb_id = 162 WHERE igdb_id = 508 AND name = 'Nintendo Switch 2'")
    )
    # Unlink releases (restore NULL platform_id).
    row = conn.execute(
        sa.text("SELECT id FROM platforms WHERE name = 'Nintendo Switch 2'")
    ).fetchone()
    if row:
        conn.execute(
            sa.text("UPDATE game_releases SET platform_id = NULL WHERE platform_id = :pid"),
            {"pid": row[0]},
        )
