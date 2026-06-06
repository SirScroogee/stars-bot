"""
Handlers для раздела пополнения баланса.
"""
import logging
import time
from decimal import Decimal

from aiogram import F, Router, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sqlalchemy import select

from src.bot.keyboards.menu import MenuCallback
from src.bot.keyboards.deposit import (
    DepositCallback,
    get_amount_input_keyboard,
    get_back_to_deposit_keyboard,
    get_payment_error_keyboard,
    get_payment_method_keyboard,
    get_payment_pending_keyboard,
    get_platega_payment_keyboard,
    get_ton_payment_keyboard,
)
from src.db.models import Transaction, BalanceLedger
from src.db.session import async_session_factory
from src.locales import t, get_user_locale
from src.services.user_service import UserService
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
from src.services.telegram_logger import tg_logger
from src.services.bot_settings_service import get_cryptobot_fee
from src.services.platega_service import (
    PlategaConfigError,
    PlategaError,
    build_platega_payment_text,
    create_platega_payment,
    process_platega_payment,
)
from src.bot.menu_media import edit_menu_message

logger = logging.getLogger(__name__)

router = Router(name="deposit")

# Константы
MIN_DEPOSIT = Decimal("0.50")
MAX_DEPOSIT = Decimal("1000000.00")  # Практически без ограничения


class DepositStates(StatesGroup):
    """Состояния для пополнения баланса."""

    waiting_amount = State()
    waiting_method = State()
    waiting_payment = State()  # CryptoBot
    waiting_ton_payment = State()  # TON прямой перевод
    waiting_platega_payment = State()  # СБП Platega


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


async def safe_callback_answer(callback: CallbackQuery) -> None:
    """Ignore expired callback answers; Telegram allows answering only briefly."""
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        if "query is too old" not in str(e):
            raise


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


# ==================== ПОПОЛНЕНИЕ БАЛАНСА ====================

@router.callback_query(F.data == MenuCallback.DEPOSIT)
async def callback_deposit_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало пополнения баланса - ввод суммы."""
    await state.clear()
    await state.set_state(DepositStates.waiting_amount)
    await state.update_data(bot_message_id=callback.message.message_id)

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
        await state.update_data(lang=lang)

        sent_message = await edit_menu_message(
            callback,
            "deposit",
            text=(
                f"{t('deposit.title', lang)}\n\n"
                f"{t('deposit.current_balance', lang, balance=f'{db_user.balance_usdt:,.2f}')}\n\n"
                f"{t('deposit.enter_amount_prompt', lang)}\n"
                f"{t('deposit.amount_range', lang, min=str(MIN_DEPOSIT), max=f'{MAX_DEPOSIT:,.0f}')}"
            ),
            reply_markup=get_amount_input_keyboard(lang),
        )
        if sent_message:
            await state.update_data(bot_message_id=sent_message.message_id)

    await safe_callback_answer(callback)


@router.message(DepositStates.waiting_amount)
async def message_deposit_amount(message: Message, state: FSMContext) -> None:
    """Ввод суммы пополнения."""
    await safe_delete_message(message)

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    lang = data.get("lang", "ru")

    # Проверяем что пользователь отправил текст
    if not message.text:
        return

    try:
        amount = Decimal(message.text.strip().replace(",", "."))
    except Exception:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('deposit.title', lang)}\n\n"
                    f"{t('deposit.invalid_amount', lang)}\n\n"
                    f"{t('deposit.enter_amount_prompt', lang)}\n"
                    f"{t('deposit.amount_range', lang, min=str(MIN_DEPOSIT), max=f'{MAX_DEPOSIT:,.0f}')}"
                ),
                reply_markup=get_amount_input_keyboard(lang),
            )
        return

    if amount < MIN_DEPOSIT:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('deposit.title', lang)}\n\n"
                    f"{t('deposit.min_error', lang, min=str(MIN_DEPOSIT))}\n\n"
                    f"{t('deposit.enter_amount_prompt', lang)}\n"
                    f"{t('deposit.amount_range', lang, min=str(MIN_DEPOSIT), max=f'{MAX_DEPOSIT:,.0f}')}"
                ),
                reply_markup=get_amount_input_keyboard(lang),
            )
        return

    if amount > MAX_DEPOSIT:
        if bot_message_id:
            await edit_bot_message(
                message.bot,
                message.chat.id,
                bot_message_id,
                text=(
                    f"{t('deposit.title', lang)}\n\n"
                    f"{t('deposit.max_error', lang, max=f'{MAX_DEPOSIT:,.0f}')}\n\n"
                    f"{t('deposit.enter_amount_prompt', lang)}\n"
                    f"{t('deposit.amount_range', lang, min=str(MIN_DEPOSIT), max=f'{MAX_DEPOSIT:,.0f}')}"
                ),
                reply_markup=get_amount_input_keyboard(lang),
            )
        return

    await state.update_data(amount=str(amount))
    await state.set_state(DepositStates.waiting_method)

    if bot_message_id:
        await edit_bot_message(
            message.bot,
            message.chat.id,
            bot_message_id,
            text=(
                f"{t('deposit.select_method', lang)}\n\n"
                f"{t('deposit.amount_label', lang, amount=f'{amount:,.2f}')}\n\n"
                f"{t('deposit.select_method_prompt', lang)}"
            ),
            reply_markup=get_payment_method_keyboard(lang),
        )


@router.callback_query(F.data == DepositCallback.PAY_CRYPTOBOT, DepositStates.waiting_method)
async def callback_pay_cryptobot(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата через CryptoBot - создание инвойса."""
    await _create_cryptobot_payment(callback, state)


@router.callback_query(F.data == DepositCallback.PAY_TON, DepositStates.waiting_method)
async def callback_pay_ton(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата через прямой перевод TON."""
    await _create_ton_payment(callback, state)


@router.callback_query(F.data == DepositCallback.PAY_PLATEGA_SBP, DepositStates.waiting_method)
async def callback_pay_platega_sbp(callback: CallbackQuery, state: FSMContext) -> None:
    """Оплата пополнения через СБП Platega."""
    await _create_platega_payment(callback, state)


async def _create_cryptobot_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Создать инвойс и показать кнопку оплаты."""
    data = await state.get_data()
    amount = Decimal(data.get("amount", "0"))
    lang = data.get("lang", "ru")
    user_id = callback.from_user.id

    # Добавляем комиссию CryptoBot из БД
    cryptobot_fee = await get_cryptobot_fee()
    amount_with_fee = (amount * (1 + cryptobot_fee)).quantize(Decimal("0.01"))

    # Показываем сообщение о создании инвойса
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('deposit.creating', lang)}\n\n"
            f"{t('deposit.to_pay', lang, amount=f'{amount_with_fee:,.2f}')}\n"
            f"{t('deposit.will_credit', lang, amount=f'{amount:,.2f}')}\n\n"
            f"{t('deposit.please_wait', lang)}"
        ),
    )

    try:
        # Создаём инвойс в CryptoPay (с наценкой)
        invoice = await create_deposit_invoice(
            amount=amount_with_fee,
            user_id=user_id,
        )

        # Сохраняем данные инвойса в состояние
        await state.update_data(
            invoice_id=invoice.invoice_id,
            pay_url=invoice.bot_invoice_url,
            amount_with_fee=str(amount_with_fee),
        )
        await state.set_state(DepositStates.waiting_payment)

        # Показываем кнопку оплаты
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('deposit.cryptobot_title', lang)}\n\n"
                f"{t('deposit.to_pay', lang, amount=f'{amount_with_fee:,.2f}')}\n"
                f"{t('deposit.will_credit', lang, amount=f'{amount:,.2f}')}\n\n"
                f"{t('deposit.cryptobot_instructions', lang)}\n"
                f"{t('deposit.payment_time', lang)}"
            ),
            reply_markup=get_payment_pending_keyboard(invoice.bot_invoice_url, lang),
        )

    except Exception as e:
        logger.error(f"Failed to create invoice: {e}")
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('deposit.error_title', lang)}\n\n"
                f"{t('deposit.error_create', lang)}"
            ),
            reply_markup=get_payment_error_keyboard(lang),
        )
        # Не очищаем state — пользователь может выбрать другой способ оплаты

    await callback.answer()


async def _create_ton_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Создать платёж TON и показать ссылку на оплату."""
    data = await state.get_data()
    amount = Decimal(data.get("amount", "0"))
    lang = data.get("lang", "ru")
    user_id = callback.from_user.id

    # Показываем сообщение о получении курса
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('deposit.ton_title', lang)}\n\n"
            f"{t('deposit.amount_label', lang, amount=f'{amount:,.2f}')}\n\n"
            f"{t('deposit.ton_getting_rate', lang)}"
        ),
    )

    # Получаем курс TON/USD
    ton_rate = await get_ton_usd_rate()

    if not ton_rate:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('deposit.error_title', lang)}\n\n"
                f"{t('deposit.ton_rate_error', lang)}"
            ),
            reply_markup=get_payment_error_keyboard(lang),
        )
        # Не очищаем state — пользователь может выбрать другой способ оплаты
        await callback.answer()
        return

    # Рассчитываем сумму в TON
    amount_ton = (amount / ton_rate).quantize(Decimal("0.0001"))

    # Генерируем уникальный комментарий
    payment_comment = generate_payment_comment(user_id, amount)

    # Создаём URL для оплаты
    ton_url = await create_ton_payment_url(amount_ton, payment_comment)

    # Сохраняем данные в состояние
    await state.update_data(
        payment_comment=payment_comment,
        amount_ton=str(amount_ton),
        ton_rate=str(ton_rate),
        payment_created_at=int(time.time()),
    )
    await state.set_state(DepositStates.waiting_ton_payment)

    # Показываем кнопку оплаты
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('deposit.ton_title', lang)}\n\n"
            f"{t('deposit.amount_label', lang, amount=f'{amount:,.2f}')}\n"
            f"{t('deposit.ton_amount', lang, amount=f'{amount_ton:,.4f}')}\n"
            f"{t('deposit.ton_rate', lang, rate=f'{ton_rate:,.2f}')}\n\n"
            f"{t('deposit.ton_instructions', lang)}"
            f"\n{t('deposit.ton_warning', lang)}"
            f"\n\n{t('deposit.check_after_pay', lang)}\n\n"
            f"{t('deposit.payment_time', lang)}"
        ),
        reply_markup=get_ton_payment_keyboard(ton_url, lang),
    )

    await callback.answer()


async def _create_platega_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Создать платеж СБП Platega для пополнения баланса."""
    data = await state.get_data()
    amount = Decimal(data.get("amount", "0"))
    lang = data.get("lang", "ru")
    user_id = callback.from_user.id

    await safe_edit_message(
        callback.message,
        text=(
            "🏦 <b>Оплата по СБП</b>\n\n"
            f"{t('deposit.amount_label', lang, amount=f'{amount:,.2f}')}\n\n"
            f"{t('deposit.please_wait', lang)}"
        ),
    )

    try:
        async with async_session_factory() as session:
            created = await create_platega_payment(
                session,
                user_id=user_id,
                operation_type="deposit",
                amount_usdt=amount,
                description=f"Balance deposit: {amount} USDT",
                metadata={"amount_usdt": str(amount)},
                message_id=callback.message.message_id,
            )
            await session.commit()

        await state.update_data(platega_payment_id=created.payment.id)
        await state.set_state(DepositStates.waiting_platega_payment)

        text = build_platega_payment_text(
            title="🏦 <b>Оплата по СБП</b>",
            item_line=f"Зачислится: <b>{amount:,.2f} USDT</b>",
            amount_usdt=created.amount_to_pay_usdt,
            amount_rub=created.amount_with_fee_rub,
            fee_percent=created.fee_percent,
            ttl_minutes=created.ttl_minutes,
        )
        await safe_edit_message(
            callback.message,
            text=text,
            reply_markup=get_platega_payment_keyboard(created.pay_url, lang),
        )

    except PlategaConfigError as e:
        await safe_edit_message(
            callback.message,
            text=f"❌ <b>СБП временно недоступен</b>\n\n{e}",
            reply_markup=get_payment_error_keyboard(lang),
        )
    except PlategaError as e:
        logger.error("Failed to create Platega deposit payment: %s", e)
        await safe_edit_message(
            callback.message,
            text="❌ <b>Ошибка создания СБП-платежа</b>\n\nПопробуйте позже или выберите другой способ оплаты.",
            reply_markup=get_payment_error_keyboard(lang),
        )

    await callback.answer()


@router.callback_query(F.data == DepositCallback.CHECK_PAYMENT, DepositStates.waiting_payment)
async def callback_check_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка оплаты CryptoBot."""
    data = await state.get_data()
    invoice_id = data.get("invoice_id")
    amount = Decimal(data.get("amount", "0"))  # Сумма без наценки (зачисляется)
    lang = data.get("lang", "ru")

    if not invoice_id:
        await callback.answer(t("deposit.error_not_found", lang), show_alert=True)
        return

    # Проверяем статус инвойса
    invoice = await check_invoice_status(invoice_id)

    if not invoice:
        await callback.answer(t("deposit.error_check", lang), show_alert=True)
        return

    if invoice.status == "paid":
        # Оплачено! Зачисляем на баланс
        user_id = callback.from_user.id
        external_id = f"cryptobot:{invoice_id}"

        async with async_session_factory() as session:
            # Проверяем идемпотентность - не была ли эта транзакция уже обработана
            existing_tx = await session.execute(
                select(Transaction).where(Transaction.external_id == external_id)
            )
            if existing_tx.scalar_one_or_none():
                logger.warning(f"Duplicate CryptoBot payment detected: {external_id}")
                await callback.answer(
                    t("deposit.already_credited", lang),
                    show_alert=True,
                )
                await state.clear()
                return

            user_service = UserService(session)
            # Блокируем строку для предотвращения race condition
            db_user = await user_service.get_user_for_update(user_id)

            if db_user:
                # Сохраняем балансы до изменений
                balance_usdt_before = db_user.balance_usdt

                # Зачисляем на баланс (без наценки)
                db_user.balance_usdt += amount

                # Создаём запись о транзакции с external_id для идемпотентности
                transaction = Transaction(
                    user_id=user_id,
                    type="deposit",
                    amount_usdt=amount,
                    description=f"CryptoBot deposit",
                    external_id=external_id,
                )
                session.add(transaction)

                # Запись в ledger
                ledger = BalanceLedger(
                    user_id=user_id,
                    operation="credit",
                    amount_stars=Decimal("0"),
                    amount_usdt=amount,
                    amount_premium=0,
                    balance_stars_before=db_user.balance_stars,
                    balance_stars_after=db_user.balance_stars,
                    balance_usdt_before=balance_usdt_before,
                    balance_usdt_after=db_user.balance_usdt,
                    balance_premium_before=db_user.balance_premium_months,
                    balance_premium_after=db_user.balance_premium_months,
                    description=f"CryptoBot deposit: +{amount} USDT",
                )
                session.add(ledger)

                await session.commit()

                new_balance = db_user.balance_usdt

                await safe_edit_message(
                    callback.message,
                    text=t("deposit.success", lang, amount=f"{amount:,.2f}", balance=f"{new_balance:,.2f}"),
                    reply_markup=get_back_to_deposit_keyboard(lang),
                )

                logger.info(f"User {user_id} deposited {amount} USDT via CryptoBot (external_id={external_id})")

                # Логируем в Telegram
                await tg_logger.log_deposit(
                    user_id=user_id,
                    username=callback.from_user.username,
                    amount=amount,
                    currency="USDT (CryptoBot)",
                )
            else:
                await safe_edit_message(
                    callback.message,
                    text=t("deposit.error_user", lang),
                    reply_markup=get_back_to_deposit_keyboard(lang),
                )

        await state.clear()

    elif invoice.status == "expired":
        # Инвойс истёк
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('deposit.expired_title', lang)}\n\n"
                f"{t('deposit.expired_text', lang)}"
            ),
            reply_markup=get_back_to_deposit_keyboard(lang),
        )
        await state.clear()

    else:
        # Ещё не оплачено
        await callback.answer(
            t("deposit.not_received", lang),
            show_alert=True,
        )
        return

    await callback.answer()


@router.callback_query(F.data == DepositCallback.CHECK_PLATEGA_PAYMENT, DepositStates.waiting_platega_payment)
async def callback_check_platega_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка оплаты СБП."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    payment_id = data.get("platega_payment_id")

    if not payment_id:
        await callback.answer(t("deposit.error_not_found", lang), show_alert=True)
        return

    result = await process_platega_payment(int(payment_id), bot=callback.bot, force_check=True)
    if result.status == "pending":
        await callback.answer(t("deposit.not_received", lang), show_alert=True)
        return
    if result.final:
        await state.clear()
        await callback.answer()
        return

    await callback.answer(result.message or t("deposit.error_check", lang), show_alert=True)


@router.callback_query(F.data == DepositCallback.CANCEL_PAYMENT, DepositStates.waiting_platega_payment)
async def callback_cancel_platega_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена ожидания оплаты СБП."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await state.clear()
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('deposit.cancelled_title', lang)}\n\n"
            f"{t('deposit.cancelled_text', lang)}"
        ),
        reply_markup=get_back_to_deposit_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == DepositCallback.CANCEL_PAYMENT, DepositStates.waiting_payment)
async def callback_cancel_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена платежа CryptoBot."""
    data = await state.get_data()
    invoice_id = data.get("invoice_id")
    lang = data.get("lang", "ru")

    # Удаляем инвойс в CryptoPay
    if invoice_id:
        await delete_invoice(invoice_id)

    await state.clear()

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('deposit.cancelled_title', lang)}\n\n"
            f"{t('deposit.cancelled_text', lang)}"
        ),
        reply_markup=get_back_to_deposit_keyboard(lang),
    )

    await callback.answer()


# ==================== TON PAYMENT ====================


@router.callback_query(F.data == DepositCallback.CHECK_TON_PAYMENT, DepositStates.waiting_ton_payment)
async def callback_check_ton_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка оплаты TON."""
    data = await state.get_data()
    amount = Decimal(data.get("amount", "0"))
    amount_ton = Decimal(data.get("amount_ton", "0"))
    payment_comment = data.get("payment_comment", "")
    payment_created_at = data.get("payment_created_at", 0)
    lang = data.get("lang", "ru")

    if not payment_comment:
        await callback.answer(t("deposit.error_not_found", lang), show_alert=True)
        return

    # Проверяем не истекло ли время (30 минут)
    if int(time.time()) - payment_created_at > 1800:
        await safe_edit_message(
            callback.message,
            text=(
                f"{t('deposit.expired_title', lang)}\n\n"
                f"{t('deposit.expired_text', lang)}"
            ),
            reply_markup=get_back_to_deposit_keyboard(lang),
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
        # Оплачено! Зачисляем на баланс
        user_id = callback.from_user.id
        external_id = f"ton:{payment['event_id']}"

        async with async_session_factory() as session:
            # Проверяем идемпотентность - не была ли эта транзакция уже обработана
            existing_tx = await session.execute(
                select(Transaction).where(Transaction.external_id == external_id)
            )
            if existing_tx.scalar_one_or_none():
                logger.warning(f"Duplicate TON payment detected: {external_id}")
                await callback.answer(
                    t("deposit.already_credited", lang),
                    show_alert=True,
                )
                await state.clear()
                return

            user_service = UserService(session)
            # Блокируем строку для предотвращения race condition
            db_user = await user_service.get_user_for_update(user_id)

            if db_user:
                # Сохраняем балансы до изменений
                balance_usdt_before = db_user.balance_usdt

                db_user.balance_usdt += amount

                # Создаём запись о транзакции с external_id для идемпотентности
                transaction = Transaction(
                    user_id=user_id,
                    type="deposit",
                    amount_usdt=amount,
                    description=f"TON deposit ({payment['amount_ton']:.4f} TON)",
                    external_id=external_id,
                )
                session.add(transaction)

                # Запись в ledger
                ledger = BalanceLedger(
                    user_id=user_id,
                    operation="credit",
                    amount_stars=Decimal("0"),
                    amount_usdt=amount,
                    amount_premium=0,
                    balance_stars_before=db_user.balance_stars,
                    balance_stars_after=db_user.balance_stars,
                    balance_usdt_before=balance_usdt_before,
                    balance_usdt_after=db_user.balance_usdt,
                    balance_premium_before=db_user.balance_premium_months,
                    balance_premium_after=db_user.balance_premium_months,
                    description=f"TON deposit: +{amount} USDT ({payment['amount_ton']:.4f} TON)",
                )
                session.add(ledger)

                await session.commit()

                new_balance = db_user.balance_usdt

                await safe_edit_message(
                    callback.message,
                    text=t("deposit.success_ton", lang, ton=f"{payment['amount_ton']:,.4f}", amount=f"{amount:,.2f}", balance=f"{new_balance:,.2f}"),
                    reply_markup=get_back_to_deposit_keyboard(lang),
                )

                logger.info(f"User {user_id} deposited {amount} USDT via TON (external_id={external_id})")

                # Логируем в Telegram
                await tg_logger.log_deposit(
                    user_id=user_id,
                    username=callback.from_user.username,
                    amount=amount,
                    currency=f"USDT (TON {payment['amount_ton']:.4f})",
                )
            else:
                await safe_edit_message(
                    callback.message,
                    text=t("deposit.error_user", lang),
                    reply_markup=get_back_to_deposit_keyboard(lang),
                )

        await state.clear()
    else:
        await callback.answer(
            t("deposit.not_received", lang),
            show_alert=True,
        )
        return

    await callback.answer()


@router.callback_query(F.data == DepositCallback.CANCEL_PAYMENT, DepositStates.waiting_ton_payment)
async def callback_cancel_ton_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена платежа TON."""
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.clear()

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('deposit.cancelled_title', lang)}\n\n"
            f"{t('deposit.cancelled_text', lang)}"
        ),
        reply_markup=get_back_to_deposit_keyboard(lang),
    )

    await callback.answer()


@router.callback_query(F.data == DepositCallback.BACK_TO_AMOUNT)
async def callback_back_to_amount(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к вводу суммы."""
    await state.set_state(DepositStates.waiting_amount)
    user = callback.from_user
    data = await state.get_data()
    lang = data.get("lang", "ru")

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        balance = db_user.balance_usdt if db_user else Decimal("0")

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('deposit.title', lang)}\n\n"
                f"{t('deposit.current_balance', lang, balance=f'{balance:,.2f}')}\n\n"
                f"{t('deposit.enter_amount_prompt', lang)}\n"
                f"{t('deposit.amount_range', lang, min=str(MIN_DEPOSIT), max=f'{MAX_DEPOSIT:,.0f}')}"
            ),
            reply_markup=get_amount_input_keyboard(lang),
        )

    await safe_callback_answer(callback)


@router.callback_query(F.data == DepositCallback.BACK_TO_METHOD)
async def callback_back_to_method(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору способа оплаты."""
    await state.set_state(DepositStates.waiting_method)

    data = await state.get_data()
    amount = Decimal(data.get("amount", "0"))
    lang = data.get("lang", "ru")

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('deposit.select_method', lang)}\n\n"
            f"{t('deposit.amount_label', lang, amount=f'{amount:,.2f}')}\n\n"
            f"{t('deposit.select_method_prompt', lang)}"
        ),
        reply_markup=get_payment_method_keyboard(lang),
    )
    await callback.answer()
