"""
Handlers для раздела покупки Premium.
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
from src.bot.keyboards.premium import (
    PremiumCallback,
    get_back_to_premium_keyboard,
    get_confirm_premium_keyboard,
    get_duration_keyboard,
    get_payment_error_keyboard,
    get_premium_menu_keyboard,
    get_premium_payment_method_keyboard,
    get_premium_payment_pending_keyboard,
    get_premium_ton_payment_keyboard,
    get_premium_recipient_keyboard,
)
from src.db.session import async_session_factory
from src.db.models import ProductType, PaymentProvider, Transaction
from src.locales import t, get_user_locale
from src.services.user_service import UserService
from src.services.order_service import OrderService
from src.services.recipient_service import validate_premium_recipient
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

logger = logging.getLogger(__name__)

router = Router(name="premium")

# Дефолтные цены Premium в USDT (используются если настройки недоступны)
DEFAULT_PREMIUM_PRICES = {
    3: Decimal("8.99"),
    6: Decimal("15.99"),
    12: Decimal("28.99"),
}


async def get_premium_settings() -> dict[int, Decimal]:
    """Получить цены Premium из настроек."""
    try:
        settings = await get_bot_settings()
        return {
            3: Decimal(settings.get("premium_price_3m", "8.99")),
            6: Decimal(settings.get("premium_price_6m", "15.99")),
            12: Decimal(settings.get("premium_price_12m", "28.99")),
        }
    except Exception:
        return DEFAULT_PREMIUM_PRICES.copy()


async def get_premium_price(duration: int) -> Decimal:
    """Получить цену Premium из базы данных."""
    prices = await get_premium_settings()
    return prices.get(duration, DEFAULT_PREMIUM_PRICES.get(duration, Decimal("8.99")))


def calculate_affordable_months(balance: Decimal, prices: dict[int, Decimal]) -> int:
    """Рассчитать сколько месяцев Premium можно купить на баланс."""
    # Проверяем от большего к меньшему
    for months in [12, 6, 3]:
        if balance >= prices.get(months, Decimal("999")):
            return months
    return 0


class BuyPremiumStates(StatesGroup):
    """Состояния для покупки Premium."""

    waiting_recipient = State()
    waiting_duration = State()
    waiting_payment = State()
    waiting_cryptobot_payment = State()
    waiting_ton_payment = State()


class WithdrawPremiumStates(StatesGroup):
    """Состояния для получения Premium с баланса."""

    waiting_recipient = State()
    waiting_duration = State()
    waiting_confirm = State()


def get_premium_menu_text(balance_usdt: Decimal, balance_premium: int, lang: str = "ru") -> str:
    """Текст главного меню Premium."""
    return (
        f"{t('premium_section.menu.title', lang)}\n\n"
        f"{t('premium_section.menu.description', lang)}\n\n"
        f"{t('premium_section.menu.balance', lang, balance_usdt=f'{balance_usdt:,.2f}', balance_premium=balance_premium)}"
    )


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


# ==================== ГЛАВНОЕ МЕНЮ PREMIUM ====================

@router.callback_query(F.data == MenuCallback.BUY_PREMIUM)
async def callback_premium_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню раздела Premium."""
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
        await state.update_data(
            bot_message_id=callback.message.message_id,
            lang=lang,
        )

        await safe_edit_message(
            callback.message,
            text=get_premium_menu_text(db_user.balance_usdt, db_user.balance_premium_months, lang),
            reply_markup=get_premium_menu_keyboard(lang),
        )

    await callback.answer()


@router.callback_query(F.data == PremiumCallback.BACK_TO_PREMIUM)
async def callback_back_to_premium(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в меню Premium."""
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
        await state.update_data(
            bot_message_id=callback.message.message_id,
            lang=lang,
        )

        await safe_edit_message(
            callback.message,
            text=get_premium_menu_text(db_user.balance_usdt, db_user.balance_premium_months, lang),
            reply_markup=get_premium_menu_keyboard(lang),
        )

    await callback.answer()


# ==================== ПОКУПКА PREMIUM ====================

@router.callback_query(F.data == PremiumCallback.BUY)
async def callback_buy_premium(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало покупки Premium - выбор получателя."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.set_state(BuyPremiumStates.waiting_recipient)
    await state.update_data(mode="buy", bot_message_id=callback.message.message_id)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.recipient.title', lang)}\n\n"
            f"{t('premium_section.recipient.enter', lang)}"
        ),
        reply_markup=get_premium_recipient_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == PremiumCallback.RECIPIENT_SELF, BuyPremiumStates.waiting_recipient)
async def callback_recipient_self_buy(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор себя как получателя (покупка)."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")
    duration_preset = data.get("duration_preset", False)
    duration = data.get("duration")

    # Проверяем наличие username
    if not user.username:
        await callback.answer(
            t("common.set_username_full", lang).strip(),
            show_alert=True,
        )
        return

    # Проверяем получателя через Fragment API (даже для себя - у пользователя может быть Premium)
    success, recipient_info, error_msg = await validate_premium_recipient(user.username, lang)

    if not success:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.recipient.title', lang)}\n\n"
                f"{error_msg}\n\n"
                f"{t('premium_section.recipient.enter', lang)}"
            ),
            reply_markup=get_premium_recipient_keyboard(lang),
        )
        await callback.answer()
        return

    await state.update_data(
        recipient_id=recipient_info.recipient_id,
        recipient_username=user.username,
        recipient_display_name=recipient_info.display_name,
    )

    # Получаем баланс пользователя и цены
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        user_balance = db_user.balance_usdt if db_user else Decimal("0")

    prices = await get_premium_settings()
    affordable_months = calculate_affordable_months(user_balance, prices)

    # Если срок уже выбран (из inline калькулятора) - сразу к оплате
    if duration_preset and duration and duration in [3, 6, 12]:
        await state.set_state(BuyPremiumStates.waiting_payment)
        price_usdt = prices.get(duration, Decimal("0"))
        recipient = user.username

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.payment.title', lang)}\n\n"
                f"{t('premium_section.payment.info', lang, username=recipient, months=duration, price=f'{price_usdt:,.2f}')}"
                f"\n\n{t('common.payment.select', lang)}"
            ),
            reply_markup=get_premium_payment_method_keyboard(lang),
        )
        await callback.answer()
        return

    await state.set_state(BuyPremiumStates.waiting_duration)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('premium_section.duration.title', lang)}\n\n"
            f"{t('premium_section.duration.recipient_info', lang, username=user.username)}\n\n"
            f"{t('premium_section.duration.buy_info', lang, price_3=f'{prices[3]:,.2f}', price_6=f'{prices[6]:,.2f}', price_12=f'{prices[12]:,.2f}', affordable_months=affordable_months, balance=f'{user_balance:,.2f}')}"
            f"\n\n{t('premium_section.duration.select', lang)}"
        ),
        reply_markup=get_duration_keyboard(lang),
    )
    await callback.answer()


@router.message(BuyPremiumStates.waiting_recipient)
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
    duration_preset = data.get("duration_preset", False)
    duration = data.get("duration")

    if not username or len(username) < 3:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.recipient.title', lang)}\n\n"
                    f"{t('common.recipient.invalid', lang)}\n\n"
                    f"{t('premium_section.recipient.enter', lang)}"
                ),
                reply_markup=get_premium_recipient_keyboard(lang),
            )
        return

    # Проверяем получателя через Fragment API
    success, recipient_info, error_msg = await validate_premium_recipient(username, lang)

    if not success:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.recipient.title', lang)}\n\n"
                    f"{error_msg}\n\n"
                    f"{t('premium_section.recipient.enter', lang)}"
                ),
                reply_markup=get_premium_recipient_keyboard(lang),
            )
        return

    # Сохраняем данные получателя
    await state.update_data(
        recipient_username=username,
        recipient_id=recipient_info.recipient_id,
        recipient_display_name=recipient_info.display_name,
    )

    # Получаем баланс пользователя и цены
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(message.from_user.id)
        user_balance = db_user.balance_usdt if db_user else Decimal("0")

    prices = await get_premium_settings()
    affordable_months = calculate_affordable_months(user_balance, prices)

    # Если срок уже выбран (из inline калькулятора) - сразу к оплате
    if duration_preset and duration and duration in [3, 6, 12]:
        await state.set_state(BuyPremiumStates.waiting_payment)
        price_usdt = prices.get(duration, Decimal("0"))

        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.payment.title', lang)}\n\n"
                    f"{t('premium_section.payment.info', lang, username=username, months=duration, price=f'{price_usdt:,.2f}')}"
                    f"\n\n{t('common.payment.select', lang)}"
                ),
                reply_markup=get_premium_payment_method_keyboard(lang),
            )
        return

    await state.set_state(BuyPremiumStates.waiting_duration)

    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('premium_section.duration.title', lang)}\n\n"
                f"{t('premium_section.duration.recipient_info', lang, username=username)}\n\n"
                f"{t('premium_section.duration.buy_info', lang, price_3=f'{prices[3]:,.2f}', price_6=f'{prices[6]:,.2f}', price_12=f'{prices[12]:,.2f}', affordable_months=affordable_months, balance=f'{user_balance:,.2f}')}"
                f"\n\n{t('premium_section.duration.select', lang)}"
            ),
            reply_markup=get_duration_keyboard(lang),
        )


@router.callback_query(F.data == PremiumCallback.BACK_TO_RECIPIENT)
async def callback_back_to_recipient(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору получателя."""
    data = await state.get_data()
    mode = data.get("mode", "buy")
    lang = data.get("lang", "ru")

    if mode == "buy":
        await state.set_state(BuyPremiumStates.waiting_recipient)
    else:
        await state.set_state(WithdrawPremiumStates.waiting_recipient)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.recipient.title', lang)}\n\n"
            f"{t('premium_section.recipient.enter', lang)}"
        ),
        reply_markup=get_premium_recipient_keyboard(lang),
    )
    await callback.answer()


# Обработка выбора срока через кнопки
@router.callback_query(F.data.startswith("premium:duration:"), BuyPremiumStates.waiting_duration)
async def callback_duration_buy(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор срока Premium (покупка)."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    try:
        duration = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer(t("common.validation.invalid_duration", lang), show_alert=True)
        return

    # Валидация: допустимые сроки 3, 6, 12 месяцев
    if duration not in [3, 6, 12]:
        await callback.answer(t("common.validation.duration_range", lang), show_alert=True)
        return

    await state.update_data(duration=duration)
    await state.set_state(BuyPremiumStates.waiting_payment)

    recipient = data.get("recipient_username", "me")
    price_usdt = await get_premium_price(duration)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.payment.title', lang)}\n\n"
            f"{t('premium_section.payment.info', lang, username=recipient, months=duration, price=f'{price_usdt:,.2f}')}"
            f"\n\n{t('common.payment.select', lang)}"
        ),
        reply_markup=get_premium_payment_method_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == PremiumCallback.BACK_TO_DURATION)
async def callback_back_to_duration(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору срока."""
    user = callback.from_user
    data = await state.get_data()
    mode = data.get("mode", "buy")
    lang = data.get("lang", "ru")

    recipient_username = data.get("recipient_username", "")

    if mode == "buy":
        await state.set_state(BuyPremiumStates.waiting_duration)

        # Получаем баланс пользователя и цены
        async with async_session_factory() as session:
            user_service = UserService(session)
            db_user = await user_service.get_user(user.id)
            user_balance = db_user.balance_usdt if db_user else Decimal("0")

        prices = await get_premium_settings()
        affordable_months = calculate_affordable_months(user_balance, prices)

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('premium_section.duration.title', lang)}\n\n"
                f"{t('premium_section.duration.recipient_info', lang, username=recipient_username)}\n\n"
                f"{t('premium_section.duration.buy_info', lang, price_3=f'{prices[3]:,.2f}', price_6=f'{prices[6]:,.2f}', price_12=f'{prices[12]:,.2f}', affordable_months=affordable_months, balance=f'{user_balance:,.2f}')}"
                f"\n\n{t('premium_section.duration.select', lang)}"
            ),
            reply_markup=get_duration_keyboard(lang),
        )
    else:
        await state.set_state(WithdrawPremiumStates.waiting_duration)

        # Получаем баланс Premium
        async with async_session_factory() as session:
            user_service = UserService(session)
            db_user = await user_service.get_user(user.id)
            max_months = db_user.balance_premium_months if db_user else 0

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('premium_section.duration.title', lang)}\n\n"
                f"{t('premium_section.duration.recipient_info', lang, username=recipient_username)}\n\n"
                f"{t('premium_section.duration.withdraw_info', lang, available=max_months)}"
                f"\n\n{t('premium_section.duration.select', lang)}"
            ),
            reply_markup=get_duration_keyboard(lang),
        )

    await callback.answer()


@router.callback_query(F.data == PremiumCallback.BACK_TO_PAYMENT)
async def callback_back_to_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору способа оплаты (после ошибки)."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    duration = data.get("duration", 3)
    recipient_username = data.get("recipient_username", "")
    price_usdt = await get_premium_price(duration)

    await state.set_state(BuyPremiumStates.waiting_payment)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.payment.title', lang)}\n\n"
            f"{t('premium_section.payment.info', lang, username=recipient_username, months=duration, price=f'{price_usdt:,.2f}')}"
            f"\n\n{t('common.payment.select', lang)}"
        ),
        reply_markup=get_premium_payment_method_keyboard(lang),
    )
    await callback.answer()


# Выбор способа оплаты
@router.callback_query(F.data == PremiumCallback.PAY_BALANCE, BuyPremiumStates.waiting_payment)
async def callback_pay_balance(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата с баланса USDT."""
    user = callback.from_user
    data = await state.get_data()
    duration = data.get("duration", 3)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")
    price_usdt = await get_premium_price(duration)

    # Тип продукта — PREMIUM, количество месяцев передаётся в quantity
    product_type = ProductType.PREMIUM

    async with async_session_factory() as session:
        user_service = UserService(session)
        order_service = OrderService(session)

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
            product_type=product_type,
            quantity=duration,
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
            logger.info(f"Premium order {order.id} enqueued for user {user.id}")
        else:
            logger.warning(f"OrderWorker not available, order {order.id} will be recovered on restart")

        try:
            msg = await callback.message.edit_text(
                text=(
                    f"{t('common.order.created_title', lang, order_key=order.order_key)}\n\n"
                    f"<blockquote>{t('common.order.recipient', lang, username=recipient_username)}\n"
                    f"{t('common.order.quantity_premium', lang, months=duration)}\n"
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

    await state.clear()
    await callback.answer()


@router.callback_query(F.data == PremiumCallback.PAY_CRYPTOBOT, BuyPremiumStates.waiting_payment)
async def callback_pay_cryptobot(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата через CryptoBot."""
    data = await state.get_data()
    duration = data.get("duration", 3)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")
    price_usdt = await get_premium_price(duration)
    user_id = callback.from_user.id

    # Добавляем комиссию CryptoBot из БД
    cryptobot_fee = await get_cryptobot_fee()
    amount_with_fee = (price_usdt * (1 + cryptobot_fee)).quantize(Decimal("0.01"))

    # Показываем сообщение о создании инвойса
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.cryptobot_payment.title', lang)}\n\n"
            f"{t('common.cryptobot_payment.premium_info', lang, recipient=recipient_username, months=duration, price=f'{amount_with_fee:,.2f}')}"
            f"\n\n{t('common.cryptobot_payment.creating', lang)}"
        ),
    )

    try:
        # Создаём инвойс в CryptoPay
        invoice = await create_deposit_invoice(
            amount=amount_with_fee,
            user_id=user_id,
            description=f"Premium purchase: {duration} months",
        )

        # Сохраняем данные инвойса
        await state.update_data(
            invoice_id=invoice.invoice_id,
            pay_url=invoice.bot_invoice_url,
            amount_with_fee=str(amount_with_fee),
        )
        await state.set_state(BuyPremiumStates.waiting_cryptobot_payment)

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.cryptobot_payment.title', lang)}\n\n"
                f"{t('common.cryptobot_payment.premium_info', lang, recipient=recipient_username, months=duration, price=f'{amount_with_fee:,.2f}')}"
                f"\n\n{t('common.cryptobot_payment.instructions', lang)}"
            ),
            reply_markup=get_premium_payment_pending_keyboard(invoice.bot_invoice_url, lang),
        )

    except Exception as e:
        logger.error(f"Failed to create invoice for premium: {e}")
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


@router.callback_query(F.data == PremiumCallback.PAY_TON, BuyPremiumStates.waiting_payment)
async def callback_pay_ton(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата TON."""
    data = await state.get_data()
    duration = data.get("duration", 3)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")
    price_usdt = await get_premium_price(duration)
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
    payment_comment = generate_payment_comment(user_id, price_usdt, "premium", duration)

    # Создаём URL для оплаты
    ton_url = await create_ton_payment_url(amount_ton, payment_comment)

    # Сохраняем данные
    await state.update_data(
        payment_comment=payment_comment,
        amount_ton=str(amount_ton),
        ton_rate=str(ton_rate),
        payment_created_at=int(time.time()),
    )
    await state.set_state(BuyPremiumStates.waiting_ton_payment)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.ton_payment.title', lang)}\n\n"
            f"{t('common.ton_payment.premium_info', lang, recipient=recipient_username, months=duration, ton_amount=f'{amount_ton:,.4f}', usdt_amount=f'{price_usdt:,.2f}')}"
            f"\n\n{t('common.ton_payment.instructions', lang)}"
            f"\n{t('common.ton_payment.warning', lang)}"
        ),
        reply_markup=get_premium_ton_payment_keyboard(ton_url, lang),
    )

    await callback.answer()


# ==================== ПРОВЕРКА ОПЛАТЫ CRYPTOBOT ====================

@router.callback_query(F.data == PremiumCallback.CHECK_PAYMENT, BuyPremiumStates.waiting_cryptobot_payment)
async def callback_check_cryptobot_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка оплаты CryptoBot для Premium."""
    data = await state.get_data()
    invoice_id = data.get("invoice_id")
    duration = data.get("duration", 3)
    recipient_username = data.get("recipient_username")
    price_usdt = await get_premium_price(duration)
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
        external_id = f"cryptobot_premium:{invoice_id}"

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
                # Создаём заказ на Premium
                order, created = await order_service.create_order(
                    user_id=user_id,
                    recipient_username=recipient_username,
                    product_type=ProductType.PREMIUM,
                    quantity=duration,
                    price_usdt=price_usdt,
                    payment_provider=PaymentProvider.CRYPTOBOT,
                )

                if created:
                    # Записываем транзакцию с привязкой к заказу
                    transaction = Transaction(
                        user_id=user_id,
                        order_id=order.id,
                        type="premium_purchase",
                        amount_usdt=price_usdt,
                        description=f"Premium purchase via CryptoBot: {duration} months",
                        external_id=external_id,
                    )
                    session.add(transaction)
                    await session.commit()

                    # Добавляем в очередь
                    worker = get_order_worker()
                    if worker:
                        await worker.enqueue_order(order.id)
                        logger.info(f"Premium order {order.id} enqueued (CryptoBot)")

                    try:
                        msg = await callback.message.edit_text(
                            text=(
                                f"{t('common.order.created_title', lang, order_key=order.order_key)}\n\n"
                                f"<blockquote>{t('common.order.recipient', lang, username=recipient_username)}\n"
                                f"{t('common.order.quantity_premium', lang, months=duration)}\n"
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
            reply_markup=get_back_to_premium_keyboard(lang),
        )
        await state.clear()

    else:
        await callback.answer(t("common.payment_errors.not_received", lang), show_alert=True)


# ==================== ПРОВЕРКА ОПЛАТЫ TON ====================

@router.callback_query(F.data == PremiumCallback.CHECK_TON_PAYMENT, BuyPremiumStates.waiting_ton_payment)
async def callback_check_ton_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка оплаты TON для Premium."""
    data = await state.get_data()
    duration = data.get("duration", 3)
    recipient_username = data.get("recipient_username")
    price_usdt = await get_premium_price(duration)
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
            reply_markup=get_back_to_premium_keyboard(lang),
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
        external_id = f"ton_premium:{payment['event_id']}"

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
                # Создаём заказ на Premium
                order, created = await order_service.create_order(
                    user_id=user_id,
                    recipient_username=recipient_username,
                    product_type=ProductType.PREMIUM,
                    quantity=duration,
                    price_usdt=price_usdt,
                    payment_provider=PaymentProvider.TON,
                )

                if created:
                    # Записываем транзакцию с привязкой к заказу
                    transaction = Transaction(
                        user_id=user_id,
                        order_id=order.id,
                        type="premium_purchase",
                        amount_usdt=price_usdt,
                        description=f"Premium purchase via TON: {duration} months ({payment['amount_ton']:.4f} TON)",
                        external_id=external_id,
                    )
                    session.add(transaction)
                    await session.commit()

                    # Добавляем в очередь
                    worker = get_order_worker()
                    if worker:
                        await worker.enqueue_order(order.id)
                        logger.info(f"Premium order {order.id} enqueued (TON)")

                    try:
                        msg = await callback.message.edit_text(
                            text=(
                                f"{t('common.order.created_title', lang, order_key=order.order_key)}\n\n"
                                f"<blockquote>{t('common.order.recipient', lang, username=recipient_username)}\n"
                                f"{t('common.order.quantity_premium', lang, months=duration)}\n"
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

@router.callback_query(F.data == PremiumCallback.CANCEL_PAYMENT, BuyPremiumStates.waiting_cryptobot_payment)
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
        reply_markup=get_back_to_premium_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == PremiumCallback.CANCEL_PAYMENT, BuyPremiumStates.waiting_ton_payment)
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
        reply_markup=get_back_to_premium_keyboard(lang),
    )
    await callback.answer()


# ==================== ПОЛУЧЕНИЕ PREMIUM С БАЛАНСА ====================

@router.callback_query(F.data == PremiumCallback.WITHDRAW)
async def callback_withdraw_premium(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало получения Premium с баланса - выбор получателя."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user or db_user.balance_premium_months < 3:
            await callback.answer(
                t("premium_section.duration.insufficient", lang),
                show_alert=True,
            )
            return

    await state.set_state(WithdrawPremiumStates.waiting_recipient)
    await state.update_data(mode="withdraw", bot_message_id=callback.message.message_id)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.recipient.title', lang)}\n\n"
            f"{t('premium_section.recipient.enter', lang)}"
        ),
        reply_markup=get_premium_recipient_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == PremiumCallback.RECIPIENT_SELF, WithdrawPremiumStates.waiting_recipient)
async def callback_recipient_self_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор себя как получателя (получение с баланса)."""
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")

    # Проверяем наличие username
    if not user.username:
        await callback.answer(
            t("common.set_username_full", lang).strip(),
            show_alert=True,
        )
        return

    # Проверяем получателя через Fragment API (у пользователя может быть Premium)
    success, recipient_info, error_msg = await validate_premium_recipient(user.username, lang)

    if not success:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('common.recipient.title', lang)}\n\n"
                f"{error_msg}\n\n"
                f"{t('premium_section.recipient.enter', lang)}"
            ),
            reply_markup=get_premium_recipient_keyboard(lang),
        )
        await callback.answer()
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        max_months = db_user.balance_premium_months if db_user else 0

    await state.update_data(
        recipient_id=recipient_info.recipient_id,
        recipient_username=user.username,
        recipient_display_name=recipient_info.display_name,
        max_months=max_months,
    )
    await state.set_state(WithdrawPremiumStates.waiting_duration)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('premium_section.duration.title', lang)}\n\n"
            f"{t('premium_section.duration.recipient_info', lang, username=user.username)}\n\n"
            f"{t('premium_section.duration.withdraw_info', lang, available=max_months)}"
            f"\n\n{t('premium_section.duration.select', lang)}"
        ),
        reply_markup=get_duration_keyboard(lang),
    )
    await callback.answer()


@router.message(WithdrawPremiumStates.waiting_recipient)
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
                    f"{t('premium_section.recipient.enter', lang)}"
                ),
                reply_markup=get_premium_recipient_keyboard(lang),
            )
        return

    # Проверяем получателя через Fragment API
    success, recipient_info, error_msg = await validate_premium_recipient(username, lang)

    if not success:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('common.recipient.title', lang)}\n\n"
                    f"{error_msg}\n\n"
                    f"{t('premium_section.recipient.enter', lang)}"
                ),
                reply_markup=get_premium_recipient_keyboard(lang),
            )
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        sender = await user_service.get_user(message.from_user.id)
        max_months = sender.balance_premium_months if sender else 0

    # Сохраняем данные получателя
    await state.update_data(
        recipient_username=username,
        recipient_id=recipient_info.recipient_id,
        recipient_display_name=recipient_info.display_name,
        max_months=max_months,
    )
    await state.set_state(WithdrawPremiumStates.waiting_duration)

    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('premium_section.duration.title', lang)}\n\n"
                f"{t('premium_section.duration.recipient_info', lang, username=username)}\n\n"
                f"{t('premium_section.duration.withdraw_info', lang, available=max_months)}"
                f"\n\n{t('premium_section.duration.select', lang)}"
            ),
            reply_markup=get_duration_keyboard(lang),
        )


# Обработка выбора срока через кнопки (получение с баланса)
@router.callback_query(F.data.startswith("premium:duration:"), WithdrawPremiumStates.waiting_duration)
async def callback_duration_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор срока Premium (получение с баланса)."""
    data = await state.get_data()
    max_months = data.get("max_months", 0)
    lang = data.get("lang", "ru")

    try:
        duration = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer(t("common.validation.invalid_duration", lang), show_alert=True)
        return

    # Валидация: допустимые сроки 3, 6, 12 месяцев
    if duration not in [3, 6, 12]:
        await callback.answer(t("common.validation.duration_range", lang), show_alert=True)
        return

    if duration > max_months:
        await callback.answer(
            t("premium_section.duration.insufficient_balance", lang, months=max_months),
            show_alert=True,
        )
        return

    await state.update_data(duration=duration)
    await state.set_state(WithdrawPremiumStates.waiting_confirm)

    recipient = data.get("recipient_username", "me")

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('common.confirmation.title', lang)}\n\n"
            f"{t('premium_section.payment.recipient', lang, username=recipient)}\n"
            f"{t('premium_section.payment.duration', lang, months=duration)}\n\n"
            f"{t('premium_section.confirm.send', lang)}"
        ),
        reply_markup=get_confirm_premium_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == PremiumCallback.CONFIRM, WithdrawPremiumStates.waiting_confirm)
async def callback_confirm_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение отправки Premium с баланса."""
    user = callback.from_user
    data = await state.get_data()
    duration = data.get("duration", 3)
    recipient_username = data.get("recipient_username")
    lang = data.get("lang", "ru")

    # Тип продукта — PREMIUM, количество месяцев передаётся в quantity
    product_type = ProductType.PREMIUM

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

        if not sender or sender.balance_premium_months < duration:
            await callback.answer(
                t("premium_section.duration.insufficient_balance", lang, months=sender.balance_premium_months if sender else 0),
                show_alert=True,
            )
            # Не очищаем state - пользователь может изменить срок
            return

        # Создаём заказ (идемпотентно)
        order, created = await order_service.create_order(
            user_id=user.id,
            recipient_username=recipient_username,
            product_type=product_type,
            quantity=duration,
            price_usdt=Decimal("0"),  # Бесплатно с баланса Premium месяцев
            payment_provider=PaymentProvider.BALANCE,
        )

        if not created:
            await callback.answer(
                t("common.duplicate_request", lang),
                show_alert=True,
            )
            await state.clear()
            return

        # Списываем месяцы Premium с баланса отправителя
        sender.balance_premium_months -= duration
        await session.commit()

        # Добавляем заказ в очередь на обработку
        worker = get_order_worker()
        if worker:
            await worker.enqueue_order(order.id)
            logger.info(f"Premium withdraw order {order.id} enqueued for user {user.id}")
        else:
            logger.warning(f"OrderWorker not available, order {order.id} will be recovered on restart")

        try:
            msg = await callback.message.edit_text(
                text=(
                    f"{t('common.order.created_title', lang, order_key=order.order_key)}\n\n"
                    f"<blockquote>{t('common.order.recipient', lang, username=recipient_username)}\n"
                    f"{t('common.order.quantity_premium', lang, months=duration)}</blockquote>\n\n"
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


