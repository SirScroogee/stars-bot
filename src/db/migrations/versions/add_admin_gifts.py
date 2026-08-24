"""add admin gifts audit table

Revision ID: add_admin_gifts
Revises: add_lava_payments
Create Date: 2026-08-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_admin_gifts"
down_revision: Union[str, None] = "add_lava_payments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_gifts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("admin_username_snapshot", sa.String(length=255), nullable=True),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_username_snapshot", sa.String(length=255), nullable=True),
        sa.Column("recipient_was_banned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("gift_id", sa.String(length=255), nullable=False),
        sa.Column("gift_emoji", sa.String(length=32), nullable=True),
        sa.Column("gift_star_count", sa.Integer(), nullable=False),
        sa.Column("pay_for_upgrade", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("gift_text", sa.String(length=128), nullable=False),
        sa.Column("bot_balance_before", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["recipient_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_key", name="uq_admin_gifts_operation_key"),
    )
    op.create_index(
        "ix_admin_gifts_recipient_created", "admin_gifts", ["recipient_id", "created_at"]
    )
    op.create_index(
        "ix_admin_gifts_admin_created", "admin_gifts", ["admin_id", "created_at"]
    )
    op.create_index(
        "ix_admin_gifts_status_created", "admin_gifts", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_admin_gifts_status_created", table_name="admin_gifts")
    op.drop_index("ix_admin_gifts_admin_created", table_name="admin_gifts")
    op.drop_index("ix_admin_gifts_recipient_created", table_name="admin_gifts")
    op.drop_table("admin_gifts")
