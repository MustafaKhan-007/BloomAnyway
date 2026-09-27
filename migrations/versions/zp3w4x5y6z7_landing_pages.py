"""Landing pages the owner builds in Studio, with a draft and a live copy.

Revision ID: zp3w4x5y6z7
Revises: zo2v3w4x5y6
Create Date: 2026-09-28

``published_json`` starts empty and means "not on the site". A page is
therefore invisible from the moment it is created until somebody presses
Publish, which is what lets a half-built page sit there for a week without
anyone having to hide it first.
"""
import sqlalchemy as sa
from alembic import op

revision = "zp3w4x5y6z7"
down_revision = "zo2v3w4x5y6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "landing_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("published_json", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_landing_pages_slug"),
    )
    with op.batch_alter_table("landing_pages") as batch:
        batch.create_index("ix_landing_pages_slug", ["slug"])


def downgrade():
    with op.batch_alter_table("landing_pages") as batch:
        batch.drop_index("ix_landing_pages_slug")
    op.drop_table("landing_pages")
