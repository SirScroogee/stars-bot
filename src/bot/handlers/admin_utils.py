"""
Общие утилиты для админ-панели.
"""
from datetime import datetime, timedelta, timezone

from aiogram.types import CallbackQuery, Message

from src.db.session import async_session_factory
from src.services.user_service import UserService

# Московское время (UTC+3)
MOSCOW_TZ = timezone(timedelta(hours=3))


def to_moscow_time(dt: datetime) -> datetime:
    """Конвертировать UTC время в московское (UTC+3)."""
    if dt is None:
        return None
    # Если datetime без timezone, считаем что это UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MOSCOW_TZ)


def format_percent(value: str) -> str:
    """Форматировать процент - убрать .0 если целое число."""
    try:
        float_val = float(value)
        if float_val == int(float_val):
            return str(int(float_val))
        return str(float_val)
    except (ValueError, TypeError):
        return value


async def check_admin(callback: CallbackQuery) -> bool:
    """Проверить права администратора (для callback)."""
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(callback.from_user.id)
        if not db_user or not db_user.is_admin:
            await callback.answer("У вас нет прав администратора", show_alert=True)
            return False
    return True


async def check_admin_message(message: Message) -> bool:
    """Проверить права администратора (для message)."""
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(message.from_user.id)
        if not db_user or not db_user.is_admin:
            await message.answer("У вас нет прав администратора")
            return False
    return True
