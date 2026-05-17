"""add_check_restrictions

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-01-02 12:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6g7h8'
down_revision: Union[str, None] = 'b2c3d4e5f6g7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add restriction fields to checks table
    op.add_column('checks', sa.Column('require_premium', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('checks', sa.Column('require_new_user', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('checks', 'require_new_user')
    op.drop_column('checks', 'require_premium')
