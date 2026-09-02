"""How a drip-fed course is scheduled: same interval, own dates, or own gaps.

Revision ID: z8k6f7g8h9i0
Revises: z7j5e6f7g8h9
Create Date: 2026-09-02

"""
import sqlalchemy as sa
from alembic import op

revision = "z8k6f7g8h9i0"
down_revision = "z7j5e6f7g8h9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column(
            "drip_mode", sa.String(length=12), nullable=False,
            server_default="interval"))


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_column("drip_mode")
