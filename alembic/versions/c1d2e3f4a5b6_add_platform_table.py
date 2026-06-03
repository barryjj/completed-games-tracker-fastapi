"""Add platforms table and link game_releases.

Creates the 'platforms' table with IGDB-sourced and custom rows,
seeds it with the canonical platform list, then adds a nullable
platform_id FK to game_releases and runs a data migration to wire up
all existing platform strings to their Platform rows.
"""

from alembic import op
import sqlalchemy as sa

revision = "c1d2e3f4a5b6"
down_revision = "a0ad79436a20"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Seed data
# Each row: (igdb_id, name, display_name, color, is_custom)
# display_name=None means "same as name" (the model property returns name).
# color must be a Catppuccin accent key or None.
# ---------------------------------------------------------------------------
_PLATFORMS = [
    # -- PC / Digital --
    (6,    "PC (Microsoft Windows)", "PC",       "sapphire", False),
    (14,   "Mac",                    None,        "sky",      False),
    (3,    "Linux",                  None,        "sapphire", False),
    (39,   "iOS",                    None,        "sky",      False),
    (34,   "Android",                None,        "green",    False),
    # Steam is not an IGDB platform — custom row so existing Steam entries stay labelled "Steam"
    (None, "Steam",                  None,        "teal",     True),

    # -- PlayStation --
    (167,  "PlayStation 5",          "PS5",       "lavender", False),
    (48,   "PlayStation 4",          "PS4",       "lavender", False),
    (9,    "PlayStation 3",          "PS3",       "lavender", False),
    (8,    "PlayStation 2",          "PS2",       "lavender", False),
    (7,    "PlayStation",            "PS1",       "lavender", False),
    (38,   "PlayStation Portable",   "PSP",       "lavender", False),
    (46,   "PlayStation Vita",       "PS Vita",   "lavender", False),

    # -- Xbox --
    (169,  "Xbox Series X|S",        None,        "green",    False),
    (49,   "Xbox One",               None,        "green",    False),
    (12,   "Xbox 360",               None,        "green",    False),
    (11,   "Xbox",                   None,        "green",    False),

    # -- Nintendo (modern) --
    (130,  "Nintendo Switch",        "Switch",    "red",      False),
    # Nintendo Switch 2 (igdb_id=162) added in migration d2e3f4a5b6c7
    (41,   "Wii U",                  None,        "red",      False),
    (5,    "Wii",                    None,        "red",      False),

    # -- Nintendo (handheld) --
    (37,   "Nintendo 3DS",           "3DS",       "red",      False),
    (137,  "New Nintendo 3DS",       "New 3DS",   "red",      False),
    (20,   "Nintendo DS",            "DS",        "red",      False),
    (24,   "Game Boy Advance",       "GBA",       "red",      False),
    (22,   "Game Boy Color",         "GBC",       "red",      False),
    (33,   "Game Boy",               "GB",        "red",      False),

    # -- Nintendo (home console) --
    (4,    "Nintendo 64",            "N64",       "red",      False),
    (21,   "GameCube",               None,        "red",      False),
    (19,   "Super Nintendo Entertainment System", "SNES", "red", False),
    (18,   "Nintendo Entertainment System",       "NES",  "red", False),

    # -- Sega --
    (23,   "Dreamcast",              None,        "yellow",   False),
    (32,   "Sega Saturn",            None,        "yellow",   False),
    (29,   "Sega Mega Drive/Genesis","Genesis",   "yellow",   False),
    (35,   "Sega Game Gear",         None,        "yellow",   False),
    (64,   "Sega Master System/Mark III", "Master System", "yellow", False),

    # -- Atari --
    (59,   "Atari 2600",             None,        "peach",    False),
    (63,   "Atari ST/STE",           None,        "peach",    False),
]

# Mapping from the raw platform strings that may already exist in game_releases
# to the canonical Platform.name that should be linked.
# Extend this list as more platforms are added over time.
_PLATFORM_STRING_MAP = {
    # exact matches
    "Steam":                             "Steam",
    "Nintendo DS":                       "Nintendo DS",
    "Nintendo Entertainment System":     "Nintendo Entertainment System",
    "NES":                               "Nintendo Entertainment System",
    "SNES":                              "Super Nintendo Entertainment System",
    "Super Nintendo Entertainment System": "Super Nintendo Entertainment System",
    "Nintendo 64":                       "Nintendo 64",
    "N64":                               "Nintendo 64",
    "GameCube":                          "GameCube",
    "Game Boy":                          "Game Boy",
    "GB":                                "Game Boy",
    "Game Boy Color":                    "Game Boy Color",
    "GBC":                               "Game Boy Color",
    "Game Boy Advance":                  "Game Boy Advance",
    "GBA":                               "Game Boy Advance",
    "Nintendo 3DS":                      "Nintendo 3DS",
    "3DS":                               "Nintendo 3DS",
    "New Nintendo 3DS":                  "New Nintendo 3DS",
    "Nintendo Switch":                   "Nintendo Switch",
    "Switch":                            "Nintendo Switch",
    "Wii":                               "Wii",
    "Wii U":                             "Wii U",
    "PlayStation":                       "PlayStation",
    "PS1":                               "PlayStation",
    "PlayStation 2":                     "PlayStation 2",
    "PS2":                               "PlayStation 2",
    "PlayStation 3":                     "PlayStation 3",
    "PS3":                               "PlayStation 3",
    "PlayStation 4":                     "PlayStation 4",
    "PS4":                               "PlayStation 4",
    "PlayStation 5":                     "PlayStation 5",
    "PS5":                               "PlayStation 5",
    "PlayStation Portable":              "PlayStation Portable",
    "PSP":                               "PlayStation Portable",
    "PlayStation Vita":                  "PlayStation Vita",
    "PS Vita":                           "PlayStation Vita",
    "Xbox":                              "Xbox",
    "Xbox 360":                          "Xbox 360",
    "Xbox One":                          "Xbox One",
    "Xbox Series X|S":                   "Xbox Series X|S",
    "Xbox Series X":                     "Xbox Series X|S",
    "Xbox Series S":                     "Xbox Series X|S",
    "PC":                                "PC (Microsoft Windows)",
    "PC (Microsoft Windows)":            "PC (Microsoft Windows)",
    "Windows":                           "PC (Microsoft Windows)",
    "Mac":                               "Mac",
    "macOS":                             "Mac",
    "Linux":                             "Linux",
    "iOS":                               "iOS",
    "Android":                           "Android",
    "Dreamcast":                         "Dreamcast",
    "Sega Saturn":                       "Sega Saturn",
    "Genesis":                           "Sega Mega Drive/Genesis",
    "Sega Genesis":                      "Sega Mega Drive/Genesis",
    "Sega Mega Drive":                   "Sega Mega Drive/Genesis",
    "Sega Mega Drive/Genesis":           "Sega Mega Drive/Genesis",
    "Sega Game Gear":                    "Sega Game Gear",
    "Game Gear":                         "Sega Game Gear",
    "Sega Master System":                "Sega Master System/Mark III",
    "Master System":                     "Sega Master System/Mark III",
    "Atari 2600":                        "Atari 2600",
}


def upgrade():
    # 1. Create platforms table
    op.create_table(
        "platforms",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("igdb_id", sa.Integer, nullable=True, unique=True),
        sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("display_name", sa.String, nullable=True),
        sa.Column("color", sa.String, nullable=True),
        sa.Column("is_custom", sa.Boolean, nullable=False, server_default="0"),
    )
    op.create_index("ix_platforms_id", "platforms", ["id"], unique=False)

    # 2. Seed platforms
    conn = op.get_bind()
    for igdb_id, name, display_name, color, is_custom in _PLATFORMS:
        conn.execute(
            sa.text(
                "INSERT INTO platforms (igdb_id, name, display_name, color, is_custom) "
                "VALUES (:igdb_id, :name, :display_name, :color, :is_custom)"
            ),
            {"igdb_id": igdb_id, "name": name, "display_name": display_name,
             "color": color, "is_custom": 1 if is_custom else 0},
        )

    # 3. Add platform_id column to game_releases (nullable FK — SQLite batch).
    # SQLite batch mode requires named FK constraints; we skip the inline FK
    # declaration here (SQLite doesn't enforce FKs unless PRAGMA is on anyway)
    # and rely on the ORM relationship for integrity.
    with op.batch_alter_table("game_releases") as batch_op:
        batch_op.add_column(sa.Column("platform_id", sa.Integer, nullable=True))
        batch_op.create_index("ix_game_releases_platform_id", ["platform_id"], unique=False)

    # 4. Data migration — link existing platform strings to Platform rows
    # Build a name → id lookup from what we just inserted.
    rows = conn.execute(sa.text("SELECT id, name FROM platforms")).fetchall()
    name_to_id = {row[1]: row[0] for row in rows}

    # For every distinct platform string in game_releases, resolve via _PLATFORM_STRING_MAP.
    distinct = conn.execute(
        sa.text("SELECT DISTINCT platform FROM game_releases WHERE platform IS NOT NULL")
    ).fetchall()
    for (platform_str,) in distinct:
        canonical = _PLATFORM_STRING_MAP.get(platform_str)
        if canonical and canonical in name_to_id:
            conn.execute(
                sa.text(
                    "UPDATE game_releases SET platform_id = :pid WHERE platform = :pstr"
                ),
                {"pid": name_to_id[canonical], "pstr": platform_str},
            )


def downgrade():
    with op.batch_alter_table("game_releases") as batch_op:
        batch_op.drop_index("ix_game_releases_platform_id")
        batch_op.drop_column("platform_id")

    op.drop_index("ix_platforms_id", table_name="platforms")
    op.drop_table("platforms")
