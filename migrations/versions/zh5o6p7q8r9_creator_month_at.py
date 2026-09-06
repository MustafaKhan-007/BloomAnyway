"""Remember who has already been Creator of the Month.

The card is only a name, a handle and a photo in the settings table, so once
it changes there is nothing left to say whose it was. Keeping the date on the
member is what lets the picker pass over whoever has just had it.

Revision ID: zh5o6p7q8r9
Revises: zg4n5o6p7q8
Create Date: 2026-09-06

"""
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "zh5o6p7q8r9"
down_revision = "zg4n5o6p7q8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("creator_month_at", sa.DateTime(), nullable=True))

    # Whoever is on the card right now has had it — matched on the handle,
    # which is the only thing the card and a profile have in common.
    bind = op.get_bind()
    handle = bind.execute(sa.text(
        "SELECT value FROM settings WHERE key = 'creator_instagram'"
    )).scalar()
    handle = (handle or "").strip().lstrip("@").lower()
    if not handle:
        return
    rows = bind.execute(sa.text(
        "SELECT id, links_json FROM users WHERE deleted_at IS NULL "
        "AND links_json IS NOT NULL AND links_json <> ''"
    )).fetchall()
    for user_id, links_json in rows:
        if handle in (links_json or "").lower():
            bind.execute(
                sa.text("UPDATE users SET creator_month_at = :at WHERE id = :uid"),
                {"at": datetime.utcnow(), "uid": user_id},
            )
            break


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("creator_month_at")
