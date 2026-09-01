"""when a product's promo code stops working

Revision ID: c5f3a4b5c6d7
Revises: b4e2f3a4b5c6
"""
import sqlalchemy as sa
from alembic import op

revision = "c5f3a4b5c6d7"
down_revision = "b4e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("promo_ends_at", sa.DateTime()))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("promo_ends_at")
