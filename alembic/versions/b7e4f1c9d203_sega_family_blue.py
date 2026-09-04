"""Sega family moves from yellow to blue

The platform palette is brand-derived: Nintendo red, Xbox green, PlayStation
lavender. Sega is blue -- the logo, Sonic -- and yellow was simply wrong on
those terms. It also collided with the yellow used for an import row whose
platform matched nothing, which is a chip that appears in the same table.

Blue is already held by the Meta/Oculus family (3 rows). They share it: a Quest
game and a Dreamcast game rarely appear on the same screen, and Sega has both
the stronger brand claim and three times the rows.

Revision ID: b7e4f1c9d203
Revises: a3f8c2d5e701
"""

from alembic import op

revision = "b7e4f1c9d203"
down_revision = "a3f8c2d5e701"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The family carries the fallback colour; each platform also stores its own,
    # and Platform.effective_color prefers the platform's, so both must move or
    # the rows keep rendering yellow.
    op.execute("UPDATE platform_families SET color = 'blue' WHERE name = 'Sega'")
    op.execute(
        "UPDATE platforms SET color = 'blue' "
        "WHERE color = 'yellow' AND family_id = (SELECT id FROM platform_families WHERE name = 'Sega')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE platforms SET color = 'yellow' "
        "WHERE color = 'blue' AND family_id = (SELECT id FROM platform_families WHERE name = 'Sega')"
    )
    op.execute("UPDATE platform_families SET color = 'yellow' WHERE name = 'Sega'")
