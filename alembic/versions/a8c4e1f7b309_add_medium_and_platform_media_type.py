"""add entry medium + platform media_type

Revision ID: a8c4e1f7b309
Revises: f7d3c9a1b2e4
Create Date: 2026-07-24

Two halves of the detail pane's Format line (#164):

- users' copies get `medium` ('digital' | 'physical' | NULL) plus a
  `medium_user_set` guard, mirroring the other *_user_set flags.
- platforms get `media_type`, the word a physical copy reads as ('disc',
  'cartridge', 'umd', 'floppy'). Seeded here for the known platforms; NULL
  means digital-only or unknown, and the UI shows a plain "Physical".

media_type lives on the platforms table (not a code map) to match how the other
per-platform attributes work — color, display_name, aliases — so it stays
editable in the platforms admin.
"""

import sqlalchemy as sa
from alembic import op

revision = "a8c4e1f7b309"
down_revision = "f7d3c9a1b2e4"
branch_labels = None
depends_on = None


# Grouped by media so the intent is readable; matched on platforms.name.
_DISC = [
    "PlayStation",
    "PlayStation 2",
    "PlayStation 3",
    "PlayStation 4",
    "PlayStation 5",
    "PlayStation VR",
    "PlayStation VR2",
    "Nintendo GameCube",
    "Wii",
    "Wii U",
    "Xbox",
    "Xbox 360",
    "Xbox One",
    "Xbox Series X|S",
    "Dreamcast",
    "Sega CD",
    "Sega CD 32X",
    "Sega Saturn",
    "Neo Geo CD",
    "Amiga CD32",
    "Atari Jaguar CD",
    "3DO Interactive Multiplayer",
    "Turbografx-16/PC Engine CD",
    "PC (Microsoft Windows)",
    "Mac",
]

_CARTRIDGE = [
    "Nintendo Entertainment System",
    "Family Computer",
    "Super Nintendo Entertainment System",
    "Nintendo 64",
    "Game Boy",
    "Game Boy Color",
    "Game Boy Advance",
    "Nintendo DS",
    "Nintendo DSi",
    "Nintendo 3DS",
    "New Nintendo 3DS",
    "Nintendo Switch",
    "Nintendo Switch 2",
    "Virtual Boy",
    "Sega Mega Drive/Genesis",
    "Sega 32X",
    "Sega Master System/Mark III",
    "Sega Game Gear",
    "SG-1000",
    "Neo Geo AES",
    "Neo Geo MVS",
    "Neo Geo Pocket",
    "Neo Geo Pocket Color",
    "Atari 2600",
    "Atari 5200",
    "Atari 7800",
    "Atari Lynx",
    "Atari Jaguar",
    "TurboGrafx-16/PC Engine",
    "PC Engine SuperGrafx",
    "WonderSwan",
    "WonderSwan Color",
    "PlayStation Vita",
    "Evercade",
]

_UMD = ["PlayStation Portable"]

_FLOPPY = ["DOS", "Amiga", "Atari ST/STE", "Atari 8-bit", "64DD"]


def upgrade() -> None:
    op.add_column("user_library", sa.Column("medium", sa.String(), nullable=True))
    op.add_column("user_library", sa.Column("medium_user_set", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("platforms", sa.Column("media_type", sa.String(), nullable=True))

    platforms = sa.table("platforms", sa.column("name", sa.String), sa.column("media_type", sa.String))
    for media, names in (("disc", _DISC), ("cartridge", _CARTRIDGE), ("umd", _UMD), ("floppy", _FLOPPY)):
        op.execute(platforms.update().where(platforms.c.name.in_(names)).values(media_type=media))

    # Backfill existing entries with the same rules the importers now apply, so
    # the Format line is populated for a library that predates this column.
    # Steam is always digital.
    op.execute(
        """
        UPDATE user_library SET medium = 'digital'
        WHERE medium IS NULL AND release_id IN (
            SELECT id FROM game_releases WHERE source = 'steam'
        )
        """
    )
    # PSN: present in the purchased feed => a digital entitlement (owned or via
    # the PS Plus catalog). Trophy/played-only history with no purchase behind
    # it is the disc signature.
    op.execute(
        """
        UPDATE user_library SET medium = 'digital'
        WHERE medium IS NULL AND release_id IN (
            SELECT id FROM game_releases
            WHERE source = 'psn' AND json_extract(raw_data, '$.sources') LIKE '%purchased%'
        )
        """
    )
    op.execute(
        """
        UPDATE user_library SET medium = 'physical'
        WHERE medium IS NULL AND release_id IN (
            SELECT id FROM game_releases
            WHERE source = 'psn' AND json_extract(raw_data, '$.sources') NOT LIKE '%purchased%'
        )
        """
    )


def downgrade() -> None:
    op.drop_column("platforms", "media_type")
    op.drop_column("user_library", "medium_user_set")
    op.drop_column("user_library", "medium")
