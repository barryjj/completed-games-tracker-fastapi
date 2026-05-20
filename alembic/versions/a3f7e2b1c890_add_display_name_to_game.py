"""Add display_name to Game."""
from alembic import op
import sqlalchemy as sa

revision = "a3f7e2b1c890"
down_revision = "cf19768819ef"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("games", sa.Column("display_name", sa.String(), nullable=True))


def downgrade():
    op.drop_column("games", "display_name")
