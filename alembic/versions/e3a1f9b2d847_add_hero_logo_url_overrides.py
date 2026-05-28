"""Add hero_url_override and logo_url_override to user_library."""
from alembic import op
import sqlalchemy as sa

revision = "e3a1f9b2d847"
down_revision = "a07e802095df"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_library", sa.Column("hero_url_override", sa.String(), nullable=True))
    op.add_column("user_library", sa.Column("logo_url_override", sa.String(), nullable=True))


def downgrade():
    op.drop_column("user_library", "logo_url_override")
    op.drop_column("user_library", "hero_url_override")
