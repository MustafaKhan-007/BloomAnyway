"""product assets (uploaded PDF/Word files for the on-site reader)

Revision ID: e2b6c9147a55
Revises: d1a4f6b82c30
Create Date: 2026-07-11 03:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e2b6c9147a55'
down_revision = 'd1a4f6b82c30'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'product_assets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=160), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('mime', sa.String(length=120), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('product_assets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_product_assets_product_id'),
                              ['product_id'], unique=False)


def downgrade():
    with op.batch_alter_table('product_assets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_product_assets_product_id'))
    op.drop_table('product_assets')
