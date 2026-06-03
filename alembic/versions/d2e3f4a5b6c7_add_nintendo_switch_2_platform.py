"""Add Nintendo Switch 2 platform row.

IGDB platform ID 162 — confirmed from IGDB platform list.
Omitted from the initial seed (c1d2e3f4a5b6) because the ID wasn't
known at seed-authoring time; added here as a follow-up.
"""

from alembic import op
import sqlalchemy as sa

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    # Insert only if not already present (idempotent re-run safety).
    existing = conn.execute(
        sa.text("SELECT id FROM platforms WHERE igdb_id = 162")
    ).fetchone()
    if not existing:
        conn.execute(
            sa.text(
                "INSERT INTO platforms (igdb_id, name, display_name, color, is_custom) "
                "VALUES (162, 'Nintendo Switch 2', 'Switch 2', 'red', 0)"
            )
        )


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM platforms WHERE igdb_id = 162"))
