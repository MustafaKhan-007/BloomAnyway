"""forum tags, one-level comment replies, uploaded avatars

Revision ID: c9e5a7b30f18
Revises: b7f3c1a9d2e4
Create Date: 2026-07-07 20:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9e5a7b30f18'
down_revision = 'b7f3c1a9d2e4'
branch_labels = None
depends_on = None


def upgrade():
    # --- forum topic tags ----------------------------------------------------
    op.create_table(
        'forum_tags',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('slug', sa.String(length=60), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['forum_categories.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category_id', 'slug', name='uq_tag_category_slug'),
    )
    with op.batch_alter_table('forum_tags', schema=None) as batch_op:
        batch_op.create_index('ix_forum_tags_category_id', ['category_id'], unique=False)

    # --- posts get an optional tag ------------------------------------------
    with op.batch_alter_table('forum_posts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tag_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_forum_posts_tag_id', 'forum_tags', ['tag_id'], ['id'])
        batch_op.create_index('ix_forum_posts_tag_id', ['tag_id'], unique=False)

    # --- comments get one level of replies ----------------------------------
    with op.batch_alter_table('forum_comments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_forum_comments_parent_id', 'forum_comments', ['parent_id'], ['id'])
        batch_op.create_index('ix_forum_comments_parent_id', ['parent_id'], unique=False)

    # --- uploaded avatars (stored in DB so they survive deploys) ------------
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('avatar_data', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('avatar_mime', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('avatar_mime')
        batch_op.drop_column('avatar_data')

    with op.batch_alter_table('forum_comments', schema=None) as batch_op:
        batch_op.drop_index('ix_forum_comments_parent_id')
        batch_op.drop_constraint('fk_forum_comments_parent_id', type_='foreignkey')
        batch_op.drop_column('parent_id')

    with op.batch_alter_table('forum_posts', schema=None) as batch_op:
        batch_op.drop_index('ix_forum_posts_tag_id')
        batch_op.drop_constraint('fk_forum_posts_tag_id', type_='foreignkey')
        batch_op.drop_column('tag_id')

    with op.batch_alter_table('forum_tags', schema=None) as batch_op:
        batch_op.drop_index('ix_forum_tags_category_id')
    op.drop_table('forum_tags')
