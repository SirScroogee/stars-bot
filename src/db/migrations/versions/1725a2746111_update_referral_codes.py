"""update_referral_codes

Revision ID: 1725a2746111
Revises: c3d4e5f6g7h8
Create Date: 2026-01-02 13:33:03.810293

"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1725a2746111'
down_revision: Union[str, None] = 'c3d4e5f6g7h8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def generate_new_referral_code() -> str:
    """Генерировать новый реферальный код (7 символов, только буквы и цифры)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(7))


def upgrade() -> None:
    # Получаем соединение
    connection = op.get_bind()

    # Получаем всех пользователей
    result = connection.execute(sa.text("SELECT id, referral_code FROM users"))
    users = result.fetchall()

    # Множество для отслеживания уникальности новых кодов
    used_codes = set()

    for user_id, old_code in users:
        # Генерируем уникальный новый код
        new_code = generate_new_referral_code()
        while new_code in used_codes:
            new_code = generate_new_referral_code()
        used_codes.add(new_code)

        # Обновляем referral_code пользователя
        connection.execute(
            sa.text("UPDATE users SET referral_code = :new_code WHERE id = :user_id"),
            {"new_code": new_code, "user_id": user_id}
        )

        # Обновляем referrer_code у рефералов (те кто пришёл по старому коду)
        connection.execute(
            sa.text("UPDATE users SET referrer_code = :new_code WHERE referrer_code = :old_code"),
            {"new_code": new_code, "old_code": old_code}
        )


def downgrade() -> None:
    # Откат невозможен - старые коды не сохраняются
    pass
