"""remember a buyer's answers in a fillable course PDF

Revision ID: b5c2d3e4f6a7
Revises: c5f3a4b5c6d7
"""
import sqlalchemy as sa
from alembic import op

revision = "b5c2d3e4f6a7"
down_revision = "c5f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    # JSON map of PDF form-field id -> value, so a fillable workbook keeps what
    # the buyer typed across sessions (per purchase, like reading progress).
    with op.batch_alter_table("course_progress") as batch:
        batch.add_column(sa.Column("form_data_json", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("course_progress") as batch:
        batch.drop_column("form_data_json")
