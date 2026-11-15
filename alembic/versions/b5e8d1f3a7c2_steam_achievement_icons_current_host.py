"""Steam achievement icons move to Steam's current image host

GetSchemaForGame returns icons on steamcdn-a.akamaihd.net, a retired host
that still serves older games but 404s newer ones, so their unlocks showed
a broken image. The same path is served for every game from
shared.akamai.steamstatic.com/community_assets. Syncs now store the current
host (steam_achievements._icon); this rewrites what is already stored.

Downgrade is a no-op: the old host is the broken one.

Revision ID: b5e8d1f3a7c2
Revises: a41d7c3e9b52
"""

import sqlalchemy as sa
from alembic import op

revision = "b5e8d1f3a7c2"
down_revision = "a41d7c3e9b52"
branch_labels = None
depends_on = None

_LEGACY = "https://steamcdn-a.akamaihd.net/steamcommunity/public/images/apps/"
_CURRENT = "https://shared.akamai.steamstatic.com/community_assets/images/apps/"


def upgrade() -> None:
    for column in ("icon_url", "icon_locked_url"):
        op.get_bind().execute(
            sa.text(
                f"UPDATE achievement_definitions SET {column} = :current || substr({column}, :n) "
                f"WHERE source = 'steam' AND {column} LIKE :legacy || '%'"
            ),
            {"current": _CURRENT, "legacy": _LEGACY, "n": len(_LEGACY) + 1},
        )


def downgrade() -> None:
    pass
