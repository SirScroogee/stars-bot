"""add_fragment_stel_dt

Revision ID: add_fragment_stel_dt
Revises: add_check_photo_file_id
Create Date: 2026-06-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_fragment_stel_dt"
down_revision: Union[str, None] = "add_check_photo_file_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fragment_accounts",
        sa.Column("stel_dt", sa.String(length=20), nullable=False, server_default="-300"),
    )


def downgrade() -> None:
    op.drop_column("fragment_accounts", "stel_dt")
