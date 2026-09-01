"""lessons inside a module: pin a content asset to a lesson

Revision ID: a4b1c2d3e4f5
Revises: c5f3a4b5c6d7
"""
import sqlalchemy as sa
from alembic import op

revision = "a4b1c2d3e4f5"
down_revision = "c5f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    # 1-based lesson number within a module. NULL = module-level content (a
    # module intro), shown before the lessons. The module (module_index) stays
    # the drip unit, so gating needs to know nothing about lessons.
    with op.batch_alter_table("product_assets") as batch:
        batch.add_column(sa.Column("lesson_index", sa.Integer(), nullable=True))
        batch.create_index("ix_product_assets_lesson_index", ["lesson_index"])


def downgrade():
    with op.batch_alter_table("product_assets") as batch:
        batch.drop_index("ix_product_assets_lesson_index")
        batch.drop_column("lesson_index")
