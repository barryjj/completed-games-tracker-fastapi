"""Split cover_url_override into per-orientation columns.

The single cover_url_override column wasn't expressive enough — vertical
(600x900 library art) and horizontal (460x215 header) covers are different
aspect ratios and the right one to show depends on context. Replacing with
two columns so the user can override each independently (or just one).

The old column was never surfaced in the UI; any existing values are kept
by mapping them into cover_url_override_v (the more common case for grid
cards) so we don't lose user data.
"""
from alembic import op
import sqlalchemy as sa

revision = "688d317c9f81"
down_revision = "67e112bf732c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_library") as batch:
        batch.add_column(sa.Column("cover_url_override_v", sa.String(), nullable=True))
        batch.add_column(sa.Column("cover_url_override_h", sa.String(), nullable=True))

    # Preserve any existing values: cover_url_override → cover_url_override_v
    # (single override defaulted to vertical / library card art).
    op.execute(
        "UPDATE user_library SET cover_url_override_v = cover_url_override "
        "WHERE cover_url_override IS NOT NULL"
    )

    with op.batch_alter_table("user_library") as batch:
        batch.drop_column("cover_url_override")


def downgrade():
    with op.batch_alter_table("user_library") as batch:
        batch.add_column(sa.Column("cover_url_override", sa.String(), nullable=True))

    op.execute(
        "UPDATE user_library SET cover_url_override = "
        "COALESCE(cover_url_override_v, cover_url_override_h)"
    )

    with op.batch_alter_table("user_library") as batch:
        batch.drop_column("cover_url_override_h")
        batch.drop_column("cover_url_override_v")
