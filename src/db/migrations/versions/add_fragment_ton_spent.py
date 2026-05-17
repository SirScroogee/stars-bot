"""add_fragment_ton_spent

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-01-23 13:00:00.000000

Добавление поля fragment_ton_spent в таблицу orders для хранения
суммы TON потраченной на покупку через Fragment.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6g7h8i9j0k1'
down_revision: Union[str, None] = 'add_order_message_id'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('fragment_ton_spent', sa.Numeric(18, 9), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('orders', 'fragment_ton_spent')
