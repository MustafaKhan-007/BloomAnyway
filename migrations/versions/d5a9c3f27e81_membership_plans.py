"""standalone membership plans (not products)

Revision ID: d5a9c3f27e81
Revises: c4f7a2e91b53
Create Date: 2026-07-15 14:35:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd5a9c3f27e81'
down_revision = 'c4f7a2e91b53'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'membership_plans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tier', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('tagline', sa.String(length=160), nullable=True),
        sa.Column('price_cents', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='USD'),
        sa.Column('period', sa.String(length=20), nullable=False, server_default='month'),
        sa.Column('ls_variant_id', sa.String(length=40), nullable=True),
        sa.Column('ls_checkout_url', sa.String(length=500), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tier'),
    )
    op.create_index('ix_membership_plans_ls_variant_id', 'membership_plans',
                    ['ls_variant_id'], unique=False)

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('grants_membership')


def downgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('grants_membership', sa.String(length=20), nullable=True))
    op.drop_index('ix_membership_plans_ls_variant_id', table_name='membership_plans')
    op.drop_table('membership_plans')
