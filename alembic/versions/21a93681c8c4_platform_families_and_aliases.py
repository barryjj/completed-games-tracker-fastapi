"""Add platform_families and platform_aliases tables.

platform_families — groups platforms by manufacturer/ecosystem.
  Seeded from IGDB's platform_family data. color on the family is
  the default accent for all member platforms (individual platform
  color still wins when set).

platform_aliases — user-managed abbreviations / alternate names for
  a platform row. resolve_platform_id checks these after name and
  display_name so "PS4", "PSX", "SNES" etc. all find the right row.

Also adds Platform.family_id FK.

Revision ID: 21a93681c8c4
Revises: c697b4c8225b
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "21a93681c8c4"
down_revision = "c697b4c8225b"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Families seed data — (id, name, igdb_id, color)
# IGDB platform_family IDs sourced from their API.
# ---------------------------------------------------------------------------
_FAMILIES = [
    (1,  "PlayStation",  1,  "lavender"),
    (2,  "Xbox",         2,  "green"),
    (3,  "Nintendo",     3,  "red"),
    (4,  "Sega",         4,  "yellow"),
    (5,  "Atari",        5,  "peach"),
    (6,  "PC",           None, "sapphire"),
    (7,  "Neo Geo",      None, "maroon"),
    (8,  "TurboGrafx",   None, "mauve"),
    (9,  "Meta/Oculus",  None, "blue"),
    (10, "Arcade",       None, "flamingo"),
]

# ---------------------------------------------------------------------------
# Platform → family assignments  (platform_id, family_id)
# ---------------------------------------------------------------------------
_PLATFORM_FAMILY = [
    # PlayStation
    (7,  1), (8,  1), (9,  1), (10, 1), (11, 1),
    (12, 1), (13, 1), (44, 1), (45, 1),
    # Xbox
    (14, 2), (15, 2), (16, 2), (17, 2),
    # Nintendo
    (18, 3), (19, 3), (20, 3), (21, 3), (22, 3), (23, 3),
    (24, 3), (25, 3), (26, 3), (27, 3), (28, 3), (29, 3),
    (30, 3), (38, 3), (39, 3), (40, 3), (41, 3), (42, 3),
    (43, 3),
    # Sega
    (31, 4), (32, 4), (33, 4), (34, 4), (35, 4),
    (46, 4), (47, 4), (48, 4), (49, 4),
    # Atari
    (36, 5), (37, 5), (50, 5), (51, 5), (52, 5),
    (53, 5), (54, 5), (55, 5),
    # PC
    (1, 6), (2, 6), (3, 6), (6, 6), (58, 6), (59, 6), (60, 6),
    # Neo Geo
    (68, 7), (69, 7), (70, 7), (71, 7), (72, 7),
    # TurboGrafx
    (65, 8), (66, 8), (67, 8),
    # Meta/Oculus
    (62, 9), (63, 9), (64, 9),
    # Arcade
    (73, 10),
]

# ---------------------------------------------------------------------------
# Aliases seed data — (platform_id, alias)
# Common abbreviations and alternate names. Users can add more via UI.
# ---------------------------------------------------------------------------
_ALIASES = [
    # PlayStation
    (11, "PS1"), (11, "PSX"), (11, "PlayStation 1"),
    (10, "PS2"), (10, "PlayStation 2"),
    (9,  "PS3"), (9,  "PlayStation 3"),
    (8,  "PS4"), (8,  "PlayStation 4"),
    (7,  "PS5"), (7,  "PlayStation 5"),
    (12, "PSP"),
    (13, "PS Vita"), (13, "Vita"), (13, "PSV"),
    (44, "PSVR"),
    (45, "PSVR2"),
    # Xbox
    (17, "Xbox OG"), (17, "Original Xbox"),
    (16, "X360"), (16, "Xbox 360"),
    (15, "XBO"), (15, "Xbox One"),
    (14, "XSX"), (14, "Series X"), (14, "Xbox Series X"),
    # Nintendo
    (30, "NES"), (30, "Famicom"),
    (29, "SNES"), (29, "Super NES"), (29, "Super Nintendo"),
    (40, "Famicom"), (40, "FC"),
    (27, "N64"), (27, "Nintendo 64"),
    (28, "GCN"), (28, "GameCube"), (28, "GC"),
    (20, "Wii"),
    (19, "Wii U"),
    (18, "Switch"),
    (38, "Switch 2"),
    (26, "GB"), (26, "Game Boy"),
    (25, "GBC"),
    (24, "GBA"),
    (23, "DS"), (23, "Nintendo DS"),
    (21, "3DS"),
    # Sega
    (33, "Genesis"), (33, "Mega Drive"), (33, "MD"),
    (32, "Saturn"),
    (31, "DC"), (31, "Dreamcast"),
    (35, "Master System"), (35, "SMS"),
    (34, "Game Gear"), (34, "GG"),
    # PC
    (1,  "Windows"), (1, "PC"), (1, "Win"),
    (6,  "Steam"),
    (3,  "Linux"),
    (2,  "macOS"), (2, "Mac"),
    # Atari
    (36, "Atari"), (36, "2600"), (36, "VCS"),
    # Meta/Oculus
    (62, "Oculus Quest"), (62, "Quest"),
    (63, "Quest 2"),
    (64, "Quest 3"),
    # TurboGrafx
    (65, "TG16"), (65, "PC Engine"), (65, "PCE"),
    # Mobile
    (4,  "iPhone"), (4, "iPad"), (4, "Apple"),
    (5,  "Google Play"),
]


def upgrade() -> None:
    # ── platform_families ──────────────────────────────────────────────────
    op.create_table(
        "platform_families",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("igdb_id", sa.Integer, nullable=True, unique=True),
        sa.Column("color", sa.String, nullable=True),
    )

    op.bulk_insert(
        sa.table(
            "platform_families",
            sa.column("id", sa.Integer),
            sa.column("name", sa.String),
            sa.column("igdb_id", sa.Integer),
            sa.column("color", sa.String),
        ),
        [{"id": id_, "name": name, "igdb_id": igdb_id, "color": color}
         for id_, name, igdb_id, color in _FAMILIES],
    )

    # ── platform_aliases ───────────────────────────────────────────────────
    op.create_table(
        "platform_aliases",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform_id", sa.Integer,
                  sa.ForeignKey("platforms.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("alias", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )

    op.bulk_insert(
        sa.table(
            "platform_aliases",
            sa.column("platform_id", sa.Integer),
            sa.column("alias", sa.String),
        ),
        [{"platform_id": pid, "alias": alias} for pid, alias in _ALIASES],
    )

    # ── platforms.family_id FK ─────────────────────────────────────────────
    with op.batch_alter_table("platforms") as batch_op:
        batch_op.add_column(sa.Column("family_id", sa.Integer, nullable=True))
        batch_op.create_foreign_key(
            "fk_platforms_family_id",
            "platform_families",
            ["family_id"], ["id"],
        )

    # Assign family_id for known platforms
    conn = op.get_bind()
    for platform_id, family_id in _PLATFORM_FAMILY:
        conn.execute(
            sa.text("UPDATE platforms SET family_id = :fid WHERE id = :pid"),
            {"fid": family_id, "pid": platform_id},
        )


def downgrade() -> None:
    with op.batch_alter_table("platforms") as batch_op:
        batch_op.drop_constraint("fk_platforms_family_id", type_="foreignkey")
        batch_op.drop_column("family_id")
    op.drop_table("platform_aliases")
    op.drop_table("platform_families")
