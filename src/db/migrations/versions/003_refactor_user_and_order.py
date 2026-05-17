"""Refactor users and orders tables

- Remove first_name, last_name from users
- Change referrer_id to referrer_code in users
- Remove recipient_id, price_stars from orders

Revision ID: 003_refactor
Revises: 002_tx_metadata
Create Date: 2025-12-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_refactor"
down_revision: Union[str, None] = "002_tx_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Добавляем новую колонку referrer_code в users
    op.add_column("users", sa.Column("referrer_code", sa.String(32), nullable=True))

    # 2. Мигрируем данные: конвертируем referrer_id в referrer_code
    # Находим referral_code реферера и записываем в referrer_code
    op.execute("""
        UPDATE users u
        SET referrer_code = (
            SELECT referral_code FROM users r WHERE r.id = u.referrer_id
        )
        WHERE u.referrer_id IS NOT NULL
    """)

    # 3. Удаляем foreign key constraint для referrer_id
    op.drop_constraint("users_referrer_id_fkey", "users", type_="foreignkey")

    # 4. Удаляем старые колонки из users
    op.drop_column("users", "referrer_id")
    op.drop_column("users", "first_name")
    op.drop_column("users", "last_name")

    # 5. Удаляем колонки из orders
    op.drop_column("orders", "recipient_id")
    op.drop_column("orders", "price_stars")


def downgrade() -> None:
    # Восстанавливаем колонки в orders
    op.add_column("orders", sa.Column("price_stars", sa.Numeric(18, 2), nullable=True))
    op.add_column("orders", sa.Column("recipient_id", sa.BigInteger(), nullable=True))

    # Восстанавливаем колонки в users
    op.add_column("users", sa.Column("last_name", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("first_name", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("referrer_id", sa.BigInteger(), nullable=True))

    # Мигрируем данные обратно: referrer_code -> referrer_id
    op.execute("""
        UPDATE users u
        SET referrer_id = (
            SELECT id FROM users r WHERE r.referral_code = u.referrer_code
        )
        WHERE u.referrer_code IS NOT NULL
    """)

    # Восстанавливаем foreign key
    op.create_foreign_key(
        "users_referrer_id_fkey", "users", "users",
        ["referrer_id"], ["id"]
    )

    # Удаляем новую колонку
    op.drop_column("users", "referrer_code")
