"""Add default brand-name platform aliases (Nintendo→NES, Playstation→PS1, Sega→Mega Drive)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "00af031b8b8d"
down_revision = "23829ba2e1c3"
branch_labels = None
depends_on = None

# alias → canonical platform name
_ALIASES = {
    "Nintendo": "Nintendo Entertainment System",
    "Playstation": "PlayStation",
    "Sega": "Sega Mega Drive/Genesis",
}


def upgrade():
    conn = op.get_bind()
    for alias, platform_name in _ALIASES.items():
        row = conn.execute(
            text("SELECT id FROM platforms WHERE name = :name"),
            {"name": platform_name},
        ).fetchone()
        if not row:
            continue
        platform_id = row[0]
        exists = conn.execute(
            text("SELECT 1 FROM platform_aliases WHERE platform_id = :pid AND alias = :alias"),
            {"pid": platform_id, "alias": alias},
        ).fetchone()
        if not exists:
            conn.execute(
                text("INSERT INTO platform_aliases (platform_id, alias) VALUES (:pid, :alias)"),
                {"pid": platform_id, "alias": alias},
            )


def downgrade():
    conn = op.get_bind()
    for alias in _ALIASES:
        conn.execute(
            text("DELETE FROM platform_aliases WHERE alias = :alias"),
            {"alias": alias},
        )
