"""Add fragment_accounts table

Revision ID: add_fragment_accounts
Revises: 6e478d2aba14
Create Date: 2025-01-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "add_fragment_accounts"
down_revision: Union[str, None] = "e5f6g7h8i9j0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fragment_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("tonapi_key", sa.Text(), nullable=False),
        sa.Column("mnemonic_encrypted", sa.Text(), nullable=False),
        sa.Column("wallet_address", sa.String(100), nullable=True),
        sa.Column("fragment_hash", sa.String(100), nullable=False),
        sa.Column("stel_token_encrypted", sa.Text(), nullable=False),
        sa.Column("stel_ssid_encrypted", sa.Text(), nullable=False),
        sa.Column("stel_ton_token_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ton_balance", sa.Numeric(18, 6), nullable=True),
        sa.Column("last_session_check", sa.DateTime(), nullable=True),
        sa.Column("last_balance_check", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("total_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successful_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_ton_spent", sa.Numeric(18, 6), nullable=False, server_default="0.000000"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fragment_accounts_status", "fragment_accounts", ["status"])
    op.create_index("ix_fragment_accounts_is_active", "fragment_accounts", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_fragment_accounts_is_active", table_name="fragment_accounts")
    op.drop_index("ix_fragment_accounts_status", table_name="fragment_accounts")
    op.drop_table("fragment_accounts")
