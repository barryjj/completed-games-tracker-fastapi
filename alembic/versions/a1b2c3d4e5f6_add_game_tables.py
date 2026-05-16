"""Add game tables: games, game_releases, game_artwork, user_library, user_achievements, completions"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "643c9155ebea"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "games",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("game_type", sa.String(), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("games.id"), nullable=True),
        sa.Column("igdb_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("igdb_id", name="uq_games_igdb_id"),
    )
    op.create_index(op.f("ix_games_id"), "games", ["id"], unique=False)

    op.create_table(
        "game_releases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), sa.ForeignKey("games.id"), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "platform", name="uq_release_game_platform"),
    )
    op.create_index(op.f("ix_game_releases_id"), "game_releases", ["id"], unique=False)

    op.create_table(
        "game_artwork",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("game_releases.id"), nullable=False),
        sa.Column("artwork_type", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("release_id", "artwork_type", "source", name="uq_artwork_release_type_source"),
    )
    op.create_index(op.f("ix_game_artwork_id"), "game_artwork", ["id"], unique=False)

    op.create_table(
        "user_library",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("game_releases.id"), nullable=False),
        sa.Column("playtime_minutes", sa.Integer(), nullable=True),
        sa.Column("last_played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cover_url_override", sa.String(), nullable=True),
        sa.Column("import_source", sa.String(), nullable=False),
        sa.Column("parent_entry_id", sa.Integer(), sa.ForeignKey("user_library.id"), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "release_id", name="uq_library_user_release"),
    )
    op.create_index(op.f("ix_user_library_id"), "user_library", ["id"], unique=False)

    op.create_table(
        "user_achievements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("library_entry_id", sa.Integer(), sa.ForeignKey("user_library.id"), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon_url", sa.String(), nullable=True),
        sa.Column("unlocked", sa.Boolean(), nullable=False),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("library_entry_id", "external_id", name="uq_achievement_entry_external"),
    )
    op.create_index(op.f("ix_user_achievements_id"), "user_achievements", ["id"], unique=False)

    op.create_table(
        "completions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("library_entry_id", sa.Integer(), sa.ForeignKey("user_library.id"), nullable=False),
        sa.Column("completed_at", sa.Date(), nullable=False),
        sa.Column("playthroughs", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_completions_id"), "completions", ["id"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_completions_id"), table_name="completions")
    op.drop_table("completions")
    op.drop_index(op.f("ix_user_achievements_id"), table_name="user_achievements")
    op.drop_table("user_achievements")
    op.drop_index(op.f("ix_user_library_id"), table_name="user_library")
    op.drop_table("user_library")
    op.drop_index(op.f("ix_game_artwork_id"), table_name="game_artwork")
    op.drop_table("game_artwork")
    op.drop_index(op.f("ix_game_releases_id"), table_name="game_releases")
    op.drop_table("game_releases")
    op.drop_index(op.f("ix_games_id"), table_name="games")
    op.drop_table("games")
