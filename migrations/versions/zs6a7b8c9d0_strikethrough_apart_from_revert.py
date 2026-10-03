"""The struck-through price and the one a launch goes back up to, apart.

Revision ID: zs6a7b8c9d0
Revises: zr5y6z7a8b9
Create Date: 2026-10-03

``compare_at_cents`` was doing two jobs at once: it was the number drawn with
a line through it beside the price, and it was the figure the "reverting to
… on …" countdown named. Neither could be set without setting the other, and
the day the countdown ran out took the strikethrough away with it.

Both halves are carried over as they stand, so every page reads the same the
moment this runs: everybody keeps the strikethrough they had, and a product
with a revert date keeps naming the same figure in its countdown. Only a
product with no date set comes out with no revert price, which is what it
already meant.
"""
import sqlalchemy as sa
from alembic import op

revision = "zs6a7b8c9d0"
down_revision = "zr5y6z7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("strikethrough_cents", sa.Integer(),
                                   nullable=True))
        batch.add_column(sa.Column("reverts_to_cents", sa.Integer(),
                                   nullable=True))
    op.execute("UPDATE products SET strikethrough_cents = compare_at_cents")
    op.execute("""
        UPDATE products
           SET reverts_to_cents = compare_at_cents
         WHERE price_reverts_at IS NOT NULL
    """)
    with op.batch_alter_table("products") as batch:
        batch.drop_column("compare_at_cents")


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("compare_at_cents", sa.Integer(),
                                   nullable=True))
    # One column, two meanings again. The strikethrough is the one that was
    # on every product rather than only the ones counting down, so it wins
    # where a product has both and they disagree.
    op.execute("""
        UPDATE products
           SET compare_at_cents = COALESCE(strikethrough_cents, reverts_to_cents)
    """)
    with op.batch_alter_table("products") as batch:
        batch.drop_column("reverts_to_cents")
        batch.drop_column("strikethrough_cents")
