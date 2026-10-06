"""Booked a seat, didn't turn up — the row the time-out is counted from.

Revision ID: zt7b8c9d0e1
Revises: zs6a7b8c9d0
Create Date: 2026-10-06

Unique on ``application_id`` on purpose: the sweep that settles a finished
session runs opportunistically off whatever request happens to arrive, and
more than one can reach the same session. One miss is one row.

Nothing is backfilled. Seats already settled as ``no_show`` stay as they are
and earn nobody a strike — the rule did not exist when they missed, and
starting with a handful of members already two steps up a ladder they were
never told about is not the way to introduce one.
"""
import sqlalchemy as sa
from alembic import op

revision = "zt7b8c9d0e1"
down_revision = "zs6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "support_group_no_shows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=True),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(length=10), nullable=False,
                  server_default="seat"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("forgiven_at", sa.DateTime(), nullable=True),
        sa.Column("forgiven_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["meeting_id"], ["support_group_meetings.id"]),
        sa.ForeignKeyConstraint(["application_id"],
                                ["support_group_applications.id"]),
        sa.ForeignKeyConstraint(["forgiven_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", name="uq_no_show_application"),
    )
    with op.batch_alter_table("support_group_no_shows") as batch:
        batch.create_index("ix_support_group_no_shows_user_id", ["user_id"])
        batch.create_index("ix_support_group_no_shows_meeting_id",
                           ["meeting_id"])
        batch.create_index("ix_support_group_no_shows_application_id",
                           ["application_id"])
        batch.create_index("ix_support_group_no_shows_created_at",
                           ["created_at"])


def downgrade():
    with op.batch_alter_table("support_group_no_shows") as batch:
        batch.drop_index("ix_support_group_no_shows_created_at")
        batch.drop_index("ix_support_group_no_shows_application_id")
        batch.drop_index("ix_support_group_no_shows_meeting_id")
        batch.drop_index("ix_support_group_no_shows_user_id")
    op.drop_table("support_group_no_shows")
