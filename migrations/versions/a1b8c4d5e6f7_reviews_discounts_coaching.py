"""reel reviews, discount codes, coaching requests

Revision ID: a1b8c4d5e6f7
Revises: f9c2b7e410a8
Create Date: 2026-07-19 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b8c4d5e6f7'
down_revision = 'f9c2b7e410a8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'reel_review_applications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('week_key', sa.Date(), nullable=False),
        sa.Column('reel_url', sa.String(length=500), nullable=False),
        sa.Column('disk_name', sa.String(length=64), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('mime', sa.String(length=120), nullable=False, server_default='video/mp4'),
        sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('selected', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'week_key', name='uq_reel_app_user_week'),
    )
    op.create_index('ix_reel_review_applications_user_id', 'reel_review_applications', ['user_id'])
    op.create_index('ix_reel_review_applications_week_key', 'reel_review_applications', ['week_key'])

    op.create_table(
        'reel_reviews',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=160), nullable=False),
        sa.Column('body', sa.Text(), nullable=False, server_default=''),
        sa.Column('review_disk_name', sa.String(length=64), nullable=True),
        sa.Column('review_mime', sa.String(length=120), nullable=True),
        sa.Column('review_filename', sa.String(length=255), nullable=True),
        sa.Column('published', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['reel_review_applications.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('application_id'),
    )

    op.create_table(
        'discount_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=60), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )

    op.create_table(
        'coaching_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False, server_default=''),
        sa.Column('preferred_times', sa.String(length=300), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_coaching_requests_user_id', 'coaching_requests', ['user_id'])


def downgrade():
    op.drop_index('ix_coaching_requests_user_id', table_name='coaching_requests')
    op.drop_table('coaching_requests')
    op.drop_table('discount_codes')
    op.drop_table('reel_reviews')
    op.drop_index('ix_reel_review_applications_week_key', table_name='reel_review_applications')
    op.drop_index('ix_reel_review_applications_user_id', table_name='reel_review_applications')
    op.drop_table('reel_review_applications')
