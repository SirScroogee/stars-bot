"""
Handlers для раздела покупки звёзд.
"""
import logging
import time
from decimal import Decimal

from aiogram import F, Router, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sqlalchemy import select

from src.bot.keyboards.menu import MenuCallback
from src.bot.keyboards.stars import (
    StarsCallback,
    get_amount_keyboard,
    get_back_to_stars_keyboard,
    get_balance_confirm_keyboard,
    get_confirm_withdraw_keyboard,
    get_payment_error_keyboard,
    get_payment_method_keyboard,
    get_recipient_keyboard,
    get_stars_menu_keyboard,
    get_stars_payment_pending_keyboard,
    get_stars_ton_payment_keyboard,
)
from src.db.session import async_session_factory
from src.db.models import ProductType, PaymentProvider, Transaction
from src.locales import t, get_user_locale
from src.services.user_service import UserService
from src.services.order_service import OrderService
from src.services.fragment_account_service import FragmentAccountService
from src.services.recipient_service import validate_stars_recipient
from src.services.bot_settings_service import get_bot_settings, get_cryptobot_fee
from src.services.cryptopay_service import (
    create_deposit_invoice,
    check_invoice_status,
    delete_invoice,
)
from src.services.ton_payment_service import (
    get_ton_usd_rate,
    generate_payment_comment,
    create_ton_payment_url,
    check_ton_payment,
)
from src.workers.order_worker import get_order_worker
from src.bot.menu_media import edit_menu_message

logger = logging.getLogger(__name__)

router = Router(name="stars")

# Дефолтные константы (используются если настройки недоступны)
DEFAULT_MIN_STARS = 50
DEFAULT_MAX_STARS = 10000
DEFAULT_STAR_PRICE_USDT = Decimal("0.02")


async def get_stars_settings() -> tuple[int, int, Decimal]:
    """Получить настройки звёзд."""
    try:
        settings = await get_bot_settings()
        min_stars = int(settings.get("min_stars", DEFAULT_MIN_STARS))
        max_stars = int(settings.get("max_stars", DEFAULT_MAX_STARS))
        star_price = Decimal(settings.get("star_price_usdt", str(DEFAULT_STAR_PRICE_USDT)))
        return min_stars, max_stars, star_price
    except Exception:
        return DEFAULT_MIN_STARS, DEFAULT_MAX_STARS, DEFAULT_STAR_PRICE_USDT


class BuyStarsStates(StatesGroup):
    """Состояния для покупки звёзд."""

    waiting_recipient = State()
    waiting_amount = State()
    waiting_payment = State()
    waiting_balance_confirm = State()
    waiting_cryptobot_payment = State()
    waiting_ton_payment = State()


class WithdrawStarsStates(StatesGroup):
    """Состояния для получения звёзд с баланса."""

    waiting_recipient = State()
    waiting_amount = State()
    waiting_confirm = State()


def get_stars_menu_text(balance_usdt: Decimal, balance_stars: Decimal, lang: str = "ru") -> str:
    """Текст главного меню звёзд."""
    return (
        f"{t('stars_section.menu.title', lang)}\n\n"
        f"{t('stars_section.menu.description', lang)}\n\n"
        f"{t('stars_section.menu.balance', lang, balance_usdt=f'{balance_usdt:,.2f}', balance_stars=f'{balance_stars:,.0f}')}"
    )


async def safe_delete_message(message: Message) -> None:
    """Безопасное удаление сообщения."""
    try:
        await message.delete()
    except Exception:
        pass  # Игнорируем ошибки удаления


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


# ==================== ГЛАВНОЕ МЕНЮ ЗВЁЗД ====================

@router.callback_query(F.data == MenuCallback.BUY_STARS)
async def callback_stars_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню раздела звёзд."""
    await state.clear()
    user = callback.from_user
    # Определяем язык до проверки пользователя
    lang = get_user_locale(user.language_code)

    # Получаем настройки
    min_stars, max_stars, star_price = await get_stars_settings()

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Обновляем язык из БД если есть
        lang = db_user.language_code or lang
        # Сохраняем настройки в state для использования в других хендлерах
        await state.update_data(
            bot_message_id=callback.message.message_id,
            lang=lang,
            min_stars=min_stars,
            max_stars=max_stars,
            star_price=str(star_price),
        )

        sent_message = await edit_menu_message(
            callback,
            "stars",
            text=get_stars_menu_text(db_user.balance_usdt, db_user.balance_stars, lang),
            reply_markup=get_stars_menu_keyboard(lang),
        )
        if sent_message:
            await state.update_data(bot_message_id=sent_message.message_id)

    await callback.answer()


@router.callback_query(F.data == StarsCallback.BACK_TO_STARS)
async def callback_back_to_stars(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в меню звёзд."""
    await state.clear()
    user = callback.from_user
    # Определяем язык до проверки пользователя
    lang = get_user_locale(user.language_code)

    # Получаем настройки
    min_stars, max_stars, star_price = await get_stars_settings()

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Обновляем язык из БД если есть
        lang = db_user.language_code or lang
        await state.update_data(
            bot_message_id=callback.message.message_id,
            lang=lang,
            min_stars=min_stars,
            max_stars=max_stars,
            star_price=str(star_price),
        )

        sent_message = await edit_menu_message(
            callback,
            "stars",
            text=get_stars_menu_text(db_user.balance_usdt, db_user.balance_stars, lang),
            reply_markup=get_stars_menu_keyboard(lang),
        )
        if sent_message:
            await state.update_data(bot_message_id=sent_message.message_id)

    await callback.answer()


# ==================== ПОКУПКА ЗВЁЗД ====================

@router.callback_query(F.data == StarsCallback.BUY)
async def callback_buy_stars(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало покупки звёзд - выбор получателя."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.set_state(BuyStarsStates.waiting_recipient)
    await state.update_data(mode="buy", bot_message_id=callback.message.message_id)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.recipient.title', lang)}\n\n"
            f"{t('stars_section.recipient.enter', lang)}"
        ),
        reply_markup=get_recipient_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == StarsCallback.RECIPIENT_SELF, BuyStarsStates.waiting_recipient)
async def callback_recipient_self_buy(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор себя как получателя (покупка)."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")
    amount_preset = data.get("amount_preset", False)
    amount = data.get("amount")
    min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
    max_stars = data.get("max_stars", DEFAULT_MAX_STARS)
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))

    # Проверяем наличие username
    if not user.username:
        await callback.answer(
            t("common.set_username_full", lang).strip(),
            show_alert=True,
        )
        return

    # Проверяем получателя через Fragment API
    success, recipient_info, error_msg = await validate_stars_recipient(user.username, lang)

    if not success:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.recipient.title', lang)}\n\n"
                f"{error_msg}\n\n"
                f"{t('stars_section.recipient.enter', lang)}"
            ),
            reply_markup=get_recipient_keyboard(lang),
        )
        await callback.answer()
        return

    await state.update_data(
        recipient_id=recipient_info.recipient_id,
        recipient_username=user.username,
        recipient_display_name=recipient_info.display_name,
    )

    # Если количество уже выбрано (из inline калькулятора) - сразу к оплате
    if amount_preset and amount:
        await state.set_state(BuyStarsStates.waiting_payment)
        price_usdt = amount * star_price
        recipient = user.username

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.payment.title', lang)}\n\n"
                f"{t('stars_section.payment.info', lang, username=recipient, amount=f'{amount:,}', price=f'{price_usdt:,.2f}')}"
                f"\n\n{t('common.payment.select', lang)}"
            ),
            reply_markup=get_payment_method_keyboard(lang),
        )
    else:
        await state.set_state(BuyStarsStates.waiting_amount)

        # Рассчитываем цены и доступное количество
        min_price = min_stars * star_price
        max_price = max_stars * star_price

        async with async_session_factory() as session:
            user_service = UserService(session)
            db_user = await user_service.get_user(user.id)
            user_balance = db_user.balance_usdt if db_user else Decimal("0")
            afford_stars = int(user_balance / star_price)

        # Сохраняем доступное количество для кнопки "На весь баланс"
        await state.update_data(afford_stars=afford_stars)

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.amount.title', lang)}\n\n"
                f"{t('stars_section.amount.recipient_info', lang, username=user.username)}\n\n"
                f"{t('stars_section.amount.info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', min_price=f'{min_price:.2f}', max_price=f'{max_price:.2f}', afford=f'{afford_stars:,}', balance=f'{user_balance:.2f}')}"
                f"\n\n{t('stars_section.amount.select', lang)}"
            ),
            reply_markup=get_amount_keyboard(lang, max_stars=afford_stars),
        )
    await callback.answer()


@router.message(BuyStarsStates.waiting_recipient)
async def message_recipient_username_buy(message: Message, state: FSMContext) -> None:
    """Ввод username получателя (покупка)."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    username = message.text.strip().lstrip("@")
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")

    if not username or len(username) < 3:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.recipient.title', lang)}\n\n"
                    f"{t('common.recipient.invalid', lang)}\n\n"
                    f"{t('stars_section.recipient.enter', lang)}"
                ),
                reply_markup=get_recipient_keyboard(lang),
            )
        return

    # Проверяем получателя через Fragment API
    success, recipient_info, error_msg = await validate_stars_recipient(username, lang)

    if not success:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.recipient.title', lang)}\n\n"
                    f"{error_msg}\n\n"
                    f"{t('stars_section.recipient.enter', lang)}"
                ),
                reply_markup=get_recipient_keyboard(lang),
            )
        return

    # Сохраняем данные получателя
    await state.update_data(
        recipient_username=username,
        recipient_id=recipient_info.recipient_id,
        recipient_display_name=recipient_info.display_name,
    )

    # Показываем имя если есть
    display_name = recipient_info.display_name or username

    # Проверяем, выбрано ли количество заранее (из inline калькулятора)
    amount_preset = data.get("amount_preset", False)
    amount = data.get("amount")

    if amount_preset and amount:
        # Сразу к оплате
        await state.set_state(BuyStarsStates.waiting_payment)
        star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))
        price_usdt = amount * star_price

        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.payment.title', lang)}\n\n"
                    f"{t('stars_section.payment.info', lang, username=username, amount=f'{amount:,}', price=f'{price_usdt:,.2f}')}"
                    f"\n\n{t('common.payment.select', lang)}"
                ),
                reply_markup=get_payment_method_keyboard(lang),
            )
    else:
        await state.set_state(BuyStarsStates.waiting_amount)
        min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
        max_stars = data.get("max_stars", DEFAULT_MAX_STARS)
        star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))

        # Рассчитываем цены и доступное количество
        min_price = min_stars * star_price
        max_price = max_stars * star_price

        async with async_session_factory() as session:
            user_service = UserService(session)
            db_user = await user_service.get_user(message.from_user.id)
            user_balance = db_user.balance_usdt if db_user else Decimal("0")
            afford_stars = int(user_balance / star_price)

        # Сохраняем доступное количество для кнопки "На весь баланс"
        await state.update_data(afford_stars=afford_stars)

        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.amount.title', lang)}\n\n"
                    f"{t('stars_section.amount.recipient_info', lang, username=username)}\n\n"
                    f"{t('stars_section.amount.info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', min_price=f'{min_price:.2f}', max_price=f'{max_price:.2f}', afford=f'{afford_stars:,}', balance=f'{user_balance:.2f}')}"
                    f"\n\n{t('stars_section.amount.select', lang)}"
                ),
                reply_markup=get_amount_keyboard(lang, max_stars=afford_stars),
            )


@router.callback_query(F.data == StarsCallback.BACK_TO_RECIPIENT)
async def callback_back_to_recipient(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору получателя."""
    data = await state.get_data()
    mode = data.get("mode", "buy")
    lang = data.get("lang", "ru")

    if mode == "buy":
        await state.set_state(BuyStarsStates.waiting_recipient)
    else:
        await state.set_state(WithdrawStarsStates.waiting_recipient)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.recipient.title', lang)}\n\n"
            f"{t('stars_section.recipient.enter', lang)}"
        ),
        reply_markup=get_recipient_keyboard(lang),
    )
    await callback.answer()


# Обработка выбора количества через кнопки
@router.callback_query(F.data.startswith("stars:amount:"), BuyStarsStates.waiting_amount)
async def callback_amount_buy(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор количества звёзд (покупка)."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
    max_stars = data.get("max_stars", DEFAULT_MAX_STARS)

    amount_str = callback.data.split(":")[-1]

    # Обработка "На весь баланс"
    if amount_str == "all":
        amount = data.get("afford_stars", 0)
        if amount < min_stars:
            await callback.answer(
                t("common.validation.amount_range", lang, min=min_stars, max=max_stars),
                show_alert=True,
            )
            return
    else:
        try:
            amount = int(amount_str)
        except ValueError:
            await callback.answer(t("common.validation.invalid_amount", lang), show_alert=True)
            return

    # Валидация количества
    if amount < min_stars or amount > max_stars:
        await callback.answer(
            t("common.validation.amount_range", lang, min=min_stars, max=max_stars),
            show_alert=True,
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(BuyStarsStates.waiting_payment)

    recipient = data.get("recipient_username", "me")
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))
    price_usdt = amount * star_price

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.payment.title', lang)}\n\n"
            f"{t('stars_section.payment.info', lang, username=recipient, amount=f'{amount:,}', price=f'{price_usdt:,.2f}')}"
            f"\n\n{t('common.payment.select', lang)}"
        ),
        reply_markup=get_payment_method_keyboard(lang),
    )
    await callback.answer()


@router.message(BuyStarsStates.waiting_amount)
async def message_amount_buy(message: Message, state: FSMContext) -> None:
    """Ввод произвольного количества звёзд (покупка)."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")
    min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
    max_stars = data.get("max_stars", DEFAULT_MAX_STARS)
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))

    # Рассчитываем цены
    min_price = min_stars * star_price
    max_price = max_stars * star_price

    try:
        amount = int(message.text.strip())
    except ValueError:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.amount.title', lang)}\n\n"
                    f"{t('common.amount.enter_number', lang)}\n\n"
                    f"{t('stars_section.amount.info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', min_price=f'{min_price:.2f}', max_price=f'{max_price:.2f}', afford='0', afford_price='0.00')}"
                    f"\n\n{t('stars_section.amount.select', lang)}"
                ),
                reply_markup=get_amount_keyboard(lang),
            )
        return

    if amount < min_stars:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.amount.title', lang)}\n\n"
                    f"{t('common.amount.min_error', lang, min=min_stars)}\n\n"
                    f"{t('stars_section.amount.info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', min_price=f'{min_price:.2f}', max_price=f'{max_price:.2f}', afford='0', afford_price='0.00')}"
                    f"\n\n{t('stars_section.amount.select', lang)}"
                ),
                reply_markup=get_amount_keyboard(lang),
            )
        return

    if amount > max_stars:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.amount.title', lang)}\n\n"
                    f"{t('common.amount.max_error', lang, max=f'{max_stars:,}')}\n\n"
                    f"{t('stars_section.amount.info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', min_price=f'{min_price:.2f}', max_price=f'{max_price:.2f}', afford='0', afford_price='0.00')}"
                    f"\n\n{t('stars_section.amount.select', lang)}"
                ),
                reply_markup=get_amount_keyboard(lang),
            )
        return

    await state.update_data(amount=amount)
    await state.set_state(BuyStarsStates.waiting_payment)

    price_usdt = amount * star_price
    recipient = data.get("recipient_username", "me")

    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('common.payment.title', lang)}\n\n"
                f"{t('stars_section.payment.info', lang, username=recipient, amount=f'{amount:,}', price=f'{price_usdt:,.2f}')}"
                f"\n\n{t('common.payment.select', lang)}"
            ),
            reply_markup=get_payment_method_keyboard(lang),
        )


@router.callback_query(F.data == StarsCallback.BACK_TO_AMOUNT)
async def callback_back_to_amount(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору количества."""
    data = await state.get_data()
    mode = data.get("mode", "buy")
    lang = data.get("lang", "ru")
    min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
    max_stars = data.get("max_stars", DEFAULT_MAX_STARS)
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))
    recipient_username = data.get("recipient_username", "")
    user = callback.from_user

    if mode == "buy":
        await state.set_state(BuyStarsStates.waiting_amount)

        # Рассчитываем цены и доступное количество
        min_price = min_stars * star_price
        max_price = max_stars * star_price

        async with async_session_factory() as session:
            user_service = UserService(session)
            db_user = await user_service.get_user(user.id)
            user_balance = db_user.balance_usdt if db_user else Decimal("0")
            afford_stars = int(user_balance / star_price)

        await state.update_data(afford_stars=afford_stars)

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.amount.title', lang)}\n\n"
                f"{t('stars_section.amount.recipient_info', lang, username=recipient_username)}\n\n"
                f"{t('stars_section.amount.info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', min_price=f'{min_price:.2f}', max_price=f'{max_price:.2f}', afford=f'{afford_stars:,}', balance=f'{user_balance:.2f}')}"
                f"\n\n{t('stars_section.amount.select', lang)}"
            ),
            reply_markup=get_amount_keyboard(lang, max_stars=afford_stars),
        )
    else:
        await state.set_state(WithdrawStarsStates.waiting_amount)
        available_stars = data.get("afford_stars", 0)

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.amount.title', lang)}\n\n"
                f"{t('stars_section.amount.recipient_info', lang, username=recipient_username)}\n\n"
                f"{t('stars_section.amount.withdraw_info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', available=f'{available_stars:,}')}"
                f"\n\n{t('stars_section.amount.select', lang)}"
            ),
            reply_markup=get_amount_keyboard(lang, max_stars=available_stars),
        )
    await callback.answer()


@router.callback_query(F.data == StarsCallback.BACK_TO_PAYMENT)
async def callback_back_to_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору способа оплаты (после ошибки)."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    amount = data.get("amount", 0)
    recipient_username = data.get("recipient_username", "")
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))
    price_usdt = amount * star_price

    await state.set_state(BuyStarsStates.waiting_payment)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.payment.title', lang)}\n\n"
            f"{t('stars_section.payment.info', lang, username=recipient_username, amount=f'{amount:,}', price=f'{price_usdt:,.2f}')}"
            f"\n\n{t('common.payment.select', lang)}"
        ),
        reply_markup=get_payment_method_keyboard(lang),
    )
    await callback.answer()


# Выбор способа оплаты
@router.callback_query(F.data == StarsCallback.PAY_BALANCE, BuyStarsStates.waiting_payment)
async def callback_pay_balance(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать подтверждение оплаты с баланса."""
    data = await state.get_data()
    amount = data.get("amount", 0)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))
    price_usdt = amount * star_price

    # Проверяем баланс
    async with async_session_factory() as session:
        user_service = UserService(session)
        sender = await user_service.get_user(callback.from_user.id)

        if not sender or sender.balance_usdt < price_usdt:
            await callback.answer(
                t("common.insufficient_balance", lang),
                show_alert=True,
            )
            return

    await state.set_state(BuyStarsStates.waiting_balance_confirm)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.balance_payment.title', lang)}\n\n"
            f"{t('common.balance_payment.stars_info', lang, recipient=recipient_username, amount=f'{amount:,}', price=f'{price_usdt:,.2f}')}"
            f"\n\n{t('common.balance_payment.confirm_text', lang)}"
        ),
        reply_markup=get_balance_confirm_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == StarsCallback.CANCEL_BALANCE, BuyStarsStates.waiting_balance_confirm)
async def callback_cancel_balance(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена оплаты с баланса."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.clear()

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.payment_status.cancelled_title', lang)}\n\n"
            f"{t('common.payment_errors.cancelled', lang)}"
        ),
        reply_markup=get_back_to_stars_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == StarsCallback.CONFIRM_BALANCE, BuyStarsStates.waiting_balance_confirm)
async def callback_confirm_balance(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение оплаты с баланса USDT."""
    user = callback.from_user
    data = await state.get_data()
    amount = data.get("amount", 0)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))
    price_usdt = amount * star_price

    async with async_session_factory() as session:
        user_service = UserService(session)
        order_service = OrderService(session)
        fragment_service = FragmentAccountService(session)

        if not await fragment_service.get_all_active_accounts():
            await callback.answer(
                t("common.recipient.errors.service_unavailable", lang),
                show_alert=True,
            )
            return

        # Блокируем строку пользователя для предотвращения race condition
        sender = await user_service.get_user_for_update(user.id)

        if not sender or sender.balance_usdt < price_usdt:
            await callback.answer(
                t("common.insufficient_balance", lang),
                show_alert=True,
            )
            # Не очищаем state - пользователь может выбрать другой способ оплаты
            return

        # Списываем USDT с баланса СРАЗУ (до создания заказа)
        sender.balance_usdt -= price_usdt

        # Создаём заказ
        order, created = await order_service.create_order(
            user_id=user.id,
            recipient_username=recipient_username,
            product_type=ProductType.STARS,
            quantity=amount,
            price_usdt=price_usdt,
            payment_provider=PaymentProvider.BALANCE,
        )

        if not created:
            # Откатываем списание если заказ дубликат
            sender.balance_usdt += price_usdt
            await callback.answer(
                t("common.duplicate_request", lang),
                show_alert=True,
            )
            await state.clear()
            return

        await session.commit()

        # Добавляем заказ в очередь на обработку
        worker = get_order_worker()
        if worker:
            await worker.enqueue_order(order.id)
            logger.info(f"Order {order.id} enqueued for user {user.id}")
        else:
            logger.warning(f"OrderWorker not available, order {order.id} will be recovered on restart")

        order_text = (
            f"{t('common.order.created_title', lang, order_key=order.order_key)}\n\n"
            f"{t('common.balance_payment.stars_info', lang, recipient=recipient_username, amount=f'{amount:,}', price=f'{price_usdt:,.2f}')}\n"
            f"{t('common.order.processing', lang)}"
        )
        msg = None
        try:
            msg = await callback.message.edit_text(text=order_text, parse_mode="HTML")
        except Exception as e:
            logger.debug(f"Failed to edit message: {e}")
            try:
                msg = await callback.message.edit_caption(caption=order_text, parse_mode="HTML")
            except Exception as caption_error:
                logger.debug(f"Failed to edit caption: {caption_error}")
                msg = await callback.message.answer(text=order_text, parse_mode="HTML")

        if msg:
            order.message_id = msg.message_id
            await session.commit()

    await state.clear()
    await callback.answer()


@router.callback_query(F.data == StarsCallback.PAY_CRYPTOBOT, BuyStarsStates.waiting_payment)
async def callback_pay_cryptobot(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата через CryptoBot."""
    data = await state.get_data()
    amount = data.get("amount", 0)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))
    price_usdt = amount * star_price
    user_id = callback.from_user.id

    # Добавляем комиссию CryptoBot из БД
    cryptobot_fee = await get_cryptobot_fee()
    amount_with_fee = (price_usdt * (1 + cryptobot_fee)).quantize(Decimal("0.01"))

    # Показываем сообщение о создании инвойса
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.cryptobot_payment.title', lang)}\n\n"
            f"{t('common.cryptobot_payment.stars_info', lang, recipient=recipient_username, amount=f'{amount:,}', price=f'{amount_with_fee:,.2f}')}"
            f"\n\n{t('common.cryptobot_payment.creating', lang)}"
        ),
    )

    try:
        # Создаём инвойс в CryptoPay
        invoice = await create_deposit_invoice(
            amount=amount_with_fee,
            user_id=user_id,
            description=f"Stars purchase: {amount} stars",
        )

        # Сохраняем данные инвойса
        await state.update_data(
            invoice_id=invoice.invoice_id,
            pay_url=invoice.bot_invoice_url,
            amount_with_fee=str(amount_with_fee),
        )
        await state.set_state(BuyStarsStates.waiting_cryptobot_payment)

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.cryptobot_payment.title', lang)}\n\n"
                f"{t('common.cryptobot_payment.stars_info', lang, recipient=recipient_username, amount=f'{amount:,}', price=f'{amount_with_fee:,.2f}')}"
                f"\n\n{t('common.cryptobot_payment.instructions', lang)}"
            ),
            reply_markup=get_stars_payment_pending_keyboard(invoice.bot_invoice_url, lang),
        )

    except Exception as e:
        logger.error(f"Failed to create invoice for stars: {e}")
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.payment_status.error_title', lang)}\n\n"
                f"{t('common.payment_errors.invoice_create_error', lang)}"
            ),
            reply_markup=get_payment_error_keyboard(lang),
        )
        # Не очищаем state — пользователь может выбрать другой способ оплаты

    await callback.answer()


@router.callback_query(F.data == StarsCallback.PAY_TON, BuyStarsStates.waiting_payment)
async def callback_pay_ton(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата TON."""
    data = await state.get_data()
    amount = data.get("amount", 0)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")
    star_price = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT)))
    price_usdt = amount * star_price
    user_id = callback.from_user.id

    # Показываем сообщение о получении курса
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.ton_payment.title', lang)}\n\n"
            f"{t('common.ton_payment.getting_rate', lang)}"
        ),
    )

    # Получаем курс TON/USD
    ton_rate = await get_ton_usd_rate()

    if not ton_rate:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.payment_status.error_title', lang)}\n\n"
                f"{t('common.payment_errors.ton_rate_error', lang)}"
            ),
            reply_markup=get_payment_error_keyboard(lang),
        )
        # Не очищаем state — пользователь может выбрать другой способ оплаты
        await callback.answer()
        return

    # Рассчитываем сумму в TON
    amount_ton = (price_usdt / ton_rate).quantize(Decimal("0.0001"))

    # Генерируем уникальный комментарий
    payment_comment = generate_payment_comment(user_id, price_usdt, "stars", amount)

    # Создаём URL для оплаты
    ton_url = await create_ton_payment_url(amount_ton, payment_comment)

    # Сохраняем данные
    await state.update_data(
        payment_comment=payment_comment,
        amount_ton=str(amount_ton),
        ton_rate=str(ton_rate),
        payment_created_at=int(time.time()),
    )
    await state.set_state(BuyStarsStates.waiting_ton_payment)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.ton_payment.title', lang)}\n\n"
            f"{t('common.ton_payment.stars_info', lang, recipient=recipient_username, amount=f'{amount:,}', ton_amount=f'{amount_ton:,.4f}', usdt_amount=f'{price_usdt:,.2f}')}"
            f"\n\n{t('common.ton_payment.instructions', lang)}"
            f"\n{t('common.ton_payment.warning', lang)}"
        ),
        reply_markup=get_stars_ton_payment_keyboard(ton_url, lang),
    )

    await callback.answer()


# ==================== ПРОВЕРКА ОПЛАТЫ CRYPTOBOT ====================

@router.callback_query(F.data == StarsCallback.CHECK_PAYMENT, BuyStarsStates.waiting_cryptobot_payment)
async def callback_check_cryptobot_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка оплаты CryptoBot для Stars."""
    data = await state.get_data()
    invoice_id = data.get("invoice_id")
    amount = data.get("amount", 0)
    recipient_username = data.get("recipient_username")
    price_usdt = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT))) * amount
    lang = data.get("lang", "ru")
    user_id = callback.from_user.id

    if not invoice_id:
        await callback.answer(t("common.payment_errors.invoice_not_found", lang), show_alert=True)
        return

    # Проверяем статус инвойса
    invoice = await check_invoice_status(invoice_id)

    if not invoice:
        await callback.answer(t("common.payment_errors.invoice_check_error", lang), show_alert=True)
        return

    if invoice.status == "paid":
        # Оплачено! Создаём заказ
        external_id = f"cryptobot_stars:{invoice_id}"

        async with async_session_factory() as session:
            # Проверяем идемпотентность
            existing_tx = await session.execute(
                select(Transaction).where(Transaction.external_id == external_id)
            )
            if existing_tx.scalar_one_or_none():
                await callback.answer(t("common.payment_errors.already_processed", lang), show_alert=True)
                await state.clear()
                return

            user_service = UserService(session)
            order_service = OrderService(session)
            db_user = await user_service.get_user(user_id)

            if db_user:
                # Создаём заказ на Stars
                order, created = await order_service.create_order(
                    user_id=user_id,
                    recipient_username=recipient_username,
                    product_type=ProductType.STARS,
                    quantity=amount,
                    price_usdt=price_usdt,
                    payment_provider=PaymentProvider.CRYPTOBOT,
                )

                if created:
                    # Записываем транзакцию с привязкой к заказу
                    transaction = Transaction(
                        user_id=user_id,
                        order_id=order.id,
                        type="stars_purchase",
                        amount_usdt=price_usdt,
                        description=f"Stars purchase via CryptoBot: {amount} stars",
                        external_id=external_id,
                    )
                    session.add(transaction)
                    await session.commit()

                    # Добавляем в очередь
                    worker = get_order_worker()
                    if worker:
                        await worker.enqueue_order(order.id)
                        logger.info(f"Stars order {order.id} enqueued (CryptoBot)")

                    try:
                        msg = await callback.message.edit_text(
                            text=(
                                f"{t('common.order.created_title', lang, order_key=order.order_key)}\n\n"
                                f"<blockquote>{t('common.order.recipient', lang, username=recipient_username)}\n"
                                f"{t('common.order.quantity_stars', lang, amount=f'{amount:,}')}\n"
                                f"{t('common.order.price', lang, price=f'{price_usdt:,.2f}')}</blockquote>\n\n"
                                f"{t('common.order.processing', lang)}"
                            ),
                            parse_mode="HTML",
                        )
                        # Сохраняем message_id для редактирования при изменении статуса
                        order.message_id = msg.message_id
                        await session.commit()
                    except Exception as e:
                        logger.debug(f"Failed to edit message: {e}")
                else:
                    await callback.answer(t("common.payment_errors.order_exists", lang), show_alert=True)
            else:
                await callback.answer(t("common.payment_errors.user_not_found", lang), show_alert=True)

        await state.clear()

    elif invoice.status == "expired":
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.payment_status.expired_title', lang)}\n\n"
                f"{t('common.payment_errors.invoice_expired', lang)}"
            ),
            reply_markup=get_back_to_stars_keyboard(lang),
        )
        await state.clear()

    else:
        await callback.answer(t("common.payment_errors.not_received", lang), show_alert=True)


# ==================== ПРОВЕРКА ОПЛАТЫ TON ====================

@router.callback_query(F.data == StarsCallback.CHECK_TON_PAYMENT, BuyStarsStates.waiting_ton_payment)
async def callback_check_ton_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка оплаты TON для Stars."""
    data = await state.get_data()
    amount = data.get("amount", 0)
    recipient_username = data.get("recipient_username")
    price_usdt = Decimal(data.get("star_price", str(DEFAULT_STAR_PRICE_USDT))) * amount
    amount_ton = Decimal(data.get("amount_ton", "0"))
    payment_comment = data.get("payment_comment", "")
    payment_created_at = data.get("payment_created_at", 0)
    lang = data.get("lang", "ru")
    user_id = callback.from_user.id

    if not payment_comment:
        await callback.answer(t("common.payment_errors.payment_not_found", lang), show_alert=True)
        return

    # Проверяем не истекло ли время (30 минут)
    if int(time.time()) - payment_created_at > 1800:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.payment_status.expired_title', lang)}\n\n"
                f"{t('common.payment_errors.payment_expired', lang)}"
            ),
            reply_markup=get_back_to_stars_keyboard(lang),
        )
        await state.clear()
        await callback.answer()
        return

    # Проверяем оплату
    payment = await check_ton_payment(
        comment=payment_comment,
        expected_amount_ton=amount_ton,
        since_timestamp=payment_created_at,
    )

    if payment:
        external_id = f"ton_stars:{payment['event_id']}"

        async with async_session_factory() as session:
            # Проверяем идемпотентность
            existing_tx = await session.execute(
                select(Transaction).where(Transaction.external_id == external_id)
            )
            if existing_tx.scalar_one_or_none():
                await callback.answer(t("common.payment_errors.already_processed", lang), show_alert=True)
                await state.clear()
                return

            user_service = UserService(session)
            order_service = OrderService(session)
            db_user = await user_service.get_user(user_id)

            if db_user:
                # Создаём заказ на Stars
                order, created = await order_service.create_order(
                    user_id=user_id,
                    recipient_username=recipient_username,
                    product_type=ProductType.STARS,
                    quantity=amount,
                    price_usdt=price_usdt,
                    payment_provider=PaymentProvider.TON,
                )

                if created:
                    # Записываем транзакцию с привязкой к заказу
                    transaction = Transaction(
                        user_id=user_id,
                        order_id=order.id,
                        type="stars_purchase",
                        amount_usdt=price_usdt,
                        description=f"Stars purchase via TON: {amount} stars ({payment['amount_ton']:.4f} TON)",
                        external_id=external_id,
                    )
                    session.add(transaction)
                    await session.commit()

                    # Добавляем в очередь
                    worker = get_order_worker()
                    if worker:
                        await worker.enqueue_order(order.id)
                        logger.info(f"Stars order {order.id} enqueued (TON)")

                    try:
                        msg = await callback.message.edit_text(
                            text=(
                                f"{t('common.order.created_title', lang, order_key=order.order_key)}\n\n"
                                f"<blockquote>{t('common.order.recipient', lang, username=recipient_username)}\n"
                                f"{t('common.order.quantity_stars', lang, amount=f'{amount:,}')}\n"
                                f"{t('common.order.price', lang, price=f'{price_usdt:,.2f}')}</blockquote>\n\n"
                                f"{t('common.order.processing', lang)}"
                            ),
                            parse_mode="HTML",
                        )
                        # Сохраняем message_id для редактирования при изменении статуса
                        order.message_id = msg.message_id
                        await session.commit()
                    except Exception as e:
                        logger.debug(f"Failed to edit message: {e}")
                else:
                    await callback.answer(t("common.payment_errors.order_exists", lang), show_alert=True)
            else:
                await callback.answer(t("common.payment_errors.user_not_found", lang), show_alert=True)

        await state.clear()
    else:
        await callback.answer(t("common.payment_errors.not_received", lang), show_alert=True)


# ==================== ОТМЕНА ОПЛАТЫ ====================

@router.callback_query(F.data == StarsCallback.CANCEL_PAYMENT, BuyStarsStates.waiting_cryptobot_payment)
async def callback_cancel_cryptobot_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена оплаты CryptoBot."""
    data = await state.get_data()
    invoice_id = data.get("invoice_id")
    lang = data.get("lang", "ru")

    if invoice_id:
        await delete_invoice(invoice_id)

    await state.clear()

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.payment_status.cancelled_title', lang)}\n\n"
            f"{t('common.payment_errors.cancelled', lang)}"
        ),
        reply_markup=get_back_to_stars_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == StarsCallback.CANCEL_PAYMENT, BuyStarsStates.waiting_ton_payment)
async def callback_cancel_ton_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена оплаты TON."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.clear()

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.payment_status.cancelled_title', lang)}\n\n"
            f"{t('common.payment_errors.cancelled', lang)}"
        ),
        reply_markup=get_back_to_stars_keyboard(lang),
    )
    await callback.answer()


# ==================== ПОЛУЧЕНИЕ ЗВЁЗД С БАЛАНСА ====================

@router.callback_query(F.data == StarsCallback.WITHDRAW)
async def callback_withdraw_stars(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало получения звёзд с баланса - выбор получателя."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")

    # Загружаем настройки
    min_stars, max_stars, star_price = await get_stars_settings()

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user or db_user.balance_stars < min_stars:
            await callback.answer(
                t("stars_section.amount.insufficient", lang, min=min_stars),
                show_alert=True,
            )
            return

    await state.set_state(WithdrawStarsStates.waiting_recipient)
    await state.update_data(
        mode="withdraw",
        bot_message_id=callback.message.message_id,
        min_stars=min_stars,
        max_stars=max_stars,
    )

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.recipient.title', lang)}\n\n"
            f"{t('stars_section.recipient.enter', lang)}"
        ),
        reply_markup=get_recipient_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == StarsCallback.RECIPIENT_SELF, WithdrawStarsStates.waiting_recipient)
async def callback_recipient_self_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор себя как получателя (получение с баланса)."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")
    min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
    max_stars = data.get("max_stars", DEFAULT_MAX_STARS)

    # Проверяем наличие username
    if not user.username:
        await callback.answer(
            t("common.set_username_full", lang).strip(),
            show_alert=True,
        )
        return

    # Проверяем получателя через Fragment API
    success, recipient_info, error_msg = await validate_stars_recipient(user.username, lang)

    if not success:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.recipient.title', lang)}\n\n"
                f"{error_msg}\n\n"
                f"{t('stars_section.recipient.enter', lang)}"
            ),
            reply_markup=get_recipient_keyboard(lang),
        )
        await callback.answer()
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        available_stars = int(db_user.balance_stars) if db_user else 0
        max_amount = min(available_stars, max_stars)

    await state.update_data(
        recipient_id=recipient_info.recipient_id,
        recipient_username=user.username,
        recipient_display_name=recipient_info.display_name,
        max_amount=max_amount,
        afford_stars=available_stars,  # Для кнопки "На весь баланс"
    )
    await state.set_state(WithdrawStarsStates.waiting_amount)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.amount.title', lang)}\n\n"
            f"{t('stars_section.amount.recipient_info', lang, username=user.username)}\n\n"
            f"{t('stars_section.amount.withdraw_info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', available=f'{available_stars:,}')}"
            f"\n\n{t('stars_section.amount.select', lang)}"
        ),
        reply_markup=get_amount_keyboard(lang, max_stars=available_stars),
    )
    await callback.answer()


@router.message(WithdrawStarsStates.waiting_recipient)
async def message_recipient_username_withdraw(message: Message, state: FSMContext) -> None:
    """Ввод username получателя (получение с баланса)."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    username = message.text.strip().lstrip("@")
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")

    if not username or len(username) < 3:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.recipient.title', lang)}\n\n"
                    f"{t('common.recipient.invalid', lang)}\n\n"
                    f"{t('stars_section.recipient.enter', lang)}"
                ),
                reply_markup=get_recipient_keyboard(lang),
            )
        return

    # Проверяем получателя через Fragment API
    success, recipient_info, error_msg = await validate_stars_recipient(username, lang)

    if not success:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.recipient.title', lang)}\n\n"
                    f"{error_msg}\n\n"
                    f"{t('stars_section.recipient.enter', lang)}"
                ),
                reply_markup=get_recipient_keyboard(lang),
            )
        return

    min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
    max_stars = data.get("max_stars", DEFAULT_MAX_STARS)

    async with async_session_factory() as session:
        user_service = UserService(session)
        sender = await user_service.get_user(message.from_user.id)
        available_stars = int(sender.balance_stars) if sender else 0
        max_amount = min(available_stars, max_stars)

    # Сохраняем данные получателя
    await state.update_data(
        recipient_username=username,
        recipient_id=recipient_info.recipient_id,
        recipient_display_name=recipient_info.display_name,
        max_amount=max_amount,
        afford_stars=available_stars,  # Для кнопки "На весь баланс"
    )
    await state.set_state(WithdrawStarsStates.waiting_amount)

    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('common.amount.title', lang)}\n\n"
                f"{t('stars_section.amount.recipient_info', lang, username=username)}\n\n"
                f"{t('stars_section.amount.withdraw_info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', available=f'{available_stars:,}')}"
                f"\n\n{t('stars_section.amount.select', lang)}"
            ),
            reply_markup=get_amount_keyboard(lang, max_stars=available_stars),
        )


# Обработка выбора количества через кнопки (получение с баланса)
@router.callback_query(F.data.startswith("stars:amount:"), WithdrawStarsStates.waiting_amount)
async def callback_amount_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор количества звёзд (получение с баланса)."""
    data = await state.get_data()
    max_amount = data.get("max_amount", DEFAULT_MAX_STARS)
    min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
    lang = data.get("lang", "ru")

    amount_str = callback.data.split(":")[-1]

    # Обработка "На весь баланс"
    if amount_str == "all":
        amount = data.get("afford_stars", 0)
        if amount < min_stars:
            await callback.answer(
                t("common.amount.min_error", lang, min=min_stars),
                show_alert=True,
            )
            return
    else:
        try:
            amount = int(amount_str)
        except ValueError:
            await callback.answer(t("common.validation.invalid_amount", lang), show_alert=True)
            return

    # Валидация минимума
    if amount < min_stars:
        await callback.answer(
            t("common.amount.min_error", lang, min=min_stars),
            show_alert=True,
        )
        return

    if amount > max_amount:
        await callback.answer(
            t("stars_section.amount.insufficient_balance", lang, amount=max_amount),
            show_alert=True,
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(WithdrawStarsStates.waiting_confirm)

    recipient = data.get("recipient_username", "me")

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.confirmation.title', lang)}\n\n"
            f"{t('stars_section.payment.withdraw_info', lang, username=recipient, amount=f'{amount:,}')}"
            f"\n\n{t('stars_section.confirm.send', lang)}"
        ),
        reply_markup=get_confirm_withdraw_keyboard(lang),
    )
    await callback.answer()


@router.message(WithdrawStarsStates.waiting_amount)
async def message_amount_withdraw(message: Message, state: FSMContext) -> None:
    """Ввод произвольного количества звёзд (получение с баланса)."""
    await safe_delete_message(message)

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    max_amount = data.get("max_amount", DEFAULT_MAX_STARS)
    min_stars = data.get("min_stars", DEFAULT_MIN_STARS)
    lang = data.get("lang", "ru")

    try:
        amount = int(message.text.strip())
    except ValueError:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.amount.title', lang)}\n\n"
                    f"{t('common.amount.enter_number', lang)}\n\n"
                    f"{t('stars_section.amount.withdraw_info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', available=f'{max_amount:,}')}"
                    f"\n\n{t('stars_section.amount.select', lang)}"
                ),
                reply_markup=get_amount_keyboard(lang),
            )
        return

    if amount < min_stars:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.amount.title', lang)}\n\n"
                    f"{t('common.amount.min_error', lang, min=min_stars)}\n\n"
                    f"{t('stars_section.amount.withdraw_info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', available=f'{max_amount:,}')}"
                    f"\n\n{t('stars_section.amount.select', lang)}"
                ),
                reply_markup=get_amount_keyboard(lang),
            )
        return

    if amount > max_amount:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.amount.title', lang)}\n\n"
                    f"{t('stars_section.amount.insufficient_balance', lang, amount=f'{max_amount:,}')}\n\n"
                    f"{t('stars_section.amount.withdraw_info', lang, min=f'{min_stars:,}', max=f'{max_stars:,}', available=f'{max_amount:,}')}"
                    f"\n\n{t('stars_section.amount.select', lang)}"
                ),
                reply_markup=get_amount_keyboard(lang),
            )
        return

    await state.update_data(amount=amount)
    await state.set_state(WithdrawStarsStates.waiting_confirm)

    recipient = data.get("recipient_username", "me")

    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('common.confirmation.title', lang)}\n\n"
                f"{t('stars_section.payment.withdraw_info', lang, username=recipient, amount=f'{amount:,}')}"
                f"\n\n{t('stars_section.confirm.send', lang)}"
            ),
            reply_markup=get_confirm_withdraw_keyboard(lang),
        )


@router.callback_query(F.data == StarsCallback.CONFIRM, WithdrawStarsStates.waiting_confirm)
async def callback_confirm_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение отправки звёзд с баланса."""
    user = callback.from_user
    data = await state.get_data()
    amount = data.get("amount", 0)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")

    # Проверка на наличие получателя
    if not recipient_username:
        await callback.answer(t("common.recipient_not_specified", lang), show_alert=True)
        await state.clear()
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        order_service = OrderService(session)

        # Блокируем строку для предотвращения race condition
        sender = await user_service.get_user_for_update(user.id)

        if not sender or sender.balance_stars < amount:
            await callback.answer(
                t("stars_section.amount.insufficient_balance", lang, amount=int(sender.balance_stars) if sender else 0),
                show_alert=True,
            )
            # Не очищаем state - пользователь может изменить количество
            return

        # Создаём заказ (идемпотентно)
        order, created = await order_service.create_order(
            user_id=user.id,
            recipient_username=recipient_username,
            product_type=ProductType.STARS,
            quantity=amount,
            price_usdt=Decimal("0"),  # Бесплатно с баланса звёзд
            payment_provider=PaymentProvider.BALANCE,
        )

        if not created:
            # Дубликат запроса — заказ уже существует
            await callback.answer(
                t("common.duplicate_request", lang),
                show_alert=True,
            )
            await state.clear()
            return

        # Списываем звёзды с баланса отправителя
        sender.balance_stars -= Decimal(amount)
        await session.commit()

        # Добавляем заказ в очередь на обработку
        worker = get_order_worker()
        if worker:
            await worker.enqueue_order(order.id)
            logger.info(f"Order {order.id} enqueued for user {user.id}")
        else:
            logger.warning(f"OrderWorker not available, order {order.id} will be recovered on restart")

        try:
            msg = await callback.message.edit_text(
                text=(
                    f"{t('common.order.created_title', lang, order_key=order.order_key)}\n\n"
                    f"<blockquote>{t('common.order.recipient', lang, username=recipient_username)}\n"
                    f"{t('common.order.quantity_stars', lang, amount=f'{amount:,}')}</blockquote>\n\n"
                    f"{t('common.order.processing', lang)}"
                ),
                parse_mode="HTML",
            )
            # Сохраняем message_id для редактирования при изменении статуса
            order.message_id = msg.message_id
            await session.commit()
        except Exception as e:
            logger.debug(f"Failed to edit message: {e}")

    await state.clear()
    await callback.answer()
