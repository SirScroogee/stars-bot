"""Administrator wizard for sending native Telegram Gifts from the bot."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import func, select, update

from src.bot.callback_utils import safe_callback_answer
from src.bot.handlers.admin_utils import check_admin, check_admin_message
from src.bot.keyboards.admin import AdminCallback, get_admin_menu_keyboard
from src.bot.keyboards.admin_gifts import (
    archived_gift_choose_keyboard,
    archived_gift_delete_keyboard,
    archived_gift_input_keyboard,
    archived_gift_item_keyboard,
    archived_gift_manage_keyboard,
    admin_gift_catalog_keyboard,
    admin_gift_comment_keyboard,
    admin_gift_confirm_keyboard,
    admin_gift_payment_wait_keyboard,
    admin_gift_result_keyboard,
    admin_gift_search_keyboard,
    admin_gift_selected_keyboard,
)
from src.db.models import (
    AdminGift,
    AdminGiftPaymentStatus,
    AdminGiftStatus,
    ArchivedGift,
    User,
)
from src.db.session import async_session_factory
from src.services.admin_gift_service import (
    AdminGiftService,
    GiftInvoiceAlreadyPaidError,
    GiftSendOutcome,
)
from src.services.archived_gift_service import (
    ArchivedGiftAlreadyExistsError,
    ArchivedGiftService,
)
from src.services.telegram_logger import tg_logger


logger = logging.getLogger(__name__)
router = Router(name="admin_gifts")
_confirmation_locks: dict[int, asyncio.Lock] = {}

TELEGRAM_GIFT_TEXT_LIMIT = 128
ARCHIVED_GIFT_TITLE_LIMIT = 100
ARCHIVED_GIFT_EMOJI_LIMIT = 32
ARCHIVED_GIFT_MAX_ID = 2**63 - 1
ARCHIVED_GIFT_MAX_PRICE = 1_000_000_000


class AdminGiftStates(StatesGroup):
    waiting_recipient = State()
    waiting_comment = State()
    waiting_archive_gift_id = State()
    waiting_archive_title = State()
    waiting_archive_star_count = State()
    waiting_archive_emoji = State()
    waiting_archive_sticker = State()
    waiting_archive_edit_title = State()
    waiting_archive_edit_star_count = State()
    waiting_archive_edit_emoji = State()
    waiting_archive_edit_sticker = State()


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


def validate_archived_gift_id(value: str) -> str:
    gift_id = value.strip()
    if not gift_id.isascii() or not gift_id.isdigit():
        raise ValueError("ID должен состоять только из цифр.")
    parsed = int(gift_id)
    if parsed <= 0 or parsed > ARCHIVED_GIFT_MAX_ID:
        raise ValueError("ID находится вне допустимого диапазона Telegram.")
    return str(parsed)


def validate_archived_gift_title(value: str) -> str:
    title = value.strip()
    if not title:
        raise ValueError("Название не может быть пустым.")
    if len(title) > ARCHIVED_GIFT_TITLE_LIMIT:
        raise ValueError(
            f"Название должно быть не длиннее {ARCHIVED_GIFT_TITLE_LIMIT} символов."
        )
    return title


def validate_archived_gift_price(value: str) -> int:
    raw = value.strip()
    if not raw.isascii() or not raw.isdigit():
        raise ValueError("Стоимость должна быть целым числом Stars.")
    price = int(raw)
    if price <= 0 or price > ARCHIVED_GIFT_MAX_PRICE:
        raise ValueError(
            f"Стоимость должна быть от 1 до {ARCHIVED_GIFT_MAX_PRICE:,} Stars."
        )
    return price


def validate_archived_gift_emoji(value: str) -> str:
    emoji = value.strip()
    if not emoji:
        raise ValueError("Эмодзи не может быть пустым.")
    if len(emoji) > ARCHIVED_GIFT_EMOJI_LIMIT:
        raise ValueError(
            f"Эмодзи должно быть не длиннее {ARCHIVED_GIFT_EMOJI_LIMIT} символов."
        )
    return emoji


def _available_gift_dicts(gifts) -> list[dict]:
    return [
        _serialize_gift(gift)
        for gift in gifts.gifts
        if gift.remaining_count is None or gift.remaining_count > 0
    ]


def _recipient_name(data: dict) -> str:
    username = data.get("recipient_username")
    return f"@{username}" if username else f"ID: {data.get('recipient_id')}"


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
        except TelegramNetworkError:
            if attempt:
                raise
            logger.warning("Retrying Telegram Gift preflight after a network error")
            await asyncio.sleep(0.5)
    raise RuntimeError("Telegram Gift preflight retry loop ended unexpectedly")


async def _get_bot_star_balance(bot: Bot):
    for attempt in range(2):
        try:
            return await bot.get_my_star_balance(request_timeout=12)
        except TelegramNetworkError:
            if attempt:
                raise
            logger.warning("Retrying Telegram Stars balance check after a network error")
            await asyncio.sleep(0.5)
    raise RuntimeError("Telegram Stars balance retry loop ended unexpectedly")


async def _list_archived_gifts(*, active_only: bool = False) -> list[ArchivedGift]:
    async with async_session_factory() as session:
        return await ArchivedGiftService(session).list_gifts(active_only=active_only)


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
        "Эти подарки отправляются по сохранённому ID и могут быть отклонены Telegram."
        + empty,
        archived_gift_choose_keyboard(gifts, page),
    )


async def _show_archive_manager(
    bot: Bot,
    state: FSMContext,
    *,
    page: int = 0,
    notice: str | None = None,
) -> None:
    await _delete_preview(bot, state)
    await state.set_state(None)
    await state.update_data(archive_draft=None, archive_edit_id=None)
    data = await state.get_data()
    gifts = await _list_archived_gifts()
    prefix = f"{notice}\n\n" if notice else ""
    empty = "\n\n<i>Каталог пока пуст.</i>" if not gifts else ""
    await _edit_controller(
        bot,
        state,
        prefix
        + "🗃 <b>Управление архивными подарками</b>\n\n"
        "Каталог общий для всех администраторов. Цена задаётся вручную и должна "
        "совпадать с реальной стоимостью подарка в Telegram."
        + empty,
        archived_gift_manage_keyboard(
            gifts,
            page,
            has_recipient=bool(data.get("recipient_id")),
        ),
    )


async def _show_archive_item(
    bot: Bot,
    state: FSMContext,
    archived_gift_id: int,
    *,
    notice: str | None = None,
) -> None:
    await _delete_preview(bot, state)
    gift = await _get_archived_gift(archived_gift_id)
    if gift is None:
        await _show_archive_manager(
            bot,
            state,
            notice="❌ <b>Архивный подарок больше не существует.</b>",
        )
        return
    await state.set_state(None)
    await state.update_data(archive_edit_id=None)
    data = await state.get_data()
    prefix = f"{notice}\n\n" if notice else ""
    status = "✅ Включён" if gift.is_active else "⛔ Выключен"
    preview = "настроено" if gift.sticker_file_id else "нет"
    await _edit_controller(
        bot,
        state,
        prefix
        + "🗃 <b>Архивный подарок</b>\n\n"
        f"Название: <b>{html.escape(gift.title)}</b>\n"
        f"Эмодзи: {html.escape(gift.emoji or '🎁')}\n"
        f"Gift ID: <code>{html.escape(gift.gift_id)}</code>\n"
        f"Стоимость: <b>{gift.star_count:,} Stars</b>\n"
        f"Превью: <b>{preview}</b>\n"
        f"Статус: <b>{status}</b>\n\n"
        "Telegram не предоставляет безопасную проверку архивного ID без попытки покупки.",
        archived_gift_item_keyboard(
            gift,
            has_recipient=bool(data.get("recipient_id")),
        ),
    )
    if gift.sticker_file_id:
        try:
            preview_message = await bot.send_sticker(
                chat_id=data["controller_chat_id"],
                sticker=gift.sticker_file_id,
            )
            await state.update_data(gift_preview_message_id=preview_message.message_id)
        except Exception as exc:
            logger.info("Could not show archived Gift preview: %s", exc)


async def _show_archive_edit_prompt(
    bot: Bot,
    state: FSMContext,
    archived_gift_id: int,
    field: str,
    *,
    error: str | None = None,
) -> None:
    gift = await _get_archived_gift(archived_gift_id)
    if gift is None:
        await _show_archive_manager(
            bot,
            state,
            notice="❌ <b>Подарок был удалён другим администратором.</b>",
        )
        return
    state_map = {
        "title": AdminGiftStates.waiting_archive_edit_title,
        "price": AdminGiftStates.waiting_archive_edit_star_count,
        "emoji": AdminGiftStates.waiting_archive_edit_emoji,
        "sticker": AdminGiftStates.waiting_archive_edit_sticker,
    }
    prompt_map = {
        "title": "Отправьте новое название.",
        "price": "Отправьте новую стоимость целым числом Stars.",
        "emoji": "Отправьте новое эмодзи.",
        "sticker": "Отправьте новый стикер для превью.",
    }
    if field not in state_map:
        raise ValueError(f"Unknown archived Gift edit field: {field}")
    await state.update_data(archive_edit_id=gift.id)
    await state.set_state(state_map[field])
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        prefix
        + f"✏️ <b>Редактирование: {html.escape(gift.title)}</b>\n\n"
        + prompt_map[field],
        archived_gift_input_keyboard(
            f"admin:gifts:archive:item:{gift.id}",
            optional_callback=(
                f"admin:gifts:archive:edit:sticker:clear:{gift.id}"
                if field == "sticker"
                else None
            ),
            optional_text="Убрать превью",
        ),
    )


async def _show_archive_add_id_prompt(
    bot: Bot, state: FSMContext, *, error: str | None = None
) -> None:
    await state.set_state(AdminGiftStates.waiting_archive_gift_id)
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        prefix
        + "➕ <b>Новый архивный подарок · шаг 1/5</b>\n\n"
        "Отправьте числовой <code>gift_id</code>. ID нельзя будет изменить после создания.",
        archived_gift_input_keyboard("admin:gifts:archive:manage"),
    )


async def _show_archive_add_title_prompt(
    bot: Bot, state: FSMContext, *, error: str | None = None
) -> None:
    await state.set_state(AdminGiftStates.waiting_archive_title)
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        prefix
        + "➕ <b>Новый архивный подарок · шаг 2/5</b>\n\n"
        "Отправьте понятное название подарка.",
        archived_gift_input_keyboard("admin:gifts:archive:add:back:id"),
    )


async def _show_archive_add_price_prompt(
    bot: Bot, state: FSMContext, *, error: str | None = None
) -> None:
    await state.set_state(AdminGiftStates.waiting_archive_star_count)
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        prefix
        + "➕ <b>Новый архивный подарок · шаг 3/5</b>\n\n"
        "Отправьте исходную стоимость подарка целым числом Telegram Stars.\n\n"
        "<b>Важно:</b> Telegram не сообщает цену скрытого подарка до попытки отправки.",
        archived_gift_input_keyboard("admin:gifts:archive:add:back:title"),
    )


async def _show_archive_add_emoji_prompt(
    bot: Bot, state: FSMContext, *, error: str | None = None
) -> None:
    await state.set_state(AdminGiftStates.waiting_archive_emoji)
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        prefix
        + "➕ <b>Новый архивный подарок · шаг 4/5</b>\n\n"
        "Отправьте эмодзи для кнопки и сообщений.",
        archived_gift_input_keyboard(
            "admin:gifts:archive:add:back:price",
            optional_callback="admin:gifts:archive:add:emoji:default",
            optional_text="🎁 Использовать 🎁",
        ),
    )


async def _show_archive_add_sticker_prompt(
    bot: Bot, state: FSMContext, *, error: str | None = None
) -> None:
    await state.set_state(AdminGiftStates.waiting_archive_sticker)
    prefix = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        prefix
        + "➕ <b>Новый архивный подарок · шаг 5/5</b>\n\n"
        "Отправьте стикер для превью или пропустите этот шаг.",
        archived_gift_input_keyboard(
            "admin:gifts:archive:add:back:emoji",
            optional_callback="admin:gifts:archive:add:sticker:skip",
            optional_text="Без превью",
        ),
    )


async def _create_archived_gift_from_draft(
    bot: Bot,
    state: FSMContext,
    *,
    admin_id: int,
    admin_username: str | None,
    sticker_file_id: str | None,
) -> None:
    data = await state.get_data()
    draft = data.get("archive_draft") or {}
    required = {"gift_id", "title", "star_count", "emoji"}
    if not required.issubset(draft):
        await _show_archive_add_id_prompt(
            bot,
            state,
            error="Черновик устарел. Заполните подарок заново.",
        )
        return
    try:
        async with async_session_factory() as session:
            gift = await ArchivedGiftService(session).create(
                gift_id=draft["gift_id"],
                title=draft["title"],
                emoji=draft["emoji"],
                star_count=draft["star_count"],
                sticker_file_id=sticker_file_id,
                admin_id=admin_id,
            )
    except ArchivedGiftAlreadyExistsError:
        await _show_archive_add_id_prompt(
            bot,
            state,
            error="Подарок с таким Gift ID уже есть в архиве.",
        )
        return
    await state.update_data(archive_draft=None)
    try:
        await tg_logger.log_admin_action(
            admin_id=admin_id,
            admin_username=admin_username,
            action="Добавлен архивный Telegram Gift",
            details=(
                f"Запись: #{gift.id}\n"
                f"Название: {html.escape(gift.title)}\n"
                f"Gift ID: {html.escape(gift.gift_id)}\n"
                f"Цена: {gift.star_count} Stars"
            ),
        )
    except Exception as exc:
        logger.error("Could not log archived Gift creation: %s", exc)
    await _show_archive_item(
        bot,
        state,
        gift.id,
        notice="✅ <b>Архивный подарок добавлен.</b>",
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


async def _start_recipient_search(
    bot: Bot,
    state: FSMContext,
    *,
    chat_id: int,
    message_id: int,
    error: str | None = None,
) -> None:
    await _delete_preview(bot, state)
    await state.clear()
    await state.set_state(AdminGiftStates.waiting_recipient)
    await state.update_data(
        controller_chat_id=chat_id,
        controller_message_id=message_id,
    )
    error_text = f"❌ <b>{html.escape(error)}</b>\n\n" if error else ""
    await _edit_controller(
        bot,
        state,
        error_text
        + "🎁 <b>Подарить подарок</b>\n\n"
        "Найдите зарегистрированного пользователя. Отправьте:\n"
        "• Telegram ID;\n"
        "• <code>@username</code> или username без @;\n"
        "• реферальный код.\n\n"
        "<i>Отправить подарок незарегистрированному пользователю нельзя.</i>",
        admin_gift_search_keyboard(),
    )


async def _load_catalog(bot: Bot, state: FSMContext) -> str | None:
    try:
        gifts, balance = await asyncio.gather(
            bot.get_available_gifts(request_timeout=20),
            bot.get_my_star_balance(request_timeout=20),
        )
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


async def _ensure_preview(bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("gift_preview_message_id"):
        return
    gift = data.get("selected_gift") or {}
    sticker_file_id = gift.get("sticker_file_id")
    if not sticker_file_id:
        return
    try:
        preview = await bot.send_sticker(
            chat_id=data["controller_chat_id"],
            sticker=sticker_file_id,
        )
        await state.update_data(gift_preview_message_id=preview.message_id)
    except Exception as exc:
        logger.info("Could not show Telegram Gift sticker preview: %s", exc)


async def _show_selected(bot: Bot, state: FSMContext) -> None:
    await state.set_state(None)
    data = await state.get_data()
    gift = data.get("selected_gift")
    if not gift:
        await _show_catalog(bot, state, error="Сначала выберите подарок.")
        return
    await _ensure_preview(bot, state)
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
        "\n\n⚠️ <b>Архивный ID не входит в текущий каталог Telegram.</b> "
        "Цена указана вручную, а доступность будет известна только при отправке."
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


async def _resume_gift_attempt(bot: Bot, attempt_id: int) -> tuple[AdminGift, str, str]:
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
    if attempt.status in {
        AdminGiftStatus.UNKNOWN.value,
        AdminGiftStatus.SENDING.value,
    }:
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
            gifts, balance = await asyncio.gather(
                bot.get_available_gifts(request_timeout=20),
                bot.get_my_star_balance(request_timeout=20),
            )
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


@router.callback_query(F.data == "admin:gifts:archive:manage")
async def callback_admin_gift_archive_manage(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)
    await _show_archive_manager(bot, state)


@router.callback_query(F.data.regexp(r"^admin:gifts:archive:manage:page:\d+$"))
async def callback_admin_gift_archive_manage_page(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)
    await _show_archive_manager(bot, state, page=int(callback.data.rsplit(":", 1)[-1]))


@router.callback_query(F.data == "admin:gifts:archive:choose")
async def callback_admin_gift_archive_choose(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
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


@router.callback_query(F.data.regexp(r"^admin:gifts:archive:item:\d+$"))
async def callback_admin_gift_archive_item(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)
    await _show_archive_item(bot, state, int(callback.data.rsplit(":", 1)[-1]))


@router.callback_query(F.data == "admin:gifts:archive:add")
async def callback_admin_gift_archive_add(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)
    await _delete_preview(bot, state)
    await state.update_data(archive_draft={})
    await _show_archive_add_id_prompt(bot, state)


@router.message(AdminGiftStates.waiting_archive_gift_id)
async def message_admin_gift_archive_id(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    try:
        gift_id = validate_archived_gift_id(message.text or "")
    except ValueError as exc:
        await _show_archive_add_id_prompt(bot, state, error=str(exc))
        return
    async with async_session_factory() as session:
        existing = await ArchivedGiftService(session).get_by_gift_id(gift_id)
    if existing is not None:
        await _show_archive_add_id_prompt(
            bot,
            state,
            error=f"Gift ID уже сохранён как «{existing.title}».",
        )
        return
    data = await state.get_data()
    draft = dict(data.get("archive_draft") or {})
    draft["gift_id"] = gift_id
    await state.update_data(archive_draft=draft)
    await _show_archive_add_title_prompt(bot, state)


@router.message(AdminGiftStates.waiting_archive_title)
async def message_admin_gift_archive_title(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    try:
        title = validate_archived_gift_title(message.text or "")
    except ValueError as exc:
        await _show_archive_add_title_prompt(bot, state, error=str(exc))
        return
    data = await state.get_data()
    draft = dict(data.get("archive_draft") or {})
    draft["title"] = title
    await state.update_data(archive_draft=draft)
    await _show_archive_add_price_prompt(bot, state)


@router.message(AdminGiftStates.waiting_archive_star_count)
async def message_admin_gift_archive_price(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    try:
        price = validate_archived_gift_price(message.text or "")
    except ValueError as exc:
        await _show_archive_add_price_prompt(bot, state, error=str(exc))
        return
    data = await state.get_data()
    draft = dict(data.get("archive_draft") or {})
    draft["star_count"] = price
    await state.update_data(archive_draft=draft)
    await _show_archive_add_emoji_prompt(bot, state)


@router.message(AdminGiftStates.waiting_archive_emoji)
async def message_admin_gift_archive_emoji(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    try:
        emoji = validate_archived_gift_emoji(message.text or "")
    except ValueError as exc:
        await _show_archive_add_emoji_prompt(bot, state, error=str(exc))
        return
    data = await state.get_data()
    draft = dict(data.get("archive_draft") or {})
    draft["emoji"] = emoji
    await state.update_data(archive_draft=draft)
    await _show_archive_add_sticker_prompt(bot, state)


@router.message(AdminGiftStates.waiting_archive_sticker)
async def message_admin_gift_archive_sticker(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    if message.sticker is None:
        await _show_archive_add_sticker_prompt(
            bot,
            state,
            error="Нужно отправить стикер или нажать «Без превью».",
        )
        return
    await _create_archived_gift_from_draft(
        bot,
        state,
        admin_id=message.from_user.id,
        admin_username=message.from_user.username,
        sticker_file_id=message.sticker.file_id,
    )


@router.callback_query(F.data == "admin:gifts:archive:add:emoji:default")
async def callback_admin_gift_archive_default_emoji(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)
    data = await state.get_data()
    draft = dict(data.get("archive_draft") or {})
    draft["emoji"] = "🎁"
    await state.update_data(archive_draft=draft)
    await _show_archive_add_sticker_prompt(bot, state)


@router.callback_query(F.data == "admin:gifts:archive:add:sticker:skip")
async def callback_admin_gift_archive_skip_sticker(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback, "Сохраняю…")
    await _create_archived_gift_from_draft(
        bot,
        state,
        admin_id=callback.from_user.id,
        admin_username=callback.from_user.username,
        sticker_file_id=None,
    )


@router.callback_query(
    F.data.in_(
        {
            "admin:gifts:archive:add:back:id",
            "admin:gifts:archive:add:back:title",
            "admin:gifts:archive:add:back:price",
            "admin:gifts:archive:add:back:emoji",
        }
    )
)
async def callback_admin_gift_archive_add_back(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)
    target = callback.data.rsplit(":", 1)[-1]
    prompts = {
        "id": _show_archive_add_id_prompt,
        "title": _show_archive_add_title_prompt,
        "price": _show_archive_add_price_prompt,
        "emoji": _show_archive_add_emoji_prompt,
    }
    await prompts[target](bot, state)


@router.callback_query(F.data.regexp(r"^admin:gifts:archive:set:[01]:\d+$"))
async def callback_admin_gift_archive_set_active(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    parts = callback.data.split(":")
    is_active = parts[-2] == "1"
    archived_gift_id = int(parts[-1])
    await safe_callback_answer(
        callback,
        "Включаю…" if is_active else "Выключаю…",
    )
    try:
        async with async_session_factory() as session:
            gift = await ArchivedGiftService(session).set_active(
                archived_gift_id,
                is_active=is_active,
            )
    except LookupError:
        await _show_archive_manager(
            bot,
            state,
            notice="❌ <b>Подарок уже удалён.</b>",
        )
        return
    try:
        await tg_logger.log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action=(
                "Включён архивный Telegram Gift"
                if gift.is_active
                else "Выключен архивный Telegram Gift"
            ),
            details=(
                f"Название: {html.escape(gift.title)}\n"
                f"Gift ID: {html.escape(gift.gift_id)}"
            ),
        )
    except Exception as exc:
        logger.error("Could not log archived Gift status change: %s", exc)
    await _show_archive_item(
        bot,
        state,
        gift.id,
        notice="✅ <b>Статус архивного подарка изменён.</b>",
    )


@router.callback_query(F.data.regexp(r"^admin:gifts:archive:delete:\d+$"))
async def callback_admin_gift_archive_delete(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)
    archived_gift_id = int(callback.data.rsplit(":", 1)[-1])
    gift = await _get_archived_gift(archived_gift_id)
    if gift is None:
        await _show_archive_manager(bot, state)
        return
    await _delete_preview(bot, state)
    await _edit_controller(
        bot,
        state,
        "🗑 <b>Удалить архивный подарок?</b>\n\n"
        f"{html.escape(gift.emoji or '🎁')} <b>{html.escape(gift.title)}</b>\n"
        f"Gift ID: <code>{html.escape(gift.gift_id)}</code>\n\n"
        "Сохранённые операции отправки останутся в журнале.",
        archived_gift_delete_keyboard(gift.id),
    )


@router.callback_query(F.data.regexp(r"^admin:gifts:archive:delete:confirm:\d+$"))
async def callback_admin_gift_archive_delete_confirm(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback, "Удаляю…")
    archived_gift_id = int(callback.data.rsplit(":", 1)[-1])
    try:
        async with async_session_factory() as session:
            gift = await ArchivedGiftService(session).delete(archived_gift_id)
    except LookupError:
        gift = None
    if gift is not None:
        try:
            await tg_logger.log_admin_action(
                admin_id=callback.from_user.id,
                admin_username=callback.from_user.username,
                action="Удалён архивный Telegram Gift",
                details=(
                    f"Название: {html.escape(gift.title)}\n"
                    f"Gift ID: {html.escape(gift.gift_id)}"
                ),
            )
        except Exception as exc:
            logger.error("Could not log archived Gift deletion: %s", exc)
    await _show_archive_manager(
        bot,
        state,
        notice="✅ <b>Архивный подарок удалён.</b>",
    )


@router.callback_query(
    F.data.regexp(r"^admin:gifts:archive:edit:(title|price|emoji|sticker):\d+$")
)
async def callback_admin_gift_archive_edit(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback)
    parts = callback.data.split(":")
    field = parts[-2]
    archived_gift_id = int(parts[-1])
    await _delete_preview(bot, state)
    await _show_archive_edit_prompt(bot, state, archived_gift_id, field)


async def _save_archived_gift_edit(
    state: FSMContext,
    bot: Bot,
    *,
    admin_id: int,
    admin_username: str | None,
    **values,
) -> None:
    data = await state.get_data()
    archived_gift_id = data.get("archive_edit_id")
    if not archived_gift_id:
        await _show_archive_manager(
            bot,
            state,
            notice="❌ <b>Сессия редактирования устарела.</b>",
        )
        return
    try:
        async with async_session_factory() as session:
            gift = await ArchivedGiftService(session).update_fields(
                archived_gift_id,
                **values,
            )
    except LookupError:
        await _show_archive_manager(
            bot,
            state,
            notice="❌ <b>Подарок был удалён другим администратором.</b>",
        )
        return
    field_labels = {
        "title": "Название",
        "emoji": "Эмодзи",
        "star_count": "Стоимость",
        "sticker_file_id": "Превью",
    }
    changes: list[str] = []
    for field, value in values.items():
        if field == "sticker_file_id":
            rendered = "установлено" if value else "удалено"
        elif field == "star_count":
            rendered = f"{value} Stars"
        else:
            rendered = html.escape(str(value))
        changes.append(f"{field_labels.get(field, field)}: {rendered}")
    try:
        await tg_logger.log_admin_action(
            admin_id=admin_id,
            admin_username=admin_username,
            action="Изменён архивный Telegram Gift",
            details=(
                f"Название: {html.escape(gift.title)}\n"
                f"Gift ID: {html.escape(gift.gift_id)}\n"
                + "\n".join(changes)
            ),
        )
    except Exception as exc:
        logger.error("Could not log archived Gift edit: %s", exc)
    await _show_archive_item(
        bot,
        state,
        gift.id,
        notice="✅ <b>Изменения сохранены.</b>",
    )


@router.message(AdminGiftStates.waiting_archive_edit_title)
async def message_admin_gift_archive_edit_title(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    try:
        value = validate_archived_gift_title(message.text or "")
    except ValueError as exc:
        data = await state.get_data()
        await _show_archive_edit_prompt(
            bot,
            state,
            int(data.get("archive_edit_id") or 0),
            "title",
            error=str(exc),
        )
        return
    await _save_archived_gift_edit(
        state,
        bot,
        admin_id=message.from_user.id,
        admin_username=message.from_user.username,
        title=value,
    )


@router.message(AdminGiftStates.waiting_archive_edit_star_count)
async def message_admin_gift_archive_edit_price(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    try:
        value = validate_archived_gift_price(message.text or "")
    except ValueError as exc:
        data = await state.get_data()
        await _show_archive_edit_prompt(
            bot,
            state,
            int(data.get("archive_edit_id") or 0),
            "price",
            error=str(exc),
        )
        return
    await _save_archived_gift_edit(
        state,
        bot,
        admin_id=message.from_user.id,
        admin_username=message.from_user.username,
        star_count=value,
    )


@router.message(AdminGiftStates.waiting_archive_edit_emoji)
async def message_admin_gift_archive_edit_emoji(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    try:
        value = validate_archived_gift_emoji(message.text or "")
    except ValueError as exc:
        data = await state.get_data()
        await _show_archive_edit_prompt(
            bot,
            state,
            int(data.get("archive_edit_id") or 0),
            "emoji",
            error=str(exc),
        )
        return
    await _save_archived_gift_edit(
        state,
        bot,
        admin_id=message.from_user.id,
        admin_username=message.from_user.username,
        emoji=value,
    )


@router.message(AdminGiftStates.waiting_archive_edit_sticker)
async def message_admin_gift_archive_edit_sticker(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin_message(message):
        return
    await _delete_admin_input(message)
    if message.sticker is None:
        data = await state.get_data()
        await _show_archive_edit_prompt(
            bot,
            state,
            int(data.get("archive_edit_id") or 0),
            "sticker",
            error="Отправьте стикер или нажмите «Убрать превью».",
        )
        return
    await _save_archived_gift_edit(
        state,
        bot,
        admin_id=message.from_user.id,
        admin_username=message.from_user.username,
        sticker_file_id=message.sticker.file_id,
    )


@router.callback_query(
    F.data.regexp(r"^admin:gifts:archive:edit:sticker:clear:\d+$")
)
async def callback_admin_gift_archive_edit_sticker_clear(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await safe_callback_answer(callback, "Удаляю превью…")
    archived_gift_id = int(callback.data.rsplit(":", 1)[-1])
    await state.update_data(archive_edit_id=archived_gift_id)
    await _save_archived_gift_edit(
        state,
        bot,
        admin_id=callback.from_user.id,
        admin_username=callback.from_user.username,
        sticker_file_id=None,
    )


@router.message(AdminGiftStates.waiting_recipient)
async def message_admin_gift_recipient(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await check_admin_message(message):
        return
    value = message.text or ""
    await _delete_admin_input(message)
    user = await _find_registered_user(value)
    if user is None:
        data = await state.get_data()
        await _start_recipient_search(
            bot,
            state,
            chat_id=data["controller_chat_id"],
            message_id=data["controller_message_id"],
            error="Пользователь не найден среди зарегистрированных в боте.",
        )
        return

    await state.update_data(
        recipient_id=user.id,
        recipient_username=user.username,
        recipient_is_banned=user.is_banned,
    )
    await _edit_controller(
        bot,
        state,
        "⏳ <b>Загружаю доступные подарки Telegram…</b>",
        None,
    )
    error = await _load_catalog(bot, state)
    await _show_catalog(bot, state, error=error)


@router.callback_query(F.data == "admin:gifts:recipient")
async def callback_admin_gift_recipient(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    data = await state.get_data()
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
    await safe_callback_answer(callback, "Обновляю каталог…")
    error = await _load_catalog(bot, state)
    await _show_catalog(bot, state, error=error)


@router.callback_query(F.data == "admin:gifts:catalog")
async def callback_admin_gift_catalog(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await _delete_preview(bot, state)
    await _show_catalog(bot, state)
    await safe_callback_answer(callback)


@router.callback_query(F.data.regexp(r"^admin:gifts:page:\d+$"))
async def callback_admin_gift_page(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    data = await state.get_data()
    if not all(
        data.get(key)
        for key in (
            "controller_chat_id",
            "controller_message_id",
            "recipient_id",
            "available_gifts",
        )
    ):
        await _start_recipient_search(
            bot,
            state,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            error="Сессия выбора подарка устарела. Выберите получателя заново.",
        )
        await safe_callback_answer(callback, "Сессия выбора обновлена")
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
    data = await state.get_data()
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
    await _show_selected(bot, state)
    await safe_callback_answer(callback)


@router.callback_query(F.data == "admin:gifts:comment")
async def callback_admin_gift_comment(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
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
    async with lock:
        await _process_admin_gift_confirm(callback, state, bot)


async def _process_admin_gift_confirm(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    selected = data.get("selected_gift")
    operation_key = data.get("operation_key")
    gift_text = data.get("gift_text")
    recipient_id = data.get("recipient_id")
    if (
        not selected
        or not operation_key
        or gift_text is None
        or not recipient_id
    ):
        await _start_recipient_search(
            bot,
            state,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            error="Данные отправки устарели. Выберите получателя заново.",
        )
        await safe_callback_answer(
            callback,
            "Данные устарели. Форма восстановлена.",
            show_alert=True,
        )
        return

    await safe_callback_answer(callback, "Проверяю и отправляю…")
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
        if recipient is None:
            await _start_recipient_search(
                bot,
                state,
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                error="Пользователь больше не зарегистрирован в боте.",
            )
            return

        service = AdminGiftService(session, bot)
        attempt, created_attempt = await service.create_or_get_attempt(
            operation_key=operation_key,
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            recipient_id=recipient.id,
            recipient_username=recipient.username,
            recipient_was_banned=recipient.is_banned,
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
            attempt, status, text = await _resume_gift_attempt(bot, attempt.id)
            reply_markup = (
                admin_gift_payment_wait_keyboard(attempt.id)
                if status in {
                    AdminGiftStatus.AWAITING_PAYMENT.value,
                    AdminGiftStatus.PENDING.value,
                }
                else admin_gift_result_keyboard()
            )
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
                        f"Получатель: {recipient.id}\n"
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
            "Подарок не был отправлен. Автоматического повтора не будет.\n\n"
            f"🧾 Операция: <code>#{outcome.attempt.id}</code>\n"
            f"Ошибка: <code>{html.escape((outcome.error or 'неизвестная ошибка')[:500])}</code>"
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
            except GiftInvoiceAlreadyPaidError:
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
    except GiftInvoiceAlreadyPaidError as exc:
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
                "↩️ <b>Повторная оплата возвращена</b>\n\n"
                "Этот счёт уже был оплачен ранее, поэтому Telegram Stars "
                "возвращены на ваш аккаунт.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "⚠️ <b>Обнаружена повторная оплата</b>\n\n"
                "Автоматический возврат не удался. Обратитесь к владельцу бота и "
                f"передайте Charge ID: <code>{html.escape(successful.telegram_payment_charge_id)}</code>",
                parse_mode="HTML",
            )
        try:
            await tg_logger.log_error(
                error_type="AdminGiftDuplicateCharge",
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

    attempt, status, text = await _resume_gift_attempt(bot, attempt.id)
    reply_markup = (
        admin_gift_payment_wait_keyboard(attempt.id)
        if status in {
            AdminGiftStatus.AWAITING_PAYMENT.value,
            AdminGiftStatus.PENDING.value,
        }
        else admin_gift_result_keyboard()
    )
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
    attempt, status, text = await _resume_gift_attempt(bot, attempt_id)
    reply_markup = (
        admin_gift_payment_wait_keyboard(attempt.id)
        if status in {
            AdminGiftStatus.AWAITING_PAYMENT.value,
            AdminGiftStatus.PENDING.value,
        }
        else admin_gift_result_keyboard()
    )
    await state.clear()
    await callback.message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:gifts:cancel")
async def callback_admin_gift_cancel(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await check_admin(callback):
        return
    await _delete_preview(bot, state)
    await state.clear()
    await callback.message.edit_text(
        "🔐 <b>Админ-панель</b>\n\nВыберите раздел:",
        reply_markup=get_admin_menu_keyboard(),
        parse_mode="HTML",
    )
    await safe_callback_answer(callback, "Отправка отменена")
