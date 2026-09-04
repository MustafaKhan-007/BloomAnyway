"""A day a launch price goes back up to the compare-at price.

Revision ID: zb9i0j1k2l3
Revises: za8h9i0j1k2
Create Date: 2026-09-04

"""
import sqlalchemy as sa
from alembic import op

revision = "zb9i0j1k2l3"
down_revision = "za8h9i0j1k2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("price_reverts_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("price_reverts_at")
