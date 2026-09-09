"""A gift carries a note, and is only handed over once.

Orders already knew who a product was bought for. This is what the buyer
wrote to go with it, and the stamp that says both of them have been told —
so a replayed payment can't send the same gift twice.

Revision ID: zj7q8r9s0t1
Revises: zi6p7q8r9s0
Create Date: 2026-09-09

"""
import sqlalchemy as sa
from alembic import op

revision = "zj7q8r9s0t1"
down_revision = "zi6p7q8r9s0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("gift_note", sa.String(length=400),
                                   nullable=True))
        batch.add_column(sa.Column("gift_told_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("gift_told_at")
        batch.drop_column("gift_note")
