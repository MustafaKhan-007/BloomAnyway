"""Remember that a member's reel was once on the home page.

Reel of the Week entries are cleared out every Monday, so the only lasting
record of having been featured is this stamp. Anyone still marked as featured
when this runs is carried over; earlier weeks are already gone.

Revision ID: ze2l3m4n5o6
Revises: zd1k2l3m4n5
Create Date: 2026-09-06

"""
import sqlalchemy as sa
from alembic import op

revision = "ze2l3m4n5o6"
down_revision = "zd1k2l3m4n5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("reel_featured_at", sa.DateTime(), nullable=True))

    bind = op.get_bind()
    if "reel_submissions" not in sa.inspect(bind).get_table_names():
        return
    # One featured entry a week at most, so this is a handful of rows.
    carried = bind.execute(sa.text(
        "SELECT user_id, MIN(created_at) FROM reel_submissions "
        "WHERE featured GROUP BY user_id"
    )).fetchall()
    for user_id, first_at in carried:
        bind.execute(
            sa.text("UPDATE users SET reel_featured_at = :at WHERE id = :uid"),
            {"at": first_at, "uid": user_id},
        )


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("reel_featured_at")
