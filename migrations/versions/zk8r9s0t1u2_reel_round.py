"""A week of Reel of the Week can hold more than one round.

Entries used to be one per member per week, so once a reel had been featured
the rest of the week was closed to everybody who had already entered. A round
now opens on Monday and again whenever a reel is featured, and an entry says
which round it belongs to. Everything already in the table is round 0 of its
week, which is what it was.

Revision ID: zk8r9s0t1u2
Revises: zj7q8r9s0t1
Create Date: 2026-09-10

"""
import sqlalchemy as sa
from alembic import op

revision = "zk8r9s0t1u2"
down_revision = "zj7q8r9s0t1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("reel_submissions") as batch:
        batch.add_column(sa.Column("round_key", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.create_index("ix_reel_submissions_round_key", ["round_key"])
        batch.drop_constraint("uq_reel_sub_user_week", type_="unique")
        batch.create_unique_constraint(
            "uq_reel_sub_user_round", ["user_id", "week_key", "round_key"])


def downgrade():
    # Two rounds in one week can leave a member with two entries, which the
    # old constraint has no room for. The later one goes.
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM reel_submissions WHERE id NOT IN ("
        "  SELECT MIN(id) FROM reel_submissions GROUP BY user_id, week_key)"
    ))
    with op.batch_alter_table("reel_submissions") as batch:
        batch.drop_constraint("uq_reel_sub_user_round", type_="unique")
        batch.create_unique_constraint("uq_reel_sub_user_week",
                                       ["user_id", "week_key"])
        batch.drop_index("ix_reel_submissions_round_key")
        batch.drop_column("round_key")
