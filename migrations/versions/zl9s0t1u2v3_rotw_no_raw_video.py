"""Reel of the Week entries stop carrying a raw video.

Entering asked for the Instagram link, the share count and the raw file the
reel was cut from. The first two are what the entry is judged on — a reel
that has already travelled far enough to qualify is, by definition, one you
can go and watch. The file was a large upload nobody ever opened.

The four columns that held it go. The files themselves are left where they
are: no row points at them once this has run, which is exactly what
``reel_uploads.sweep_orphans()`` clears on its next pass.

Revision ID: zl9s0t1u2v3
Revises: zk8r9s0t1u2
Create Date: 2026-09-11

"""
import sqlalchemy as sa
from alembic import op

revision = "zl9s0t1u2v3"
down_revision = "zk8r9s0t1u2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("reel_submissions") as batch:
        batch.drop_column("disk_name")
        batch.drop_column("filename")
        batch.drop_column("mime")
        batch.drop_column("size")


def downgrade():
    # The columns come back empty. Whatever files they used to name will have
    # been swept as orphans by now, so there is nothing to point them at.
    with op.batch_alter_table("reel_submissions") as batch:
        batch.add_column(sa.Column("disk_name", sa.String(length=64)))
        batch.add_column(sa.Column("filename", sa.String(length=255)))
        batch.add_column(sa.Column("mime", sa.String(length=120),
                                   nullable=False, server_default="video/mp4"))
        batch.add_column(sa.Column("size", sa.Integer(), nullable=False,
                                   server_default="0"))
