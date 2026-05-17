"""
Middleware для проверки забаненных пользователей.
"""
import logging
import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery, InlineQuery

from src.db.session import async_session_factory
from src.services.user_service import UserService
from src.locales import t

logger = logging.getLogger(__name__)

# Кэш бан-статуса: user_id -> (is_banned, language_code, timestamp)
_ban_cache: Dict[int, tuple[bool, str, float]] = {}
_CACHE_TTL = 60.0  # Время жизни кэша в секундах


def _get_cached_ban_status(user_id: int) -> tuple[bool, str] | None:
    """Получить бан-статус из кэша если не устарел."""
    if user_id in _ban_cache:
        is_banned, lang, cached_at = _ban_cache[user_id]
        if time.time() - cached_at < _CACHE_TTL:
            return is_banned, lang
        # Кэш устарел - удаляем
        del _ban_cache[user_id]
    return None


def _set_cached_ban_status(user_id: int, is_banned: bool, lang: str) -> None:
    """Сохранить бан-статус в кэш."""
    _ban_cache[user_id] = (is_banned, lang, time.time())


def invalidate_ban_cache(user_id: int) -> None:
    """Инвалидировать кэш для пользователя (вызывать при бане/разбане)."""
    _ban_cache.pop(user_id, None)


class BanCheckMiddleware(BaseMiddleware):
    """
    Middleware проверяет, забанен ли пользователь.
    Если забанен - отправляет сообщение о блокировке.

    Поддерживает: Message, CallbackQuery, InlineQuery.
    Использует кэширование для снижения нагрузки на БД.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Получаем user_id из события
        user_id = None

        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else None
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else None
        elif isinstance(event, InlineQuery):
            user_id = event.from_user.id if event.from_user else None

        # Если не удалось получить user_id - пропускаем
        if not user_id:
            return await handler(event, data)

        # Проверяем кэш
        cached = _get_cached_ban_status(user_id)
        if cached is not None:
            is_banned, lang = cached
            if is_banned:
                return await self._handle_banned_user(event, lang)
            return await handler(event, data)

        # Проверяем бан в БД
        async with async_session_factory() as session:
            user_service = UserService(session)
            user = await user_service.get_user(user_id)

            if user:
                lang = user.language_code or "ru"
                is_banned = user.is_banned
                # Кэшируем результат
                _set_cached_ban_status(user_id, is_banned, lang)

                if is_banned:
                    return await self._handle_banned_user(event, lang)
            else:
                # Пользователь не найден - не забанен
                _set_cached_ban_status(user_id, False, "ru")

        # Пользователь не забанен - продолжаем обработку
        return await handler(event, data)

    async def _handle_banned_user(
        self,
        event: TelegramObject,
        lang: str,
    ) -> None:
        """Обработать запрос от забаненного пользователя."""
        banned_text = t("errors.banned", lang)

        if isinstance(event, Message):
            await event.answer(banned_text)
        elif isinstance(event, CallbackQuery):
            # Редактируем сообщение вместо показа alert
            try:
                await event.message.edit_text(banned_text)
            except Exception as e:
                logger.warning(f"Failed to edit message for banned user: {e}")
            await event.answer()
        elif isinstance(event, InlineQuery):
            # Для inline query показываем пустой результат с сообщением
            try:
                from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
                await event.answer(
                    results=[
                        InlineQueryResultArticle(
                            id="banned",
                            title=banned_text,
                            input_message_content=InputTextMessageContent(
                                message_text=banned_text
                            ),
                        )
                    ],
                    cache_time=1,
                    is_personal=True,
                )
            except Exception as e:
                logger.warning(f"Failed to answer inline query for banned user: {e}")

        return None
