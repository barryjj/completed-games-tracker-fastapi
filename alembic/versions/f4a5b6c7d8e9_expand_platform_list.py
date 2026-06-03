"""Expand platform list and fix GameCube canonical name.

Adds ~38 platforms covering all 14 Catppuccin accent groups:
  red       – Nintendo add-ons/handhelds (Virtual Boy, Famicom, DSi, Satellaview, 64DD)
  lavender  – PlayStation VR, PSVR2
  yellow    – Sega add-ons (32X, CD, SG-1000, CD 32X)
  peach     – Atari full family, 3DO, Evercade
  sapphire  – DOS, Amiga, Amiga CD32
  sky       – Web browser
  blue      – Meta Quest / Oculus
  mauve     – TurboGrafx / NEC PC Engine family
  maroon    – Neo Geo family
  flamingo  – Arcade
  pink      – WonderSwan family
  rosewater – Playdate

Also fixes the GameCube canonical name from "GameCube" → "Nintendo GameCube"
(IGDB igdb_id=21) while keeping display_name="GameCube" so the badge is unchanged.
"""

from alembic import op
import sqlalchemy as sa

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None

# (igdb_id, name, display_name, color, is_custom)
_NEW_PLATFORMS = [
    # Nintendo
    (87,  "Virtual Boy",                None,          "red",       False),
    (99,  "Family Computer",            "Famicom",     "red",       False),
    (159, "Nintendo DSi",               "DSi",         "red",       False),
    (306, "Satellaview",                None,          "red",       False),
    (416, "64DD",                       None,          "red",       False),
    # PlayStation VR
    (165, "PlayStation VR",             "PSVR",        "lavender",  False),
    (390, "PlayStation VR2",            "PSVR2",       "lavender",  False),
    # Sega
    (30,  "Sega 32X",                   None,          "yellow",    False),
    (78,  "Sega CD",                    None,          "yellow",    False),
    (84,  "SG-1000",                    None,          "yellow",    False),
    (482, "Sega CD 32X",                None,          "yellow",    False),
    # Atari + era peers
    (60,  "Atari 7800",                 None,          "peach",     False),
    (61,  "Atari Lynx",                 None,          "peach",     False),
    (62,  "Atari Jaguar",               None,          "peach",     False),
    (65,  "Atari 8-bit",                None,          "peach",     False),
    (66,  "Atari 5200",                 None,          "peach",     False),
    (410, "Atari Jaguar CD",            None,          "peach",     False),
    (50,  "3DO Interactive Multiplayer","3DO",          "peach",     False),
    (309, "Evercade",                   None,          "peach",     False),
    # PC / Digital
    (13,  "DOS",                        None,          "sapphire",  False),
    (16,  "Amiga",                      None,          "sapphire",  False),
    (114, "Amiga CD32",                 None,          "sapphire",  False),
    (82,  "Web browser",                None,          "sky",       False),
    # VR / Meta
    (384, "Oculus Quest",               None,          "blue",      False),
    (386, "Meta Quest 2",               None,          "blue",      False),
    (471, "Meta Quest 3",               None,          "blue",      False),
    # TurboGrafx / NEC
    (86,  "TurboGrafx-16/PC Engine",    "TG-16",       "mauve",     False),
    (128, "PC Engine SuperGrafx",       "SuperGrafx",  "mauve",     False),
    (150, "Turbografx-16/PC Engine CD", "TG-16 CD",    "mauve",     False),
    # Neo Geo
    (79,  "Neo Geo MVS",                None,          "maroon",    False),
    (80,  "Neo Geo AES",                None,          "maroon",    False),
    (119, "Neo Geo Pocket",             None,          "maroon",    False),
    (120, "Neo Geo Pocket Color",       None,          "maroon",    False),
    (136, "Neo Geo CD",                 None,          "maroon",    False),
    # Arcade
    (52,  "Arcade",                     None,          "flamingo",  False),
    # WonderSwan
    (57,  "WonderSwan",                 None,          "pink",      False),
    (123, "WonderSwan Color",           None,          "pink",      False),
    # Playdate
    (381, "Playdate",                   None,          "rosewater", False),
]

# String aliases for any raw platform strings already in game_releases.
_NEW_STRING_MAP = {
    "DOS":                              "DOS",
    "Arcade":                           "Arcade",
    "Virtual Boy":                      "Virtual Boy",
    "Family Computer":                  "Family Computer",
    "Famicom":                          "Family Computer",
    "Nintendo DSi":                     "Nintendo DSi",
    "DSi":                              "Nintendo DSi",
    "Satellaview":                      "Satellaview",
    "64DD":                             "64DD",
    "PlayStation VR":                   "PlayStation VR",
    "PSVR":                             "PlayStation VR",
    "PlayStation VR2":                  "PlayStation VR2",
    "PSVR2":                            "PlayStation VR2",
    "Sega 32X":                         "Sega 32X",
    "32X":                              "Sega 32X",
    "Sega CD":                          "Sega CD",
    "Mega CD":                          "Sega CD",
    "Sega CD 32X":                      "Sega CD 32X",
    "Mega CD 32X":                      "Sega CD 32X",
    "SG-1000":                          "SG-1000",
    "Atari 7800":                       "Atari 7800",
    "Atari Lynx":                       "Atari Lynx",
    "Atari Jaguar":                     "Atari Jaguar",
    "Atari Jaguar CD":                  "Atari Jaguar CD",
    "Atari 5200":                       "Atari 5200",
    "Atari 8-bit":                      "Atari 8-bit",
    "3DO":                              "3DO Interactive Multiplayer",
    "3DO Interactive Multiplayer":      "3DO Interactive Multiplayer",
    "Evercade":                         "Evercade",
    "Amiga":                            "Amiga",
    "Amiga CD32":                       "Amiga CD32",
    "Web browser":                      "Web browser",
    "Browser":                          "Web browser",
    "Oculus Quest":                     "Oculus Quest",
    "Meta Quest 2":                     "Meta Quest 2",
    "Meta Quest 3":                     "Meta Quest 3",
    "TurboGrafx-16":                    "TurboGrafx-16/PC Engine",
    "TurboGrafx-16/PC Engine":          "TurboGrafx-16/PC Engine",
    "PC Engine":                        "TurboGrafx-16/PC Engine",
    "TG-16":                            "TurboGrafx-16/PC Engine",
    "SuperGrafx":                       "PC Engine SuperGrafx",
    "PC Engine SuperGrafx":             "PC Engine SuperGrafx",
    "TurboGrafx CD":                    "Turbografx-16/PC Engine CD",
    "PC Engine CD":                     "Turbografx-16/PC Engine CD",
    "TG-16 CD":                         "Turbografx-16/PC Engine CD",
    "Neo Geo":                          "Neo Geo AES",
    "Neo Geo AES":                      "Neo Geo AES",
    "Neo Geo MVS":                      "Neo Geo MVS",
    "Neo Geo Pocket":                   "Neo Geo Pocket",
    "Neo Geo Pocket Color":             "Neo Geo Pocket Color",
    "Neo Geo CD":                       "Neo Geo CD",
    "WonderSwan":                       "WonderSwan",
    "WonderSwan Color":                 "WonderSwan Color",
    "Playdate":                         "Playdate",
}


def upgrade():
    conn = op.get_bind()

    # Fix GameCube canonical name to match IGDB, preserving display.
    conn.execute(
        sa.text(
            "UPDATE platforms SET name = 'Nintendo GameCube', display_name = 'GameCube' "
            "WHERE igdb_id = 21 AND name = 'GameCube'"
        )
    )

    # Insert new platforms (skip if already present by igdb_id or name).
    for igdb_id, name, display_name, color, is_custom in _NEW_PLATFORMS:
        existing = conn.execute(
            sa.text("SELECT id FROM platforms WHERE igdb_id = :igdb_id OR name = :name"),
            {"igdb_id": igdb_id, "name": name},
        ).fetchone()
        if not existing:
            conn.execute(
                sa.text(
                    "INSERT INTO platforms (igdb_id, name, display_name, color, is_custom) "
                    "VALUES (:igdb_id, :name, :display_name, :color, :is_custom)"
                ),
                {
                    "igdb_id": igdb_id,
                    "name": name,
                    "display_name": display_name,
                    "color": color,
                    "is_custom": 1 if is_custom else 0,
                },
            )

    # Build name → id map and link any existing game_releases.
    rows = conn.execute(sa.text("SELECT id, name FROM platforms")).fetchall()
    name_to_id = {row[1]: row[0] for row in rows}

    distinct = conn.execute(
        sa.text(
            "SELECT DISTINCT platform FROM game_releases "
            "WHERE platform IS NOT NULL AND platform_id IS NULL"
        )
    ).fetchall()
    for (platform_str,) in distinct:
        canonical = _NEW_STRING_MAP.get(platform_str)
        if canonical and canonical in name_to_id:
            conn.execute(
                sa.text(
                    "UPDATE game_releases SET platform_id = :pid WHERE platform = :pstr"
                ),
                {"pid": name_to_id[canonical], "pstr": platform_str},
            )


def downgrade():
    conn = op.get_bind()

    # Unlink releases for newly inserted platforms.
    new_names = [name for _, name, _, _, _ in _NEW_PLATFORMS]
    if new_names:
        placeholders = ",".join(f":n{i}" for i in range(len(new_names)))
        params = {f"n{i}": n for i, n in enumerate(new_names)}
        subq = f"SELECT id FROM platforms WHERE name IN ({placeholders})"
        conn.execute(
            sa.text(f"UPDATE game_releases SET platform_id = NULL WHERE platform_id IN ({subq})"),
            params,
        )
        conn.execute(
            sa.text(f"DELETE FROM platforms WHERE name IN ({placeholders})"),
            params,
        )

    # Revert GameCube name fix.
    conn.execute(
        sa.text(
            "UPDATE platforms SET name = 'GameCube', display_name = NULL "
            "WHERE igdb_id = 21 AND name = 'Nintendo GameCube'"
        )
    )
