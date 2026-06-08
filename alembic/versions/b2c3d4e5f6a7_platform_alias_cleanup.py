"""Platform alias cleanup.

Changes:
  - Linux becomes an alias of PC (Microsoft Windows) (platform_id=1) rather
    than a standalone platform. No existing releases use platform_id=3.
  - Add missing aliases:
      PC (Windows) ← "Microsoft Windows"
      Mac          ← "OS X", "Mac OS"
      DOS          ← "MS-DOS"
  - Move Amiga and Amiga CD32 out of the PC family (family_id=6 → NULL).
    They are retro home computers, not PC-family platforms.
"""

from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "21a93681c8c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Move Linux alias (platform_id=3) to point at PC/Windows (platform_id=1)
    conn.execute(sa.text(
        "UPDATE platform_aliases SET platform_id = 1 WHERE platform_id = 3 AND alias = 'Linux'"
    ))

    # 2. Delete the now-empty Linux platform row
    conn.execute(sa.text("DELETE FROM platforms WHERE id = 3"))

    # 3. Add missing aliases for PC (Microsoft Windows)
    conn.execute(sa.text(
        "INSERT OR IGNORE INTO platform_aliases (platform_id, alias) VALUES (1, 'Microsoft Windows')"
    ))

    # 4. Add missing aliases for Mac (platform_id=2)
    conn.execute(sa.text(
        "INSERT OR IGNORE INTO platform_aliases (platform_id, alias) VALUES (2, 'OS X')"
    ))
    conn.execute(sa.text(
        "INSERT OR IGNORE INTO platform_aliases (platform_id, alias) VALUES (2, 'Mac OS')"
    ))

    # 5. Add MS-DOS alias for DOS platform (platform_id=58)
    conn.execute(sa.text(
        "INSERT OR IGNORE INTO platform_aliases (platform_id, alias) VALUES (58, 'MS-DOS')"
    ))

    # 6. Move Amiga (platform_id=59) and Amiga CD32 (platform_id=60) out of PC family
    conn.execute(sa.text(
        "UPDATE platforms SET family_id = NULL WHERE id IN (59, 60)"
    ))


def downgrade() -> None:
    conn = op.get_bind()

    # Re-create Linux platform and restore alias
    conn.execute(sa.text(
        "INSERT OR IGNORE INTO platforms (id, name, family_id) VALUES (3, 'Linux', 6)"
    ))
    conn.execute(sa.text(
        "UPDATE platform_aliases SET platform_id = 3 WHERE platform_id = 1 AND alias = 'Linux'"
    ))

    # Remove added aliases
    conn.execute(sa.text(
        "DELETE FROM platform_aliases WHERE platform_id = 1 AND alias = 'Microsoft Windows'"
    ))
    conn.execute(sa.text(
        "DELETE FROM platform_aliases WHERE platform_id = 2 AND alias IN ('OS X', 'Mac OS')"
    ))
    conn.execute(sa.text(
        "DELETE FROM platform_aliases WHERE platform_id = 58 AND alias = 'MS-DOS'"
    ))

    # Restore Amiga family membership
    conn.execute(sa.text(
        "UPDATE platforms SET family_id = 6 WHERE id IN (59, 60)"
    ))
