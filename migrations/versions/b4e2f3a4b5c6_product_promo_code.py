"""a running promo code on a product: the code, and what it costs with it

Revision ID: b4e2f3a4b5c6
Revises: a3d1e2f3a4b5
"""
import sqlalchemy as sa
from alembic import op

revision = "b4e2f3a4b5c6"
down_revision = "a3d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("promo_price_cents", sa.Integer()))
        batch.add_column(sa.Column("promo_code", sa.String(length=40)))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("promo_code")
        batch.drop_column("promo_price_cents")
