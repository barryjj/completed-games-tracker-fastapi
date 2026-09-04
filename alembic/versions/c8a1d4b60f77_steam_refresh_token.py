"""Store the Steam refresh token and when the cookies were captured

steamLoginSecure is a 24-hour access token; its own claims carry an rt_exp
naming a refresh token good for months. Capturing only the access token is why
the desktop app asked for a Steam sign-in daily while a browser went 89 days
without one (#208).

captured_at exists because PSN already tracks psn_npsso_captured_at and Steam
had no equivalent, so the app could not say how stale its credentials were --
or warn before a sync failed.

Revision ID: c8a1d4b60f77
Revises: b7e4f1c9d203
"""

import sqlalchemy as sa
from alembic import op

revision = "c8a1d4b60f77"
down_revision = "b7e4f1c9d203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("steam_refresh_token", sa.String(), nullable=True))
    op.add_column("users", sa.Column("steam_refresh_expires_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("steam_cookies_captured_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "steam_cookies_captured_at")
    op.drop_column("users", "steam_refresh_expires_at")
    op.drop_column("users", "steam_refresh_token")
