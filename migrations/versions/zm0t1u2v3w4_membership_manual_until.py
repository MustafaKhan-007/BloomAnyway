"""An optional expiry for a hand-set membership tier.

Revision ID: zm0t1u2v3w4
Revises: zl9s0t1u2v3
Create Date: 2026-09-17

"""
import sqlalchemy as sa
from alembic import op

revision = "zm0t1u2v3w4"
down_revision = "zl9s0t1u2v3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("membership_manual_until", sa.DateTime(),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("membership_manual_until")
