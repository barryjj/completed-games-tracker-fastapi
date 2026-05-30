"""Artwork framework: expand game_artwork + add user_artwork table.

game_artwork changes:
  - Add game_id (nullable FK → games) for game-level canonical art
  - Add mime_type, source_type_raw, is_valid, verified_at, sort_order, created_at
  - Make release_id nullable (batch rebuild required for SQLite)
  - Rename artwork_type values: 'cover' → 'cover_v', 'header' → 'cover_h'
  - Update source 'steamgriddb' → 'sgdb' for consistency
  - Replace single unique constraint with two: per-release and per-game

user_artwork (new table):
  - Per-user, per-entry or per-game explicit artwork picks
  - Migrates existing cover_url_override_v/h, hero_url_override, logo_url_override
    from user_library into this table (source='sgdb', best-guess for existing data)
  - Override columns on user_library are kept (deprecated) and will be dropped
    in a follow-on migration after rendering is switched over.

Resolution priority (implemented in code, not data):
  1. UserArtwork for this entry  → user explicitly chose this
  2. UserArtwork for this game   → user canonical for grouped view
  3. Valid GameArtwork for this release, native sources (steam/psn) before sgdb
  4. Valid GameArtwork for this game (game-level canonical)
  5. Placeholder
"""

import datetime

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e3a1f9b2d847"
branch_labels = None
depends_on = None


def upgrade():
    # ------------------------------------------------------------------ #
    # game_artwork: add columns, make release_id nullable, fix types      #
    # Requires batch mode to rebuild table on SQLite.                     #
    # ------------------------------------------------------------------ #
    with op.batch_alter_table("game_artwork", schema=None) as batch_op:
        # New columns
        batch_op.add_column(sa.Column("game_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("mime_type", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("source_type_raw", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("is_valid", sa.Boolean(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True)
        )
        # Make release_id nullable (supports game-level rows with no release)
        batch_op.alter_column("release_id", existing_type=sa.Integer(), nullable=True)
        # Replace old single constraint with two scoped ones.
        # SQLite treats NULLs as distinct in unique constraints, so:
        #   - release-level rows (release_id set, game_id null) are governed
        #     by uq_artwork_release_type_source
        #   - game-level rows (game_id set, release_id null) are governed
        #     by uq_artwork_game_type_source
        batch_op.drop_constraint("uq_artwork_release_type_source", type_="unique")
        batch_op.create_unique_constraint(
            "uq_artwork_release_type_source",
            ["release_id", "artwork_type", "source"],
        )
        batch_op.create_unique_constraint(
            "uq_artwork_game_type_source",
            ["game_id", "artwork_type", "source"],
        )
        # FK for the new game_id column
        batch_op.create_foreign_key(
            "fk_game_artwork_game_id", "games", ["game_id"], ["id"]
        )

    # Rename artwork_type values to the new consistent naming scheme
    op.execute("UPDATE game_artwork SET artwork_type = 'cover_v' WHERE artwork_type = 'cover'")
    op.execute("UPDATE game_artwork SET artwork_type = 'cover_h' WHERE artwork_type = 'header'")

    # Normalize source name (old code wrote 'steamgriddb' in some paths)
    op.execute("UPDATE game_artwork SET source = 'sgdb' WHERE source = 'steamgriddb'")

    # ------------------------------------------------------------------ #
    # user_artwork: new table for explicit per-user artwork picks          #
    # ------------------------------------------------------------------ #
    op.create_table(
        "user_artwork",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        # One of entry_id / game_id must be set; the other is NULL.
        # entry_id  → override for a specific platform entry
        # game_id   → canonical for grouped/cross-platform view
        sa.Column("entry_id", sa.Integer(), sa.ForeignKey("user_library.id"), nullable=True),
        sa.Column("game_id", sa.Integer(), sa.ForeignKey("games.id"), nullable=True),
        sa.Column("artwork_type", sa.String(), nullable=False),
        # source: 'sgdb' (auto-fill or picker), 'user_url', 'user_upload'
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=True),  # for user_upload
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # Scoped unique constraints — same SQLite-null trick as game_artwork:
        # entry-level rows governed by first, game-level rows by second.
        sa.UniqueConstraint("user_id", "entry_id", "artwork_type", name="uq_user_artwork_entry_type"),
        sa.UniqueConstraint("user_id", "game_id", "artwork_type", name="uq_user_artwork_game_type"),
    )
    op.create_index("ix_user_artwork_user", "user_artwork", ["user_id"])
    op.create_index("ix_user_artwork_entry", "user_artwork", ["entry_id"])
    op.create_index("ix_user_artwork_game", "user_artwork", ["game_id"])

    # ------------------------------------------------------------------ #
    # Migrate existing override columns → user_artwork                    #
    # Source is 'sgdb' for all — we can't distinguish auto-fill from      #
    # user-pick in historical data.                                       #
    # ------------------------------------------------------------------ #
    now = datetime.datetime.now(datetime.UTC).isoformat()
    for col, art_type in [
        ("cover_url_override_v", "cover_v"),
        ("cover_url_override_h", "cover_h"),
        ("hero_url_override",    "hero"),
        ("logo_url_override",    "logo"),
    ]:
        op.execute(f"""
            INSERT INTO user_artwork (user_id, entry_id, artwork_type, source, url, created_at)
            SELECT user_id, id, '{art_type}', 'sgdb', {col}, '{now}'
            FROM user_library
            WHERE {col} IS NOT NULL
        """)  # noqa: S608 — no user input, col/art_type are literals


def downgrade():
    # Drop user_artwork
    op.drop_table("user_artwork")

    # Revert game_artwork column additions and rename artwork_types back.
    # Note: migrated override data is NOT restored to user_library columns —
    # this direction is effectively one-way for the data migration.
    op.execute("UPDATE game_artwork SET artwork_type = 'cover' WHERE artwork_type = 'cover_v'")
    op.execute("UPDATE game_artwork SET artwork_type = 'header' WHERE artwork_type = 'cover_h'")

    with op.batch_alter_table("game_artwork", schema=None) as batch_op:
        batch_op.drop_constraint("fk_game_artwork_game_id", type_="foreignkey")
        batch_op.drop_constraint("uq_artwork_game_type_source", type_="unique")
        batch_op.drop_constraint("uq_artwork_release_type_source", type_="unique")
        batch_op.create_unique_constraint(
            "uq_artwork_release_type_source",
            ["release_id", "artwork_type", "source"],
        )
        batch_op.alter_column("release_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("created_at")
        batch_op.drop_column("sort_order")
        batch_op.drop_column("verified_at")
        batch_op.drop_column("is_valid")
        batch_op.drop_column("source_type_raw")
        batch_op.drop_column("mime_type")
        batch_op.drop_column("game_id")
