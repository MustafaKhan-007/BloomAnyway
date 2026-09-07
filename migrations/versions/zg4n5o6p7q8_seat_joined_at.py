"""Note when a seat actually opened the room, so turnout is a real number.

Finishing a session used to mark every booked seat attended, which made a
session nobody came to look exactly like a full one. Seats keep their arrival
time now, and sessions that ran before this stamp existed are left alone
rather than guessed at.

Revision ID: zg4n5o6p7q8
Revises: zf3m4n5o6p7
Create Date: 2026-09-06

"""
import sqlalchemy as sa
from alembic import op

revision = "zg4n5o6p7q8"
down_revision = "zf3m4n5o6p7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("support_group_applications") as batch:
        batch.add_column(sa.Column("joined_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("support_group_applications") as batch:
        batch.drop_column("joined_at")
