"""A bundle knows which products it hands over.

"Bundle" was only a badge on a product before this: it sold at its own price
and handed over whatever files were uploaded to it. This is where the list of
other products lives, so buying one can put each of them on the buyer's shelf.

Revision ID: zi6p7q8r9s0
Revises: zh5o6p7q8r9
Create Date: 2026-09-07

"""
import sqlalchemy as sa
from alembic import op

revision = "zi6p7q8r9s0"
down_revision = "zh5o6p7q8r9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("bundle_json", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("bundle_json")
