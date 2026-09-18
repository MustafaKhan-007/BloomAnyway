"""Images attached to community posts and comments.

Revision ID: zn1u2v3w4x5
Revises: zm0t1u2v3w4
Create Date: 2026-09-18

"""
import sqlalchemy as sa
from alembic import op

revision = "zn1u2v3w4x5"
down_revision = "zm0t1u2v3w4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "forum_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("mime", sa.String(length=40), nullable=False,
                  server_default="image/jpeg"),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["forum_posts.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["comment_id"], ["forum_comments.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("forum_images") as batch:
        batch.create_index("ix_forum_images_post_id", ["post_id"])
        batch.create_index("ix_forum_images_comment_id", ["comment_id"])


def downgrade():
    with op.batch_alter_table("forum_images") as batch:
        batch.drop_index("ix_forum_images_comment_id")
        batch.drop_index("ix_forum_images_post_id")
    op.drop_table("forum_images")
