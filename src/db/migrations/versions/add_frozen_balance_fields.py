"""add_frozen_balance_fields

Revision ID: a1b2c3d4e5f6
Revises: 98ce8ee64640
Create Date: 2026-01-01 23:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '98ce8ee64640'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add frozen balance fields to users table
    op.add_column('users', sa.Column('frozen_stars', sa.Numeric(precision=18, scale=2), nullable=False, server_default='0.00'))
    op.add_column('users', sa.Column('frozen_usdt', sa.Numeric(precision=18, scale=6), nullable=False, server_default='0.000000'))
    op.add_column('users', sa.Column('frozen_premium_months', sa.Integer(), nullable=False, server_default='0'))

    # Add payment method and frozen amounts to checks table
    op.add_column('checks', sa.Column('payment_method', sa.String(length=20), nullable=False, server_default='balance'))
    op.add_column('checks', sa.Column('frozen_usdt', sa.Numeric(precision=18, scale=6), nullable=False, server_default='0.000000'))
    op.add_column('checks', sa.Column('frozen_stars', sa.Numeric(precision=18, scale=2), nullable=False, server_default='0.00'))
    op.add_column('checks', sa.Column('frozen_premium_months', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    # Remove frozen fields from checks table
    op.drop_column('checks', 'frozen_premium_months')
    op.drop_column('checks', 'frozen_stars')
    op.drop_column('checks', 'frozen_usdt')
    op.drop_column('checks', 'payment_method')

    # Remove frozen balance fields from users table
    op.drop_column('users', 'frozen_premium_months')
    op.drop_column('users', 'frozen_usdt')
    op.drop_column('users', 'frozen_stars')
