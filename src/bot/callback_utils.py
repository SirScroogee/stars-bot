"""Helpers for Telegram callback-query expiry handling."""
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery


logger = logging.getLogger(__name__)


def is_stale_callback_error(exception: BaseException) -> bool:
    if not isinstance(exception, TelegramBadRequest):
        return False
    message = str(exception).lower()
    return "query is too old" in message or "query id is invalid" in message


async def safe_callback_answer(callback: CallbackQuery, *args, **kwargs) -> bool:
    try:
        await callback.answer(*args, **kwargs)
        return True
    except TelegramBadRequest as exc:
        if is_stale_callback_error(exc):
            logger.info("Callback query expired for user %s", callback.from_user.id)
            return False
        raise
