"""Add steam_avatar_url to User.

Populated from GetPlayerSummaries in the OpenID return handler so the Steam
configure page can show the user's avatar next to their persona name —
purely decorative.
"""
from alembic import op
import sqlalchemy as sa

revision = "a07e802095df"
down_revision = "72f5b7aa12ee"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("steam_avatar_url", sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("steam_avatar_url")
