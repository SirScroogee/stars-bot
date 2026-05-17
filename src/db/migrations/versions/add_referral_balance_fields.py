"""add_referral_balance_fields

Revision ID: d4e5f6g7h8i9
Revises: 6e478d2aba14
Create Date: 2026-01-04 12:00:00.000000

Добавление полей для реферального баланса:
- referral_balance: текущий доступный реферальный баланс для вывода
- total_referral_earnings: общий заработок с рефералов за всё время
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6g7h8i9'
down_revision: Union[str, None] = '6e478d2aba14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add referral balance fields to users table
    op.add_column('users', sa.Column('referral_balance', sa.Numeric(precision=18, scale=6), nullable=False, server_default='0.000000'))
    op.add_column('users', sa.Column('total_referral_earnings', sa.Numeric(precision=18, scale=6), nullable=False, server_default='0.000000'))


def downgrade() -> None:
    op.drop_column('users', 'total_referral_earnings')
    op.drop_column('users', 'referral_balance')
