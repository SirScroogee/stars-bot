"""
Handlers для раздела чеков.
"""
import html
import json
import logging
import secrets
from decimal import Decimal
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.bot.keyboards.menu import MenuCallback
from src.bot.keyboards.checks import (
    ChecksCallback,
    get_checks_menu_keyboard,
    get_check_content_keyboard,
    get_check_amount_keyboard,
    get_check_premium_duration_keyboard,
    get_check_activations_keyboard,
    get_check_activations_premium_keyboard,
    get_check_recipient_keyboard,
    get_check_password_keyboard,
    get_check_password_keyboard_multi,
    get_check_channel_keyboard,
    get_check_payment_keyboard,
    get_topup_keyboard,
    get_back_to_checks_keyboard,
    get_my_checks_keyboard,
    get_check_detail_keyboard,
    get_delete_confirm_keyboard,
    get_description_edit_keyboard,
    get_restrictions_keyboard,
    get_restriction_password_keyboard,
    get_restriction_user_keyboard,
    get_restriction_toggle_keyboard,
    get_restriction_subscription_keyboard,
)
from sqlalchemy import select

from src.db.models import BotChannel, Check
from src.db.session import async_session_factory
from src.locales import t, get_user_locale
from src.services.user_service import UserService
from src.services.telegram_logger import tg_logger
from src.services.bot_settings_service import get_star_price, get_premium_prices
from src.services.rub_rate_service import format_usdt_with_rub
from src.bot.handlers.deposit import DepositStates, get_deposit_payment_methods_keyboard
from src.bot.menu_media import edit_menu_message

logger = logging.getLogger(__name__)

router = Router(name="checks")


async def _is_checks_admin(user_id: int) -> bool:
    """Проверить доступ к разделу чеков."""
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user_id)
        return bool(db_user and db_user.is_admin)


class ChecksAdminOnlyMiddleware(BaseMiddleware):
    """Закрывает раздел чеков от обычных пользователей."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user and await _is_checks_admin(user.id):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("Такого раздела не существует", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("Такого раздела не существует")
        return None


router.callback_query.middleware(ChecksAdminOnlyMiddleware())
router.message.middleware(ChecksAdminOnlyMiddleware())

# Константы для звёзд
MIN_CHECK_AMOUNT = 1  # Минимум звёзд за активацию
MAX_CHECK_AMOUNT = 10000
MIN_ACTIVATIONS = 1  # Минимум активаций (1 = одноразовый, >1 = мульти)

# Лимит на количество каналов/групп в одном чеке
MAX_CHANNELS_PER_CHECK = 3


async def calculate_max_activations(
    amount_per_activation: int,
    content_type: str,
    balance_stars: Decimal,
    balance_usdt: Decimal,
    balance_premium: int,
) -> dict:
    """Рассчитать максимальное количество активаций для каждого типа баланса."""
    result = {"stars": 0, "usdt": 0, "premium": 0}

    if amount_per_activation <= 0:
        return result

    if content_type == "stars":
        # Для чека со звёздами
        if balance_stars > 0:
            result["stars"] = int(balance_stars // amount_per_activation)
        if balance_usdt > 0:
            star_price = await get_star_price()
            price_per = Decimal(amount_per_activation) * star_price
            result["usdt"] = int(balance_usdt // price_per) if price_per > 0 else 0
    else:
        # Для чека с Premium
        if balance_premium > 0:
            result["premium"] = balance_premium // amount_per_activation
        if balance_usdt > 0:
            premium_prices = await get_premium_prices()
            price_per = premium_prices.get(amount_per_activation, Decimal("0"))
            result["usdt"] = int(balance_usdt // price_per) if price_per > 0 else 0

    return result


# Доступные сроки Premium
PREMIUM_DURATIONS = [3, 6, 12]

# Пароль
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 32

# Цены берутся из БД через get_star_price() и get_premium_prices()


async def get_user_language(user_id: int, state: FSMContext, telegram_lang_code: str | None = None) -> str:
    """Получить язык пользователя из state или из БД.

    Args:
        user_id: Telegram user ID
        state: FSMContext для проверки кэшированного языка
        telegram_lang_code: Код языка из Telegram (fallback)

    Returns:
        Код языка (ru/en)
    """
    # Сначала проверяем в state
    data = await state.get_data()
    lang = data.get("lang")
    if lang:
        return lang

    # Если нет в state - получаем из БД
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user_id)
        if db_user and db_user.language_code:
            lang = db_user.language_code
            # Кэшируем в state
            await state.update_data(lang=lang)
            return lang

    # Fallback на Telegram язык или русский
    lang = get_user_locale(telegram_lang_code) if telegram_lang_code else "ru"
    await state.update_data(lang=lang)
    return lang


class CreateCheckStates(StatesGroup):
    """Состояния для создания чека."""

    waiting_content = State()  # Выбор содержимого (звёзды / Premium)
    waiting_amount = State()   # Ввод суммы (для звёзд)
    waiting_premium_duration = State()  # Выбор срока Premium
    waiting_activations = State()  # Количество активаций
    waiting_recipient = State()
    waiting_password = State()
    waiting_channel = State()  # Выбор каналов из списка
    waiting_payment = State()  # Выбор способа оплаты


class EditCheckStates(StatesGroup):
    """Состояния для редактирования чека."""

    waiting_description = State()  # Ввод описания
    waiting_photo = State()
    waiting_password = State()  # Ввод пароля
    waiting_recipient = State()  # Ввод получателя


def generate_check_code() -> str:
    """Генерировать уникальный код чека."""
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12].upper()


async def build_check_detail_text(check: Check, check_link: str, lang: str = "ru") -> str:
    """Сформировать текст детального просмотра чека.

    Формат:
    - Для одноразового: заголовок, сумма в цитате, описание (если есть), ссылка
    - Для мульти: заголовок, суммы в цитате, активации в цитате, описание, ссылка
    """
    is_multi = check.max_activations > 1
    remaining = check.max_activations - check.current_activations

    # Убираем https:// из ссылки
    short_link = check_link.replace("https://", "")

    # Определяем содержимое и цену
    if check.content_type == "premium":
        amount_per = check.amount_premium_months
        total_amount = amount_per * check.max_activations
        used_amount = amount_per * check.current_activations
        remaining_amount = amount_per * remaining
        unit = t("checks.units.months_premium", lang)
        # Цена Premium из БД
        premium_prices = await get_premium_prices()
        price_per = premium_prices.get(amount_per, Decimal("0"))
        price_total = price_per * check.max_activations
    else:
        amount_per = int(check.amount_stars)
        total_amount = amount_per * check.max_activations
        used_amount = amount_per * check.current_activations
        remaining_amount = amount_per * remaining
        unit = t("checks.units.stars", lang)
        # Цена Stars из БД
        star_price = await get_star_price()
        price_per = Decimal(amount_per) * star_price
        price_total = Decimal(total_amount) * star_price

    if is_multi:
        # Мульти-чек
        lines = [
            t("checks.share.multicheck_title", lang, code=check.code),
            "",
            # Блок сумм в цитате
            "<blockquote>" + t("checks.share.total_amount", lang, amount=total_amount, unit=unit, price=f"{price_total:.2f}"),
            t("checks.share.per_activation", lang, amount=amount_per, unit=unit, price=f"{price_per:.2f}") + "</blockquote>",
            "",
            # Блок активаций в цитате
            "<blockquote>" + t("checks.share.activations_count", lang, count=check.max_activations),
            t("checks.share.activated", lang, count=check.current_activations, amount=used_amount, unit=unit),
            t("checks.share.remaining", lang, count=remaining, amount=remaining_amount, unit=unit) + "</blockquote>",
        ]
    else:
        # Одноразовый чек
        lines = [
            t("checks.share.check_title", lang, code=check.code),
            "",
            # Сумма в цитате
            "<blockquote>" + t("checks.share.amount", lang, amount=amount_per, unit=unit, price=f"{price_per:.2f}") + "</blockquote>",
        ]

    # Добавляем описание если есть (экранируем HTML для безопасности)
    if check.description:
        safe_description = html.escape(check.description)
        lines.append(f"\n💬 {safe_description}")

    # Ссылка в скрытом блоке (spoiler)
    lines.append(f"\n{t('checks.share.copy_link', lang)}\n<tg-spoiler>{short_link}</tg-spoiler>")

    return "\n".join(lines)


async def get_available_channels(user_id: int) -> list[dict]:
    """Получить список каналов, добавленных пользователем.

    Args:
        user_id: ID пользователя Telegram

    Returns:
        list: [{"id": ..., "title": ..., "username": ...}]
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(BotChannel).where(
                BotChannel.added_by_user_id == user_id,
                BotChannel.is_active == True,
            )
        )
        channels = result.scalars().all()

        return [
            {
                "id": ch.channel_id,
                "title": ch.channel_title,
                "username": ch.channel_username or "",
            }
            for ch in channels
        ]


async def refresh_channels_from_bot(bot: Bot, user_id: int) -> list[dict]:
    """Обновить список каналов и проверить актуальность.

    Проверяет каналы пользователя в БД и обновляет их статус.
    Args:
        bot: Telegram Bot instance.
        user_id: ID пользователя, чьи каналы нужно обновить.
    Returns:
        list: Актуальный список каналов пользователя где бот админ.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(BotChannel).where(BotChannel.added_by_user_id == user_id)
        )
        channels = result.scalars().all()

        active_channels = []

        for channel in channels:
            try:
                # Проверяем, является ли бот всё ещё админом
                bot_member = await bot.get_chat_member(channel.channel_id, (await bot.get_me()).id)
                is_admin = bot_member.status in ("administrator", "creator")

                if is_admin:
                    # Обновляем информацию о канале
                    chat = await bot.get_chat(channel.channel_id)
                    channel.channel_title = chat.title or str(channel.channel_id)
                    channel.channel_username = chat.username
                    channel.is_active = True
                    active_channels.append({
                        "id": channel.channel_id,
                        "title": channel.channel_title,
                        "username": channel.channel_username or "",
                    })
                else:
                    channel.is_active = False
            except Exception as e:
                logger.warning(f"Failed to check channel {channel.channel_id}: {e}")
                channel.is_active = False

        await session.commit()
        return active_channels


def get_channel_selection_text(
    lang: str,
    available_channels: list[dict] | None,
) -> str:
    """Получить текст для экрана выбора каналов."""
    base_text = (
        f"{t('checks.channel.title', lang)}\n\n"
        f"{t('checks.channel.description', lang)}"
    )

    if not available_channels:
        base_text += f"\n\n{t('checks.channel.no_channels', lang)}"

    return base_text


async def safe_delete_message(message: Message) -> None:
    """Безопасное удаление сообщения."""
    try:
        await message.delete()
    except Exception:
        pass


async def safe_edit_message(
    message: Message,
    text: str,
    reply_markup=None,
) -> bool:
    """Безопасное редактирование сообщения. Возвращает True если успешно."""
    try:
        await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        logger.debug(f"Failed to edit message: {e}")
        try:
            await message.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return True
        except Exception as caption_error:
            logger.debug(f"Failed to edit caption: {caption_error}")
        return False


async def edit_bot_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup=None,
) -> None:
    """Редактировать сообщение бота."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to edit message: {e}")
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except Exception as caption_error:
            logger.warning(f"Failed to edit caption: {caption_error}")


def get_checks_menu_text(
    balance_usdt: Decimal,
    balance_stars: Decimal,
    balance_premium: int,
    lang: str = "ru",
) -> str:
    """Текст главного меню чеков."""
    return (
        f"{t('checks.title', lang)}\n\n"
        f"{t('checks.description', lang, balance_usdt=f'{balance_usdt:,.2f}', balance_stars=f'{balance_stars:,.0f}', balance_premium=balance_premium)}"
    )


# ==================== ГЛАВНОЕ МЕНЮ ЧЕКОВ ====================

@router.callback_query(F.data == MenuCallback.CHECKS)
async def callback_checks_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню раздела чеков."""
    await state.clear()
    user = callback.from_user
    # Определяем язык до проверки пользователя
    lang = get_user_locale(user.language_code)

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Обновляем язык из БД если есть
        lang = db_user.language_code or lang
        await state.update_data(bot_message_id=callback.message.message_id, lang=lang)

        sent_message = await edit_menu_message(
            callback,
            "checks",
            text=get_checks_menu_text(
                db_user.balance_usdt,
                db_user.balance_stars,
                db_user.balance_premium_months,
                lang,
            ),
            reply_markup=get_checks_menu_keyboard(lang),
        )
        if sent_message:
            await state.update_data(bot_message_id=sent_message.message_id)

    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_CHECKS)
async def callback_back_to_checks(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в меню чеков."""
    await state.clear()
    user = callback.from_user
    # Определяем язык до проверки пользователя
    lang = get_user_locale(user.language_code)

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Обновляем язык из БД если есть
        lang = db_user.language_code or lang
        await state.update_data(bot_message_id=callback.message.message_id, lang=lang)

        sent_message = await edit_menu_message(
            callback,
            "checks",
            text=get_checks_menu_text(
                db_user.balance_usdt,
                db_user.balance_stars,
                db_user.balance_premium_months,
                lang,
            ),
            reply_markup=get_checks_menu_keyboard(lang),
        )
        if sent_message:
            await state.update_data(bot_message_id=sent_message.message_id)

    await callback.answer()


# ==================== СОЗДАНИЕ ЧЕКА ====================

@router.callback_query(F.data == ChecksCallback.CREATE)
async def callback_create_check(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало создания чека - выбор содержимого."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.set_state(CreateCheckStates.waiting_content)
    await state.update_data(bot_message_id=callback.message.message_id)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.content.title', lang)}\n\n"
            f"{t('checks.content.description', lang)}"
        ),
        reply_markup=get_check_content_keyboard(lang),
    )
    await callback.answer()


# Выбор содержимого чека
@router.callback_query(F.data == ChecksCallback.CONTENT_STARS, CreateCheckStates.waiting_content)
async def callback_content_stars(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор звёзд как содержимого."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.update_data(content_type="stars")
    # Сразу переходим к вводу суммы (без выбора типа)
    await state.set_state(CreateCheckStates.waiting_amount)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.amount.title', lang)}\n\n"
            f"{t('checks.amount.description', lang, min=MIN_CHECK_AMOUNT, max=f'{MAX_CHECK_AMOUNT:,}')}"
        ),
        reply_markup=get_check_amount_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.CONTENT_PREMIUM, CreateCheckStates.waiting_content)
async def callback_content_premium(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор Premium как содержимого."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.update_data(content_type="premium")
    # Сразу переходим к выбору срока Premium (без выбора типа)
    await state.set_state(CreateCheckStates.waiting_premium_duration)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.premium_duration.title', lang)}\n\n"
            f"{t('checks.premium_duration.description', lang)}"
        ),
        reply_markup=get_check_premium_duration_keyboard(lang),
    )
    await callback.answer()


# Выбор срока Premium
@router.callback_query(F.data.startswith("checks:premium:"), CreateCheckStates.waiting_premium_duration)
async def callback_premium_duration(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор срока Premium."""
    months = int(callback.data.split(":")[-1])
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.update_data(amount=months, premium_months=months)

    # Всегда переходим к выбору количества активаций
    await _show_activations_screen(callback, state, months, "premium", lang)
    await callback.answer()


# Ввод суммы чека (кнопки)
@router.callback_query(F.data.startswith("checks:amount:"), CreateCheckStates.waiting_amount)
async def callback_amount_preset(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор суммы через кнопку."""
    amount = int(callback.data.split(":")[-1])
    await _process_amount(callback, state, amount)


# Ввод суммы чека (текст)
@router.message(CreateCheckStates.waiting_amount)
async def message_amount(message: Message, state: FSMContext) -> None:
    """Ввод суммы чека вручную."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")
    content_type = data.get("content_type", "stars")

    try:
        amount = int(message.text.strip())
    except ValueError:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('checks.amount.title', lang)}\n\n"
                    f"{t('checks.amount.invalid', lang, min=MIN_CHECK_AMOUNT, max=MAX_CHECK_AMOUNT)}\n\n"
                    f"<blockquote>{t('checks.amount.min', lang, min=MIN_CHECK_AMOUNT)}\n"
                    f"{t('checks.amount.max', lang, max=f'{MAX_CHECK_AMOUNT:,}')}</blockquote>"
                ),
                reply_markup=get_check_amount_keyboard(lang),
            )
        return

    if amount < MIN_CHECK_AMOUNT or amount > MAX_CHECK_AMOUNT:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('checks.amount.title', lang)}\n\n"
                    f"{t('checks.amount.invalid', lang, min=MIN_CHECK_AMOUNT, max=MAX_CHECK_AMOUNT)}\n\n"
                    f"<blockquote>{t('checks.amount.min', lang, min=MIN_CHECK_AMOUNT)}\n"
                    f"{t('checks.amount.max', lang, max=f'{MAX_CHECK_AMOUNT:,}')}</blockquote>"
                ),
                reply_markup=get_check_amount_keyboard(lang),
            )
        return

    await state.update_data(amount=amount)

    # Всегда переходим к выбору количества активаций
    await state.set_state(CreateCheckStates.waiting_activations)
    if bot_message_id:
        # Получаем информацию о балансе
        async with async_session_factory() as session:
            user_service = UserService(session)
            db_user = await user_service.get_user(message.from_user.id)
            if db_user:
                max_acts = await calculate_max_activations(
                    amount_per_activation=amount,
                    content_type=content_type,
                    balance_stars=db_user.balance_stars,
                    balance_usdt=db_user.balance_usdt,
                    balance_premium=db_user.balance_premium_months,
                )
                # Формируем текст о балансе
                if content_type == "stars":
                    balance_info = t('checks.activations.balance_info_stars', lang,
                                     usdt_balance=f"{db_user.balance_usdt:.2f}",
                                     usdt_acts=max_acts['usdt'],
                                     stars_balance=f"{db_user.balance_stars:,.0f}",
                                     stars_acts=max_acts['stars'])
                else:
                    balance_info = t('checks.activations.balance_info_premium', lang,
                                     usdt_balance=f"{db_user.balance_usdt:.2f}",
                                     usdt_acts=max_acts['usdt'],
                                     premium_balance=db_user.balance_premium_months,
                                     premium_acts=max_acts['premium'])
            else:
                balance_info = ""

        # Выбираем правильную клавиатуру в зависимости от типа содержимого
        if content_type == "premium":
            keyboard = get_check_activations_premium_keyboard(lang)
        else:
            keyboard = get_check_activations_keyboard(lang)

        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('checks.activations.title', lang)}\n\n"
                f"{balance_info}\n\n"
                f"{t('checks.activations.description', lang, min=MIN_ACTIVATIONS)}"
            ),
            reply_markup=keyboard,
        )


async def _show_activations_screen(callback: CallbackQuery, state: FSMContext, amount: int, content_type: str, lang: str) -> None:
    """Показать экран выбора количества активаций с информацией о балансе."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Рассчитываем максимальное количество активаций для каждого баланса
        max_acts = await calculate_max_activations(
            amount_per_activation=amount,
            content_type=content_type,
            balance_stars=db_user.balance_stars,
            balance_usdt=db_user.balance_usdt,
            balance_premium=db_user.balance_premium_months,
        )

        # Формируем текст о балансе
        if content_type == "stars":
            balance_info = t('checks.activations.balance_info_stars', lang,
                             usdt_balance=f"{db_user.balance_usdt:.2f}",
                             usdt_acts=max_acts['usdt'],
                             stars_balance=f"{db_user.balance_stars:,.0f}",
                             stars_acts=max_acts['stars'])
        else:
            balance_info = t('checks.activations.balance_info_premium', lang,
                             usdt_balance=f"{db_user.balance_usdt:.2f}",
                             usdt_acts=max_acts['usdt'],
                             premium_balance=db_user.balance_premium_months,
                             premium_acts=max_acts['premium'])

        await state.set_state(CreateCheckStates.waiting_activations)

        # Выбираем правильную клавиатуру в зависимости от типа содержимого
        if content_type == "premium":
            keyboard = get_check_activations_premium_keyboard(lang)
        else:
            keyboard = get_check_activations_keyboard(lang)

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('checks.activations.title', lang)}\n\n"
                f"{balance_info}\n\n"
                f"{t('checks.activations.description', lang, min=MIN_ACTIVATIONS)}"
            ),
            reply_markup=keyboard,
        )


async def _process_amount(callback: CallbackQuery, state: FSMContext, amount: int) -> None:
    """Обработка выбранной суммы."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    content_type = data.get("content_type", "stars")

    await state.update_data(amount=amount)

    # Всегда переходим к выбору количества активаций
    await _show_activations_screen(callback, state, amount, content_type, lang)
    await callback.answer()


# Ввод количества активаций (кнопки)
@router.callback_query(F.data.startswith("checks:activations:"), CreateCheckStates.waiting_activations)
async def callback_activations_preset(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор количества активаций через кнопку."""
    value = callback.data.split(":")[-1]

    if value == "max":
        # Рассчитываем максимальное количество активаций
        data = await state.get_data()
        lang = data.get("lang", "ru")
        amount = data.get("amount", 0)
        content_type = data.get("content_type", "stars")

        async with async_session_factory() as session:
            user_service = UserService(session)
            db_user = await user_service.get_user(callback.from_user.id)

            if not db_user:
                await callback.answer(t("common.user_not_found", lang), show_alert=True)
                return

            max_acts = await calculate_max_activations(
                amount_per_activation=amount,
                content_type=content_type,
                balance_stars=db_user.balance_stars,
                balance_usdt=db_user.balance_usdt,
                balance_premium=db_user.balance_premium_months,
            )

            # Выбираем максимум из всех доступных балансов
            if content_type == "stars":
                activations = max(max_acts['usdt'], max_acts['stars'])
            else:
                activations = max(max_acts['usdt'], max_acts['premium'])

            if activations < 1:
                await callback.answer(t("checks.activations.insufficient_balance", lang), show_alert=True)
                return
    else:
        activations = int(value)

    await _process_activations(callback, state, activations)


# Ввод количества активаций (текст)
@router.message(CreateCheckStates.waiting_activations)
async def message_activations(message: Message, state: FSMContext) -> None:
    """Ввод количества активаций вручную."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")

    try:
        activations = int(message.text.strip())
    except ValueError:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('checks.activations.title', lang)}\n\n"
                    f"{t('checks.activations.invalid_min', lang, min=MIN_ACTIVATIONS)}"
                ),
                reply_markup=get_check_activations_keyboard(lang),
            )
        return

    if activations < MIN_ACTIVATIONS:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('checks.activations.title', lang)}\n\n"
                    f"{t('checks.activations.invalid_min', lang, min=MIN_ACTIVATIONS)}"
                ),
                reply_markup=get_check_activations_keyboard(lang),
            )
        return

    # Определяем тип чека по количеству активаций
    check_type = "single" if activations == 1 else "multi"
    await state.update_data(max_activations=activations, check_type=check_type, recipient_username=None)

    # Переходим к выбору получателя
    await state.set_state(CreateCheckStates.waiting_recipient)

    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('checks.recipient.title', lang)}\n\n"
                f"{t('checks.recipient.description', lang)}"
            ),
            reply_markup=get_check_recipient_keyboard(lang),
        )


async def _process_activations(callback: CallbackQuery, state: FSMContext, activations: int) -> None:
    """Обработка выбранного количества активаций."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    # Определяем тип чека по количеству активаций
    check_type = "single" if activations == 1 else "multi"
    await state.update_data(max_activations=activations, check_type=check_type, recipient_username=None)

    # Переходим к выбору получателя
    await state.set_state(CreateCheckStates.waiting_recipient)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.recipient.title', lang)}\n\n"
            f"{t('checks.recipient.description', lang)}"
        ),
        reply_markup=get_check_recipient_keyboard(lang),
    )
    await callback.answer()


# Пропуск получателя
@router.callback_query(F.data == ChecksCallback.SKIP_RECIPIENT, CreateCheckStates.waiting_recipient)
async def callback_skip_recipient(callback: CallbackQuery, state: FSMContext) -> None:
    """Пропуск выбора получателя (чек для всех)."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.update_data(recipient_username=None)
    await state.set_state(CreateCheckStates.waiting_password)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.password.title', lang)}\n\n"
            f"{t('checks.password.description', lang)}"
        ),
        reply_markup=get_check_password_keyboard(lang),
    )
    await callback.answer()


# Ввод получателя (текст)
@router.message(CreateCheckStates.waiting_recipient)
async def message_recipient(message: Message, state: FSMContext) -> None:
    """Ввод username или ID получателя."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")
    check_type = data.get("check_type", "single")

    text_input = message.text.strip().lstrip("@")
    keyboard = get_check_recipient_keyboard(lang)

    # Проверяем, является ли ввод числом (Telegram ID)
    recipient_id = None
    recipient_username = None

    if text_input.isdigit():
        recipient_id = int(text_input)
        if recipient_id < 1:
            if bot_message_id:
                await edit_bot_message(
                    message.bot,
                    message.chat.id,
                    bot_message_id,
                    text=(
                        f"{t('checks.recipient.title', lang)}\n\n"
                        f"{t('checks.recipient.invalid', lang)}\n\n"
                        f"{t('checks.recipient.description', lang)}"
                    ),
                    reply_markup=keyboard,
                )
            return
        recipient_line = t("checks.recipient.set_id", lang, user_id=recipient_id)
    else:
        # Это username
        if not text_input or len(text_input) < 3:
            if bot_message_id:
                await edit_bot_message(
                    message.bot,
                    message.chat.id,
                    bot_message_id,
                    text=(
                        f"{t('checks.recipient.title', lang)}\n\n"
                        f"{t('checks.recipient.invalid', lang)}\n\n"
                        f"{t('checks.recipient.description', lang)}"
                    ),
                    reply_markup=keyboard,
                )
            return
        recipient_username = text_input
        # Экранируем HTML для безопасности
        recipient_line = t("checks.recipient.set_username", lang, username=html.escape(recipient_username))

    await state.update_data(recipient_username=recipient_username, recipient_id=recipient_id)
    await state.set_state(CreateCheckStates.waiting_password)

    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('checks.password.title', lang)}\n\n"
                f"{recipient_line}\n\n"
                f"{t('checks.password.description', lang)}"
            ),
            reply_markup=get_check_password_keyboard(lang),
        )


# Пропуск пароля
@router.callback_query(F.data == ChecksCallback.SKIP_PASSWORD, CreateCheckStates.waiting_password)
async def callback_skip_password(callback: CallbackQuery, state: FSMContext) -> None:
    """Пропуск ввода пароля."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    user_id = callback.from_user.id

    bot_info = await callback.bot.get_me()
    available_channels = await get_available_channels(user_id)

    await state.update_data(
        password=None,
        selected_channel_ids=[],
        available_channels=available_channels,
    )
    await state.set_state(CreateCheckStates.waiting_channel)

    # Лимит не достигнут, т.к. пока ничего не выбрано
    await safe_edit_message(
        callback.message,
        text=get_channel_selection_text(lang, available_channels),
        reply_markup=get_check_channel_keyboard(
            lang, available_channels, set(), bot_info.username,
            limit_reached=False, max_channels=MAX_CHANNELS_PER_CHECK,
        ),
    )
    await callback.answer()


# Ввод пароля (текст)
@router.message(CreateCheckStates.waiting_password)
async def message_password(message: Message, state: FSMContext) -> None:
    """Ввод пароля чека."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")
    check_type = data.get("check_type", "single")
    user_id = message.from_user.id

    password = message.text.strip()

    # Валидация длины пароля
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        keyboard = get_check_password_keyboard(lang) if check_type == "single" else get_check_password_keyboard_multi(lang)
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('checks.password.title', lang)}\n\n"
                    f"{t('checks.password.invalid', lang)}\n\n"
                    f"{t('checks.password.description', lang)}"
                ),
                reply_markup=keyboard,
            )
        return

    bot_info = await message.bot.get_me()
    available_channels = await get_available_channels(user_id)

    await state.update_data(
        password=password,
        selected_channel_ids=[],
        available_channels=available_channels,
    )
    await state.set_state(CreateCheckStates.waiting_channel)

    # Лимит не достигнут, т.к. пока ничего не выбрано
    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('checks.password.set', lang)}\n\n"
                f"{get_channel_selection_text(lang, available_channels)}"
            ),
            reply_markup=get_check_channel_keyboard(
                lang, available_channels, set(), bot_info.username,
                limit_reached=False, max_channels=MAX_CHANNELS_PER_CHECK,
            ),
        )


# Пропуск канала
@router.callback_query(F.data == ChecksCallback.SKIP_CHANNEL, CreateCheckStates.waiting_channel)
async def callback_skip_channel(callback: CallbackQuery, state: FSMContext) -> None:
    """Пропуск выбора канала."""
    await state.update_data(required_channel=None, selected_channel_ids=[])
    await _show_payment(callback, state)


# Обновить список каналов
@router.callback_query(F.data == "checks:channel:refresh", CreateCheckStates.waiting_channel)
async def callback_channel_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    """Обновить список каналов."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    data = await state.get_data()
    selected_channel_ids = data.get("selected_channel_ids", set())
    if isinstance(selected_channel_ids, list):
        selected_channel_ids = set(selected_channel_ids)

    await callback.answer(t("checks.channel.refreshing", lang))

    # Обновляем каналы из Telegram API (только каналы этого пользователя)
    available_channels = await refresh_channels_from_bot(callback.bot, callback.from_user.id)

    # Убираем из выбранных каналы, которые больше недоступны
    available_ids = {ch["id"] for ch in available_channels}
    selected_channel_ids = selected_channel_ids & available_ids
    # Сохраняем как list для корректной JSON-сериализации FSM state
    await state.update_data(selected_channel_ids=list(selected_channel_ids), available_channels=available_channels)

    bot_info = await callback.bot.get_me()

    # Лимит достигнут если выбрано MAX_CHANNELS_PER_CHECK каналов
    limit_reached = len(selected_channel_ids) >= MAX_CHANNELS_PER_CHECK

    await safe_edit_message(
        callback.message,
        text=get_channel_selection_text(lang, available_channels),
        reply_markup=get_check_channel_keyboard(
            lang, available_channels, selected_channel_ids, bot_info.username,
            limit_reached=limit_reached, max_channels=MAX_CHANNELS_PER_CHECK,
        ),
    )


# Toggle канал (выбрать/убрать)
@router.callback_query(F.data.startswith("checks:channel:toggle:"), CreateCheckStates.waiting_channel)
async def callback_channel_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Переключить выбор канала."""
    channel_id = int(callback.data.split(":")[-1])

    data = await state.get_data()
    lang = data.get("lang", "ru")
    selected_channel_ids = data.get("selected_channel_ids", set())
    available_channels = data.get("available_channels", [])

    if isinstance(selected_channel_ids, list):
        selected_channel_ids = set(selected_channel_ids)

    # Toggle
    if channel_id in selected_channel_ids:
        selected_channel_ids.discard(channel_id)
    else:
        # Проверяем лимит перед добавлением
        if len(selected_channel_ids) >= MAX_CHANNELS_PER_CHECK:
            await callback.answer(
                t("checks.channel.limit_reached", lang, limit=MAX_CHANNELS_PER_CHECK),
                show_alert=True,
            )
            return
        selected_channel_ids.add(channel_id)

    # Сохраняем как list для корректной JSON-сериализации FSM state
    await state.update_data(selected_channel_ids=list(selected_channel_ids))

    bot_info = await callback.bot.get_me()

    # Если нет available_channels в state, получаем из БД
    if not available_channels:
        available_channels = await get_available_channels(callback.from_user.id)
        await state.update_data(available_channels=available_channels)

    # Лимит достигнут если выбрано MAX_CHANNELS_PER_CHECK каналов
    limit_reached = len(selected_channel_ids) >= MAX_CHANNELS_PER_CHECK

    await safe_edit_message(
        callback.message,
        text=get_channel_selection_text(lang, available_channels),
        reply_markup=get_check_channel_keyboard(
            lang, available_channels, selected_channel_ids, bot_info.username,
            limit_reached=limit_reached, max_channels=MAX_CHANNELS_PER_CHECK,
        ),
    )
    await callback.answer()


# Подтвердить выбор каналов
@router.callback_query(F.data == "checks:channel:confirm", CreateCheckStates.waiting_channel)
async def callback_channel_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтвердить выбор каналов и перейти к выбору оплаты."""
    data = await state.get_data()
    selected_channel_ids = data.get("selected_channel_ids", set())
    available_channels = data.get("available_channels", [])

    if isinstance(selected_channel_ids, list):
        selected_channel_ids = set(selected_channel_ids)

    if selected_channel_ids:
        # Сохраняем ID каналов в JSON (работает и для приватных групп)
        channel_ids = [str(ch_id) for ch_id in selected_channel_ids]
        await state.update_data(required_channel=json.dumps(channel_ids))
    else:
        await state.update_data(required_channel=None)

    await _show_payment(callback, state)


# Обработчик нажатия на кнопку лимита (просто показываем alert)
@router.callback_query(F.data == "checks:channel:limit_info")
async def callback_channel_limit_info(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать информацию о лимите."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    await callback.answer(
        t("checks.channel.limit_reached", lang, limit=MAX_CHANNELS_PER_CHECK),
        show_alert=True,
    )


@router.callback_query(F.data == "checks:restrict:limit_info")
async def callback_restrict_limit_info(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать информацию о лимите (в разделе ограничений)."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    await callback.answer(
        t("checks.channel.limit_reached", lang, limit=MAX_CHANNELS_PER_CHECK),
        show_alert=True,
    )


async def _show_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать экран выбора способа оплаты."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    data = await state.get_data()

    content_type = data.get("content_type", "stars")
    amount = data.get("amount", 0)
    max_activations = data.get("max_activations", 1)

    total = amount * max_activations

    # Рассчитываем стоимость в USDT (цены из БД)
    if content_type == "premium":
        premium_prices = await get_premium_prices()
        price_usdt = premium_prices.get(amount, Decimal("0")) * max_activations
    else:
        star_price = await get_star_price()
        price_usdt = Decimal(str(total)) * star_price

    # Получаем балансы пользователя
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        await state.set_state(CreateCheckStates.waiting_payment)
        await state.update_data(price_usdt=float(price_usdt))

        # Формируем текст
        if content_type == "premium":
            summary_text = t(
                "checks.payment.summary_premium", lang,
                amount=amount,
                activations=max_activations,
                total=total,
                price_usdt=f"{price_usdt:.2f}",
            )
        else:
            summary_text = t(
                "checks.payment.summary_stars", lang,
                amount=f"{amount:,}",
                activations=max_activations,
                total=f"{total:,}",
                price_usdt=f"{price_usdt:.2f}",
            )

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('checks.payment.title', lang)}\n\n"
                f"{summary_text}"
            ),
            reply_markup=get_check_payment_keyboard(
                lang=lang,
                content_type=content_type,
                total_stars=total if content_type == "stars" else 0,
                total_premium=total if content_type == "premium" else 0,
                user_balance_stars=float(db_user.balance_stars),
                user_balance_premium=db_user.balance_premium_months,
                user_balance_usdt=float(db_user.balance_usdt),
                price_usdt=float(price_usdt),
            ),
        )

    await callback.answer()


async def _create_check(callback: CallbackQuery, state: FSMContext) -> None:
    """Создать чек без подтверждения."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    data = await state.get_data()

    content_type = data.get("content_type", "stars")
    check_type = data.get("check_type", "single")
    amount = data.get("amount", 0)
    max_activations = data.get("max_activations", 1)
    recipient_username = data.get("recipient_username")
    recipient_id = data.get("recipient_id")
    password = data.get("password")
    required_channel = data.get("required_channel")
    payment_method = data.get("payment_method", "stars")
    price_usdt = Decimal(str(data.get("price_usdt", 0)))

    total = amount * max_activations

    async with async_session_factory() as session:
        user_service = UserService(session)
        # SELECT FOR UPDATE для предотвращения race condition при создании чека
        db_user = await user_service.get_user_for_update(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Инициализируем переменные для заморозки
        frozen_usdt = Decimal("0")
        frozen_stars = Decimal("0")
        frozen_premium_months = 0

        # Проверяем баланс и замораживаем средства в зависимости от способа оплаты
        if payment_method == "usdt":
            if db_user.balance_usdt < price_usdt:
                await callback.answer(t("checks.payment.insufficient_usdt", lang), show_alert=True)
                return
            frozen_usdt = price_usdt
            db_user.balance_usdt -= price_usdt
            db_user.frozen_usdt += price_usdt
        elif payment_method == "premium":
            if db_user.balance_premium_months < total:
                await callback.answer(t("checks.payment.insufficient_premium", lang), show_alert=True)
                return
            frozen_premium_months = total
            db_user.balance_premium_months -= total
            db_user.frozen_premium_months += total
        else:  # stars
            if db_user.balance_stars < Decimal(str(total)):
                await callback.answer(t("checks.payment.insufficient_balance", lang), show_alert=True)
                return
            frozen_stars = Decimal(str(total))
            db_user.balance_stars -= frozen_stars
            db_user.frozen_stars += frozen_stars

        # Генерируем код чека
        code = generate_check_code()

        # Создаём чек
        check = Check(
            code=code,
            creator_id=user.id,
            check_type=check_type,
            content_type=content_type,
            amount_stars=Decimal(str(amount)) if content_type == "stars" else Decimal("0"),
            amount_premium_months=amount if content_type == "premium" else 0,
            max_activations=max_activations,
            recipient_username=recipient_username,
            recipient_id=recipient_id,
            password=password,
            required_channel=required_channel,
            payment_method=payment_method,
            frozen_usdt=frozen_usdt,
            frozen_stars=frozen_stars,
            frozen_premium_months=frozen_premium_months,
        )

        session.add(check)
        await session.commit()

        # Получаем username бота для ссылки
        bot_info = await callback.bot.get_me()
        bot_username = bot_info.username
        check_link = f"https://t.me/{bot_username}?start=check_{code}"

        # Логируем создание
        if content_type == "premium":
            log_text = f"{amount} мес. Premium"
        else:
            log_text = f"{amount}⭐"

        payment_log = f"via {payment_method}"
        if payment_method == "usdt":
            payment_log = f"via USDT ({frozen_usdt}$)"

        logger.info(f"User {user.id} created check {code}: {log_text} × {max_activations} {payment_log}")

        # Логируем в Telegram
        await tg_logger.log_check_created(
            check_code=code,
            creator_id=user.id,
            creator_username=callback.from_user.username,
            amount_stars=Decimal(str(amount)),
            max_activations=max_activations,
        )

        # Показываем детальное меню чека
        text = await build_check_detail_text(check, check_link, lang)

        await safe_edit_message(
            callback.message,
            text=text,
            reply_markup=get_check_detail_keyboard(
                check_id=check.id,
                is_active=True,
                check_code=check.code,
                back_to="my",
                lang=lang,
                has_description=False,
                has_photo=False,
            ),
        )

    await state.clear()
    await callback.answer()


async def _show_confirmation(callback: CallbackQuery, state: FSMContext) -> None:
    """Создать чек (экран подтверждения убран для упрощения UX)."""
    await _create_check(callback, state)


# ==================== ОБРАБОТЧИКИ ВЫБОРА ОПЛАТЫ ====================

@router.callback_query(F.data == ChecksCallback.PAY_BALANCE, CreateCheckStates.waiting_payment)
async def callback_pay_balance(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата с баланса звёзд/Premium."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")

    content_type = data.get("content_type", "stars")
    amount = data.get("amount", 0)
    max_activations = data.get("max_activations", 1)
    total = amount * max_activations

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Проверяем достаточно ли баланса
        if content_type == "premium":
            if db_user.balance_premium_months < total:
                await callback.answer(
                    t("checks.payment.insufficient_premium", lang),
                    show_alert=True,
                )
                return
            await state.update_data(payment_method="premium")
        else:
            if db_user.balance_stars < Decimal(str(total)):
                await callback.answer(
                    t("checks.payment.insufficient_balance", lang),
                    show_alert=True,
                )
                return
            await state.update_data(payment_method="stars")

    # Сразу создаём чек без подтверждения
    await _create_check(callback, state)


@router.callback_query(F.data == ChecksCallback.PAY_USDT, CreateCheckStates.waiting_payment)
async def callback_pay_usdt(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата с баланса USDT."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")

    price_usdt = Decimal(str(data.get("price_usdt", 0)))

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Проверяем достаточно ли USDT
        if db_user.balance_usdt < price_usdt:
            # Недостаточно - показываем экран пополнения
            amount_needed = float(price_usdt - db_user.balance_usdt)
            await safe_edit_message(
                callback.message,
                text=(
                    f"{t('checks.payment.topup_title', lang)}\n\n"
                    f"{t('checks.payment.topup_description', lang, amount=f'{amount_needed:.2f}')}"
                ),
                reply_markup=get_topup_keyboard(lang, amount_needed),
            )
            await callback.answer()
            return

        await state.update_data(payment_method="usdt")

    # Сразу создаём чек без подтверждения
    await _create_check(callback, state)


@router.callback_query(F.data == ChecksCallback.TOP_UP, CreateCheckStates.waiting_payment)
async def callback_topup_redirect(callback: CallbackQuery, state: FSMContext) -> None:
    """Перенаправление на пополнение баланса."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")
    price_usdt = Decimal(str(data.get("price_usdt", 0)))

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Вычисляем сколько нужно пополнить
        amount_needed = price_usdt - db_user.balance_usdt
        if amount_needed <= 0:
            # Баланс уже достаточный - вернёмся к оплате
            await _show_payment(callback, state)
            return

        # Округляем вверх до 0.01
        amount_needed = amount_needed.quantize(Decimal("0.01"))

    # Очищаем текущее состояние и переходим в депозит
    await state.clear()
    await state.set_state(DepositStates.waiting_method)
    await state.update_data(
        amount=str(amount_needed),
        lang=lang,
        bot_message_id=callback.message.message_id,
    )

    # Показываем экран выбора способа оплаты
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('deposit.select_method', lang)}\n\n"
            f"{t('deposit.amount_label', lang, amount=await format_usdt_with_rub(amount_needed))}\n\n"
            f"{t('deposit.select_method_prompt', lang)}"
        ),
        reply_markup=await get_deposit_payment_methods_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_PAYMENT, CreateCheckStates.waiting_payment)
async def callback_back_to_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору способа оплаты."""
    await _show_payment(callback, state)


# ==================== НАВИГАЦИЯ НАЗАД ====================

@router.callback_query(F.data == ChecksCallback.BACK_TO_CONTENT)
async def callback_back_to_content(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору содержимого чека."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.set_state(CreateCheckStates.waiting_content)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.content.title', lang)}\n\n"
            f"{t('checks.content.description', lang)}"
        ),
        reply_markup=get_check_content_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_AMOUNT)
async def callback_back_to_amount(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к вводу суммы (для Stars) или выбору срока (для Premium)."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    content_type = data.get("content_type", "stars")

    # Для Premium чека показываем выбор срока, а не количества звёзд
    if content_type == "premium":
        await state.set_state(CreateCheckStates.waiting_premium_duration)
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('checks.premium_duration.title', lang)}\n\n"
                f"{t('checks.premium_duration.description', lang)}"
            ),
            reply_markup=get_check_premium_duration_keyboard(lang),
        )
    else:
        await state.set_state(CreateCheckStates.waiting_amount)
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('checks.amount.title', lang)}\n\n"
                f"{t('checks.amount.description', lang, min=MIN_CHECK_AMOUNT, max=f'{MAX_CHECK_AMOUNT:,}')}"
            ),
            reply_markup=get_check_amount_keyboard(lang),
        )
    await callback.answer()


@router.callback_query(F.data == "checks:back:premium_duration")
async def callback_back_to_premium_duration(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору срока Premium."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.set_state(CreateCheckStates.waiting_premium_duration)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.premium_duration.title', lang)}\n\n"
            f"{t('checks.premium_duration.description', lang)}"
        ),
        reply_markup=get_check_premium_duration_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_ACTIVATIONS)
async def callback_back_to_activations(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору количества активаций."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    amount = data.get("amount", 0)
    content_type = data.get("content_type", "stars")

    await _show_activations_screen(callback, state, amount, content_type, lang)
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_RECIPIENT)
async def callback_back_to_recipient(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору получателя."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.set_state(CreateCheckStates.waiting_recipient)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.recipient.title', lang)}\n\n"
            f"{t('checks.recipient.description', lang)}"
        ),
        reply_markup=get_check_recipient_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_PASSWORD)
async def callback_back_to_password(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к вводу пароля."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.set_state(CreateCheckStates.waiting_password)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('checks.password.title', lang)}\n\n"
            f"{t('checks.password.description', lang)}"
        ),
        reply_markup=get_check_password_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_CHANNEL)
async def callback_back_to_channel(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору канала."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    selected_channel_ids = data.get("selected_channel_ids", set())
    available_channels = data.get("available_channels", [])

    if isinstance(selected_channel_ids, list):
        selected_channel_ids = set(selected_channel_ids)

    await state.set_state(CreateCheckStates.waiting_channel)

    bot_info = await callback.bot.get_me()

    # Если нет available_channels в state, получаем из БД
    if not available_channels:
        available_channels = await get_available_channels(callback.from_user.id)
        await state.update_data(available_channels=available_channels)

    # Лимит достигнут если выбрано MAX_CHANNELS_PER_CHECK каналов
    limit_reached = len(selected_channel_ids) >= MAX_CHANNELS_PER_CHECK

    await safe_edit_message(
        callback.message,
        text=get_channel_selection_text(lang, available_channels),
        reply_markup=get_check_channel_keyboard(
            lang, available_channels, selected_channel_ids, bot_info.username,
            limit_reached=limit_reached, max_channels=MAX_CHANNELS_PER_CHECK,
        ),
    )
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_PAYMENT)
async def callback_back_to_payment_from_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору способа оплаты."""
    await _show_payment(callback, state)


# ==================== МОИ ЧЕКИ / ИСТОРИЯ ====================

CHECKS_PER_PAGE = 10


async def _get_user_checks(user_id: int, active_only: bool = True) -> list[Check]:
    """Получить чеки пользователя."""
    async with async_session_factory() as session:
        if active_only:
            query = select(Check).where(
                Check.creator_id == user_id,
                Check.is_active == True,
                Check.current_activations < Check.max_activations,
            ).order_by(Check.created_at.desc())
        else:
            query = select(Check).where(
                Check.creator_id == user_id,
            ).order_by(Check.created_at.desc())

        result = await session.execute(query)
        return list(result.scalars().all())


async def _get_check_by_id(check_id: int) -> Check | None:
    """Получить чек по ID."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Check).where(Check.id == check_id)
        )
        return result.scalar_one_or_none()


async def _get_user_channels(user_id: int) -> list[dict]:
    """Получить каналы, где бот является администратором и которые добавил пользователь."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(BotChannel).where(
                BotChannel.added_by_user_id == user_id,
                BotChannel.is_active == True,
            )
        )
        channels = result.scalars().all()

        return [
            {
                "id": ch.channel_id,
                "username": ch.channel_username,
                "title": ch.channel_title,
            }
            for ch in channels
        ]


def _format_check_for_list(check: Check) -> dict:
    """Форматировать чек для списка."""
    if check.content_type == "premium":
        amount = check.amount_premium_months
    else:
        amount = int(check.amount_stars)

    return {
        "id": check.id,
        "code": check.code,
        "content_type": check.content_type,
        "amount": amount,
        "remaining": check.max_activations - check.current_activations,
        "total": check.max_activations,
        "is_active": check.is_active and check.current_activations < check.max_activations,
        "activations": check.current_activations,
    }


@router.callback_query(F.data == ChecksCallback.MY_CHECKS)
async def callback_my_checks(callback: CallbackQuery, state: FSMContext) -> None:
    """Просмотр активных чеков."""
    await _show_my_checks(callback, state, page=0)


@router.callback_query(F.data.startswith(ChecksCallback.MY_CHECKS_PAGE))
async def callback_my_checks_page(callback: CallbackQuery, state: FSMContext) -> None:
    """Пагинация активных чеков."""
    page = int(callback.data.split(":")[-1])
    await _show_my_checks(callback, state, page=page)


async def _show_my_checks(callback: CallbackQuery, state: FSMContext, page: int = 0) -> None:
    """Показать список активных чеков."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    checks = await _get_user_checks(user.id, active_only=True)

    if not checks:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('checks.my_checks.title', lang)}\n\n"
                f"{t('checks.my_checks.empty', lang)}"
            ),
            reply_markup=get_back_to_checks_keyboard(lang),
        )
        await callback.answer()
        return

    # Пагинация
    total_pages = (len(checks) + CHECKS_PER_PAGE - 1) // CHECKS_PER_PAGE
    start_idx = page * CHECKS_PER_PAGE
    end_idx = start_idx + CHECKS_PER_PAGE
    page_checks = checks[start_idx:end_idx]

    checks_data = [_format_check_for_list(c) for c in page_checks]

    await safe_edit_message(
        callback.message,
        text=t('checks.my_checks.title', lang),
        reply_markup=get_my_checks_keyboard(checks_data, page, total_pages, lang),
    )
    await callback.answer()


@router.callback_query(F.data == ChecksCallback.BACK_TO_MY_CHECKS)
async def callback_back_to_my_checks(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к списку активных чеков."""
    await _show_my_checks(callback, state, page=0)


@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery) -> None:
    """Обработчик для noop callback (кнопка номера страницы в пагинации)."""
    await callback.answer()


# ==================== ПРОСМОТР ДЕТАЛИ ЧЕКА ====================

@router.callback_query(F.data.startswith(ChecksCallback.VIEW_CHECK))
async def callback_view_check(callback: CallbackQuery, state: FSMContext) -> None:
    """Просмотр детальной информации о чеке."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    # Парсим callback data: checks:view:123 или checks:view:123:history
    parts = callback.data.replace(ChecksCallback.VIEW_CHECK, "").split(":")
    check_id = int(parts[0])
    back_to = parts[1] if len(parts) > 1 else "my"

    check = await _get_check_by_id(check_id)

    if not check:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    # Проверяем что это чек пользователя
    if check.creator_id != callback.from_user.id:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    # Формируем информацию о чеке
    bot_info = await callback.bot.get_me()
    check_link = f"https://t.me/{bot_info.username}?start=check_{check.code}"

    text = await build_check_detail_text(check, check_link, lang)

    is_fully_used = check.current_activations >= check.max_activations
    is_active = check.is_active and not is_fully_used

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_check_detail_keyboard(
            check_id=check.id,
            is_active=is_active,
            check_code=check.code,
            back_to=back_to,
            lang=lang,
            has_description=bool(check.description),
            has_photo=bool(check.photo_file_id),
        ),
    )
    await callback.answer()


# ==================== УДАЛЕНИЕ ЧЕКА ====================

# ВАЖНО: callback_delete_confirm должен быть ПЕРЕД callback_delete_check,
# т.к. "checks:delete:confirm:" более специфичен чем "checks:delete:"

@router.callback_query(F.data.startswith("checks:delete:confirm:"))
async def callback_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение удаления и возврат средств."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        # SELECT FOR UPDATE для предотвращения race condition при удалении
        result = await session.execute(
            select(Check).where(Check.id == check_id).with_for_update()
        )
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        if not check.is_active:
            await callback.answer(t("checks.my_checks.already_deleted", lang), show_alert=True)
            return

        # Получаем пользователя с блокировкой для безопасного изменения баланса
        user_service = UserService(session)
        db_user = await user_service.get_user_for_update(callback.from_user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Рассчитываем сколько нужно вернуть
        remaining_activations = check.max_activations - check.current_activations

        # Возвращаем замороженные средства пропорционально оставшимся активациям
        if check.payment_method == "usdt" and check.frozen_usdt > 0:
            refund_usdt = (check.frozen_usdt / check.max_activations) * remaining_activations
            db_user.balance_usdt += refund_usdt
            db_user.frozen_usdt -= refund_usdt
        elif check.payment_method == "stars" and check.frozen_stars > 0:
            refund_stars = (check.frozen_stars / check.max_activations) * remaining_activations
            db_user.balance_stars += refund_stars
            db_user.frozen_stars -= refund_stars
        elif check.payment_method == "premium" and check.frozen_premium_months > 0:
            refund_premium = int((check.frozen_premium_months / check.max_activations) * remaining_activations)
            db_user.balance_premium_months += refund_premium
            db_user.frozen_premium_months -= refund_premium

        # Деактивируем чек
        check.is_active = False

        await session.commit()

        logger.info(f"User {callback.from_user.id} deleted check {check.code}")

    await safe_edit_message(
        callback.message,
        text=t('checks.deleted.message', lang),
        reply_markup=get_back_to_checks_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(ChecksCallback.DELETE_CHECK))
async def callback_delete_check(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение удаления чека."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.replace(ChecksCallback.DELETE_CHECK, ""))

    check = await _get_check_by_id(check_id)

    if not check or check.creator_id != callback.from_user.id:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    text = (
        f"{t('checks.delete.title', lang)}\n\n"
        f"{t('checks.delete.confirm_text', lang)}"
    )

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_delete_confirm_keyboard(check_id, lang),
    )
    await callback.answer()


# ==================== РЕДАКТИРОВАНИЕ ОПИСАНИЯ ====================

MAX_DESCRIPTION_LENGTH = 200


@router.callback_query(F.data.startswith(ChecksCallback.ADD_DESCRIPTION) & ~F.data.contains("remove"))
async def callback_add_description(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало редактирования описания чека."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.replace(ChecksCallback.ADD_DESCRIPTION, ""))

    check = await _get_check_by_id(check_id)

    if not check or check.creator_id != callback.from_user.id:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    if not check.is_active:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    await state.set_state(EditCheckStates.waiting_description)
    await state.update_data(
        edit_check_id=check_id,
        bot_message_id=callback.message.message_id,
    )

    has_description = bool(check.description)

    if has_description:
        # Экранируем HTML для безопасности
        safe_description = html.escape(check.description)
        text = (
            f"{t('checks.edit_description.title', lang)}\n\n"
            f"{t('checks.edit_description.description_with_current', lang, current=safe_description, max=MAX_DESCRIPTION_LENGTH)}"
        )
    else:
        text = (
            f"{t('checks.edit_description.title', lang)}\n\n"
            f"{t('checks.edit_description.description', lang, max=MAX_DESCRIPTION_LENGTH)}"
        )

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_description_edit_keyboard(check_id, has_description, lang),
    )
    await callback.answer()


@router.message(EditCheckStates.waiting_description)
async def message_description(message: Message, state: FSMContext) -> None:
    """Ввод нового описания."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")
    check_id = data.get("edit_check_id")

    if not check_id:
        await state.clear()
        return

    description = message.text.strip()

    # Валидация длины
    if len(description) > MAX_DESCRIPTION_LENGTH:
        if bot_message_id:
            check = await _get_check_by_id(check_id)
            has_description = bool(check.description) if check else False
            text = (
                f"{t('checks.edit_description.title', lang)}\n\n"
                f"{t('checks.edit_description.too_long', lang, current=len(description), max=MAX_DESCRIPTION_LENGTH)}\n\n"
                f"{t('checks.edit_description.enter_new', lang, max=MAX_DESCRIPTION_LENGTH)}"
            )
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=text,
                reply_markup=get_description_edit_keyboard(check_id, has_description, lang),
            )
        return

    # Сохраняем описание
    async with async_session_factory() as session:
        result = await session.execute(
            select(Check).where(Check.id == check_id)
        )
        check = result.scalar_one_or_none()

        if not check or check.creator_id != message.from_user.id:
            await state.clear()
            return

        check.description = description
        await session.commit()

        logger.info(f"User {message.from_user.id} updated description for check {check.code}")

    await state.clear()

    # Возвращаемся к детальному просмотру чека
    if bot_message_id:
        check = await _get_check_by_id(check_id)
        if check:
            bot_info = await message.bot.get_me()
            check_link = f"https://t.me/{bot_info.username}?start=check_{check.code}"

            text = await build_check_detail_text(check, check_link, lang)

            is_fully_used = check.current_activations >= check.max_activations
            is_active = check.is_active and not is_fully_used

            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=text,
                reply_markup=get_check_detail_keyboard(
                    check_id=check.id,
                    is_active=is_active,
                    check_code=check.code,
                    back_to="my",
                    lang=lang,
                    has_description=bool(check.description),
                    has_photo=bool(check.photo_file_id),
                ),
            )


@router.callback_query(F.data.regexp(r"^checks:photo:\d+$"))
async def callback_add_photo(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    check_id = int(callback.data.replace(ChecksCallback.ADD_PHOTO, ""))
    check = await _get_check_by_id(check_id)

    if not check or check.creator_id != callback.from_user.id or not check.is_active:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    await state.set_state(EditCheckStates.waiting_photo)
    await state.update_data(edit_check_id=check_id, bot_message_id=callback.message.message_id)
    await safe_edit_message(
        callback.message,
        text="Отправьте фото, которое нужно прикрепить к чеку.",
        reply_markup=get_description_edit_keyboard(check_id, bool(check.description), lang),
    )
    await callback.answer()


@router.message(EditCheckStates.waiting_photo)
async def message_check_photo(message: Message, state: FSMContext) -> None:
    await safe_delete_message(message)

    data = await state.get_data()
    check_id = data.get("edit_check_id")
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")

    if not check_id:
        await state.clear()
        return

    if not message.photo:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text="Нужно отправить именно фото.",
                reply_markup=get_description_edit_keyboard(check_id, False, lang),
            )
        return

    photo_file_id = message.photo[-1].file_id

    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != message.from_user.id:
            await state.clear()
            return

        check.photo_file_id = photo_file_id
        await session.commit()

    await state.clear()

    if bot_message_id:
        check = await _get_check_by_id(check_id)
        if check:
            bot_info = await message.bot.get_me()
            check_link = f"https://t.me/{bot_info.username}?start=check_{check.code}"
            text = await build_check_detail_text(check, check_link, lang)
            is_fully_used = check.current_activations >= check.max_activations
            is_active = check.is_active and not is_fully_used
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=text,
                reply_markup=get_check_detail_keyboard(
                    check_id=check.id,
                    is_active=is_active,
                    check_code=check.code,
                    back_to="my",
                    lang=lang,
                    has_description=bool(check.description),
                    has_photo=bool(check.photo_file_id),
                ),
            )


@router.callback_query(F.data.startswith("checks:photo:remove:"))
async def callback_remove_photo(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    check_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        check.photo_file_id = None
        await session.commit()

        bot_info = await callback.bot.get_me()
        check_link = f"https://t.me/{bot_info.username}?start=check_{check.code}"
        text = await build_check_detail_text(check, check_link, lang)
        is_fully_used = check.current_activations >= check.max_activations
        is_active = check.is_active and not is_fully_used

        await safe_edit_message(
            callback.message,
            text=text,
            reply_markup=get_check_detail_keyboard(
                check_id=check.id,
                is_active=is_active,
                check_code=check.code,
                back_to="my",
                lang=lang,
                has_description=bool(check.description),
                has_photo=False,
            ),
        )

    await state.clear()
    await callback.answer("Фото удалено")


@router.callback_query(F.data.startswith("checks:description:remove:"))
async def callback_remove_description(callback: CallbackQuery, state: FSMContext) -> None:
    """Удаление описания чека."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    check_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        result = await session.execute(
            select(Check).where(Check.id == check_id)
        )
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        check.description = None
        await session.commit()

        # Обновляем отображение чека
        bot_info = await callback.bot.get_me()
        check_link = f"https://t.me/{bot_info.username}?start=check_{check.code}"

        text = await build_check_detail_text(check, check_link, lang)

        is_fully_used = check.current_activations >= check.max_activations
        is_active = check.is_active and not is_fully_used

        await safe_edit_message(
            callback.message,
            text=text,
            reply_markup=get_check_detail_keyboard(
                check_id=check.id,
                is_active=is_active,
                check_code=check.code,
                back_to="my",
                lang=lang,
                has_description=False,
                has_photo=bool(check.photo_file_id),
            ),
        )

        logger.info(f"User {callback.from_user.id} removed description for check {check.code}")

    await state.clear()
    await callback.answer(t("checks.edit_description.removed", lang))


# ==================== ОГРАНИЧЕНИЯ ====================

async def _show_restrictions_menu(callback: CallbackQuery, check_id: int, lang: str) -> None:
    """Показать меню ограничений."""
    check = await _get_check_by_id(check_id)
    if not check:
        return

    text = (
        f"{t('checks.restrictions.title', lang)}\n\n"
        f"{t('checks.restrictions.description', lang)}"
    )

    has_subscription = bool(check.required_channel)

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_restrictions_keyboard(
            check_id=check_id,
            lang=lang,
            has_password=bool(check.password),
            has_recipient=bool(check.recipient_username or check.recipient_id),
            require_premium=check.require_premium,
            require_new_user=check.require_new_user,
            has_subscription=has_subscription,
        ),
    )


@router.callback_query(F.data.startswith(ChecksCallback.RESTRICTIONS))
async def callback_restrictions(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню ограничений чека."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.replace(ChecksCallback.RESTRICTIONS, ""))

    check = await _get_check_by_id(check_id)

    if not check or check.creator_id != callback.from_user.id:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    if not check.is_active:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    await _show_restrictions_menu(callback, check_id, lang)
    await callback.answer()


# ==================== ПАРОЛЬ ====================

@router.callback_query(F.data.regexp(r"^checks:restrict:password:(\d+)$"))
async def callback_restrict_password(callback: CallbackQuery, state: FSMContext) -> None:
    """Настройка пароля чека."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.split(":")[-1])
    check = await _get_check_by_id(check_id)

    if not check or check.creator_id != callback.from_user.id:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    await state.set_state(EditCheckStates.waiting_password)
    await state.update_data(
        edit_check_id=check_id,
        bot_message_id=callback.message.message_id,
    )

    text = (
        f"{t('checks.restrictions.password.title', lang)}\n\n"
        f"{t('checks.restrictions.password.description', lang)}"
    )

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_restriction_password_keyboard(check_id, bool(check.password), lang),
    )
    await callback.answer()


@router.message(EditCheckStates.waiting_password)
async def message_restrict_password(message: Message, state: FSMContext) -> None:
    """Ввод нового пароля."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")
    check_id = data.get("edit_check_id")

    if not check_id:
        await state.clear()
        return

    password = message.text.strip()

    # Валидация (используем те же константы что и при создании чека)
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        if bot_message_id:
            check = await _get_check_by_id(check_id)
            text = (
                f"{t('checks.restrictions.password.title', lang)}\n\n"
                f"{t('checks.restrictions.password.description', lang)}\n"
                f"{t('checks.restrictions.password.invalid', lang)}"
            )
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=text,
                reply_markup=get_restriction_password_keyboard(check_id, bool(check.password) if check else False, lang),
            )
        return

    # Сохраняем пароль
    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != message.from_user.id:
            await state.clear()
            return

        check.password = password
        await session.commit()
        logger.info(f"User {message.from_user.id} set password for check {check.code}")

    await state.clear()

    # Возвращаемся к меню ограничений
    if bot_message_id:
        check = await _get_check_by_id(check_id)
        if check:
            text = (
                f"{t('checks.restrictions.title', lang)}\n\n"
                f"{t('checks.restrictions.description', lang)}"
            )
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=text,
                reply_markup=get_restrictions_keyboard(
                    check_id=check_id,
                    lang=lang,
                    has_password=bool(check.password),
                    has_recipient=bool(check.recipient_username or check.recipient_id),
                    require_premium=check.require_premium,
                    require_new_user=check.require_new_user,
                    has_subscription=bool(check.required_channel),
                ),
            )


@router.callback_query(F.data.regexp(r"^checks:restrict:password:remove:(\d+)$"))
async def callback_restrict_password_remove(callback: CallbackQuery, state: FSMContext) -> None:
    """Удаление пароля."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        check.password = None
        await session.commit()
        logger.info(f"User {callback.from_user.id} removed password for check {check.code}")

    await state.clear()
    await callback.answer(t("checks.restrictions.password.removed", lang))
    await _show_restrictions_menu(callback, check_id, lang)


# ==================== КОНКРЕТНЫЙ ПОЛЬЗОВАТЕЛЬ ====================

@router.callback_query(F.data.regexp(r"^checks:restrict:user:(\d+)$"))
async def callback_restrict_user(callback: CallbackQuery, state: FSMContext) -> None:
    """Настройка конкретного пользователя."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.split(":")[-1])
    check = await _get_check_by_id(check_id)

    if not check or check.creator_id != callback.from_user.id:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    await state.set_state(EditCheckStates.waiting_recipient)
    await state.update_data(
        edit_check_id=check_id,
        bot_message_id=callback.message.message_id,
    )

    text = (
        f"{t('checks.restrictions.user.title', lang)}\n\n"
        f"{t('checks.restrictions.user.description', lang)}"
    )

    has_recipient = bool(check.recipient_username or check.recipient_id)

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_restriction_user_keyboard(check_id, has_recipient, lang),
    )
    await callback.answer()


@router.message(EditCheckStates.waiting_recipient)
async def message_restrict_recipient(message: Message, state: FSMContext) -> None:
    """Ввод получателя."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    user = message.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    check_id = data.get("edit_check_id")

    if not check_id:
        await state.clear()
        return

    recipient = message.text.strip()

    # Определяем тип получателя
    recipient_username = None
    recipient_id = None

    if recipient.startswith("@"):
        username = recipient[1:]
        if len(username) >= 3:
            recipient_username = username
    elif recipient.isdigit():
        recipient_id = int(recipient)

    if not recipient_username and not recipient_id:
        if bot_message_id:
            check = await _get_check_by_id(check_id)
            has_recipient = bool(check.recipient_username or check.recipient_id) if check else False
            text = (
                f"{t('checks.restrictions.user.title', lang)}\n\n"
                f"{t('checks.restrictions.user.description', lang)}\n"
                f"{t('checks.restrictions.user.invalid', lang)}"
            )
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=text,
                reply_markup=get_restriction_user_keyboard(check_id, has_recipient, lang),
            )
        return

    # Сохраняем
    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != message.from_user.id:
            await state.clear()
            return

        check.recipient_username = recipient_username
        check.recipient_id = recipient_id
        await session.commit()
        logger.info(f"User {message.from_user.id} set recipient for check {check.code}: {recipient}")

    await state.clear()

    # Возвращаемся к меню ограничений
    if bot_message_id:
        check = await _get_check_by_id(check_id)
        if check:
            text = (
                f"{t('checks.restrictions.title', lang)}\n\n"
                f"{t('checks.restrictions.description', lang)}"
            )
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=text,
                reply_markup=get_restrictions_keyboard(
                    check_id=check_id,
                    lang=lang,
                    has_password=bool(check.password),
                    has_recipient=bool(check.recipient_username or check.recipient_id),
                    require_premium=check.require_premium,
                    require_new_user=check.require_new_user,
                    has_subscription=bool(check.required_channel),
                ),
            )


@router.callback_query(F.data.regexp(r"^checks:restrict:user:remove:(\d+)$"))
async def callback_restrict_user_remove(callback: CallbackQuery, state: FSMContext) -> None:
    """Удаление ограничения на пользователя."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        check.recipient_username = None
        check.recipient_id = None
        await session.commit()
        logger.info(f"User {callback.from_user.id} removed recipient for check {check.code}")

    await state.clear()
    await callback.answer(t("checks.restrictions.user.removed", lang))
    await _show_restrictions_menu(callback, check_id, lang)


# ==================== TELEGRAM PREMIUM ====================

@router.callback_query(F.data.regexp(r"^checks:restrict:premium:toggle:(\d+)$"))
async def callback_restrict_premium_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Переключение ограничения Telegram Premium."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        # Переключаем значение
        check.require_premium = not check.require_premium
        await session.commit()
        logger.info(f"User {callback.from_user.id} set require_premium={check.require_premium} for check {check.code}")

    msg_key = "checks.restrictions.premium_enabled" if check.require_premium else "checks.restrictions.premium_disabled"
    await callback.answer(t(msg_key, lang))
    await _show_restrictions_menu(callback, check_id, lang)


# ==================== НОВЫЕ ПОЛЬЗОВАТЕЛИ ====================

@router.callback_query(F.data.regexp(r"^checks:restrict:new_users:toggle:(\d+)$"))
async def callback_restrict_new_users_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Переключение ограничения для новых пользователей."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        # Переключаем значение
        check.require_new_user = not check.require_new_user
        await session.commit()
        logger.info(f"User {callback.from_user.id} set require_new_user={check.require_new_user} for check {check.code}")

    msg_key = "checks.restrictions.new_users_enabled" if check.require_new_user else "checks.restrictions.new_users_disabled"
    await callback.answer(t(msg_key, lang))
    await _show_restrictions_menu(callback, check_id, lang)


# ==================== ПОДПИСКА НА КАНАЛ ====================

@router.callback_query(F.data.regexp(r"^checks:restrict:subscription:(\d+)$"))
async def callback_restrict_subscription(callback: CallbackQuery, state: FSMContext) -> None:
    """Настройка обязательной подписки."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    data = await state.get_data()

    check_id = int(callback.data.split(":")[-1])
    check = await _get_check_by_id(check_id)

    if not check or check.creator_id != callback.from_user.id:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    # Получаем доступные каналы
    available_channels = await _get_user_channels(callback.from_user.id)

    # Проверяем, есть ли уже выбранные каналы в state для этого чека
    stored_check_id = data.get("restrict_check_id")
    stored_channels = data.get("restrict_selected_channels", [])
    if stored_check_id == check_id and stored_channels:
        # Используем сохранённые в state каналы
        selected_channel_ids = set(stored_channels)
    else:
        # Загружаем из БД (первое открытие или другой чек)
        selected_channel_ids = set()
        if check.required_channel:
            try:
                channels = json.loads(check.required_channel)
                if isinstance(channels, list):
                    # Каналы хранятся как ID (строки)
                    for ch_id in channels:
                        try:
                            selected_channel_ids.add(int(ch_id))
                        except (ValueError, TypeError):
                            # Старый формат - username, ищем по username
                            for ch in available_channels:
                                if ch.get("username") == ch_id:
                                    selected_channel_ids.add(ch["id"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Сохраняем в state
        await state.update_data(
            restrict_check_id=check_id,
            restrict_selected_channels=list(selected_channel_ids),
        )

    text = (
        f"{t('checks.restrictions.subscription.title', lang)}\n\n"
        f"{t('checks.restrictions.subscription.description', lang)}"
    )

    if not available_channels:
        text += f"\n\n{t('checks.restrictions.subscription.no_channels', lang)}"

    bot_info = await callback.bot.get_me()
    # Лимит достигнут если выбрано MAX_CHANNELS_PER_CHECK каналов
    limit_reached = len(selected_channel_ids) >= MAX_CHANNELS_PER_CHECK

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_restriction_subscription_keyboard(
            check_id=check_id,
            available_channels=available_channels,
            selected_channel_ids=selected_channel_ids,
            has_subscription=bool(check.required_channel),
            lang=lang,
            bot_username=bot_info.username,
            limit_reached=limit_reached,
            max_channels=MAX_CHANNELS_PER_CHECK,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^checks:restrict:subscription:toggle:(\d+):(-?\d+)$"))
async def callback_restrict_subscription_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Переключение выбора канала."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    data = await state.get_data()

    parts = callback.data.split(":")
    check_id = int(parts[-2])
    channel_id = int(parts[-1])

    selected = set(data.get("restrict_selected_channels", []))

    if channel_id in selected:
        selected.discard(channel_id)
    else:
        # Проверяем лимит перед добавлением
        if len(selected) >= MAX_CHANNELS_PER_CHECK:
            await callback.answer(
                t("checks.channel.limit_reached", lang, limit=MAX_CHANNELS_PER_CHECK),
                show_alert=True,
            )
            return
        selected.add(channel_id)

    await state.update_data(restrict_selected_channels=list(selected))

    # Получаем чек и каналы
    check = await _get_check_by_id(check_id)
    available_channels = await _get_user_channels(callback.from_user.id)

    text = (
        f"{t('checks.restrictions.subscription.title', lang)}\n\n"
        f"{t('checks.restrictions.subscription.description', lang)}"
    )

    bot_info = await callback.bot.get_me()
    # Лимит достигнут если выбрано MAX_CHANNELS_PER_CHECK каналов
    limit_reached = len(selected) >= MAX_CHANNELS_PER_CHECK

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_restriction_subscription_keyboard(
            check_id=check_id,
            available_channels=available_channels,
            selected_channel_ids=selected,
            has_subscription=bool(check.required_channel),
            lang=lang,
            bot_username=bot_info.username,
            limit_reached=limit_reached,
            max_channels=MAX_CHANNELS_PER_CHECK,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^checks:restrict:subscription:refresh:(\d+)$"))
async def callback_restrict_subscription_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    """Обновление списка каналов."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    data = await state.get_data()

    check_id = int(callback.data.split(":")[-1])
    check = await _get_check_by_id(check_id)

    if not check or check.creator_id != callback.from_user.id:
        await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
        return

    # Обновляем каналы
    available_channels = await _get_user_channels(callback.from_user.id)
    selected = set(data.get("restrict_selected_channels", []))

    text = (
        f"{t('checks.restrictions.subscription.title', lang)}\n\n"
        f"{t('checks.restrictions.subscription.description', lang)}"
    )

    if not available_channels:
        text += f"\n\n{t('checks.restrictions.subscription.no_channels', lang)}"

    bot_info = await callback.bot.get_me()
    # Лимит достигнут если выбрано MAX_CHANNELS_PER_CHECK каналов
    limit_reached = len(selected) >= MAX_CHANNELS_PER_CHECK

    await safe_edit_message(
        callback.message,
        text=text,
        reply_markup=get_restriction_subscription_keyboard(
            check_id=check_id,
            available_channels=available_channels,
            selected_channel_ids=selected,
            has_subscription=bool(check.required_channel),
            lang=lang,
            bot_username=bot_info.username,
            limit_reached=limit_reached,
            max_channels=MAX_CHANNELS_PER_CHECK,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^checks:restrict:subscription:confirm:(\d+)$"))
async def callback_restrict_subscription_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение выбора каналов."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)
    data = await state.get_data()

    check_id = int(callback.data.split(":")[-1])
    selected = set(data.get("restrict_selected_channels", []))

    # Сохраняем ID каналов (работает и для приватных групп без username)
    channel_ids = [str(ch_id) for ch_id in selected] if selected else []

    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        check.required_channel = json.dumps(channel_ids) if channel_ids else None
        await session.commit()
        logger.info(f"User {callback.from_user.id} set required_channel={channel_ids} for check {check.code}")

    await state.update_data(restrict_selected_channels=[])

    if channel_ids:
        await callback.answer(t("checks.restrictions.subscription.set_success", lang))
    else:
        await callback.answer(t("checks.restrictions.subscription.removed", lang))

    await _show_restrictions_menu(callback, check_id, lang)


@router.callback_query(F.data.regexp(r"^checks:restrict:subscription:remove:(\d+)$"))
async def callback_restrict_subscription_remove(callback: CallbackQuery, state: FSMContext) -> None:
    """Удаление требования подписки."""
    user = callback.from_user
    lang = await get_user_language(user.id, state, user.language_code)

    check_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        result = await session.execute(select(Check).where(Check.id == check_id))
        check = result.scalar_one_or_none()

        if not check or check.creator_id != callback.from_user.id:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        check.required_channel = None
        await session.commit()
        logger.info(f"User {callback.from_user.id} removed required_channel for check {check.code}")

    await state.update_data(restrict_selected_channels=[])
    await callback.answer(t("checks.restrictions.subscription.removed", lang))
    await _show_restrictions_menu(callback, check_id, lang)
