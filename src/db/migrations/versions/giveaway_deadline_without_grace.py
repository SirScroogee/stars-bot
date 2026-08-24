"""make giveaway end time the actual draw deadline

Revision ID: giveaway_no_grace
Revises: add_giveaways
Create Date: 2026-07-29 15:45:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "giveaway_no_grace"
down_revision: Union[str, None] = "add_giveaways"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE giveaways SET grace_minutes = 0 "
        "WHERE status IN ('scheduled', 'active', 'drawing')"
    )
    op.alter_column(
        "giveaways",
        "grace_minutes",
        existing_type=sa.Integer(),
        server_default=sa.text("0"),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "giveaways",
        "grace_minutes",
        existing_type=sa.Integer(),
        server_default=sa.text("15"),
        existing_nullable=False,
    )
