"""Add steam_persona_name to User.

Stores the Steam display name from the OpenID flow so we can show
"Signed in as <name>" on the Steam configure page. Not used for auth.
"""
from alembic import op
import sqlalchemy as sa

revision = "8bf781a11ec5"
down_revision = "688d317c9f81"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("steam_persona_name", sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("steam_persona_name")
