"""What a product's receipt email says it is.

Revision ID: zc0j1k2l3m4
Revises: zb9i0j1k2l3
Create Date: 2026-09-04

"""
import sqlalchemy as sa
from alembic import op

revision = "zc0j1k2l3m4"
down_revision = "zb9i0j1k2l3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("receipt_description", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("receipt_description")
