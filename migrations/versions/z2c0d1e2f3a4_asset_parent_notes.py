"""written extracts can hang off one file instead of the whole module

Revision ID: z2c0d1e2f3a4
Revises: y1b9c0d1e2f3
"""
import sqlalchemy as sa
from alembic import op

revision = "z2c0d1e2f3a4"
down_revision = "y1b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("product_assets") as batch:
        batch.add_column(sa.Column(
            "parent_asset_id", sa.Integer(),
            # Named on purpose: SQLite has no ALTER, so batch mode rebuilds the
            # table and refuses to copy across a constraint it can't name.
            sa.ForeignKey("product_assets.id",
                          name="fk_product_assets_parent_asset_id",
                          ondelete="CASCADE"),
            nullable=True,
        ))
        batch.create_index("ix_product_assets_parent_asset_id",
                           ["parent_asset_id"])


def downgrade():
    # Notes only make sense attached to their file; loose in the module they
    # would read as stray extracts, so they go with the column.
    op.execute("DELETE FROM product_assets WHERE parent_asset_id IS NOT NULL")
    with op.batch_alter_table("product_assets") as batch:
        batch.drop_index("ix_product_assets_parent_asset_id")
        batch.drop_column("parent_asset_id")
