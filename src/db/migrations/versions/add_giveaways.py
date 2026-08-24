"""add giveaways

Revision ID: add_giveaways
Revises: add_platega_payments
Create Date: 2026-07-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_giveaways"
down_revision: Union[str, None] = "add_platega_payments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "giveaways",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("photo_file_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="scheduled", nullable=False),
        sa.Column("participation_mode", sa.String(length=30), nullable=False),
        sa.Column("product_filter", sa.String(length=20), nullable=True),
        sa.Column("tickets_per_order", sa.Integer(), server_default="1", nullable=False),
        sa.Column("stars_per_ticket", sa.Integer(), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("grace_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("publish_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("publish_announcement", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("publish_results", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("announcement_message_id", sa.BigInteger(), nullable=True),
        sa.Column("results_message_id", sa.BigInteger(), nullable=True),
        sa.Column("announcement_last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("results_last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("announcement_error", sa.Text(), nullable=True),
        sa.Column("results_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("audit_json", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_giveaways_status_starts", "giveaways", ["status", "starts_at"])
    op.create_index("ix_giveaways_status_ends", "giveaways", ["status", "ends_at"])

    op.create_table(
        "giveaway_prizes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("giveaway_id", sa.Integer(), nullable=False),
        sa.Column("place", sa.Integer(), nullable=False),
        sa.Column("prize_type", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_issued", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("issued_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["giveaway_id"], ["giveaways.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("giveaway_id", "place", name="uq_giveaway_prize_place"),
    )

    op.create_table(
        "giveaway_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("giveaway_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("tickets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("purchase_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stars_purchased", sa.Integer(), server_default="0", nullable=False),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("join_notified_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["giveaway_id"], ["giveaways.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_entry_user"),
    )
    op.create_index("ix_giveaway_entries_giveaway_tickets", "giveaway_entries", ["giveaway_id", "tickets"])
    op.create_index("ix_giveaway_entries_user", "giveaway_entries", ["user_id"])

    op.create_table(
        "giveaway_entry_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("giveaway_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("tickets_awarded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("order_quantity", sa.Integer(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["giveaway_id"], ["giveaways.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("giveaway_id", "order_id", name="uq_giveaway_entry_order"),
    )
    op.create_index("ix_giveaway_entry_orders_order", "giveaway_entry_orders", ["order_id"])
    op.create_index("ix_giveaway_entry_orders_notify", "giveaway_entry_orders", ["notified_at", "tickets_awarded"])

    op.create_table(
        "giveaway_winners",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("giveaway_id", sa.Integer(), nullable=False),
        sa.Column("prize_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("place", sa.Integer(), nullable=False),
        sa.Column("tickets_snapshot", sa.Integer(), nullable=False),
        sa.Column("random_value", sa.BigInteger(), nullable=False),
        sa.Column("total_weight_before", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["giveaway_id"], ["giveaways.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["prize_id"], ["giveaway_prizes.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prize_id"),
        sa.UniqueConstraint("giveaway_id", "place", name="uq_giveaway_winner_place"),
        sa.UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_winner_user"),
    )
    op.create_index("ix_giveaway_winners_user", "giveaway_winners", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_giveaway_winners_user", table_name="giveaway_winners")
    op.drop_table("giveaway_winners")
    op.drop_index("ix_giveaway_entry_orders_notify", table_name="giveaway_entry_orders")
    op.drop_index("ix_giveaway_entry_orders_order", table_name="giveaway_entry_orders")
    op.drop_table("giveaway_entry_orders")
    op.drop_index("ix_giveaway_entries_user", table_name="giveaway_entries")
    op.drop_index("ix_giveaway_entries_giveaway_tickets", table_name="giveaway_entries")
    op.drop_table("giveaway_entries")
    op.drop_table("giveaway_prizes")
    op.drop_index("ix_giveaways_status_ends", table_name="giveaways")
    op.drop_index("ix_giveaways_status_starts", table_name="giveaways")
    op.drop_table("giveaways")
