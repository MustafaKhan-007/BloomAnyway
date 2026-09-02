"""Free days before a membership's first charge.

Revision ID: za8h9i0j1k2
Revises: z9l7g8h9i0j1
Create Date: 2026-09-02

"""
import sqlalchemy as sa
from alembic import op

revision = "za8h9i0j1k2"
down_revision = "z9l7g8h9i0j1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("membership_plans") as batch:
        batch.add_column(sa.Column(
            "trial_days", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("membership_plans") as batch:
        batch.drop_column("trial_days")
