"""
Сервис уведомлений пользователей о статусе заказов.

Отправляет сообщения пользователям когда:
- Заказ успешно выполнен
- Заказ завершился с ошибкой
"""
import logging
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select

from src.db.session import async_session_factory
from src.db.models import User
from src.locales import t
from src.bot.keyboards.stars import get_back_to_menu_keyboard
from src.services.bot_settings_service import get_bot_settings

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


async def _get_support_username() -> str:
    try:
        settings = await get_bot_settings()
        return str(settings.get("support_username") or "support").lstrip("@")
    except Exception:
        return "support"


async def get_order_processing_notice(lang: str) -> str:
    """Text shown immediately after a purchase payment is confirmed."""
    return t(
        "common.order.payment_confirmed_processing",
        lang,
        support_username=await _get_support_username(),
    )

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
    # Giveaway accounting is independent from Telegram message delivery. The
    # operation is idempotent and the background worker also reconciles misses.
    try:
        from src.services.giveaway_service import process_completed_order_for_giveaways

        await process_completed_order_for_giveaways(order.id)
    except Exception:
        logger.exception("Failed to account order %s in active giveaways", order.id)

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
        if order.payment_provider == "balance":
            resolution = t("common.order.error_refund", lang)
        else:
            resolution = t(
                "common.order.error_support",
                lang,
                support_username=await _get_support_username(),
            )

        if order.product_type == "stars":
            quantity = t("common.order.quantity_stars", lang, amount=f"{order.quantity:,}")
            text = (
                f"{title}\n\n"
                f"<blockquote>{recipient}\n"
                f"{quantity}</blockquote>\n\n"
                f"{reason}\n\n"
                f"{resolution}"
            )
        elif order.product_type == "premium":
            quantity = t("common.order.quantity_premium", lang, months=order.quantity)
            text = (
                f"{title}\n\n"
                f"<blockquote>{recipient}\n"
                f"{quantity}</blockquote>\n\n"
                f"{reason}\n\n"
                f"{resolution}"
            )
        else:
            text = (
                f"{title}\n\n"
                f"{reason}\n\n"
                f"{resolution}"
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


async def notify_order_delayed(order: "Order") -> bool:
    """Tell the buyer that payment succeeded but fulfillment is delayed."""
    if not _bot:
        logger.warning("Notification bot not set, cannot notify delayed order user")
        return False

    try:
        lang = await _get_user_language(order.user_id)
        support_username = await _get_support_username()
        title = t("common.order.delayed_title", lang, order_key=order.order_key)
        recipient = t("common.order.recipient", lang, username=order.recipient_username)
        if order.product_type == "stars":
            quantity = t("common.order.quantity_stars", lang, amount=f"{order.quantity:,}")
        else:
            quantity = t("common.order.quantity_premium", lang, months=order.quantity)
        text = (
            f"{title}\n\n"
            f"<blockquote>{recipient}\n{quantity}</blockquote>\n\n"
            f"{t('common.order.delayed_text', lang, support_username=support_username)}"
        )
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
            except TelegramBadRequest as exc:
                error_text = str(exc).lower()
                if "message is not modified" in error_text:
                    return True
                if "there is no text in the message to edit" not in error_text:
                    logger.warning(
                        "Could not edit delayed order message %s for user %s: %s",
                        order.message_id,
                        order.user_id,
                        exc,
                    )

        if not edited:
            await _bot.send_message(
                chat_id=order.user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        return True
    except TelegramForbiddenError:
        logger.info("User %s blocked delayed-order notifications", order.user_id)
        return True
    except Exception as exc:
        logger.error("Failed to notify user %s about delayed order: %s", order.user_id, exc)
        return False


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
