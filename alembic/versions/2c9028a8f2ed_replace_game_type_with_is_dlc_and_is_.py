"""Replace game_type string with is_dlc and is_collection boolean flags."""
from alembic import op
import sqlalchemy as sa

revision = "2c9028a8f2ed"
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # Add new bool columns with server_default so existing rows get False
    with op.batch_alter_table('games') as batch_op:
        batch_op.add_column(sa.Column('is_dlc', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('is_collection', sa.Boolean(), nullable=False, server_default=sa.false()))

    # Migrate existing game_type data before dropping the column
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE games SET is_dlc = 1 WHERE game_type IN ('dlc', 'expansion')"))
    conn.execute(sa.text("UPDATE games SET is_collection = 1 WHERE game_type = 'collection'"))

    # Drop the old column (batch required for SQLite)
    with op.batch_alter_table('games') as batch_op:
        batch_op.drop_column('game_type')


def downgrade():
    # Re-add game_type with a default so existing rows get 'game'
    with op.batch_alter_table('games') as batch_op:
        batch_op.add_column(sa.Column('game_type', sa.VARCHAR(), nullable=False, server_default='game'))

    conn = op.get_bind()
    conn.execute(sa.text("UPDATE games SET game_type = 'dlc' WHERE is_dlc = 1 AND is_collection = 0"))
    conn.execute(sa.text("UPDATE games SET game_type = 'collection' WHERE is_collection = 1"))

    with op.batch_alter_table('games') as batch_op:
        batch_op.drop_column('is_collection')
        batch_op.drop_column('is_dlc')
