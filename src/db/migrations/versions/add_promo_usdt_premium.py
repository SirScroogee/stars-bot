"""add_promo_usdt_premium

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-01-25 18:00:00.000000

Добавление полей bonus_usdt и bonus_premium в таблицу promo_codes
для поддержки промокодов на USDT баланс и Premium месяцы.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'h8i9j0k1l2m3'
down_revision: Union[str, None] = 'g7h8i9j0k1l2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'promo_codes',
        sa.Column('bonus_usdt', sa.Numeric(18, 2), nullable=False, server_default='0')
    )
    op.add_column(
        'promo_codes',
        sa.Column('bonus_premium', sa.Integer(), nullable=False, server_default='0')
    )


def downgrade() -> None:
    op.drop_column('promo_codes', 'bonus_usdt')
    op.drop_column('promo_codes', 'bonus_premium')
