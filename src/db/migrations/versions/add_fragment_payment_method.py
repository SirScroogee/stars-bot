"""add_fragment_payment_method

Revision ID: add_fragment_payment_method
Revises: add_fragment_stel_dt
Create Date: 2026-06-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_fragment_payment_method"
down_revision: Union[str, None] = "add_fragment_stel_dt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fragment_accounts",
        sa.Column("payment_method", sa.String(length=20), nullable=False, server_default="ton"),
    )


def downgrade() -> None:
    op.drop_column("fragment_accounts", "payment_method")
