"""allow administrator Gifts for users outside the bot database

Revision ID: external_gift_recipients
Revises: add_archived_gifts
Create Date: 2026-08-27 19:30:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "external_gift_recipients"
down_revision: Union[str, None] = "add_archived_gifts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "admin_gifts_recipient_id_fkey",
        "admin_gifts",
        type_="foreignkey",
    )


def downgrade() -> None:
    op.create_foreign_key(
        "admin_gifts_recipient_id_fkey",
        "admin_gifts",
        "users",
        ["recipient_id"],
        ["id"],
    )
