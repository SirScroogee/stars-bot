"""Initial migration

Revision ID: 001_initial
Revises:
Create Date: 2025-01-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Users
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("language_code", sa.String(10), nullable=False, server_default="ru"),
        sa.Column("balance_stars", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("balance_usdt", sa.Numeric(18, 6), nullable=False, server_default="0.000000"),
        sa.Column("referrer_id", sa.BigInteger(), nullable=True),
        sa.Column("referral_code", sa.String(32), nullable=False),
        sa.Column("is_banned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["referrer_id"], ["users.id"]),
        sa.UniqueConstraint("referral_code"),
    )

    # Orders
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_key", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_username", sa.String(255), nullable=False),
        sa.Column("product_type", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("price_stars", sa.Numeric(18, 2), nullable=False),
        sa.Column("price_usdt", sa.Numeric(18, 6), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("payment_provider", sa.String(20), nullable=False),
        sa.Column("fragment_tx_id", sa.String(255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("order_key"),
    )
    op.create_index("ix_orders_order_key", "orders", ["order_key"])
    op.create_index("ix_orders_user_status", "orders", ["user_id", "status"])
    op.create_index("ix_orders_created_at", "orders", ["created_at"])

    # Payments
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_tx_id", sa.String(255), nullable=True),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("webhook_data", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.UniqueConstraint("order_id"),
    )

    # Transactions
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("amount_stars", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("amount_usdt", sa.Numeric(18, 6), nullable=False, server_default="0.000000"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
    )
    op.create_index("ix_transactions_user_type", "transactions", ["user_id", "type"])
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])

    # Balance Ledger
    op.create_table(
        "balance_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("operation", sa.String(10), nullable=False),
        sa.Column("amount_stars", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("amount_usdt", sa.Numeric(18, 6), nullable=False, server_default="0.000000"),
        sa.Column("balance_stars_before", sa.Numeric(18, 2), nullable=False),
        sa.Column("balance_stars_after", sa.Numeric(18, 2), nullable=False),
        sa.Column("balance_usdt_before", sa.Numeric(18, 6), nullable=False),
        sa.Column("balance_usdt_after", sa.Numeric(18, 6), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
    )
    op.create_index("ix_balance_ledger_user_id", "balance_ledger", ["user_id"])
    op.create_index("ix_balance_ledger_created_at", "balance_ledger", ["created_at"])

    # Checks
    op.create_table(
        "checks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_stars", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("max_activations", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_activations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"]),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_checks_code", "checks", ["code"])

    # Check Activations
    op.create_table(
        "check_activations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_received", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["check_id"], ["checks.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("check_id", "user_id", name="uq_check_user"),
    )

    # Promo Codes
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("bonus_stars", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("bonus_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("current_uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_promo_codes_code", "promo_codes", ["code"])

    # Promo Uses
    op.create_table(
        "promo_uses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("promo_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("bonus_applied", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["promo_id"], ["promo_codes.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.UniqueConstraint("promo_id", "user_id", name="uq_promo_user"),
    )

    # Referral Earnings
    op.create_table(
        "referral_earnings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("referrer_id", sa.BigInteger(), nullable=False),
        sa.Column("referee_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("percent", sa.Integer(), nullable=False),
        sa.Column("amount_stars", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["referrer_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["referee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
    )
    op.create_index("ix_referral_earnings_referrer", "referral_earnings", ["referrer_id"])
    op.create_index("ix_referral_earnings_created_at", "referral_earnings", ["created_at"])

    # Audit Logs
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("admin_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # Settings
    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("audit_logs")
    op.drop_table("referral_earnings")
    op.drop_table("promo_uses")
    op.drop_table("promo_codes")
    op.drop_table("check_activations")
    op.drop_table("checks")
    op.drop_table("balance_ledger")
    op.drop_table("transactions")
    op.drop_table("payments")
    op.drop_table("orders")
    op.drop_table("users")