"""Add message_id field to orders table

Revision ID: add_order_message_id
Revises: add_fragment_accounts
Create Date: 2026-01-18 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_order_message_id'
down_revision: Union[str, None] = 'add_fragment_accounts'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('message_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'message_id')
