"""Autoincrement PKs and FK cascade fixes.

Adds AUTOINCREMENT to every table's integer primary key so SQLite never
reuses a freed rowid for an unrelated future row (a freed manual-library-entry
id was recycled onto a later Steam-synced entry, which is how a stale
sync_match_candidates row ended up pointing "Construction Simulator" at
"Resident Evil 4"). Also adds/fixes ondelete behavior on several foreign
keys so deleting a parent row (user, library entry, game, release) cleans
up its dependents instead of leaving orphaned rows behind.

Rebuilds every table via copy_from=<current model Table>, since SQLite has
no ALTER TABLE support for adding AUTOINCREMENT or changing a foreign key's
ON DELETE behavior — the existing (mostly unnamed) constraints reflected
from the live DB can't be dropped/recreated by name, so batch mode is
pointed straight at the already-correct model metadata instead.
"""
from alembic import op

from backend.models import Base

revision = "0a08baf534e9"
down_revision = 'ec8ef7950bd0'
branch_labels = None
depends_on = None

TABLES = [
    "users",
    "platform_families",
    "platforms",
    "platform_aliases",
    "games",
    "game_releases",
    "game_artwork",
    "user_artwork",
    "user_library",
    "user_achievements",
    "sync_match_candidates",
    "import_candidates",
    "import_rows",
    "completions",
]


def upgrade():
    for table_name in TABLES:
        with op.batch_alter_table(table_name, schema=None, recreate='always', copy_from=Base.metadata.tables[table_name]):
            pass


def downgrade():
    raise NotImplementedError("Irreversible: recreates tables from current model metadata.")
