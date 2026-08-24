"""add durable order retry and alert tracking

Revision ID: order_reliability
Revises: giveaway_no_grace
Create Date: 2026-07-30 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "order_reliability"
down_revision: Union[str, None] = "giveaway_no_grace"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("last_error_code", sa.String(length=64), nullable=True))
    op.add_column(
        "orders",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("orders", sa.Column("processing_started_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("last_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("next_retry_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("last_fragment_account_id", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("admin_alerted_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("critical_alerted_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("user_delay_notified_at", sa.DateTime(), nullable=True))

    op.create_table(
        "order_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("fragment_account_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "attempt_number", name="uq_order_attempt_number"),
    )
    op.create_index(
        "ix_order_attempts_order_started",
        "order_attempts",
        ["order_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_orders_active_age",
        "orders",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_orders_active_age", table_name="orders")
    op.drop_index("ix_order_attempts_order_started", table_name="order_attempts")
    op.drop_table("order_attempts")
    op.drop_column("orders", "user_delay_notified_at")
    op.drop_column("orders", "critical_alerted_at")
    op.drop_column("orders", "admin_alerted_at")
    op.drop_column("orders", "last_fragment_account_id")
    op.drop_column("orders", "next_retry_at")
    op.drop_column("orders", "last_attempt_at")
    op.drop_column("orders", "processing_started_at")
    op.drop_column("orders", "retry_count")
    op.drop_column("orders", "attempt_count")
    op.drop_column("orders", "last_error_code")
