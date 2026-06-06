"""add_platega_payments

Revision ID: add_platega_payments
Revises: add_fragment_payment_method
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_platega_payments"
down_revision: Union[str, None] = "add_fragment_payment_method"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platega_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("operation_type", sa.String(length=20), nullable=False),
        sa.Column("provider_tx_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("amount_usdt", sa.Numeric(18, 6), nullable=False),
        sa.Column("amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("fee_percent", sa.Numeric(8, 4), nullable=False, server_default="0"),
        sa.Column("payment_url", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_tx_id"),
    )
    op.create_index("ix_platega_payments_provider_tx_id", "platega_payments", ["provider_tx_id"])
    op.create_index("ix_platega_payments_status_created", "platega_payments", ["status", "created_at"])
    op.create_index("ix_platega_payments_user_id", "platega_payments", ["user_id"])
    op.create_index("ix_platega_payments_user_status", "platega_payments", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_platega_payments_user_status", table_name="platega_payments")
    op.drop_index("ix_platega_payments_user_id", table_name="platega_payments")
    op.drop_index("ix_platega_payments_status_created", table_name="platega_payments")
    op.drop_index("ix_platega_payments_provider_tx_id", table_name="platega_payments")
    op.drop_table("platega_payments")
