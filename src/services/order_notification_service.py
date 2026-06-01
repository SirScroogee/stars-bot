"""
Сервис уведомлений пользователей о статусе заказов.

Отправляет сообщения пользователям когда:
- Заказ успешно выполнен
- Заказ завершился с ошибкой
"""
import logging
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from src.db.session import async_session_factory
from src.db.models import User
from src.locales import t
from src.bot.keyboards.stars import get_back_to_menu_keyboard

if TYPE_CHECKING:
    from src.db.models import Order

logger = logging.getLogger(__name__)


async def _get_user_language(user_id: int) -> str:
    """Получить язык пользователя из БД."""
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(User.language_code).where(User.id == user_id)
            )
            lang = result.scalar_one_or_none()
            return lang or "ru"
    except Exception as e:
        logger.error(f"Failed to get user language: {e}")
        return "ru"

# Глобальный экземпляр бота для отправки уведомлений
_bot: Bot | None = None


def set_notification_bot(bot: Bot) -> None:
    """Установить бота для отправки уведомлений."""
    global _bot
    _bot = bot


def get_notification_bot() -> Bot | None:
    """Получить бота для отправки уведомлений."""
    return _bot


async def notify_order_completed(order: "Order") -> None:
    """
    Уведомить пользователя об успешном выполнении заказа.
    Если есть message_id — редактируем сообщение, иначе отправляем новое.

    Args:
        order: Выполненный заказ
    """
    if not _bot:
        logger.warning("Notification bot not set, cannot notify user")
        return

    try:
        # Получаем язык пользователя
        lang = await _get_user_language(order.user_id)

        # Формируем сообщение в зависимости от типа продукта
        title = t("common.order.completed_title", lang, order_key=order.order_key)
        recipient = t("common.order.recipient", lang, username=order.recipient_username)

        if order.product_type == "stars":
            quantity = t("common.order.quantity_stars", lang, amount=f"{order.quantity:,}")
            success_msg = t("common.order.stars_success", lang)
            text = (
                f"{title}\n\n"
                f"<blockquote>{recipient}\n"
                f"{quantity}</blockquote>\n\n"
                f"{success_msg}"
            )
        elif order.product_type == "premium":
            quantity = t("common.order.quantity_premium", lang, months=order.quantity)
            success_msg = t("common.order.premium_success", lang)
            text = (
                f"{title}\n\n"
                f"<blockquote>{recipient}\n"
                f"{quantity}</blockquote>\n\n"
                f"{success_msg}"
            )
        else:
            success_msg = t("common.order.generic_success", lang)
            text = f"{title}\n\n{success_msg}"

        # Всегда используем кнопку "В меню"
        keyboard = get_back_to_menu_keyboard(lang)

        edited = False
        if order.message_id:
            try:
                await _bot.edit_message_text(
                    chat_id=order.user_id,
                    message_id=order.message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                edited = True
                logger.info(f"Edited completion message for user {order.user_id}, order {order.id}")
            except TelegramBadRequest as e:
                if "there is no text in the message to edit" not in str(e).lower():
                    raise
                logger.warning(
                    "Cannot edit non-text order message %s for user %s, sending a new message",
                    order.message_id,
                    order.user_id,
                )

        if not edited:
            await _bot.send_message(
                chat_id=order.user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info(f"Sent completion notification to user {order.user_id} for order {order.id}")

    except Exception as e:
        logger.error(f"Failed to send/edit completion notification to user {order.user_id}: {e}")


async def notify_order_failed(order: "Order", error_message: str) -> None:
    """
    Уведомить пользователя об ошибке заказа.
    Если есть message_id — редактируем сообщение, иначе отправляем новое.

    Args:
        order: Неудачный заказ
        error_message: Сообщение об ошибке
    """
    if not _bot:
        logger.warning("Notification bot not set, cannot notify user")
        return

    try:
        # Получаем язык пользователя
        lang = await _get_user_language(order.user_id)

        # Человекочитаемые сообщения об ошибках
        user_error = _get_user_friendly_error(error_message, lang)

        # Формируем сообщение
        title = t("common.order.error_title", lang, order_key=order.order_key)
        recipient = t("common.order.recipient", lang, username=order.recipient_username)
        reason = t("common.order.error_reason", lang, reason=user_error)
        refund = t("common.order.error_refund", lang)

        if order.product_type == "stars":
            quantity = t("common.order.quantity_stars", lang, amount=f"{order.quantity:,}")
            text = (
                f"{title}\n\n"
                f"<blockquote>{recipient}\n"
                f"{quantity}</blockquote>\n\n"
                f"{reason}\n\n"
                f"{refund}"
            )
        elif order.product_type == "premium":
            quantity = t("common.order.quantity_premium", lang, months=order.quantity)
            text = (
                f"{title}\n\n"
                f"<blockquote>{recipient}\n"
                f"{quantity}</blockquote>\n\n"
                f"{reason}\n\n"
                f"{refund}"
            )
        else:
            text = (
                f"{title}\n\n"
                f"{reason}\n\n"
                f"{refund}"
            )

        # Всегда используем кнопку "В меню"
        keyboard = get_back_to_menu_keyboard(lang)

        edited = False
        if order.message_id:
            try:
                await _bot.edit_message_text(
                    chat_id=order.user_id,
                    message_id=order.message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                edited = True
                logger.info(f"Edited failure message for user {order.user_id}, order {order.id}")
            except TelegramBadRequest as e:
                if "there is no text in the message to edit" not in str(e).lower():
                    raise
                logger.warning(
                    "Cannot edit non-text order message %s for user %s, sending a new message",
                    order.message_id,
                    order.user_id,
                )

        if not edited:
            await _bot.send_message(
                chat_id=order.user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info(f"Sent failure notification to user {order.user_id} for order {order.id}")

    except Exception as e:
        logger.error(f"Failed to send/edit failure notification to user {order.user_id}: {e}")


def _get_user_friendly_error(error_message: str, lang: str = "ru") -> str:
    """
    Преобразовать техническую ошибку в понятное пользователю сообщение.
    """
    error_lower = error_message.lower()

    if "recipient not found" in error_lower or "not found" in error_lower:
        return t("common.order.errors.recipient_not_found", lang)

    if "insufficient funds" in error_lower or "недостаточно" in error_lower:
        return t("common.order.errors.insufficient_funds", lang)

    if "session expired" in error_lower:
        return t("common.order.errors.session_expired", lang)

    if "access denied" in error_lower:
        return t("common.order.errors.internal_error", lang)

    if "linking the wallet" in error_lower or "link wallet" in error_lower:
        return t("common.order.errors.internal_error", lang)

    if "timeout" in error_lower:
        return t("common.order.errors.timeout", lang)

    if "rate limit" in error_lower or "too many" in error_lower:
        return t("common.order.errors.rate_limit", lang)

    if "unknown product type" in error_lower:
        return t("common.order.errors.internal_error", lang)

    # Если не нашли конкретную ошибку
    return t("common.order.errors.generic", lang)
