"""a product can be several things at once, not just one

``products.type`` keeps the primary kind, so everything that reads a single
type carries on working. ``types_json`` holds the whole set when there is more
than one.

Revision ID: a3d1e2f3a4b5
Revises: z2c0d1e2f3a4
"""
import sqlalchemy as sa
from alembic import op

revision = "a3d1e2f3a4b5"
down_revision = "z2c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("types_json", sa.Text()))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("types_json")
