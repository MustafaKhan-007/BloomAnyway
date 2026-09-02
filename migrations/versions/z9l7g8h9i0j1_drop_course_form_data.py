"""Drop the fillable-PDF answers column; that reader never worked and is gone.

Revision ID: z9l7g8h9i0j1
Revises: z8k6f7g8h9i0
Create Date: 2026-09-02

"""
import sqlalchemy as sa
from alembic import op

revision = "z9l7g8h9i0j1"
down_revision = "z8k6f7g8h9i0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("course_progress") as batch:
        batch.drop_column("form_data_json")


def downgrade():
    with op.batch_alter_table("course_progress") as batch:
        batch.add_column(sa.Column("form_data_json", sa.Text(), nullable=True))
