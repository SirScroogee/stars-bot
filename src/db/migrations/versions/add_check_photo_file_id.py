"""add_check_photo_file_id

Revision ID: add_check_photo_file_id
Revises: 19cd1e83e15d
Create Date: 2026-05-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_check_photo_file_id"
down_revision: Union[str, None] = "19cd1e83e15d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("photo_file_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "photo_file_id")
