"""A membership perk can run to a date, not only for a number of months.

Revision ID: zf3m4n5o6p7
Revises: ze2l3m4n5o6
Create Date: 2026-09-06

"""
import sqlalchemy as sa
from alembic import op

revision = "zf3m4n5o6p7"
down_revision = "ze2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("perk_starts_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("perk_ends_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("perk_ends_at")
        batch.drop_column("perk_starts_at")
