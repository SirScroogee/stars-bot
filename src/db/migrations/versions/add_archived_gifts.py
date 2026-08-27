"""add administrator-managed archived Telegram Gifts

Revision ID: add_archived_gifts
Revises: allow_public_gift_invoice
Create Date: 2026-08-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_archived_gifts"
down_revision: Union[str, None] = "allow_public_gift_invoice"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "archived_gifts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gift_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("emoji", sa.String(length=32), nullable=True),
        sa.Column("star_count", sa.Integer(), nullable=False),
        sa.Column("sticker_file_id", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gift_id", name="uq_archived_gifts_gift_id"),
    )
    op.create_index(
        "ix_archived_gifts_active_title",
        "archived_gifts",
        ["is_active", "title"],
    )

    op.add_column(
        "admin_gifts",
        sa.Column(
            "gift_source",
            sa.String(length=20),
            server_default="live",
            nullable=False,
        ),
    )
    op.add_column(
        "admin_gifts",
        sa.Column("gift_title_snapshot", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "admin_gifts",
        sa.Column("archived_gift_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_admin_gifts_archived_gift_id",
        "admin_gifts",
        "archived_gifts",
        ["archived_gift_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_admin_gifts_archived_gift_id",
        "admin_gifts",
        type_="foreignkey",
    )
    op.drop_column("admin_gifts", "archived_gift_id")
    op.drop_column("admin_gifts", "gift_title_snapshot")
    op.drop_column("admin_gifts", "gift_source")
    op.drop_index("ix_archived_gifts_active_title", table_name="archived_gifts")
    op.drop_table("archived_gifts")
