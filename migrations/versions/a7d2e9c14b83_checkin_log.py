"""per-day check-in log (for the My Journey export)

Revision ID: a7d2e9c14b83
Revises: f3c81d0a5b62
Create Date: 2026-07-15 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7d2e9c14b83'
down_revision = 'f3c81d0a5b62'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'check_ins',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'day', name='uq_checkin_user_day'),
    )
    with op.batch_alter_table('check_ins', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_check_ins_user_id'), ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('check_ins', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_check_ins_user_id'))
    op.drop_table('check_ins')
