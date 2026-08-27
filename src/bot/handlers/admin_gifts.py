"""Administrator wizard for sending native Telegram Gifts from the bot."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
)
from sqlalchemy import and_, func, or_, select, update

from src.bot.callback_utils import safe_callback_answer
from src.bot.handlers.admin_utils import check_admin, check_admin_message
from src.bot.keyboards.admin import AdminCallback, get_admin_menu_keyboard
from src.bot.keyboards.admin_gifts import (
    archived_gift_choose_keyboard,
    admin_gift_catalog_keyboard,
    admin_gift_comment_keyboard,
    admin_gift_confirm_keyboard,
    admin_gift_payment_wait_keyboard,
    admin_gift_recipient_picker_keyboard,
    admin_gift_result_keyboard,
    admin_gift_search_keyboard,
    admin_gift_selected_keyboard,
)
from src.db.models import (
    AdminGift,
    AdminGiftPayment,
    AdminGiftPaymentStatus,
    AdminGiftStatus,
    ArchivedGift,
    User,
)
from src.db.session import async_session_factory
from src.services.admin_gift_service import (
    AdminGiftService,
    GiftInvoiceAlreadyPaidError,
    GiftPaymentRefundRequiredError,
    GiftSendOutcome,
)
from src.services.archived_gift_service import ArchivedGiftService
from src.services.telegram_logger import tg_logger


logger = logging.getLogger(__name__)
router = Router(name="admin_gifts")
_confirmation_locks: dict[int, asyncio.Lock] = {}
_archive_catalog_lock = asyncio.Lock()
_gift_delivery_lock = asyncio.Lock()

TELEGRAM_GIFT_TEXT_LIMIT = 128
TELEGRAM_USER_ID_MAX = 0xFFFFFFFFFF
STALE_GIFT_SENDING_MINUTES = 5


@dataclass(frozen=True, slots=True)
class GiftRecipientTarget:
    user_id: int
    username: str | None = None
    display_name: str | None = None
    is_banned: bool = False
    is_registered: bool = False


def _is_current_gift_controller(data: dict, message) -> bool:
    return bool(
        message
        and data.get("controller_chat_id") == message.chat.id
        and data.get("controller_message_id") == message.message_id
    )


async def _clear_matching_gift_state(state: FSMContext, message) -> None:
    data = await state.get_data()
    if _is_current_gift_controller(data, message):
        await state.clear()


class AdminGiftStates(StatesGroup):
    waiting_recipient = State()
    waiting_comment = State()


def telegram_text_length(value: str) -> int:
    """Count UTF-16 code units, matching Telegram entity/text limits."""
    return len(value.encode("utf-16-le")) // 2


def build_gift_text(comment: str | None) -> str:
    return (comment or "").strip()


def max_comment_length() -> int:
    return TELEGRAM_GIFT_TEXT_LIMIT


def validate_gift_pre_checkout(
    *,
    payment,
    attempt,
    currency: str,
    total_amount: int,
) -> str | None:
    if payment is None or attempt is None:
        return "Счёт не найден или устарел."
    if attempt.status != AdminGiftStatus.AWAITING_PAYMENT.value:
        return "Этот счёт больше не активен."
    if payment.status not in {
        AdminGiftPaymentStatus.INVOICE_PENDING.value,
        AdminGiftPaymentStatus.INVOICE_SENT.value,
        AdminGiftPaymentStatus.PRECHECKOUT.value,
    }:
        return "Этот счёт уже оплачен или отменён."
    if currency != "XTR" or total_amount != payment.requested_stars:
        return "Сумма счёта не совпадает. Создайте новый счёт."
    return None


def _serialize_gift(gift) -> dict:
    return {
        "id": gift.id,
        "source": "live",
        "emoji": gift.sticker.emoji or "🎁",
        "star_count": gift.star_count,
        "total_count": gift.total_count,
        "remaining_count": gift.remaining_count,
        "sticker_file_id": gift.sticker.file_id,
    }


def _serialize_archived_gift(gift: ArchivedGift) -> dict:
    return {
        "id": gift.gift_id,
        "source": "archive",
        "archived_gift_id": gift.id,
        "title": gift.title,
        "emoji": gift.emoji or "🎁",
        "star_count": gift.star_count,
        "total_count": None,
        "remaining_count": None,
        "sticker_file_id": gift.sticker_file_id,
    }


def _available_gift_dicts(gifts) -> list[dict]:
    return [
        _serialize_gift(gift)
        for gift in gifts.gifts
        if gift.remaining_count is None or gift.remaining_count > 0
    ]


def _recipient_name(data: dict) -> str:
    username = data.get("recipient_username")
    if username:
        return f"@{username}"
    display_name = data.get("recipient_display_name")
    if display_name:
        return f"{display_name} (ID: {data.get('recipient_id')})"
    return f"ID: {data.get('recipient_id')}"


def _banned_warning(data: dict) -> str:
    if not data.get("recipient_is_banned"):
        return ""
    return (
        "\n\n⚠️ <b>Пользователь заблокирован в боте.</b> "
        "Подарок всё равно можно отправить."
    )


async def _delete_admin_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception as exc:
        logger.debug("Could not delete admin Gift input %s: %s", message.message_id, exc)


async def _delete_preview(bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    preview_message_id = data.get("gift_preview_message_id")
    preview_chat_id = data.get("controller_chat_id")
    if preview_message_id and preview_chat_id:
        try:
            await bot.delete_message(preview_chat_id, preview_message_id)
        except Exception as exc:
            logger.debug("Could not delete Gift preview %s: %s", preview_message_id, exc)
    if preview_message_id:
        await state.update_data(gift_preview_message_id=None)


async def _edit_controller(
    bot: Bot,
    state: FSMContext,
    text: str,
    reply_markup,
) -> None:
    data = await state.get_data()
    await bot.edit_message_text(
        chat_id=data["controller_chat_id"],
        message_id=data["controller_message_id"],
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        request_timeout=12,
    )


async def _get_live_gifts_and_balance(bot: Bot):
    """Retry read-only Telegram Gift preflight once after a transient timeout."""
    for attempt in range(2):
        try:
            return await asyncio.gather(
                bot.get_available_gifts(request_timeout=12),
                bot.get_my_star_balance(request_timeout=12),
            )
        except (TelegramNetworkError, TelegramServerError):
            if attempt:
                raise
            logger.warning("Retrying Telegram Gift preflight after a network error")
            await asyncio.sleep(0.5)
    raise RuntimeError("Telegram Gift preflight retry loop ended unexpectedly")


async def _get_bot_star_balance(bot: Bot):
    for attempt in range(2):
        try:
            return await bot.get_my_star_balance(request_timeout=12)
        except (TelegramNetworkError, TelegramServerError):
            if attempt:
                raise
            logger.warning("Retrying Telegram Stars balance check after a network error")
            await asyncio.sleep(0.5)
    raise RuntimeError("Telegram Stars balance retry loop ended unexpectedly")


async def _list_archived_gifts(*, active_only: bool = False) -> list[ArchivedGift]:
    async with _archive_catalog_lock:
        async with async_session_factory() as session:
            service = ArchivedGiftService(session)
            await service.reconcile_catalog()
            return await service.list_gifts(active_only=active_only)


async def _get_archived_gift(archived_gift_id: int) -> ArchivedGift | None:
    async with async_session_factory() as session:
        return await ArchivedGiftService(session).get(archived_gift_id)


async def _preflight_selected_gift(bot: Bot, selected: dict) -> tuple[dict | None, object]:
    """Resolve the current snapshot without probing an archived gift by purchase."""
    if selected.get("source") == "archive":
        archived_gift_id = selected.get("archived_gift_id")
        gift = (
            await _get_archived_gift(int(archived_gift_id))
            if archived_gift_id
            else None
        )
        balance = await _get_bot_star_balance(bot)
        if gift is None or not gift.is_active:
            return None, balance
        return _serialize_archived_gift(gift), balance

    gifts, balance = await _get_live_gifts_and_balance(bot)
    live = next((gift for gift in gifts.gifts if gift.id == selected.get("id")), None)
    if live is None or (live.remaining_count is not None and live.remaining_count <= 0):
        return None, balance
    return _serialize_gift(live), balance


async def _show_archived_choose(
    bot: Bot,
    state: FSMContext,
    *,
    page: int = 0,
    error: str | None = None,
) -> None:
    data = await state.get_data()
    if not data.get("recipient_id"):
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error="Сначала выберите получателя подарка.",
        )
        return
    await state.set_state(None)
    gifts = await _list_archived_gifts(active_only=True)
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    empty = (
        "\n\n<i>Активных архивных подарков пока нет.</i>"
        if not gifts
        else ""
    )
    await _edit_controller(
        bot,
        state,
        prefix
        + "🗃 <b>Архивные подарки</b>\n\n"
        f"👤 Получатель: <b>{html.escape(_recipient_name(data))}</b>\n\n"
        "Список поддерживается автоматически и содержит только снятые с продажи "
        "неулучшаемые сезонные подарки. Telegram проверит доступность при отправке."
        + empty,
        archived_gift_choose_keyboard(gifts, page),
    )


async def _find_registered_user(value: str) -> User | None:
    query = value.strip()
    if not query:
        return None

    async with async_session_factory() as session:
        if query.isdigit():
            result = await session.execute(select(User).where(User.id == int(query)))
            user = result.scalar_one_or_none()
            if user is not None:
                return user

        username = query[1:] if query.startswith("@") else query
        if username:
            result = await session.execute(
                select(User)
                .where(
                    User.username.isnot(None),
                    func.lower(User.username) == username.lower(),
                )
                .order_by(User.updated_at.desc(), User.id)
                .limit(2)
            )
            matching_users = list(result.scalars().all())
            if len(matching_users) == 1:
                return matching_users[0]
            if len(matching_users) > 1:
                # Telegram usernames can move between accounts while historical
                # rows keep the old value. Never guess a recipient for a Gift.
                logger.warning(
                    "Ambiguous registered username %s maps to user IDs %s",
                    username,
                    [user.id for user in matching_users],
                )
                return None

            result = await session.execute(
                select(User).where(func.lower(User.referral_code) == query.lower())
            )
            return result.scalar_one_or_none()
    return None


def _registered_recipient(user: User) -> GiftRecipientTarget:
    return GiftRecipientTarget(
        user_id=user.id,
        username=user.username,
        is_banned=user.is_banned,
        is_registered=True,
    )


def validate_telegram_user_id(value: str | int) -> int:
    raw = str(value).strip()
    if not raw.isascii() or not raw.isdigit():
        raise ValueError("Telegram ID должен состоять только из цифр.")
    user_id = int(raw)
    if user_id <= 0 or user_id > TELEGRAM_USER_ID_MAX:
        raise ValueError("Telegram ID находится вне допустимого диапазона.")
    return user_id


def describe_gift_delivery_error(error: str | None) -> str:
    message = (error or "").lower()
    if any(
        code in message
        for code in ("user_id_invalid", "peer_id_invalid", "user not found")
    ):
        return (
            "Telegram не дал боту доступ к получателю. Перешлите сюда сообщение "
            "этого пользователя и попробуйте снова."
        )
    if any(code in message for code in ("input_user_deactivated", "user_deactivated")):
        return "Аккаунт получателя удалён или деактивирован."
    if "user_is_blocked" in message or "bot was blocked" in message:
        return "Получатель заблокировал бота, поэтому Telegram отклонил подарок."
    if any(code in message for code in ("user_gift_unavailable", "gift_unavailable")):
        return "Получатель сейчас не может принимать Telegram Gifts."
    if any(code in message for code in ("stargift_usage_limited", "gift is unavailable")):
        return "Этот подарок закончился или больше недоступен."
    if "balance_too_low" in message or "not enough stars" in message:
        return "На балансе бота недостаточно Telegram Stars."
    return "Telegram отклонил отправку. Подробности сохранены в журнале операции."


async def _recipient_by_id(
    user_id: int,
    *,
    username: str | None = None,
    display_name: str | None = None,
) -> GiftRecipientTarget:
    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        registered = result.scalar_one_or_none()
    if registered is not None:
        return _registered_recipient(registered)
    return GiftRecipientTarget(
        user_id=user_id,
        username=username,
        display_name=display_name,
    )


async def _resolve_recipient_input(
    value: str,
) -> tuple[GiftRecipientTarget | None, str | None]:
    query = value.strip()
    if not query:
        return None, "Отправьте Telegram ID или выберите пользователя кнопкой."
    if query.isascii() and query.isdigit():
        try:
            user_id = validate_telegram_user_id(query)
        except ValueError as exc:
            return None, str(exc)
        return await _recipient_by_id(user_id), None

    user = await _find_registered_user(query)
    if user is not None:
        return _registered_recipient(user), None
    return (
        None,
        "Username или реферальный код не найден в базе бота. Для "
        "незарегистрированного пользователя нажмите «Выбрать пользователя».",
    )


async def _remove_recipient_picker(bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = data.get("controller_chat_id")
    picker_message_id = data.get("recipient_picker_message_id")
    if picker_message_id and chat_id:
        try:
            await bot.delete_message(chat_id, picker_message_id)
        except Exception as exc:
            logger.debug("Could not delete Gift recipient picker: %s", exc)
    if data.get("recipient_picker_active") and chat_id:
        try:
            cleanup = await bot.send_message(
                chat_id,
                "Клавиатура выбора получателя закрыта.",
                reply_markup=ReplyKeyboardRemove(),
                disable_notification=True,
            )
            await bot.delete_message(chat_id, cleanup.message_id)
        except Exception as exc:
            logger.debug("Could not remove Gift recipient keyboard: %s", exc)
    if picker_message_id or data.get("recipient_picker_active"):
        await state.update_data(
            recipient_picker_message_id=None,
            recipient_picker_active=False,
        )


async def _accept_recipient(
    bot: Bot,
    state: FSMContext,
    target: GiftRecipientTarget,
) -> None:
    await _remove_recipient_picker(bot, state)
    await state.update_data(
        recipient_id=target.user_id,
        recipient_username=target.username,
        recipient_display_name=target.display_name,
        recipient_is_banned=target.is_banned,
        recipient_is_registered=target.is_registered,
    )
    await _edit_controller(
        bot,
        state,
        "⏳ <b>Загружаю доступные подарки Telegram…</b>",
        None,
    )
    error = await _load_catalog(bot, state)
    await _show_catalog(bot, state, error=error)


async def _start_recipient_search(
    bot: Bot,
    state: FSMContext,
    *,
    chat_id: int,
    message_id: int,
    error: str | None = None,
) -> None:
    await _delete_preview(bot, state)
    await _remove_recipient_picker(bot, state)
    await state.clear()
    await state.set_state(AdminGiftStates.waiting_recipient)
    request_id = secrets.randbelow(2**31)
    await state.update_data(
        controller_chat_id=chat_id,
        controller_message_id=message_id,
        recipient_request_id=request_id,
    )
    error_text = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        error_text
        + "🎁 <b>Подарить подарок</b>\n\n"
        "Перешлите сюда сообщение получателя или нажмите «Выбрать пользователя». "
        "Оба способа позволяют выбрать человека, которого нет в базе бота.\n\n"
        "Также можно отправить числовой Telegram ID. Username и реферальный код "
        "работают для пользователей из базы бота. Если Telegram не даст доступ "
        "по ID, используйте пересланное сообщение.",
        admin_gift_search_keyboard(),
    )
    try:
        picker = await bot.send_message(
            chat_id,
            "Выберите получателя подарка:",
            reply_markup=admin_gift_recipient_picker_keyboard(request_id),
            request_timeout=12,
        )
        await state.update_data(
            recipient_picker_message_id=picker.message_id,
            recipient_picker_active=True,
        )
    except Exception as exc:
        logger.warning("Could not show native Gift recipient picker: %s", exc)


async def _require_gift_wizard(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    *,
    required: tuple[str, ...] = (),
) -> dict | None:
    """Recover old admin buttons instead of letting missing FSM data crash."""
    data = await state.get_data()
    missing = [key for key in required if data.get(key) is None]
    is_current = _is_current_gift_controller(data, callback.message)
    if is_current:
        if not missing:
            return data
    elif data.get("controller_chat_id") and data.get("controller_message_id"):
        await safe_callback_answer(
            callback,
            "Это кнопка из старой сессии отправки.",
            show_alert=True,
        )
        return None

    await _start_recipient_search(
        bot,
        state,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        error="Сессия отправки устарела. Выберите получателя заново.",
    )
    await safe_callback_answer(callback, "Сессия отправки восстановлена")
    return None


async def _load_catalog(bot: Bot, state: FSMContext) -> str | None:
    try:
        gifts, balance = await _get_live_gifts_and_balance(bot)
    except Exception as exc:
        logger.warning("Could not load Telegram Gifts catalog: %s", exc)
        return "Не удалось получить каталог подарков или баланс бота. Попробуйте ещё раз."

    available = _available_gift_dicts(gifts)
    await state.update_data(
        available_gifts=available,
        bot_star_balance=balance.amount,
    )
    if not available:
        return "Telegram сейчас не возвращает ни одного доступного подарка."
    return None


async def _show_catalog(
    bot: Bot,
    state: FSMContext,
    *,
    page: int = 0,
    error: str | None = None,
) -> None:
    await state.set_state(None)
    data = await state.get_data()
    gifts = data.get("available_gifts") or []
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    text = (
        prefix
        + "🎁 <b>Выберите подарок</b>\n\n"
        f"👤 Получатель: <b>{html.escape(_recipient_name(data))}</b>\n"
        f"⭐ Баланс бота: <b>{int(data.get('bot_star_balance') or 0):,}</b> Stars\n\n"
        "Каталог получен напрямую из Telegram. Цена и наличие будут проверены ещё раз перед отправкой."
        + _banned_warning(data)
    )
    await _edit_controller(
        bot,
        state,
        text,
        admin_gift_catalog_keyboard(gifts, page),
    )


async def _show_selected(bot: Bot, state: FSMContext) -> None:
    await state.set_state(None)
    data = await state.get_data()
    gift = data.get("selected_gift")
    if not gift:
        await _show_catalog(bot, state, error="Сначала выберите подарок.")
        return
    remaining = gift.get("remaining_count")
    remaining_text = (
        f"\n📦 Осталось: <b>{remaining:,}</b>" if remaining is not None else ""
    )
    archived = gift.get("source") == "archive"
    title_text = (
        f"\nНазвание: <b>{html.escape(gift.get('title') or 'Без названия')}</b>"
        if archived
        else ""
    )
    archive_warning = (
        "\n\n⚠️ <b>Сезонный подарок снят с продажи.</b> "
        "Он отсутствует в текущем каталоге Telegram; окончательная доступность "
        "проверяется при отправке."
        if archived
        else ""
    )
    await _edit_controller(
        bot,
        state,
        "🎁 <b>Выбранный подарок</b>\n\n"
        f"👤 Получатель: <b>{html.escape(_recipient_name(data))}</b>\n"
        f"{html.escape(gift.get('emoji') or '🎁')} Стоимость: "
        f"<b>{gift['star_count']:,} Stars</b>{remaining_text}\n"
        f"⭐ Баланс бота: <b>{int(data.get('bot_star_balance') or 0):,}</b> Stars"
        + title_text
        + _banned_warning(data)
        + archive_warning
        + "\n\nДалее можно добавить комментарий к подарку.",
        admin_gift_selected_keyboard(
            "admin:gifts:archive:choose" if archived else "admin:gifts:catalog"
        ),
    )


async def _show_comment_prompt(
    bot: Bot,
    state: FSMContext,
    *,
    error: str | None = None,
) -> None:
    limit = max_comment_length()
    await state.set_state(AdminGiftStates.waiting_comment)
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        prefix
        + "✍️ <b>Комментарий к подарку</b>\n\n"
        f"Введите комментарий (до <b>{limit}</b> символов) или нажмите "
        "«Без комментария». Подарок без комментария будет отправлен без подписи.",
        admin_gift_comment_keyboard(),
    )


async def _show_confirmation(
    bot: Bot,
    state: FSMContext,
    comment: str | None,
    *,
    error: str | None = None,
) -> None:
    data = await state.get_data()
    gift = data.get("selected_gift")
    gift_text = build_gift_text(comment)
    if telegram_text_length(gift_text) > TELEGRAM_GIFT_TEXT_LIMIT:
        await _show_comment_prompt(
            bot,
            state,
            error=f"Комментарий превышает лимит {TELEGRAM_GIFT_TEXT_LIMIT} символов.",
        )
        return

    await state.set_state(None)
    await state.update_data(
        gift_comment=(comment or "").strip(),
        gift_text=gift_text,
        operation_key=secrets.token_hex(16),
    )
    data = await state.get_data()
    prefix = f"⚠️ <b>{html.escape(error)}</b>\n\n" if error else ""
    comment_preview = (
        "📝 Комментарий к подарку:\n"
        f"<blockquote>{html.escape(gift_text)}</blockquote>"
        if gift_text
        else "📝 Комментарий: <i>без комментария</i>"
    )
    archived = gift.get("source") == "archive"
    archive_warning = (
        "\n\n⚠️ <b>Архивная отправка.</b> Telegram может отклонить Gift ID. "
        "При однозначном отказе подарок не будет списан."
        if archived
        else ""
    )
    await _edit_controller(
        bot,
        state,
        prefix
        + "⚠️ <b>Подтвердите отправку</b>\n\n"
        f"👤 Получатель: <b>{html.escape(_recipient_name(data))}</b>\n"
        f"{html.escape(gift.get('emoji') or '🎁')} Стоимость: "
        f"<b>{gift['star_count']:,} Stars</b>\n"
        f"⭐ Баланс бота: <b>{int(data.get('bot_star_balance') or 0):,}</b> Stars\n\n"
        + comment_preview
        + _banned_warning(data)
        + archive_warning
        + "\n\n<b>Отправка необратима.</b>",
        admin_gift_confirm_keyboard(
            archived=archived,
            other_gift_callback=(
                "admin:gifts:archive:choose" if archived else "admin:gifts:catalog"
            ),
        ),
    )


async def _log_outcome(outcome: GiftSendOutcome) -> None:
    attempt = outcome.attempt
    recipient = (
        f"@{attempt.recipient_username_snapshot} ({attempt.recipient_id})"
        if attempt.recipient_username_snapshot
        else str(attempt.recipient_id)
    )
    details = (
        f"Операция: #{attempt.id}\n"
        f"Получатель: {html.escape(recipient)}\n"
        f"Источник: {html.escape(attempt.gift_source)}\n"
        + (
            f"Название: {html.escape(attempt.gift_title_snapshot)}\n"
            if attempt.gift_title_snapshot
            else ""
        )
        +
        f"Подарок: {html.escape(attempt.gift_id)}\n"
        f"Стоимость: {attempt.gift_star_count} Stars\n"
        f"Статус: {html.escape(attempt.status)}"
    )
    if attempt.error_message:
        details += f"\nОшибка: {html.escape(attempt.error_message[:500])}"
    await tg_logger.log_admin_action(
        admin_id=attempt.admin_id,
        admin_username=attempt.admin_username_snapshot,
        action="Отправка Telegram Gift",
        details=details,
    )


async def _best_effort_mark_unknown(attempt_id: int, error: BaseException) -> None:
    """Use a fresh transaction if the delivery session could not persist its result."""
    try:
        async with async_session_factory() as session:
            await session.execute(
                update(AdminGift)
                .where(
                    AdminGift.id == attempt_id,
                    AdminGift.status.in_(
                        [
                            AdminGiftStatus.PENDING.value,
                            AdminGiftStatus.SENDING.value,
                        ]
                    ),
                )
                .values(
                    status=AdminGiftStatus.UNKNOWN.value,
                    error_type=type(error).__name__,
                    error_message=str(error),
                    updated_at=datetime.utcnow(),
                )
            )
            await session.commit()
    except Exception as persist_exc:
        logger.critical(
            "Could not mark ambiguous Gift attempt %s as unknown: %s",
            attempt_id,
            persist_exc,
        )


def _attempt_recipient_name(attempt: AdminGift) -> str:
    if attempt.recipient_username_snapshot:
        return f"@{attempt.recipient_username_snapshot}"
    return f"ID: {attempt.recipient_id}"


def _attempt_reply_markup(attempt_id: int, status: str):
    if status in {
        AdminGiftStatus.AWAITING_PAYMENT.value,
        AdminGiftStatus.PENDING.value,
        AdminGiftStatus.SENDING.value,
    }:
        return admin_gift_payment_wait_keyboard(attempt_id)
    return admin_gift_result_keyboard()


async def _edit_attempt_controller(
    bot: Bot,
    attempt: AdminGift,
    text: str,
    reply_markup,
) -> None:
    if not attempt.controller_chat_id or not attempt.controller_message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=attempt.controller_chat_id,
            message_id=attempt.controller_message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.info("Could not edit Gift controller for attempt %s: %s", attempt.id, exc)


async def _issue_payment_invoice(
    bot: Bot,
    service: AdminGiftService,
    attempt: AdminGift,
    requested_stars: int,
) -> tuple[object, bool]:
    payment, created = await service.create_payment_request(
        attempt_id=attempt.id,
        admin_id=attempt.admin_id,
        requested_stars=requested_stars,
    )
    # A process may stop after persisting the payment but before sendInvoice (or
    # before mark_invoice_sent). Re-send the same payload on recovery; the
    # pre-checkout reservation still guarantees that it can only be paid once.
    if not created and payment.status != AdminGiftPaymentStatus.INVOICE_PENDING.value:
        return payment, False
    requested_stars = payment.requested_stars

    description = (
        f"Не хватает {requested_stars} Stars для подарка пользователю "
        f"{_attempt_recipient_name(attempt)}. Счёт можно переслать и оплатить с другого аккаунта."
    )
    try:
        invoice_message = await bot.send_invoice(
            chat_id=attempt.admin_id,
            title="Stars для подарка",
            description=description[:255],
            payload=payment.invoice_payload,
            currency="XTR",
            prices=[
                LabeledPrice(
                    label="Недостающие Stars",
                    amount=requested_stars,
                )
            ],
            provider_token="",
            request_timeout=30,
        )
    except Exception as exc:
        await service.mark_invoice_failed(payment.id, exc)
        raise

    try:
        payment = await service.mark_invoice_sent(payment.id, invoice_message.message_id)
    except Exception as exc:
        # The invoice itself already exists and remains payable because
        # pre-checkout accepts invoice_pending. Do not issue a duplicate invoice.
        logger.critical(
            "Invoice %s for Gift attempt %s was sent but could not be finalized: %s",
            payment.id,
            attempt.id,
            exc,
        )
        payment.invoice_message_id = invoice_message.message_id
    return payment, True


async def _refund_paid_topups(
    bot: Bot,
    service: AdminGiftService,
    attempt: AdminGift,
) -> tuple[int, int]:
    """Refund every paid top-up to the Telegram user who actually paid it."""
    refunded = 0
    failed = 0
    for payment in await service.get_refundable_payments(attempt.id):
        amount = payment.paid_stars or payment.requested_stars
        if not payment.telegram_payment_charge_id or not payment.payer_id:
            failed += amount
            try:
                await service.mark_payment_refunded(
                    payment.id,
                    error=RuntimeError("Paid Gift invoice has no charge ID or payer ID"),
                )
            except Exception as persist_exc:
                logger.critical(
                    "Could not persist invalid Gift refund state for payment %s: %s",
                    payment.id,
                    persist_exc,
                )
            continue
        try:
            result = await bot.refund_star_payment(
                user_id=payment.payer_id,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                request_timeout=30,
            )
            if not result:
                raise RuntimeError("Telegram returned False from refundStarPayment")
        except Exception as exc:
            error_text = str(exc).lower()
            already_refunded = (
                "charge_already_refunded" in error_text
                or "already refunded" in error_text
            )
            if already_refunded:
                refunded += amount
                try:
                    await service.mark_payment_refunded(payment.id)
                except Exception as persist_exc:
                    logger.critical(
                        "Gift refund was already complete but could not be persisted "
                        "for payment %s: %s",
                        payment.id,
                        persist_exc,
                    )
                continue

            failed += amount
            try:
                await service.mark_payment_refunded(payment.id, error=exc)
            except Exception as persist_exc:
                logger.critical(
                    "Could not persist failed Gift refund for payment %s: %s",
                    payment.id,
                    persist_exc,
                )
        else:
            refunded += amount
            try:
                await service.mark_payment_refunded(payment.id)
            except Exception as persist_exc:
                # The money is already back with the payer. Leave a critical log;
                # a later retry treats Telegram's already-refunded response as success.
                logger.critical(
                    "Gift refund succeeded but could not be persisted for payment %s: %s",
                    payment.id,
                    persist_exc,
                )
    return refunded, failed


async def _resume_gift_attempt_unlocked(
    bot: Bot, attempt_id: int
) -> tuple[AdminGift, str, str]:
    """Recheck shared bot balance and continue an already persisted Gift attempt."""
    async with async_session_factory() as session:
        service = AdminGiftService(session, bot)
        attempt = await service.get_attempt(attempt_id)
        active_payment = (
            await service.get_active_payment(attempt_id) if attempt else None
        )
        has_paid_topups = (
            bool(await service.get_paid_payments(attempt_id)) if attempt else False
        )
    if attempt is None:
        raise LookupError("Gift attempt not found")

    if attempt.status == AdminGiftStatus.SENDING.value:
        stale_before = datetime.utcnow() - timedelta(
            minutes=STALE_GIFT_SENDING_MINUTES
        )
        async with async_session_factory() as session:
            service = AdminGiftService(session, bot)
            attempt, sealed = await service.mark_stale_sending_unknown(
                attempt.id,
                stale_before=stale_before,
            )
        if sealed:
            return (
                attempt,
                attempt.status,
                "⚠️ <b>Результат прерванной отправки неизвестен</b>\n\n"
                "Процесс остановился после начала отправки. Подарок мог быть "
                "доставлен, поэтому повтор и автоматический возврат запрещены.\n"
                f"🧾 Операция: <code>#{attempt.id}</code>",
            )
        return (
            attempt,
            attempt.status,
            "⏳ <b>Telegram ещё обрабатывает отправку</b>\n\n"
            "Подождите несколько минут и проверьте операцию снова.\n"
            f"🧾 Операция: <code>#{attempt.id}</code>",
        )

    if attempt.status == AdminGiftStatus.AWAITING_PAYMENT.value:
        if (
            active_payment is not None
            and active_payment.status
            == AdminGiftPaymentStatus.INVOICE_PENDING.value
        ):
            try:
                async with async_session_factory() as session:
                    service = AdminGiftService(session, bot)
                    payment, sent = await _issue_payment_invoice(
                        bot,
                        service,
                        attempt,
                        active_payment.requested_stars,
                    )
            except Exception as exc:
                async with async_session_factory() as recovery_session:
                    recovery_service = AdminGiftService(recovery_session, bot)
                    recovered = await recovery_service.get_attempt(attempt.id)
                attempt = recovered or attempt
                return (
                    attempt,
                    attempt.status,
                    "❌ <b>Не удалось восстановить Telegram-счёт</b>\n\n"
                    "Попробуйте ещё раз позже. "
                    f"Ошибка: <code>{html.escape(str(exc)[:300])}</code>\n"
                    f"🧾 Операция: <code>#{attempt.id}</code>",
                )
            if sent:
                return (
                    attempt,
                    attempt.status,
                    "💳 <b>Счёт восстановлен</b>\n\n"
                    f"К оплате: <b>{payment.requested_stars} Stars</b>. "
                    "Его можно переслать и оплатить с любого аккаунта.\n"
                    f"🧾 Операция: <code>#{attempt.id}</code>",
                )
        return (
            attempt,
            attempt.status,
            "⏳ <b>Оплата ещё не получена</b>\n\n"
            "Оплатите выставленный Telegram-счёт или перешлите его другому человеку. "
            "После оплаты подарок отправится автоматически.",
        )
    if attempt.status == AdminGiftStatus.SUCCEEDED.value:
        return (
            attempt,
            attempt.status,
            "✅ <b>Подарок уже отправлен</b>\n\n"
            f"👤 Получатель: <b>{html.escape(_attempt_recipient_name(attempt))}</b>\n"
            f"🧾 Операция: <code>#{attempt.id}</code>",
        )
    if attempt.status == AdminGiftStatus.CANCELLED.value:
        return (
            attempt,
            attempt.status,
            "🚫 <b>Операция отменена</b>\n\n"
            "Неоплаченный Telegram-счёт больше не принимается.\n"
            f"🧾 Операция: <code>#{attempt.id}</code>",
        )
    if attempt.status == AdminGiftStatus.FAILED.value:
        async with async_session_factory() as session:
            service = AdminGiftService(session, bot)
            refundable = await service.get_refundable_payments(attempt.id)
            refunded, refund_failed = (
                await _refund_paid_topups(bot, service, attempt)
                if refundable
                else (0, 0)
            )
        refund_text = ""
        if refunded:
            refund_text += f"\nВозвращено плательщикам: <b>{refunded} Stars</b>."
        if refund_failed:
            refund_text += (
                f"\nНе удалось вернуть: <b>{refund_failed} Stars</b>. "
                "Нажмите проверку позже или выполните возврат вручную."
            )
        return (
            attempt,
            attempt.status,
            "❌ <b>Отправка подарка завершилась ошибкой</b>\n\n"
            "Автоматический повтор подарка запрещён."
            + refund_text
            + f"\n🧾 Операция: <code>#{attempt.id}</code>",
        )
    if attempt.status == AdminGiftStatus.UNKNOWN.value:
        return (
            attempt,
            attempt.status,
            "⚠️ <b>Операцию нельзя безопасно повторить</b>\n\n"
            f"Текущий статус: <code>{html.escape(attempt.status)}</code>\n"
            f"🧾 Операция: <code>#{attempt.id}</code>",
        )

    archived = attempt.gift_source == "archive"
    try:
        if archived:
            balance = await _get_bot_star_balance(bot)
            target_star_count = attempt.gift_star_count
            target_emoji = attempt.gift_emoji
        else:
            gifts, balance = await _get_live_gifts_and_balance(bot)
            live = next(
                (gift for gift in gifts.gifts if gift.id == attempt.gift_id),
                None,
            )
            if live is None or (
                live.remaining_count is not None and live.remaining_count <= 0
            ):
                error = RuntimeError("Gift became unavailable after the Stars payment")
                async with async_session_factory() as session:
                    service = AdminGiftService(session, bot)
                    attempt = await service.fail_pending_attempt(attempt.id, error)
                    refunded, refund_failed = await _refund_paid_topups(
                        bot,
                        service,
                        attempt,
                    )
                refund_text = (
                    f"\n✅ Возвращено плательщикам: <b>{refunded} Stars</b>"
                    if refunded
                    else ""
                )
                if refund_failed:
                    refund_text += (
                        "\n⚠️ Не удалось автоматически вернуть: "
                        f"<b>{refund_failed} Stars</b>. Требуется ручная проверка."
                    )
                return (
                    attempt,
                    attempt.status,
                    "❌ <b>Подарок закончился до завершения оплаты</b>\n\n"
                    "Отправка отменена."
                    + refund_text
                    + f"\n🧾 Операция: <code>#{attempt.id}</code>",
                )
            target_star_count = live.star_count
            target_emoji = live.sticker.emoji
    except Exception as exc:
        return (
            attempt,
            attempt.status,
            "❌ <b>Не удалось проверить Telegram</b>\n\n"
            f"Попробуйте ещё раз позже. Ошибка: <code>{html.escape(str(exc)[:300])}</code>",
        )

    if has_paid_topups and balance.amount < target_star_count:
        # The successful_payment update can arrive just before the newly received
        # Stars become visible through getMyStarBalance. Avoid issuing a second
        # invoice because of this short propagation window.
        for delay in (1, 2):
            await asyncio.sleep(delay)
            try:
                balance = await bot.get_my_star_balance(request_timeout=20)
            except Exception as exc:
                logger.info("Could not recheck bot Stars balance after payment: %s", exc)
                break
            if balance.amount >= target_star_count:
                break

    async with async_session_factory() as session:
        service = AdminGiftService(session, bot)
        attempt = await service.refresh_attempt_gift(
            attempt.id,
            gift_star_count=target_star_count,
            gift_emoji=target_emoji,
            bot_balance_before=balance.amount,
        )
        if balance.amount < target_star_count:
            missing = target_star_count - balance.amount
            try:
                payment, created = await _issue_payment_invoice(
                    bot,
                    service,
                    attempt,
                    missing,
                )
            except Exception as exc:
                attempt = await service.get_attempt(attempt.id) or attempt
                return (
                    attempt,
                    attempt.status,
                    "❌ <b>Не удалось выставить счёт</b>\n\n"
                    f"Ошибка: <code>{html.escape(str(exc)[:300])}</code>\n"
                    f"🧾 Операция: <code>#{attempt.id}</code>",
                )
            action = "выставлен" if created else "уже ожидает оплаты"
            return (
                attempt,
                AdminGiftStatus.AWAITING_PAYMENT.value,
                f"💳 <b>Дополнительный счёт {action}</b>\n\n"
                f"Не хватает ещё: <b>{payment.requested_stars} Stars</b>. "
                "После оплаты отправка продолжится автоматически.\n"
                f"🧾 Операция: <code>#{attempt.id}</code>",
            )

        try:
            outcome = await service.send_attempt(attempt.id, attempt.admin_id)
        except Exception as exc:
            logger.exception(
                "Paid Gift attempt %s ended without a safely persisted result",
                attempt.id,
            )
            try:
                await session.rollback()
            except Exception:
                pass
            await _best_effort_mark_unknown(attempt.id, exc)
            async with async_session_factory() as recovery_session:
                recovery_service = AdminGiftService(recovery_session, bot)
                recovered = await recovery_service.get_attempt(attempt.id)
            attempt = recovered or attempt
            return (
                attempt,
                AdminGiftStatus.UNKNOWN.value,
                "⚠️ <b>Результат отправки неизвестен</b>\n\n"
                "Подарок мог быть доставлен, поэтому платёж не возвращён. "
                "Не повторяйте операцию до ручной проверки.\n"
                f"🧾 Операция: <code>#{attempt.id}</code>",
            )
        attempt = outcome.attempt
        if outcome.status == AdminGiftStatus.SUCCEEDED.value:
            text = (
                "✅ <b>Оплата получена, подарок отправлен</b>\n\n"
                f"👤 Получатель: <b>{html.escape(_attempt_recipient_name(attempt))}</b>\n"
                f"{html.escape(attempt.gift_emoji or '🎁')} Списано: "
                f"<b>{attempt.gift_star_count} Stars</b>\n"
                f"🧾 Операция: <code>#{attempt.id}</code>"
            )
        elif outcome.status == AdminGiftStatus.FAILED.value:
            refunded, refund_failed = await _refund_paid_topups(bot, service, attempt)
            text = (
                "❌ <b>Telegram отклонил отправку подарка</b>\n\n"
                f"{html.escape(describe_gift_delivery_error(outcome.error))}\n"
                f"Возвращено плательщикам: <b>{refunded} Stars</b>."
            )
            if refund_failed:
                text += (
                    f"\n⚠️ Не удалось вернуть {refund_failed} Stars — нужна ручная проверка."
                )
            text += f"\n🧾 Операция: <code>#{attempt.id}</code>"
        elif outcome.status == AdminGiftStatus.UNKNOWN.value:
            text = (
                "⚠️ <b>Результат отправки неизвестен</b>\n\n"
                "Подарок мог быть доставлен, поэтому платёж не возвращён автоматически. "
                "Не повторяйте операцию до ручной проверки.\n"
                f"🧾 Операция: <code>#{attempt.id}</code>"
            )
        else:
            text = (
                "⏳ <b>Операция уже обрабатывается</b>\n\n"
                f"🧾 Операция: <code>#{attempt.id}</code>"
            )

    if outcome.performed:
        try:
            await _log_outcome(outcome)
        except Exception as exc:
            logger.error("Could not publish resumed Gift log: %s", exc)
    return attempt, outcome.status, text


async def _resume_gift_attempt(bot: Bot, attempt_id: int) -> tuple[AdminGift, str, str]:
    async with _gift_delivery_lock:
        return await _resume_gift_attempt_unlocked(bot, attempt_id)


async def recover_admin_gift_operations(bot: Bot) -> dict[str, int]:
    """Continue charged Gifts and seal interrupted sends during startup."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(AdminGift.id, AdminGift.status)
            .outerjoin(
                AdminGiftPayment,
                AdminGiftPayment.gift_attempt_id == AdminGift.id,
            )
            .where(
                or_(
                    and_(
                        AdminGift.status == AdminGiftStatus.PENDING.value,
                        AdminGiftPayment.status
                        == AdminGiftPaymentStatus.PAID.value,
                    ),
                    and_(
                        AdminGift.status == AdminGiftStatus.FAILED.value,
                        AdminGiftPayment.status.in_(
                            [
                                AdminGiftPaymentStatus.PAID.value,
                                AdminGiftPaymentStatus.REFUND_FAILED.value,
                            ]
                        ),
                    ),
                    AdminGift.status == AdminGiftStatus.SENDING.value,
                )
            )
            .distinct()
            .order_by(AdminGift.id)
        )
        attempts = list(result.all())

    recovered = 0
    failed = 0
    for attempt_id, initial_status in attempts:
        try:
            if initial_status == AdminGiftStatus.SENDING.value:
                async with async_session_factory() as session:
                    service = AdminGiftService(session, bot)
                    await service.mark_stale_sending_unknown(
                        attempt_id,
                        stale_before=datetime.utcnow() + timedelta(seconds=1),
                    )
            attempt, status, text = await _resume_gift_attempt(bot, attempt_id)
            reply_markup = _attempt_reply_markup(attempt.id, status)
            if attempt.controller_chat_id and attempt.controller_message_id:
                await _edit_attempt_controller(bot, attempt, text, reply_markup)
            else:
                await bot.send_message(
                    attempt.admin_id,
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            recovered += 1
        except Exception as exc:
            failed += 1
            logger.exception(
                "Could not recover admin Gift attempt %s during startup",
                attempt_id,
            )
            try:
                await tg_logger.log_error(
                    error_type="AdminGiftStartupRecoveryError",
                    error_message=str(exc),
                    details=f"Операция: #{attempt_id}",
                )
            except Exception:
                pass
    return {
        "found": len(attempts),
        "recovered": recovered,
        "failed": failed,
    }


@router.callback_query(F.data == AdminCallback.GIFTS)
async def callback_admin_gifts(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    if not await check_admin(callback):
        return
    await _start_recipient_search(
        bot,
        state,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
    )
    await safe_callback_answer(callback)


@router.callback_query(F.data == "admin:gifts:archive:choose")
async def callback_admin_gift_archive_choose(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    if await _require_gift_wizard(
        callback, state, bot, required=("recipient_id",)
    ) is None:
        return
    await safe_callback_answer(callback)
    await _delete_preview(bot, state)
    await _show_archived_choose(bot, state)


@router.callback_query(F.data.regexp(r"^admin:gifts:archive:choose:page:\d+$"))
async def callback_admin_gift_archive_choose_page(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    if await _require_gift_wizard(
        callback, state, bot, required=("recipient_id",)
    ) is None:
        return
    await safe_callback_answer(callback)
    await _show_archived_choose(
        bot,
        state,
        page=int(callback.data.rsplit(":", 1)[-1]),
    )


@router.callback_query(F.data.regexp(r"^admin:gifts:archive:select:\d+$"))
async def callback_admin_gift_archive_select(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    if await _require_gift_wizard(
        callback, state, bot, required=("recipient_id",)
    ) is None:
        return
    await safe_callback_answer(callback, "Проверяю баланс…")
    archived_gift_id = int(callback.data.rsplit(":", 1)[-1])
    gift = await _get_archived_gift(archived_gift_id)
    if gift is None or not gift.is_active:
        await _show_archived_choose(
            bot,
            state,
            error="Подарок удалён или выключен другим администратором.",
        )
        return
    try:
        balance = await _get_bot_star_balance(bot)
    except Exception as exc:
        logger.warning("Could not load balance for archived Gift selection: %s", exc)
        await _show_archived_choose(
            bot,
            state,
            error="Не удалось получить баланс бота. Попробуйте ещё раз.",
        )
        return
    await _delete_preview(bot, state)
    await state.update_data(
        selected_gift=_serialize_archived_gift(gift),
        bot_star_balance=balance.amount,
    )
    await _show_selected(bot, state)


@router.callback_query(
    F.data.regexp(r"^admin:gifts:archive:(manage|add|sync|item|edit|delete|set)")
)
async def callback_admin_gift_archive_legacy_action(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    """Redirect buttons left in old admin messages to the automatic catalog."""
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback, "Каталог теперь обновляется автоматически")
    data = await state.get_data()
    if _is_current_gift_controller(data, callback.message) and data.get("recipient_id"):
        await _show_archived_choose(bot, state)
        return
    await _start_recipient_search(
        bot,
        state,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
    )


@router.message(AdminGiftStates.waiting_recipient, F.forward_origin)
async def message_admin_gift_forwarded_recipient(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    data = await state.get_data()
    origin = message.forward_origin
    await _delete_admin_input(message)
    if isinstance(origin, MessageOriginHiddenUser):
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error=(
                "Автор пересланного сообщения скрыт настройками приватности. "
                "Используйте кнопку выбора пользователя или Telegram ID."
            ),
        )
        return
    if not isinstance(origin, MessageOriginUser):
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error="Нужно переслать личное сообщение пользователя, а не канала или чата.",
        )
        return
    sender = origin.sender_user
    if sender.is_bot:
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error="Telegram Gifts можно отправлять только обычным пользователям.",
        )
        return
    try:
        user_id = validate_telegram_user_id(sender.id)
    except ValueError as exc:
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error=str(exc),
        )
        return
    display_name = " ".join(
        part for part in (sender.first_name, sender.last_name) if part
    ) or None
    target = await _recipient_by_id(
        user_id,
        username=sender.username,
        display_name=display_name,
    )
    await _accept_recipient(bot, state, target)


@router.message(AdminGiftStates.waiting_recipient, F.users_shared)
async def message_admin_gift_shared_recipient(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    if message.users_shared is None:
        return
    data = await state.get_data()
    await _delete_admin_input(message)
    shared = message.users_shared
    if shared.request_id != data.get("recipient_request_id"):
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error="Эта кнопка выбора получателя устарела. Выберите пользователя заново.",
        )
        return
    if len(shared.users) != 1:
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error="Нужно выбрать ровно одного пользователя.",
        )
        return

    selected = shared.users[0]
    try:
        user_id = validate_telegram_user_id(selected.user_id)
    except ValueError as exc:
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error=str(exc),
        )
        return
    display_name = " ".join(
        part for part in (selected.first_name, selected.last_name) if part
    ) or None
    target = await _recipient_by_id(
        user_id,
        username=selected.username,
        display_name=display_name,
    )
    await _accept_recipient(bot, state, target)


@router.message(AdminGiftStates.waiting_recipient)
async def message_admin_gift_recipient(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await check_admin_message(message):
        return
    value = message.text or ""
    await _delete_admin_input(message)
    target, error = await _resolve_recipient_input(value)
    if target is None:
        data = await state.get_data()
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error=error or "Не удалось определить получателя.",
        )
        return
    await _accept_recipient(bot, state, target)


@router.callback_query(F.data == "admin:gifts:recipient")
async def callback_admin_gift_recipient(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    data = await _require_gift_wizard(callback, state, bot)
    if data is None:
        return
    await _start_recipient_search(
        bot,
        state,
        chat_id=data["controller_chat_id"],
        message_id=data["controller_message_id"],
    )
    await safe_callback_answer(callback)


@router.callback_query(F.data == "admin:gifts:catalog:refresh")
async def callback_admin_gift_refresh(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    if await _require_gift_wizard(
        callback, state, bot, required=("recipient_id",)
    ) is None:
        return
    await safe_callback_answer(callback, "Обновляю каталог…")
    error = await _load_catalog(bot, state)
    await _show_catalog(bot, state, error=error)


@router.callback_query(F.data == "admin:gifts:catalog")
async def callback_admin_gift_catalog(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    data = await _require_gift_wizard(
        callback, state, bot, required=("recipient_id",)
    )
    if data is None:
        return
    await _delete_preview(bot, state)
    error = None
    if data.get("available_gifts") is None:
        error = await _load_catalog(bot, state)
    await _show_catalog(bot, state, error=error)
    await safe_callback_answer(callback)


@router.callback_query(F.data.regexp(r"^admin:gifts:page:\d+$"))
async def callback_admin_gift_page(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    if await _require_gift_wizard(
        callback,
        state,
        bot,
        required=("recipient_id", "available_gifts"),
    ) is None:
        return
    page = int(callback.data.rsplit(":", 1)[-1])
    await _show_catalog(bot, state, page=page)
    await safe_callback_answer(callback)


@router.callback_query(F.data == "admin:gifts:nop")
async def callback_admin_gift_nop(callback: CallbackQuery) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)


@router.callback_query(F.data.regexp(r"^admin:gifts:select:\d+$"))
async def callback_admin_gift_select(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    data = await _require_gift_wizard(
        callback,
        state,
        bot,
        required=("recipient_id", "available_gifts"),
    )
    if data is None:
        return
    gifts = data.get("available_gifts") or []
    index = int(callback.data.rsplit(":", 1)[-1])
    if index < 0 or index >= len(gifts):
        await safe_callback_answer(
            callback,
            "Каталог изменился. Обновите его.",
            show_alert=True,
        )
        return
    await _delete_preview(bot, state)
    await state.update_data(selected_gift=gifts[index])
    await _show_selected(bot, state)
    await safe_callback_answer(callback)


@router.callback_query(F.data == "admin:gifts:selected")
async def callback_admin_gift_selected(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    if await _require_gift_wizard(
        callback, state, bot, required=("recipient_id", "selected_gift")
    ) is None:
        return
    await _show_selected(bot, state)
    await safe_callback_answer(callback)


@router.callback_query(F.data == "admin:gifts:comment")
async def callback_admin_gift_comment(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    if await _require_gift_wizard(
        callback, state, bot, required=("recipient_id", "selected_gift")
    ) is None:
        return
    await _show_comment_prompt(bot, state)
    await safe_callback_answer(callback)


@router.message(AdminGiftStates.waiting_comment)
async def message_admin_gift_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await check_admin_message(message):
        return
    comment = message.text or ""
    await _delete_admin_input(message)
    if not comment.strip():
        await _show_comment_prompt(bot, state, error="Комментарий не может быть пустым.")
        return
    if telegram_text_length(build_gift_text(comment)) > TELEGRAM_GIFT_TEXT_LIMIT:
        await _show_comment_prompt(
            bot,
            state,
            error=f"Комментарий слишком длинный. Максимум: {max_comment_length()} символов.",
        )
        return
    await _show_confirmation(bot, state, comment)


@router.callback_query(F.data == "admin:gifts:comment:skip")
async def callback_admin_gift_comment_skip(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    if await _require_gift_wizard(
        callback, state, bot, required=("recipient_id", "selected_gift")
    ) is None:
        return
    await _show_confirmation(bot, state, None)
    await safe_callback_answer(callback)


@router.callback_query(F.data == "admin:gifts:confirm")
async def callback_admin_gift_confirm(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    lock = _confirmation_locks.setdefault(callback.from_user.id, asyncio.Lock())
    if lock.locked():
        await safe_callback_answer(
            callback,
            "Отправка уже проверяется. Дождитесь результата.",
            show_alert=True,
        )
        return
    if await _require_gift_wizard(
        callback,
        state,
        bot,
        required=("recipient_id", "selected_gift", "operation_key", "gift_text"),
    ) is None:
        return
    await safe_callback_answer(callback, "Проверяю и отправляю…")
    try:
        async with lock:
            async with _gift_delivery_lock:
                await _process_admin_gift_confirm(callback, state, bot)
    finally:
        if _confirmation_locks.get(callback.from_user.id) is lock:
            _confirmation_locks.pop(callback.from_user.id, None)


async def _process_admin_gift_confirm(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    data = await _require_gift_wizard(
        callback,
        state,
        bot,
        required=("recipient_id", "selected_gift", "operation_key", "gift_text"),
    )
    if data is None:
        return
    selected = data.get("selected_gift")
    operation_key = data.get("operation_key")
    gift_text = data.get("gift_text")
    recipient_id = data.get("recipient_id")
    archived = selected.get("source") == "archive"
    try:
        await _edit_controller(
            bot,
            state,
            (
                "⏳ <b>Проверяю архивную запись и баланс…</b>\n\n"
                if archived
                else "⏳ <b>Проверяю цену, наличие и баланс…</b>\n\n"
            )
            +
            "Не нажимайте кнопку отправки повторно.",
            None,
        )
    except TelegramNetworkError as exc:
        # A status edit is cosmetic and must not cancel the idempotent operation.
        logger.warning("Could not show Gift preflight status, continuing: %s", exc)

    try:
        verified, balance = await _preflight_selected_gift(bot, selected)
    except Exception as exc:
        logger.warning("Gift preflight failed: %s", exc)
        await _show_confirmation(
            bot,
            state,
            data.get("gift_comment"),
            error="Не удалось проверить Telegram. Подарок не отправлялся.",
        )
        return

    if verified is None:
        await state.update_data(bot_star_balance=balance.amount, selected_gift=None)
        await _delete_preview(bot, state)
        if archived:
            await _show_archived_choose(
                bot,
                state,
                error="Архивный подарок удалён или выключен.",
            )
        else:
            await _load_catalog(bot, state)
            await _show_catalog(
                bot,
                state,
                error="Выбранный подарок закончился или больше недоступен.",
            )
        return

    await state.update_data(selected_gift=verified, bot_star_balance=balance.amount)
    if verified["star_count"] != selected["star_count"]:
        await _show_confirmation(
            bot,
            state,
            data.get("gift_comment"),
            error=(
                f"Цена изменилась с {selected['star_count']} до "
                f"{verified['star_count']} Stars. "
                "Проверьте сумму и подтвердите ещё раз."
            ),
        )
        return

    gift_star_count = verified["star_count"]
    gift_id = verified["id"]
    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.id == recipient_id))
        recipient = result.scalar_one_or_none()
        recipient_username = data.get("recipient_username")
        recipient_was_banned = bool(data.get("recipient_is_banned"))
        if recipient is not None:
            recipient_username = recipient.username
            recipient_was_banned = recipient.is_banned

        service = AdminGiftService(session, bot)
        attempt, created_attempt = await service.create_or_get_attempt(
            operation_key=operation_key,
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            recipient_id=recipient_id,
            recipient_username=recipient_username,
            recipient_was_banned=recipient_was_banned,
            gift_id=gift_id,
            gift_emoji=verified.get("emoji"),
            gift_star_count=gift_star_count,
            gift_text=gift_text,
            bot_balance_before=balance.amount,
            gift_source=verified.get("source") or "live",
            gift_title=verified.get("title"),
            archived_gift_id=verified.get("archived_gift_id"),
            controller_chat_id=callback.message.chat.id,
            controller_message_id=callback.message.message_id,
        )
        if not created_attempt and attempt.status != AdminGiftStatus.PENDING.value:
            await _delete_preview(bot, state)
            await state.clear()
            attempt, status, text = await _resume_gift_attempt_unlocked(
                bot, attempt.id
            )
            reply_markup = _attempt_reply_markup(attempt.id, status)
            await callback.message.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        if balance.amount < gift_star_count:
            missing = gift_star_count - balance.amount
            try:
                payment, created = await _issue_payment_invoice(
                    bot,
                    service,
                    attempt,
                    missing,
                )
            except Exception as exc:
                logger.exception("Could not issue Stars invoice for Gift attempt %s", attempt.id)
                await _delete_preview(bot, state)
                await state.clear()
                await callback.message.edit_text(
                    "❌ <b>Не удалось выставить счёт на Stars</b>\n\n"
                    "Нажмите «Проверить оплату и отправить», чтобы попробовать снова.\n"
                    f"Ошибка: <code>{html.escape(str(exc)[:500])}</code>\n"
                    f"🧾 Операция: <code>#{attempt.id}</code>",
                    reply_markup=admin_gift_payment_wait_keyboard(attempt.id),
                    parse_mode="HTML",
                )
                return

            await _delete_preview(bot, state)
            await state.clear()
            invoice_state = "выставлен" if created else "уже ожидает оплаты"
            await callback.message.edit_text(
                f"💳 <b>Счёт {invoice_state}</b>\n\n"
                f"🎁 Стоимость подарка: <b>{gift_star_count} Stars</b>\n"
                f"⭐ Баланс бота: <b>{balance.amount} Stars</b>\n"
                f"➕ К оплате: <b>{payment.requested_stars} Stars</b>\n\n"
                "Оплатите Telegram-счёт в этом чате или перешлите его другому человеку — "
                "счёт может оплатить любой пользователь. После успешной оплаты "
                "бот ещё раз проверит баланс и автоматически отправит подарок.\n\n"
                f"🧾 Операция: <code>#{attempt.id}</code>",
                reply_markup=admin_gift_payment_wait_keyboard(attempt.id),
                parse_mode="HTML",
            )
            if created:
                try:
                    await tg_logger.log_admin_action(
                        admin_id=callback.from_user.id,
                        admin_username=callback.from_user.username,
                        action="Счёт Stars для Telegram Gift",
                        details=(
                            f"Операция: #{attempt.id}\n"
                            f"Получатель: {html.escape(_attempt_recipient_name(attempt))}\n"
                            f"Источник: {html.escape(attempt.gift_source)}\n"
                            f"К оплате: {payment.requested_stars} Stars"
                        ),
                    )
                except Exception as exc:
                    logger.error("Could not publish Gift invoice log: %s", exc)
            return

        await _edit_controller(
            bot,
            state,
            "⏳ <b>Telegram отправляет подарок…</b>\n\n"
            f"Операция аудита: <code>#{attempt.id}</code>",
            None,
        )
        try:
            outcome = await service.send_attempt(attempt.id, callback.from_user.id)
        except Exception as exc:
            logger.exception(
                "Gift attempt %s ended without a safely persisted result",
                attempt.id,
            )
            try:
                await session.rollback()
            except Exception:
                pass
            await _best_effort_mark_unknown(attempt.id, exc)
            await _delete_preview(bot, state)
            await state.clear()
            await callback.message.edit_text(
                "⚠️ <b>Не удалось надёжно определить результат отправки</b>\n\n"
                "Подарок мог быть доставлен. Не повторяйте отправку, пока не "
                "проверите получателя и транзакции бота.\n\n"
                f"🧾 Операция: <code>#{attempt.id}</code>\n"
                f"Ошибка: <code>{html.escape(str(exc)[:500])}</code>",
                reply_markup=admin_gift_result_keyboard(),
                parse_mode="HTML",
            )
            try:
                await tg_logger.log_admin_action(
                    admin_id=callback.from_user.id,
                    admin_username=callback.from_user.username,
                    action="Неоднозначный результат Telegram Gift",
                    details=(
                        f"Операция: #{attempt.id}\n"
                        f"Получатель: {recipient_id}\n"
                        f"Подарок: {html.escape(gift_id)}\n"
                        f"Ошибка аудита: {html.escape(str(exc)[:500])}"
                    ),
                )
            except Exception as log_exc:
                logger.error("Could not publish ambiguous Gift log: %s", log_exc)
            return

        refunded = 0
        refund_failed = 0
        if outcome.status == AdminGiftStatus.FAILED.value:
            refundable = await service.get_refundable_payments(outcome.attempt.id)
            if refundable:
                refunded, refund_failed = await _refund_paid_topups(
                    bot,
                    service,
                    outcome.attempt,
                )

    await _delete_preview(bot, state)
    await state.clear()
    if outcome.status == AdminGiftStatus.SUCCEEDED.value:
        result_text = (
            "✅ <b>Подарок успешно отправлен</b>\n\n"
            f"👤 Получатель: <b>{html.escape(_recipient_name(data))}</b>\n"
            f"{html.escape(selected.get('emoji') or '🎁')} Списано: "
            f"<b>{gift_star_count:,} Stars</b>\n"
            f"🧾 Операция: <code>#{outcome.attempt.id}</code>"
        )
        logger.info(
            "Admin %s sent Telegram Gift %s to user %s (attempt %s)",
            callback.from_user.id,
            gift_id,
            recipient_id,
            outcome.attempt.id,
        )
    elif outcome.status == AdminGiftStatus.FAILED.value:
        result_text = (
            "❌ <b>Telegram отклонил отправку подарка</b>\n\n"
            f"{html.escape(describe_gift_delivery_error(outcome.error))}\n\n"
            "Подарок не был отправлен. Автоматического повтора не будет.\n"
            f"🧾 Операция: <code>#{outcome.attempt.id}</code>\n"
            f"Код Telegram: <code>{html.escape((outcome.error or 'неизвестная ошибка')[:500])}</code>"
        )
        if refunded:
            result_text += f"\n↩️ Возвращено плательщикам: <b>{refunded} Stars</b>."
        if refund_failed:
            result_text += (
                f"\n⚠️ Не удалось вернуть <b>{refund_failed} Stars</b>; "
                "нужна ручная проверка."
            )
    elif outcome.status == AdminGiftStatus.UNKNOWN.value:
        result_text = (
            "⚠️ <b>Результат отправки неизвестен</b>\n\n"
            "Telegram не вернул однозначный ответ. Подарок мог быть доставлен. "
            "Не отправляйте его повторно, пока не проверите получателя и транзакции бота.\n\n"
            f"🧾 Операция: <code>#{outcome.attempt.id}</code>\n"
            f"Ошибка: <code>{html.escape((outcome.error or 'неизвестная ошибка')[:500])}</code>"
        )
    else:
        result_text = (
            "⏳ <b>Эта операция уже обрабатывается</b>\n\n"
            "Повторная отправка не выполнялась.\n\n"
            f"🧾 Операция: <code>#{outcome.attempt.id}</code>"
        )

    await callback.message.edit_text(
        result_text,
        reply_markup=admin_gift_result_keyboard(),
        parse_mode="HTML",
    )
    if outcome.performed:
        try:
            await _log_outcome(outcome)
        except Exception as exc:
            logger.error(
                "Could not publish admin Gift log for attempt %s: %s",
                outcome.attempt.id,
                exc,
            )


@router.pre_checkout_query(F.invoice_payload.startswith("agift:"))
async def pre_checkout_admin_gift(
    query: PreCheckoutQuery,
    bot: Bot,
) -> None:
    """Accept the first payer for the exact persisted payload and XTR amount."""
    error_message: str | None = None
    try:
        async with async_session_factory() as session:
            service = AdminGiftService(session, bot)
            payment, attempt = await service.get_payment_context(query.invoice_payload)

            error_message = validate_gift_pre_checkout(
                payment=payment,
                attempt=attempt,
                currency=query.currency,
                total_amount=query.total_amount,
            )
            if error_message is None:
                claimed = await service.claim_pre_checkout(
                    payment.id,
                    query.from_user.id,
                    query.id,
                )
                if not claimed:
                    error_message = (
                        "Этот счёт уже оплачивается другим пользователем. "
                        "Попросите администратора выставить новый счёт."
                    )
    except Exception as exc:
        logger.exception("Could not validate admin Gift pre-checkout")
        error_message = "Не удалось проверить оплату. Попробуйте ещё раз позже."

    await bot.answer_pre_checkout_query(
        pre_checkout_query_id=query.id,
        ok=error_message is None,
        error_message=error_message,
        request_timeout=8,
    )


@router.message(F.successful_payment.invoice_payload.startswith("agift:"))
async def successful_payment_admin_gift(message: Message, bot: Bot) -> None:
    """Record the Stars payment once and automatically continue Gift delivery."""
    successful = message.successful_payment
    record_error: Exception | None = None
    try:
        if message.from_user is None:
            raise ValueError("Successful Gift payment has no payer")
        # A charge has already happened, so retry a transient/ambiguous database
        # failure once. The payload and Telegram charge ID make this idempotent.
        for record_attempt in range(2):
            try:
                async with async_session_factory() as session:
                    service = AdminGiftService(session, bot)
                    payment, attempt, claimed = await service.record_successful_payment(
                        invoice_payload=successful.invoice_payload,
                        payer_id=message.from_user.id,
                        currency=successful.currency,
                        total_amount=successful.total_amount,
                        telegram_payment_charge_id=successful.telegram_payment_charge_id,
                        provider_payment_charge_id=successful.provider_payment_charge_id,
                    )
                record_error = None
                break
            except GiftPaymentRefundRequiredError:
                raise
            except Exception as exc:
                record_error = exc
                if record_attempt == 0:
                    logger.warning(
                        "Retrying persistence for charged Gift invoice %s: %s",
                        successful.invoice_payload,
                        exc,
                    )
                    await asyncio.sleep(0.2)
        if record_error is not None:
            raise record_error
    except GiftPaymentRefundRequiredError as exc:
        refund_error: Exception | None = None
        try:
            refunded = await bot.refund_star_payment(
                user_id=message.from_user.id,
                telegram_payment_charge_id=successful.telegram_payment_charge_id,
                request_timeout=30,
            )
            if not refunded:
                raise RuntimeError("Telegram returned False from refundStarPayment")
        except Exception as current_refund_error:
            error_text = str(current_refund_error).lower()
            if not (
                "charge_already_refunded" in error_text
                or "already refunded" in error_text
            ):
                refund_error = current_refund_error

        if refund_error is None:
            await message.answer(
                "↩️ <b>Оплата возвращена</b>\n\n"
                "Этот платёж нельзя применить к операции, поэтому Telegram Stars "
                "возвращены на ваш аккаунт.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "⚠️ <b>Платёж требует возврата</b>\n\n"
                "Автоматический возврат не удался. Обратитесь к владельцу бота и "
                f"передайте Charge ID: <code>{html.escape(successful.telegram_payment_charge_id)}</code>",
                parse_mode="HTML",
            )
        try:
            await tg_logger.log_error(
                error_type=(
                    "AdminGiftDuplicateCharge"
                    if isinstance(exc, GiftInvoiceAlreadyPaidError)
                    else "AdminGiftRejectedCharge"
                ),
                error_message=str(refund_error or exc),
                user_id=message.from_user.id,
                details=(
                    f"Charge: {successful.telegram_payment_charge_id}\n"
                    f"Refunded: {refund_error is None}"
                ),
            )
        except Exception:
            pass
        return
    except Exception as exc:
        logger.exception("Could not record successful admin Gift Stars payment")
        await message.answer(
            "⚠️ <b>Оплата получена Telegram, но не была обработана ботом</b>\n\n"
            "Не оплачивайте новые счета и обратитесь к владельцу бота.\n"
            f"Charge ID: <code>{html.escape(successful.telegram_payment_charge_id)}</code>",
            parse_mode="HTML",
        )
        try:
            await tg_logger.log_error(
                error_type="AdminGiftPaymentRecordError",
                error_message=str(exc),
                user_id=message.from_user.id,
                details=f"Charge: {successful.telegram_payment_charge_id}",
            )
        except Exception:
            pass
        return

    if claimed:
        await message.answer(
            f"✅ Получено <b>{payment.paid_stars} Stars</b>. "
            "Проверяю подарок и баланс…",
            parse_mode="HTML",
        )

    try:
        attempt, status, text = await _resume_gift_attempt(bot, attempt.id)
    except Exception as exc:
        logger.exception(
            "Charged admin Gift attempt %s could not continue",
            attempt.id,
        )
        await message.answer(
            "⚠️ <b>Оплата сохранена, продолжение временно задержано</b>\n\n"
            "Новый счёт оплачивать не нужно. Бот повторит обработку после "
            "перезапуска, либо администратор сможет нажать проверку операции.\n"
            f"🧾 Операция: <code>#{attempt.id}</code>",
            parse_mode="HTML",
        )
        try:
            await tg_logger.log_error(
                error_type="AdminGiftPaidContinuationError",
                error_message=str(exc),
                user_id=message.from_user.id,
                details=f"Операция: #{attempt.id}",
            )
        except Exception:
            pass
        return
    reply_markup = _attempt_reply_markup(attempt.id, status)
    await _edit_attempt_controller(bot, attempt, text, reply_markup)
    if message.from_user.id == attempt.admin_id:
        await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        payer_text = (
            f"✅ <b>Спасибо! Оплата {payment.paid_stars} Stars принята.</b>\n\n"
            "Бот продолжил обработку подарка. Администратор увидит итог операции."
        )
        await message.answer(payer_text, parse_mode="HTML")

    try:
        await tg_logger.log_admin_action(
            admin_id=attempt.admin_id,
            admin_username=attempt.admin_username_snapshot,
            action="Оплата Stars для Telegram Gift",
            details=(
                f"Операция: #{attempt.id}\n"
                f"Оплачено: {payment.paid_stars} Stars\n"
                f"Плательщик: {payment.payer_id}\n"
                f"Charge: {html.escape(payment.telegram_payment_charge_id or '—')}\n"
                f"Результат: {html.escape(status)}"
            ),
        )
    except Exception as exc:
        logger.error("Could not publish paid Gift log: %s", exc)


@router.callback_query(F.data.regexp(r"^admin:gifts:resume:\d+$"))
async def callback_admin_gift_resume(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    if not await check_admin(callback):
        return
    attempt_id = int(callback.data.rsplit(":", 1)[-1])
    async with async_session_factory() as session:
        service = AdminGiftService(session, bot)
        attempt = await service.get_attempt(attempt_id)
    if attempt is None or attempt.admin_id != callback.from_user.id:
        await safe_callback_answer(
            callback,
            "Операция не найдена или принадлежит другому администратору.",
            show_alert=True,
        )
        return

    await safe_callback_answer(callback, "Проверяю оплату и баланс…")
    try:
        attempt, status, text = await _resume_gift_attempt(bot, attempt_id)
    except Exception as exc:
        logger.exception("Could not resume admin Gift attempt %s", attempt_id)
        await callback.message.edit_text(
            "❌ <b>Не удалось проверить операцию</b>\n\n"
            "Состояние оплаты сохранено. Попробуйте ещё раз позже.\n"
            f"Ошибка: <code>{html.escape(str(exc)[:300])}</code>\n"
            f"🧾 Операция: <code>#{attempt_id}</code>",
            reply_markup=admin_gift_payment_wait_keyboard(attempt_id),
            parse_mode="HTML",
        )
        return
    reply_markup = _attempt_reply_markup(attempt.id, status)
    await _clear_matching_gift_state(state, callback.message)
    await callback.message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:gifts:operation:cancel:\d+$"))
async def callback_admin_gift_operation_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    if not await check_admin(callback):
        return
    attempt_id = int(callback.data.rsplit(":", 1)[-1])
    try:
        async with async_session_factory() as session:
            service = AdminGiftService(session, bot)
            outcome = await service.cancel_unpaid_attempt(
                attempt_id,
                callback.from_user.id,
            )
    except LookupError:
        await safe_callback_answer(
            callback,
            "Операция не найдена или принадлежит другому администратору.",
            show_alert=True,
        )
        return

    if not outcome.cancelled:
        await safe_callback_answer(
            callback,
            outcome.reason or "Операцию уже нельзя отменить.",
            show_alert=True,
        )
        return

    if outcome.payment and outcome.payment.invoice_message_id:
        try:
            await bot.delete_message(
                outcome.attempt.admin_id,
                outcome.payment.invoice_message_id,
            )
        except Exception as exc:
            logger.info(
                "Could not delete cancelled Gift invoice %s: %s",
                outcome.payment.id,
                exc,
            )

    await _clear_matching_gift_state(state, callback.message)
    await safe_callback_answer(callback, "Неоплаченная операция отменена")
    await callback.message.edit_text(
        "🚫 <b>Операция отменена</b>\n\n"
        "Telegram-счёт больше не принимается. Списаний и отправки подарка не было.\n"
        f"🧾 Операция: <code>#{outcome.attempt.id}</code>",
        reply_markup=admin_gift_result_keyboard(),
        parse_mode="HTML",
    )
    try:
        await tg_logger.log_admin_action(
            admin_id=outcome.attempt.admin_id,
            admin_username=outcome.attempt.admin_username_snapshot,
            action="Отмена Telegram Gift",
            details=f"Операция: #{outcome.attempt.id}\nСтатус: cancelled",
        )
    except Exception as exc:
        logger.error("Could not publish cancelled Gift log: %s", exc)


@router.callback_query(F.data == "admin:gifts:cancel")
async def callback_admin_gift_cancel(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    data = await state.get_data()
    if data.get("controller_message_id") and not _is_current_gift_controller(
        data, callback.message
    ):
        await safe_callback_answer(
            callback,
            "Это кнопка из старой сессии отправки.",
            show_alert=True,
        )
        return
    await _delete_preview(bot, state)
    await _remove_recipient_picker(bot, state)
    await state.clear()
    await callback.message.edit_text(
        "🔐 <b>Админ-панель</b>\n\nВыберите раздел:",
        reply_markup=get_admin_menu_keyboard(),
        parse_mode="HTML",
    )
    await safe_callback_answer(callback, "Отправка отменена")
