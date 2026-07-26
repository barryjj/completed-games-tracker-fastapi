"""drop digital/physical format columns

Revision ID: c5e2a91d7f30
Revises: b3f0d27ca514
Create Date: 2026-07-26

Removes the storage behind the format experiment (#164), now that the app-side
code is gone.

`user_library.medium` / `medium_user_set` and `platforms.media_type` were
write-only: nothing filtered, sorted, grouped or branched on them. The value was
also unknowable at the source — PSN cannot see physical ownership at all, since
a disc surfaces only as whatever digital entitlement it granted (a PS4 disc with
a free PS5 upgrade reports as a plain PS5 purchase; a disc whose game later hit
the catalogue reports as PS_PLUS). Asking the user to supply it put 288 rows
into the PSN import review against 53 real platform questions.

The ownership-vs-subscription distinction that *is* useful survives: it reads
from `game_releases.raw_data['membership']` and never needed these columns.

Data loss is intentional and accepted — the columns held inferences and a small
number of manual choices, not irreplaceable records. Downgrade recreates the
columns empty.
"""

import sqlalchemy as sa
from alembic import op

revision = "c5e2a91d7f30"
down_revision = "b3f0d27ca514"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("user_library", "medium_user_set")
    op.drop_column("user_library", "medium")
    op.drop_column("platforms", "media_type")


def downgrade() -> None:
    op.add_column("platforms", sa.Column("media_type", sa.VARCHAR(), nullable=True))
    op.add_column("user_library", sa.Column("medium", sa.VARCHAR(), nullable=True))
    op.add_column("user_library", sa.Column("medium_user_set", sa.Boolean(), nullable=False, server_default=sa.false()))
