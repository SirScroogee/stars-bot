"""add_transaction_external_id

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-01-13 12:00:00.000000

Добавление поля external_id в таблицу transactions для идемпотентности:
- external_id: внешний ID транзакции (event_id для TON, invoice_id для CryptoBot)
- Уникальный индекс предотвращает двойное зачисление одной и той же транзакции
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6g7h8i9j0'
down_revision: Union[str, None] = 'd4e5f6g7h8i9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add external_id column to transactions table
    op.add_column(
        'transactions',
        sa.Column('external_id', sa.String(255), nullable=True)
    )
    # Create unique index for idempotency
    op.create_index(
        'ix_transactions_external_id',
        'transactions',
        ['external_id'],
        unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_transactions_external_id', table_name='transactions')
    op.drop_column('transactions', 'external_id')
