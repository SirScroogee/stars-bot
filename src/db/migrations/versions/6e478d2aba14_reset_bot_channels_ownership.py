"""reset_bot_channels_ownership

Revision ID: 6e478d2aba14
Revises: 1725a2746111
Create Date: 2026-01-02 17:51:19.764258

Исправление проблемы с каналами:
- Из-за бага в bot_admin.py, added_by_user_id мог перезаписываться
  при повторном добавлении бота в канал другим пользователем
- Эта миграция сбрасывает все записи каналов, чтобы они не отображались
  ни у кого до правильного повторного добавления
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6e478d2aba14'
down_revision: Union[str, None] = '1725a2746111'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Сбрасываем added_by_user_id для всех каналов
    # После этого каналы не будут отображаться ни у кого
    # Когда пользователь добавит бота в канал заново, added_by_user_id установится правильно
    connection = op.get_bind()

    # Обнуляем владельца для всех каналов
    connection.execute(
        sa.text("UPDATE bot_channels SET added_by_user_id = NULL")
    )

    # Деактивируем все каналы - они активируются заново когда бот получит событие
    connection.execute(
        sa.text("UPDATE bot_channels SET is_active = FALSE")
    )


def downgrade() -> None:
    # Откат невозможен - мы не знаем предыдущие значения added_by_user_id
    pass
