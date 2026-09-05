"""A timezone somebody chose by hand, which the browser must not overwrite.

Revision ID: zd1k2l3m4n5
Revises: zc0j1k2l3m4
Create Date: 2026-09-05

"""
import sqlalchemy as sa
from alembic import op

revision = "zd1k2l3m4n5"
down_revision = "zc0j1k2l3m4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("timezone_pinned", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("timezone_pinned")
