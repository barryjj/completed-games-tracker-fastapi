"""Add completed_at_precision to completions and import_rows.

Historical spreadsheet imports often only have a month+year or bare year
for a completion date; the importer previously fabricated a day (1st of
the month, or Jan 1 for year-only) with no way to tell that apart from a
genuinely known exact date. This column records what was actually known
('day' | 'month' | 'year') so display can show "January 2012" or "2012"
instead of a misleading "January 1, 2012".

Existing rows default to 'day' (today's behavior, unchanged) — a
follow-up backfill re-derives accurate precision for already-confirmed
import history from ImportRow.raw_date, which is kept permanently for
dedup purposes.
"""
from alembic import op
import sqlalchemy as sa

revision = "ec8ef7950bd0"
down_revision = '00af031b8b8d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("completions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completed_at_precision", sa.String(), nullable=False, server_default="day"))
    with op.batch_alter_table("import_rows", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completed_at_precision", sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table("import_rows", schema=None) as batch_op:
        batch_op.drop_column("completed_at_precision")
    with op.batch_alter_table("completions", schema=None) as batch_op:
        batch_op.drop_column("completed_at_precision")
