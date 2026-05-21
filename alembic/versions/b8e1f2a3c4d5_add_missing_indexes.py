"""Add missing indexes for library query performance."""
from alembic import op

revision = "b8e1f2a3c4d5"
down_revision = "cf19768819ef"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_game_releases_game_id", "game_releases", ["game_id"])
    op.create_index("ix_games_parent_id", "games", ["parent_id"])
    op.create_index("ix_games_is_dlc", "games", ["is_dlc"])
    op.create_index("ix_games_is_collection", "games", ["is_collection"])
    op.create_index("ix_user_library_import_source", "user_library", ["import_source"])


def downgrade():
    op.drop_index("ix_user_library_import_source", table_name="user_library")
    op.drop_index("ix_games_is_collection", table_name="games")
    op.drop_index("ix_games_is_dlc", table_name="games")
    op.drop_index("ix_games_parent_id", table_name="games")
    op.drop_index("ix_game_releases_game_id", table_name="game_releases")
