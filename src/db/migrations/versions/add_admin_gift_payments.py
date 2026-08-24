"""add Telegram Stars payments for admin gifts

Revision ID: add_admin_gift_payments
Revises: add_admin_gifts
Create Date: 2026-08-24 00:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_admin_gift_payments"
down_revision: Union[str, None] = "add_admin_gifts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "admin_gifts",
        sa.Column("controller_chat_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "admin_gifts",
        sa.Column("controller_message_id", sa.BigInteger(), nullable=True),
    )

    op.create_table(
        "admin_gift_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gift_attempt_id", sa.Integer(), nullable=False),
        sa.Column("invoice_payload", sa.String(length=128), nullable=False),
        sa.Column("requested_stars", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="invoice_pending",
            nullable=False,
        ),
        sa.Column("invoice_message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_payment_charge_id", sa.String(length=255), nullable=True),
        sa.Column("provider_payment_charge_id", sa.String(length=255), nullable=True),
        sa.Column("paid_stars", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("pre_checkout_at", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("refunded_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["gift_attempt_id"],
            ["admin_gifts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invoice_payload",
            name="uq_admin_gift_payments_payload",
        ),
        sa.UniqueConstraint(
            "telegram_payment_charge_id",
            name="uq_admin_gift_payments_telegram_charge",
        ),
    )
    op.create_index(
        "ix_admin_gift_payments_attempt_status",
        "admin_gift_payments",
        ["gift_attempt_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_admin_gift_payments_attempt_status",
        table_name="admin_gift_payments",
    )
    op.drop_table("admin_gift_payments")
    op.drop_column("admin_gifts", "controller_message_id")
    op.drop_column("admin_gifts", "controller_chat_id")
