"""
Handlers для профиля и реферальной системы.
"""
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

# Московский часовой пояс (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))


def format_datetime_msk(dt: datetime, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Форматировать datetime в московское время."""
    if dt.tzinfo is None:
        # Если нет timezone, предполагаем UTC
        dt = dt.replace(tzinfo=timezone.utc)
    msk_dt = dt.astimezone(MSK_TZ)
    return msk_dt.strftime(fmt)

from src.bot.keyboards.menu import MenuCallback, get_back_button
from src.bot.keyboards.profile import (
    TRANSACTION_EMOJIS,
    ProfileCallback,
    get_back_to_referrals_keyboard,
    get_history_keyboard,
    get_language_keyboard,
    get_order_detail_keyboard,
    get_profile_keyboard,
    get_promo_back_keyboard,
    get_referrals_keyboard,
    get_transaction_detail_keyboard,
)
from src.db.session import async_session_factory
from src.locales import t, get_user_locale
from src.services.bot_settings_service import get_referral_percents
from src.services.user_service import UserService

logger = logging.getLogger(__name__)

router = Router(name="profile")


async def safe_edit_message(
    message,
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


class PromoStates(StatesGroup):
    """Состояния для ввода промокода."""

    waiting_code = State()


def format_profile_text(
    user_id: int,
    username: str | None,
    balance_usdt: Decimal,
    balance_stars: Decimal,
    balance_premium: int,
    total_deposited: Decimal,
    total_stars_bought: int,
    total_stars_usdt: Decimal,
    total_premium_months: int,
    total_premium_usdt: Decimal,
    lang: str = "ru",
) -> str:
    """Форматировать текст профиля."""
    username_display = f"@{username}" if username else t("profile.username_not_set", lang)

    return t("profile.info", lang,
        user_id=user_id,
        username=username_display,
        balance_usdt=f"{balance_usdt:,.2f}",
        balance_stars=f"{int(balance_stars):,}",
        balance_premium=balance_premium,
        deposited=f"{total_deposited:,.2f}",
        stars_bought=f"{total_stars_bought:,}",
        stars_usdt=f"{total_stars_usdt:,.2f}",
        premium_bought=total_premium_months,
        premium_usdt=f"{total_premium_usdt:,.2f}",
    )


@router.callback_query(F.data == MenuCallback.PROFILE)
async def callback_profile(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать профиль."""
    # Очищаем состояние FSM
    await state.clear()

    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        # Получаем язык пользователя
        lang = db_user.language_code if db_user else get_user_locale(user.language_code)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Получаем статистику покупок
        purchase_stats = await user_service.get_purchase_stats(user.id)

        profile_text = format_profile_text(
            user_id=user.id,
            username=db_user.username,
            balance_usdt=db_user.balance_usdt,
            balance_stars=db_user.balance_stars,
            balance_premium=db_user.balance_premium_months,
            total_deposited=purchase_stats["total_deposited_usdt"],
            total_stars_bought=purchase_stats["total_stars_bought"],
            total_stars_usdt=purchase_stats["total_stars_usdt"],
            total_premium_months=purchase_stats["total_premium_months"],
            total_premium_usdt=purchase_stats["total_premium_usdt"],
            lang=lang,
        )

        # Показываем кнопку вывода реферального баланса если есть баланс
        has_ref_balance = db_user.referral_balance > Decimal("0")

        await safe_edit_message(
            callback.message,
            text=profile_text,
            reply_markup=get_profile_keyboard(lang, has_referral_balance=has_ref_balance),
        )

    await callback.answer()


@router.callback_query(F.data == ProfileCallback.REFERRALS)
async def callback_referrals(callback: CallbackQuery) -> None:
    """Показать реферальную систему."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        # Получаем язык пользователя
        lang = db_user.language_code if db_user else get_user_locale(user.language_code)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Получаем статистику
        ref_stats = await user_service.get_referral_stats(user.id)

        # Получаем динамические проценты
        ref_percents = await get_referral_percents()

        # Формируем реферальную ссылку
        bot_info = await callback.bot.get_me()
        referral_link = f"https://t.me/{bot_info.username}?start=ref_{db_user.referral_code}"

        referrals_text = t(
            "referrals.main_info",
            lang,
            balance=f"{ref_stats['referral_balance']:,.2f}",
            earnings=f"{ref_stats['total_earnings']:,.2f}",
            link=referral_link,
            percent1=int(ref_percents[1]),
            percent2=int(ref_percents[2]),
            percent3=int(ref_percents[3]),
        )

        await safe_edit_message(
            callback.message,
            text=referrals_text,
            reply_markup=get_referrals_keyboard(lang),
        )

    await callback.answer()


@router.callback_query(F.data == ProfileCallback.REFERRAL_WITHDRAW)
async def callback_referral_withdraw(callback: CallbackQuery) -> None:
    """Вывести реферальный баланс на основной USDT баланс."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        # Получаем язык пользователя
        lang = db_user.language_code if db_user else get_user_locale(user.language_code)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Выводим баланс
        success, amount, message = await user_service.withdraw_referral_balance(user.id)
        await session.commit()

        if success:
            await callback.answer(
                t("referrals.withdraw_success", lang, amount=f"{amount:,.2f}"),
                show_alert=True,
            )
            # Обновляем страницу рефералов
            await callback_referrals(callback)
        else:
            if message == "no_balance":
                await callback.answer(
                    t("referrals.withdraw_no_balance", lang),
                    show_alert=True,
                )
            else:
                await callback.answer(
                    t("common.error", lang),
                    show_alert=True,
                )


@router.callback_query(F.data == ProfileCallback.REFERRALS_LIST)
async def callback_referrals_list(callback: CallbackQuery) -> None:
    """Показать список рефералов по уровням."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        # Получаем язык пользователя
        lang = db_user.language_code if db_user else get_user_locale(user.language_code)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        ref_stats = await user_service.get_referral_stats(user.id)

        # Получаем динамические проценты
        ref_percents = await get_referral_percents()

        referrals_list_text = t(
            "referrals.list_info",
            lang,
            total=ref_stats["total_referrals"],
            level1=ref_stats["level_1"],
            level2=ref_stats["level_2"],
            level3=ref_stats["level_3"],
            earnings=f"{ref_stats['total_earnings']:,.2f}",
            percent1=int(ref_percents[1]),
            percent2=int(ref_percents[2]),
            percent3=int(ref_percents[3]),
        )

        await safe_edit_message(
            callback.message,
            text=referrals_list_text,
            reply_markup=get_back_to_referrals_keyboard(lang),
        )

    await callback.answer()


@router.callback_query(F.data == ProfileCallback.BACK)
async def callback_profile_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Назад к профилю."""
    await callback_profile(callback, state)


PAGE_SIZE = 5  # Количество заказов на странице


@router.callback_query(F.data == ProfileCallback.HISTORY)
async def callback_history(callback: CallbackQuery) -> None:
    """История покупок — первая страница."""
    await show_order_history(callback, page=0)


@router.callback_query(F.data.startswith(ProfileCallback.HISTORY_PAGE))
async def callback_history_page(callback: CallbackQuery) -> None:
    """История покупок — пагинация."""
    page_str = callback.data.replace(ProfileCallback.HISTORY_PAGE, "")
    try:
        page = int(page_str)
    except ValueError:
        page = 0

    await show_order_history(callback, page=page)


async def show_order_history(callback: CallbackQuery, page: int = 0) -> None:
    """Показать историю заказов с пагинацией."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        # Получаем язык пользователя
        lang = db_user.language_code if db_user else get_user_locale(user.language_code)

        # Получаем только заказы (без транзакций)
        orders, total_count = await user_service.get_user_orders(
            user_id=user.id,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )

        if total_count == 0:
            await safe_edit_message(
                callback.message,
                text=(
                    f"{t('history.title', lang)}\n\n"
                    f"{t('history.empty', lang)}"
                ),
                reply_markup=get_history_keyboard([], 0, 0, PAGE_SIZE, lang),
            )
        else:
            total_pages = (total_count + PAGE_SIZE - 1) // PAGE_SIZE
            await safe_edit_message(
                callback.message,
                text=(
                    f"{t('history.title', lang)}\n\n"
                    f"<blockquote>{t('history.total', lang, count=total_count)}\n"
                    f"{t('history.page', lang, current=page + 1, total=total_pages)}</blockquote>\n\n"
                    f"{t('history.click_for_details', lang)}"
                ),
                reply_markup=get_history_keyboard(orders, page, total_count, PAGE_SIZE, lang),
            )

    await callback.answer()


@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery) -> None:
    """Пустой callback (например, для номера страницы)."""
    await callback.answer()


@router.callback_query(F.data.startswith(ProfileCallback.ORDER_VIEW))
async def callback_order_view(callback: CallbackQuery) -> None:
    """Просмотр деталей заказа."""
    user = callback.from_user
    fallback_lang = get_user_locale(user.language_code)

    order_id_str = callback.data.replace(ProfileCallback.ORDER_VIEW, "")
    try:
        order_id = int(order_id_str)
    except ValueError:
        await callback.answer(t("history.errors.invalid_order_id", fallback_lang), show_alert=True)
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        order = await user_service.get_order_by_id(order_id, user.id)

        # Получаем язык пользователя
        lang = db_user.language_code if db_user else fallback_lang

        if not order:
            await callback.answer(t("history.errors.order_not_found", lang), show_alert=True)
            return

        # Формируем текст с деталями заказа
        status_name = t(f"history.statuses.{order.status}", lang)
        status_emoji = "✅" if order.status == "completed" else ("❌" if order.status == "failed" else "🕐")

        # Тип продукта (локализованный)
        product_display = t(f"history.product_display.{order.product_type}", lang)
        if order.product_type == "stars":
            quantity_display = f"{order.quantity} {t('history.units.stars', lang)}"
        else:
            quantity_display = f"{order.quantity} {t('history.units.months', lang)} 👑"

        # Способ оплаты (локализованный)
        if order.payment_provider == "balance":
            method_display = t("history.payment_display.balance", lang)
            if order.product_type == "stars":
                withdraw_display = f"{order.quantity} {t('history.units.stars', lang)}"
            else:
                withdraw_display = f"{order.quantity} {t('history.units.months_premium', lang)}"
        elif order.payment_provider in ("cryptobot", "ton"):
            method_display = t(f"history.payment_display.{order.payment_provider}", lang)
            withdraw_display = None
        else:
            method_display = t(f"history.payment_methods.{order.payment_provider}", lang)
            withdraw_display = None

        # Локализованные метки
        info_label = t("history.order.info", lang)
        type_label = t("history.order.type_label", lang)
        quantity_label = t("history.order.quantity", lang)
        recipient_label = t("history.order.recipient", lang)
        payment_label = t("history.order.payment", lang)
        method_label = t("history.order.method", lang)
        amount_label = t("history.order.amount", lang)
        withdraw_label = t("history.order.withdraw_from_balance", lang)
        status_section = t("history.order.status", lang)
        status_label = t("history.order.status_label", lang)
        created_label = t("history.order.created_at", lang)
        completed_label = t("history.order.completed_label", lang)
        error_label = t("history.order.error", lang)

        # Формируем текст
        order_text = t("history.order.title", lang, order_key=order.order_key) + "\n\n"

        # Секция: Информация (в цитате)
        order_text += f"<blockquote><b>{info_label}</b>\n"
        order_text += f"🏷️ {type_label}: <b>{product_display}</b>\n"
        order_text += f"📊 {quantity_label}: <b>{quantity_display}</b>\n"
        order_text += f"👤 {recipient_label}: <b>@{order.recipient_username}</b></blockquote>\n\n"

        # Секция: Оплата (в цитате)
        order_text += f"<blockquote><b>{payment_label}</b>\n"
        order_text += f"🏦 {method_label}: <b>{method_display}</b>\n"
        if withdraw_display:
            order_text += f"💎 {withdraw_label}: <b>{withdraw_display}</b></blockquote>\n\n"
        else:
            order_text += f"💰 {amount_label}: <b>{order.price_usdt:,.2f} USDT</b></blockquote>\n\n"

        # Секция: Статус (в цитате)
        order_text += f"<blockquote><b>{status_section}</b>\n"
        # Убираем эмодзи из названия статуса чтобы не дублировались
        status_name_clean = status_name.lstrip("✅❌🕐⏳🚫↩️ ")
        order_text += f"{status_emoji} {status_label}: <b>{status_name_clean}</b>\n"
        order_text += f"📥 {created_label}: <b>{format_datetime_msk(order.created_at, '%d.%m.%Y %H:%M:%S')}</b>\n"

        if order.completed_at and order.status == "completed":
            order_text += f"✅ {completed_label}: <b>{format_datetime_msk(order.completed_at, '%d.%m.%Y %H:%M:%S')}</b>\n"

        if order.error_message:
            order_text += f"⚠️ {error_label}: <b>{order.error_message}</b>"

        order_text += "</blockquote>"

        await safe_edit_message(
            callback.message,
            text=order_text,
            reply_markup=get_order_detail_keyboard(lang),
        )

    await callback.answer()


@router.callback_query(F.data.startswith(ProfileCallback.TX_VIEW))
async def callback_transaction_view(callback: CallbackQuery) -> None:
    """Просмотр деталей транзакции."""
    import json

    user = callback.from_user
    fallback_lang = get_user_locale(user.language_code)

    tx_id_str = callback.data.replace(ProfileCallback.TX_VIEW, "")
    try:
        tx_id = int(tx_id_str)
    except ValueError:
        await callback.answer(t("history.errors.invalid_tx_id", fallback_lang), show_alert=True)
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        tx = await user_service.get_transaction_by_id(tx_id, user.id)

        # Получаем язык пользователя
        lang = db_user.language_code if db_user else fallback_lang

        if not tx:
            await callback.answer(t("history.errors.tx_not_found", lang), show_alert=True)
            return

        # Формируем текст с деталями транзакции
        tx_emoji = TRANSACTION_EMOJIS.get(tx.type, "💰")
        tx_name = t(f"history.transaction_types.{tx.type}", lang)

        # Парсим extra_data если есть
        extra = {}
        if tx.extra_data:
            try:
                extra = json.loads(tx.extra_data)
            except json.JSONDecodeError:
                pass

        # Локализованные метки
        amount_label = t("history.tx_labels.amount", lang)
        bonus_label = t("history.tx_labels.bonus", lang)
        payment_label = t("history.tx_labels.payment", lang)
        method_label = t("history.tx_labels.method", lang)
        recipient_label = t("history.tx_labels.recipient", lang)
        details_label = t("history.tx_labels.details", lang)
        level_label = t("history.tx_labels.level", lang)
        percent_label = t("history.tx_labels.percent", lang)
        received_label = t("history.tx_labels.received", lang)
        activations_label = t("history.tx_labels.activations", lang)
        max_label = t("history.tx_labels.max", lang)
        date_label = t("history.tx_labels.date", lang)

        # 1. Заголовок
        tx_text = f"{tx_emoji} <b>{tx_name}</b>\n\n"

        # Отображение в зависимости от типа
        if tx.type == "deposit":
            tx_text += (
                f"<b>💰 {amount_label}</b>\n"
                f"└ {tx.amount_usdt:,.2f} USDT\n\n"
            )
            provider = extra.get("provider", "—")
            tx_text += f"<b>💳 {payment_label}</b>\n"
            tx_text += f"├ {method_label}: {provider}\n"
            if extra.get("payment_id"):
                tx_text += f"└ ID: <code>{extra['payment_id']}</code>\n"
            else:
                tx_text = tx_text.replace(f"├ {method_label}:", f"└ {method_label}:")

        elif tx.type == "withdrawal":
            tx_text += (
                f"<b>⭐ {amount_label}</b>\n"
                f"└ {tx.amount_stars:,.0f} Stars\n\n"
            )
            if extra.get("recipient"):
                tx_text += (
                    f"<b>👤 {recipient_label}</b>\n"
                    f"└ @{extra['recipient']}\n"
                )

        elif tx.type == "referral":
            tx_text += (
                f"<b>⭐ {bonus_label}</b>\n"
                f"└ {tx.amount_stars:,.0f} Stars\n\n"
            )
            if extra.get("level") or extra.get("percent"):
                tx_text += f"<b>📊 {details_label}</b>\n"
                if extra.get("level"):
                    tx_text += f"├ {level_label}: {extra['level']}\n"
                if extra.get("percent"):
                    tx_text += f"└ {percent_label}: {extra['percent']}%\n"

        elif tx.type == "promo":
            if extra.get("code"):
                promo_label = t("history.tx_labels.promo_code", lang)
                tx_text += (
                    f"<b>🎁 {promo_label}</b>\n"
                    f"└ <code>{extra['code']}</code>\n\n"
                )
            tx_text += f"<b>💰 {bonus_label}</b>\n"
            if tx.amount_stars > 0:
                tx_text += f"└ {tx.amount_stars:,.0f} Stars\n"
            elif tx.amount_usdt > 0:
                tx_text += f"└ {tx.amount_usdt:,.2f} USDT\n"

        elif tx.type == "check_created":
            check_label = t("history.tx_labels.check", lang)
            if extra.get("code"):
                tx_text += (
                    f"<b>🎟 {check_label}</b>\n"
                    f"└ <code>{extra['code']}</code>\n\n"
                )
            tx_text += (
                f"<b>⭐ {amount_label}</b>\n"
                f"└ {tx.amount_stars:,.0f} Stars\n\n"
            )
            if extra.get("max_activations"):
                tx_text += (
                    f"<b>🔢 {activations_label}</b>\n"
                    f"└ {max_label}: {extra['max_activations']}\n"
                )

        elif tx.type == "check_activated":
            check_label = t("history.tx_labels.check", lang)
            if extra.get("code"):
                tx_text += (
                    f"<b>🎟 {check_label}</b>\n"
                    f"└ <code>{extra['code']}</code>\n\n"
                )
            tx_text += (
                f"<b>⭐ {received_label}</b>\n"
                f"└ {tx.amount_stars:,.0f} Stars\n"
            )

        else:
            if tx.amount_usdt > 0:
                tx_text += f"<b>💰 {amount_label}</b>\n└ {tx.amount_usdt:,.2f} USDT\n"
            if tx.amount_stars > 0:
                tx_text += f"<b>⭐ {amount_label}</b>\n└ {tx.amount_stars:,.0f} Stars\n"

        # Дата
        tx_text += f"\n<b>📅 {date_label}:</b> {format_datetime_msk(tx.created_at)} (MSK)"

        if tx.description:
            tx_text += f"\n\n📝 {tx.description}"

        await safe_edit_message(
            callback.message,
            text=tx_text,
            reply_markup=get_transaction_detail_keyboard(lang),
        )

    await callback.answer()


@router.callback_query(F.data == ProfileCallback.LANGUAGE)
async def callback_language(callback: CallbackQuery) -> None:
    """Показать выбор языка."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        current_lang = db_user.language_code if db_user else get_user_locale(user.language_code)

        await safe_edit_message(
            callback.message,
            text=(
                f"{t('language.title', current_lang)}\n\n"
                f"{t('language.description', current_lang)}"
            ),
            reply_markup=get_language_keyboard(current_lang),
        )

    await callback.answer()


@router.callback_query(F.data == ProfileCallback.LANG_RU)
async def callback_set_lang_ru(callback: CallbackQuery, state: FSMContext) -> None:
    """Установить русский язык."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        await user_service.update_language(user.id, "ru")
        await session.commit()

    # Остаёмся в меню выбора языка, но на новом языке
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('language.title', 'ru')}\n\n"
            f"{t('language.description', 'ru')}"
        ),
        reply_markup=get_language_keyboard("ru"),
    )
    await callback.answer()


@router.callback_query(F.data == ProfileCallback.LANG_EN)
async def callback_set_lang_en(callback: CallbackQuery, state: FSMContext) -> None:
    """Установить английский язык."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        await user_service.update_language(user.id, "en")
        await session.commit()

    # Остаёмся в меню выбора языка, но на новом языке
    await safe_edit_message(
        callback.message,
        text=(
            f"{t('language.title', 'en')}\n\n"
            f"{t('language.description', 'en')}"
        ),
        reply_markup=get_language_keyboard("en"),
    )
    await callback.answer()


@router.callback_query(F.data == ProfileCallback.PROMO)
async def callback_promo(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать ввод промокода."""
    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        lang = db_user.language_code if db_user else get_user_locale(user.language_code)

    await state.set_state(PromoStates.waiting_code)
    # Сохраняем ID сообщения бота и язык для последующего редактирования
    await state.update_data(promo_message_id=callback.message.message_id, lang=lang)

    await safe_edit_message(
        callback.message,
        text=(
            f"{t('promo.title', lang)}\n\n"
            f"{t('promo.enter_code', lang)}"
        ),
        reply_markup=get_promo_back_keyboard(lang),
    )
    await callback.answer()


@router.message(PromoStates.waiting_code)
async def process_promo_code(message: Message, state: FSMContext) -> None:
    """Обработать введённый промокод."""
    # Проверяем что пользователь отправил текст
    if not message.text:
        # Удаляем нетекстовое сообщение
        try:
            await message.delete()
        except Exception:
            pass
        return

    code = message.text.strip().upper()

    # Получаем ID сообщения бота и язык
    data = await state.get_data()
    bot_message_id = data.get("promo_message_id")
    lang = data.get("lang", "ru")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    if not code or len(code) < 3:
        # Редактируем сообщение бота
        if bot_message_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=bot_message_id,
                    text=t("promo.invalid", lang),
                    reply_markup=get_promo_back_keyboard(lang),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    # Активируем промокод
    async with async_session_factory() as session:
        user_service = UserService(session)
        success, message_key, bonus = await user_service.activate_promo_code(
            user_id=message.from_user.id,
            code=code,
        )
        await session.commit()

    # Формируем текст ответа
    if success:
        # Очищаем состояние только при успехе
        await state.clear()
        if message_key == "success_stars":
            response_text = t("promo.success_stars", lang, code=code, amount=f"{bonus:,}")
        elif message_key == "success_usdt":
            response_text = t("promo.success_usdt", lang, code=code, amount=f"{bonus:.2f}")
        elif message_key == "success_premium":
            response_text = t("promo.success_premium", lang, code=code, amount=bonus)
        else:
            response_text = t("promo.success", lang, code=code)
    else:
        # Маппинг ошибок на ключи локализации
        error_keys = {
            "not_found": "promo.not_found",
            "not_active": "promo.not_active",
            "expired": "promo.expired",
            "limit_reached": "promo.limit_reached",
            "already_used": "promo.already_used",
            "no_bonus": "promo.no_bonus",
            "user_not_found": "common.user_not_found",
        }
        response_text = t(error_keys.get(message_key, "promo.not_found"), lang, code=code)

    # Редактируем сообщение бота
    if bot_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text=response_text,
                reply_markup=get_promo_back_keyboard(lang),
                parse_mode="HTML",
            )
        except Exception:
            pass
