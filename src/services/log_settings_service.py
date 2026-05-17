"""
Сервис для управления настройками логирования в Telegram.
"""
import json
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Setting

logger = logging.getLogger(__name__)

# Ключ настроек в таблице settings
LOG_SETTINGS_KEY = "telegram_log_settings"

# Настройки по умолчанию
DEFAULT_LOG_SETTINGS = {
    "enabled": True,
    "group_id": -1003559150987,
    "topics": {
        "errors": {"id": 3, "enabled": True, "name": "Ошибки"},
        "payments": {"id": 5, "enabled": True, "name": "Оплаты"},
        "orders": {"id": 7, "enabled": True, "name": "Заказы"},
        "checks": {"id": 9, "enabled": True, "name": "Чеки"},
        "promo": {"id": 11, "enabled": True, "name": "Промокоды"},
        "admin": {"id": 15, "enabled": True, "name": "Админ"},
        "system": {"id": 17, "enabled": True, "name": "Система"},
        "users": {"id": 19, "enabled": True, "name": "Пользователи"},
    },
    "events": {
        # Ошибки
        "error": True,
        "order_error": True,
        # Оплаты (только успешные)
        "deposit": True,
        # Заказы (только успешные и ошибочные)
        "order_completed": True,
        "order_failed": True,
        # Чеки (только создание)
        "check_created": True,
        # Промокоды (только создание)
        "promo_created": True,
        # Админ (все важные действия)
        "admin_login": True,
        "admin_action": True,
        "user_banned": True,
        # Система
        "bot_started": True,
        "bot_stopped": True,
        # Пользователи (только регистрация)
        "user_registered": True,
    },
}


class LogSettingsService:
    """Сервис для работы с настройками логирования."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_settings(self) -> dict:
        """Получить настройки логирования."""
        result = await self._session.execute(
            select(Setting).where(Setting.key == LOG_SETTINGS_KEY)
        )
        setting = result.scalar_one_or_none()

        if not setting:
            return DEFAULT_LOG_SETTINGS.copy()

        try:
            settings = json.loads(setting.value)
            # Мержим с дефолтными, чтобы добавить новые ключи
            return self._merge_settings(DEFAULT_LOG_SETTINGS, settings)
        except json.JSONDecodeError:
            logger.error("Failed to parse log settings JSON")
            return DEFAULT_LOG_SETTINGS.copy()

    async def save_settings(self, settings: dict) -> bool:
        """Сохранить настройки логирования."""
        try:
            result = await self._session.execute(
                select(Setting).where(Setting.key == LOG_SETTINGS_KEY)
            )
            setting = result.scalar_one_or_none()

            json_value = json.dumps(settings, ensure_ascii=False)

            if setting:
                setting.value = json_value
            else:
                new_setting = Setting(
                    key=LOG_SETTINGS_KEY,
                    value=json_value,
                    description="Настройки логирования в Telegram группу",
                )
                self._session.add(new_setting)

            await self._session.flush()
            return True

        except Exception as e:
            logger.error(f"Failed to save log settings: {e}")
            return False

    async def toggle_logging(self, enabled: bool) -> bool:
        """Включить/выключить логирование."""
        settings = await self.get_settings()
        settings["enabled"] = enabled
        return await self.save_settings(settings)

    async def toggle_topic(self, topic_key: str, enabled: bool) -> bool:
        """Включить/выключить топик."""
        settings = await self.get_settings()
        if topic_key in settings["topics"]:
            settings["topics"][topic_key]["enabled"] = enabled
            return await self.save_settings(settings)
        return False

    async def toggle_event(self, event_key: str, enabled: bool) -> bool:
        """Включить/выключить событие."""
        settings = await self.get_settings()
        if event_key in settings["events"]:
            settings["events"][event_key] = enabled
            return await self.save_settings(settings)
        return False

    async def set_group_id(self, group_id: int) -> bool:
        """Установить ID группы."""
        settings = await self.get_settings()
        settings["group_id"] = group_id
        return await self.save_settings(settings)

    async def set_topic_id(self, topic_key: str, topic_id: int) -> bool:
        """Установить ID топика."""
        settings = await self.get_settings()
        if topic_key in settings["topics"]:
            settings["topics"][topic_key]["id"] = topic_id
            return await self.save_settings(settings)
        return False

    async def is_event_enabled(self, event_key: str) -> bool:
        """Проверить, включено ли событие."""
        settings = await self.get_settings()
        if not settings.get("enabled", True):
            return False
        return settings.get("events", {}).get(event_key, True)

    async def get_topic_id(self, topic_key: str) -> Optional[int]:
        """Получить ID топика (если включён)."""
        settings = await self.get_settings()

        if not settings.get("enabled", True):
            return None

        topic = settings.get("topics", {}).get(topic_key, {})
        if not topic.get("enabled", True):
            return None

        return topic.get("id")

    async def get_group_id(self) -> Optional[int]:
        """Получить ID группы."""
        settings = await self.get_settings()
        if not settings.get("enabled", True):
            return None
        return settings.get("group_id")

    def _merge_settings(self, default: dict, current: dict) -> dict:
        """Рекурсивно мержит настройки."""
        result = default.copy()
        for key, value in current.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_settings(result[key], value)
            else:
                result[key] = value
        return result


# Кэшированные настройки (обновляются периодически)
_cached_settings: Optional[dict] = None
_cache_valid = False


async def get_log_settings() -> dict:
    """Получить настройки логирования (с кэшированием)."""
    global _cached_settings, _cache_valid

    if _cache_valid and _cached_settings is not None:
        return _cached_settings

    from src.db.session import async_session_factory

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        _cached_settings = await service.get_settings()
        _cache_valid = True
        return _cached_settings


def invalidate_log_settings_cache():
    """Сбросить кэш настроек."""
    global _cache_valid
    _cache_valid = False
