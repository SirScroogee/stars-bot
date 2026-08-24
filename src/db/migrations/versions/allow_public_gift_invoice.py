"""allow any Telegram user to fund an admin gift invoice

Revision ID: allow_public_gift_invoice
Revises: add_admin_gift_payments
Create Date: 2026-08-24 02:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "allow_public_gift_invoice"
down_revision: Union[str, None] = "add_admin_gift_payments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Payers do not have to be registered bot users, so these deliberately have
    # no foreign keys to the users table.
    op.add_column(
        "admin_gift_payments",
        sa.Column("pre_checkout_payer_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "admin_gift_payments",
        sa.Column("pre_checkout_query_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "admin_gift_payments",
        sa.Column("payer_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("admin_gift_payments", "payer_id")
    op.drop_column("admin_gift_payments", "pre_checkout_query_id")
    op.drop_column("admin_gift_payments", "pre_checkout_payer_id")
