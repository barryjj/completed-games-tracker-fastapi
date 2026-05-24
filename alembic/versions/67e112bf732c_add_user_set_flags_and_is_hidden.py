"""Add user_set flags on Game + is_hidden / is_hidden_user_set on UserLibraryEntry

Establishes the user-override pattern: any time a heuristic could stomp a
user-editable field, the corresponding `_user_set` bool is checked first.
True means "the user has explicitly set this; do not touch."
"""
from alembic import op
import sqlalchemy as sa

revision = "67e112bf732c"
down_revision = '415b9dcd0449'
branch_labels = None
depends_on = None


def upgrade():
    # Game: four user-override flags for the fields the existing edit modal
    # already touches (display_name, is_dlc, is_collection, parent_id).
    with op.batch_alter_table("games") as batch:
        batch.add_column(sa.Column("display_name_user_set", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("is_dlc_user_set", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("is_collection_user_set", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("parent_id_user_set", sa.Boolean(), nullable=False, server_default=sa.false()))

    # UserLibraryEntry: the new hidden-from-default-view flag + its user-set partner.
    with op.batch_alter_table("user_library") as batch:
        batch.add_column(sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("is_hidden_user_set", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_user_library_is_hidden", ["is_hidden"])

    # Backfill: existing manual entries should be treated as user-set on display_name
    # so future cleanup heuristics (ALL CAPS normalization etc.) don't touch them.
    # We can only know an entry is manual via its UserLibraryEntry.import_source —
    # but display_name lives on Game, and one Game can have multiple releases. So
    # mark a Game as display_name_user_set if ALL of its releases come from manual
    # imports (i.e., no Steam release exists).
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE games
        SET display_name_user_set = 1
        WHERE id IN (
            SELECT g.id FROM games g
            WHERE NOT EXISTS (
                SELECT 1 FROM game_releases r
                WHERE r.game_id = g.id AND r.source != 'manual'
            )
        )
    """))


def downgrade():
    with op.batch_alter_table("user_library") as batch:
        batch.drop_index("ix_user_library_is_hidden")
        batch.drop_column("is_hidden_user_set")
        batch.drop_column("is_hidden")

    with op.batch_alter_table("games") as batch:
        batch.drop_column("parent_id_user_set")
        batch.drop_column("is_collection_user_set")
        batch.drop_column("is_dlc_user_set")
        batch.drop_column("display_name_user_set")
