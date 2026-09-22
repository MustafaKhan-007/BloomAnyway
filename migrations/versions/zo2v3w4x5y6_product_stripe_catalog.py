"""Studio keeps a product's Stripe product, price and billing period.

Revision ID: zo2v3w4x5y6
Revises: zn1u2v3w4x5
Create Date: 2026-09-22

``billing_period`` defaults to ``once`` because that is what every product
sold as before there was anything else to be. ``retired_price_ids_json``
starts empty: nothing has rotated a price yet, and the current one is still
in ``stripe_price_id`` where every existing lookup expects it.
"""
import sqlalchemy as sa
from alembic import op

revision = "zo2v3w4x5y6"
down_revision = "zn1u2v3w4x5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("stripe_product_id", sa.String(length=80),
                                   nullable=True))
        batch.add_column(sa.Column("stripe_description", sa.String(length=500),
                                   nullable=True))
        batch.add_column(sa.Column("billing_period", sa.String(length=12),
                                   nullable=False, server_default="once"))
        batch.add_column(sa.Column("retired_price_ids_json", sa.Text(),
                                   nullable=True))
        batch.add_column(sa.Column("stripe_synced_at", sa.DateTime(),
                                   nullable=True))
        batch.add_column(sa.Column("stripe_sync_error", sa.String(length=300),
                                   nullable=True))
        batch.create_index("ix_products_stripe_product_id",
                           ["stripe_product_id"])


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_index("ix_products_stripe_product_id")
        batch.drop_column("stripe_sync_error")
        batch.drop_column("stripe_synced_at")
        batch.drop_column("retired_price_ids_json")
        batch.drop_column("billing_period")
        batch.drop_column("stripe_description")
        batch.drop_column("stripe_product_id")
