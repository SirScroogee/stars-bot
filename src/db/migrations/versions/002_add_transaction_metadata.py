"""Add metadata field to transactions

Revision ID: 002_tx_metadata
Revises: 001_initial
Create Date: 2025-12-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_tx_metadata"
down_revision: Union[str, None] = "1e9c7c6405c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("extra_data", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("transactions", "extra_data")
