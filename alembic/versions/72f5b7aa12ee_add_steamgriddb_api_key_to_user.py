"""Add SteamGridDB API key to User.

Per-user key for the SGDB lookups; entered via the new SGDB configure page.
"""
from alembic import op
import sqlalchemy as sa

revision = "72f5b7aa12ee"
down_revision = "8bf781a11ec5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("steamgriddb_api_key", sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("steamgriddb_api_key")
