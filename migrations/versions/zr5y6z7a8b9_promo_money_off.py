"""A promo is money off, not a second price.

Revision ID: zr5y6z7a8b9
Revises: zq4x5y6z7a8
Create Date: 2026-09-30

A promo code is a Stripe coupon, and a coupon takes an amount off whatever
the price is on the day. Holding what it comes to instead meant a price rise
quietly turned "$10 off" into "$20 off" on the page while Stripe still took
ten. Every running sale is carried over as the difference it was already
advertising, so nothing on any page changes the moment this runs.
"""
import sqlalchemy as sa
from alembic import op

revision = "zr5y6z7a8b9"
down_revision = "zq4x5y6z7a8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("promo_off_cents", sa.Integer(), nullable=True))
    # A saved promo that was never worth anything — dearer than the product,
    # or on a product with no price — is left empty rather than carried over
    # as a negative. Nothing was advertising it anyway.
    op.execute("""
        UPDATE products
           SET promo_off_cents = price_cents - promo_price_cents
         WHERE promo_price_cents IS NOT NULL
           AND price_cents IS NOT NULL
           AND price_cents > promo_price_cents
    """)
    with op.batch_alter_table("products") as batch:
        batch.drop_column("promo_price_cents")


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("promo_price_cents", sa.Integer(), nullable=True))
    op.execute("""
        UPDATE products
           SET promo_price_cents = price_cents - promo_off_cents
         WHERE promo_off_cents IS NOT NULL
           AND price_cents IS NOT NULL
           AND price_cents >= promo_off_cents
    """)
    with op.batch_alter_table("products") as batch:
        batch.drop_column("promo_off_cents")
