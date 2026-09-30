"""Runs of a product: the same thing sold again on a different calendar.

Revision ID: zq4x5y6z7a8
Revises: zp3w4x5y6z7
Create Date: 2026-09-29

Nothing here changes an existing product. A product with no rows in
``product_rounds`` goes on its own dates exactly as it did before, and
``shop_purchases.round_id`` is empty for every purchase made until now,
which means the same thing: this buyer is on the product's own dates.
"""
import sqlalchemy as sa
from alembic import op

revision = "zq4x5y6z7a8"
down_revision = "zp3w4x5y6z7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_rounds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False,
                  server_default="draft"),
        sa.Column("drip_mode", sa.String(length=12), nullable=False,
                  server_default="interval"),
        sa.Column("drip_interval_days", sa.Integer(), nullable=False,
                  server_default="7"),
        sa.Column("drip_starts_at", sa.DateTime(), nullable=True),
        sa.Column("schedule_json", sa.Text(), nullable=True),
        sa.Column("off_shelf_at", sa.DateTime(), nullable=True),
        sa.Column("perk_membership_months", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("perk_starts_at", sa.DateTime(), nullable=True),
        sa.Column("perk_ends_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "number", name="uq_round_number"),
    )
    with op.batch_alter_table("product_rounds") as batch:
        batch.create_index("ix_product_rounds_product_id", ["product_id"])

    with op.batch_alter_table("shop_purchases") as batch:
        batch.add_column(sa.Column("round_id", sa.Integer(), nullable=True))
        batch.create_index("ix_shop_purchases_round_id", ["round_id"])
        batch.create_foreign_key("fk_shop_purchases_round_id",
                                 "product_rounds", ["round_id"], ["id"])


def downgrade():
    with op.batch_alter_table("shop_purchases") as batch:
        batch.drop_constraint("fk_shop_purchases_round_id",
                              type_="foreignkey")
        batch.drop_index("ix_shop_purchases_round_id")
        batch.drop_column("round_id")
    with op.batch_alter_table("product_rounds") as batch:
        batch.drop_index("ix_product_rounds_product_id")
    op.drop_table("product_rounds")
