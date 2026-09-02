"""Fixed release date for module one, and a date a product stops selling.

Revision ID: z7j5e6f7g8h9
Revises: 749cea858616
Create Date: 2026-09-02

"""
import sqlalchemy as sa
from alembic import op

revision = "z7j5e6f7g8h9"
down_revision = "749cea858616"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("drip_starts_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("off_shelf_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("off_shelf_at")
        batch.drop_column("drip_starts_at")
