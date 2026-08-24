"""
Handlers для админ-панели.

Защита: пользователь должен иметь is_admin=True в базе данных.
"""
import logging
import re
from datetime import datetime, timedelta, date
from decimal import Decimal
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, and_, desc, case

from src.bot.handlers.admin_utils import (
    check_admin,
    check_admin_message,
    to_moscow_time,
    format_percent as _format_percent,
    MOSCOW_TZ,
)
from src.bot.keyboards.admin import (
    AdminCallback,
    get_admin_menu_keyboard,
    get_stats_menu_keyboard,
    get_stats_section_keyboard,
    get_logs_menu_keyboard,
    get_logs_topics_keyboard,
    get_logs_events_keyboard,
    get_logs_cancel_keyboard,
    get_promo_menu_keyboard,
    get_promo_type_keyboard,
    get_promo_bonus_keyboard,
    get_promo_premium_keyboard,
    get_promo_limit_keyboard,
    get_promo_expires_keyboard,
    get_promo_confirm_keyboard,
    get_promo_code_keyboard,
    get_promo_back_keyboard,
    get_promo_list_keyboard,
    get_promo_detail_keyboard,
    get_promo_delete_confirm_keyboard,
    get_promo_empty_list_keyboard,
    get_settings_menu_keyboard,
    get_settings_stars_keyboard,
    get_settings_premium_keyboard,
    get_settings_premium_months_keyboard,
    get_settings_payments_keyboard,
    get_settings_cryptobot_keyboard,
    get_settings_ton_keyboard,
    get_settings_lava_keyboard,
    get_settings_platega_keyboard,
    get_settings_cost_keyboard,
    get_settings_referral_keyboard,
    get_settings_support_keyboard,
    get_settings_media_keyboard,
    get_settings_subscription_keyboard,
    get_settings_cancel_keyboard,
)
from src.bot.keyboards.calendar import get_calendar_keyboard
from src.db.models import (
    Order, Transaction, User, ReferralEarning,
    Check, CheckActivation, PromoCode, PromoUse, BalanceLedger, BotChannel
)
from src.db.session import async_session_factory
from src.services.user_service import UserService
from src.services.telegram_logger import tg_logger
from src.services.log_settings_service import LogSettingsService, invalidate_log_settings_cache
from src.services.bot_settings_service import BotSettingsService, invalidate_bot_settings_cache
from src.bot.menu_media import MENU_MEDIA_ITEMS, get_menu_media

logger = logging.getLogger(__name__)

# Алиасы для обратной совместимости
to_moscow = to_moscow_time

router = Router(name="admin")


async def _get_subscription_channels(session) -> list[BotChannel]:
    result = await session.execute(
        select(BotChannel)
        .where(BotChannel.is_active == True)
        .order_by(BotChannel.channel_title)
    )
    return list(result.scalars().all())


async def _make_subscription_url(bot, channel: BotChannel) -> str:
    invite_link = await bot.create_chat_invite_link(
        chat_id=channel.channel_id,
        name="Обязательная подписка",
    )
    return invite_link.invite_link


class AdminStates(StatesGroup):
    """Состояния админ-панели."""
    waiting_log_group_id = State()  # Ожидание ID группы для логов
    waiting_topic_id = State()  # Ожидание ID топика
    stats_selecting_from = State()  # Выбор даты "с"
    stats_selecting_to = State()  # Выбор даты "по"
    # Промокоды
    promo_waiting_code = State()  # Ожидание кода промокода
    promo_waiting_bonus = State()  # Ожидание значения бонуса
    promo_waiting_limit = State()  # Ожидание лимита использований
    promo_waiting_expires = State()  # Ожидание срока действия
    # Настройки
    settings_waiting_value = State()  # Ожидание нового значения настройки
    settings_waiting_media_photo = State()


def get_period_start(period: str) -> datetime | None:
    """Получить начало периода."""
    now = datetime.utcnow()
    if period == "24h":
        return now - timedelta(hours=24)
    elif period == "7d":
        return now - timedelta(days=7)
    elif period == "30d":
        return now - timedelta(days=30)
    return None


def get_period_name(period: str, date_from: str = None, date_to: str = None) -> str:
    """Название периода для отображения."""
    names = {
        "24h": "за 24 часа",
        "7d": "за 7 дней",
        "30d": "за 30 дней",
        "all": "за всё время",
    }
    if period == "custom" and date_from and date_to:
        return f"с {date_from} по {date_to}"
    return names.get(period, "")


def parse_custom_dates(date_from: str, date_to: str) -> tuple[datetime | None, datetime | None]:
    """Парсинг кастомных дат."""
    try:
        start = datetime.strptime(date_from, "%d.%m.%Y")
        end = datetime.strptime(date_to, "%d.%m.%Y").replace(hour=23, minute=59, second=59)
        return start, end
    except (ValueError, TypeError):
        return None, None


# ==================== ВХОД В АДМИНКУ ====================

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    """Команда /admin - вход в админ-панель."""
    user = message.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await message.answer("Ошибка. Используйте /start")
            return

        if not db_user.is_admin:
            logger.warning(f"Admin login failed for user {user.id}: not admin in DB")
            await message.answer("У вас нет прав администратора.")
            return

    logger.info(f"Admin login successful for user {user.id}")
    await state.clear()

    # Логируем вход в админку
    await tg_logger.log_admin_login(
        admin_id=user.id,
        admin_username=user.username,
    )

    await message.answer(
        text="🔐 <b>Админ-панель</b>\n\nВыберите раздел:",
        reply_markup=get_admin_menu_keyboard(),
        parse_mode="HTML",
    )


# ==================== ГЛАВНОЕ МЕНЮ СТАТИСТИКИ ====================

@router.callback_query(F.data == AdminCallback.STATS)
async def callback_stats_menu(callback: CallbackQuery) -> None:
    """Главное меню статистики с краткой сводкой."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        today_users = (await session.execute(
            select(func.count(User.id)).where(User.created_at >= datetime.utcnow() - timedelta(hours=24))
        )).scalar() or 0

        total_orders = (await session.execute(select(func.count(Order.id)))).scalar() or 0
        completed_orders = (await session.execute(
            select(func.count(Order.id)).where(Order.status == "completed")
        )).scalar() or 0

        total_revenue = (await session.execute(
            select(func.sum(Order.price_usdt)).where(Order.status == "completed")
        )).scalar() or Decimal("0")

        today_revenue = (await session.execute(
            select(func.sum(Order.price_usdt)).where(and_(
                Order.status == "completed",
                Order.created_at >= datetime.utcnow() - timedelta(hours=24)
            ))
        )).scalar() or Decimal("0")

    text = (
        "📊 <b>Статистика — Сводка</b>\n\n"
        f"👥 Пользователей: <b>{total_users:,}</b> (+{today_users} сегодня)\n"
        f"📦 Заказов: <b>{total_orders:,}</b> (✅ {completed_orders:,})\n"
        f"💰 Выручка: <b>${total_revenue:,.2f}</b> (+${today_revenue:,.2f} сегодня)\n\n"
        "Выберите раздел для подробностей:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.STATS_BACK)
async def callback_stats_back(callback: CallbackQuery) -> None:
    """Назад к разделам статистики."""
    await callback_stats_menu(callback)


# ==================== СТАТИСТИКА: ПОЛЬЗОВАТЕЛИ ====================

async def get_users_stats(
    period: str,
    date_from: str = None,
    date_to: str = None,
) -> str:
    """Статистика по пользователям."""
    if period == "custom" and date_from and date_to:
        period_start, period_end = parse_custom_dates(date_from, date_to)
    else:
        period_start = get_period_start(period)
        period_end = None
    period_name = get_period_name(period, date_from, date_to)

    async with async_session_factory() as session:
        # Всего пользователей
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0

        # Формируем фильтр по периоду
        def make_date_filter(field):
            if period_start and period_end:
                return and_(field >= period_start, field <= period_end)
            elif period_start:
                return field >= period_start
            return True

        # Новые за период
        if period_start:
            new_users = (await session.execute(
                select(func.count(User.id)).where(make_date_filter(User.created_at))
            )).scalar() or 0
        else:
            new_users = total_users

        # Активные (делали заказы за период)
        if period_start:
            active_users = (await session.execute(
                select(func.count(func.distinct(Order.user_id))).where(make_date_filter(Order.created_at))
            )).scalar() or 0
        else:
            active_users = (await session.execute(
                select(func.count(func.distinct(Order.user_id)))
            )).scalar() or 0

        # Конверсия (сделали хотя бы 1 успешный заказ)
        buyers = (await session.execute(
            select(func.count(func.distinct(Order.user_id))).where(Order.status == "completed")
        )).scalar() or 0
        conversion = (buyers / total_users * 100) if total_users > 0 else 0

        # Retention: пользователи с более чем 1 заказом (вернувшиеся)
        repeat_buyers_subq = (
            select(Order.user_id)
            .where(Order.status == "completed")
            .group_by(Order.user_id)
            .having(func.count(Order.id) > 1)
        )
        repeat_buyers = (await session.execute(
            select(func.count()).select_from(repeat_buyers_subq.subquery())
        )).scalar() or 0
        retention_rate = (repeat_buyers / buyers * 100) if buyers > 0 else 0

        # Забаненные
        banned_users = (await session.execute(
            select(func.count(User.id)).where(User.is_banned == True)
        )).scalar() or 0

        # Админы
        admin_users = (await session.execute(
            select(func.count(User.id)).where(User.is_admin == True)
        )).scalar() or 0

        # Пользователи с рефералом и без
        with_referral = (await session.execute(
            select(func.count(User.id)).where(User.referrer_code.isnot(None))
        )).scalar() or 0
        without_referral = total_users - with_referral
        referral_percent = (with_referral / total_users * 100) if total_users > 0 else 0

        # Пользователи с балансом > 0
        users_with_balance = (await session.execute(
            select(func.count(User.id)).where(User.balance_usdt > 0)
        )).scalar() or 0
        balance_percent = (users_with_balance / total_users * 100) if total_users > 0 else 0

        # Средний баланс
        avg_balance_usdt = (await session.execute(
            select(func.avg(User.balance_usdt))
        )).scalar() or Decimal("0")

    text = f"👥 <b>Пользователи {period_name}</b>\n\n"

    text += "<blockquote>📊 <b>Общее</b>\n"
    text += f"👤 Всего: <b>{total_users:,}</b>\n"
    text += f"🆕 Новых: <b>{new_users:,}</b>\n"
    text += f"✅ Активных: <b>{active_users:,}</b>\n"
    text += f"🚫 Забанено: <b>{banned_users:,}</b>\n"
    text += f"👑 Админов: <b>{admin_users:,}</b></blockquote>\n\n"

    text += "<blockquote>💰 <b>Покупки</b>\n"
    text += f"🛒 Покупателей: <b>{buyers:,}</b>\n"
    text += f"📈 Конверсия: <b>{conversion:.1f}%</b>\n"
    text += f"🔄 Вернулись: <b>{repeat_buyers:,}</b>\n"
    text += f"📊 Возвращаемость: <b>{retention_rate:.1f}%</b></blockquote>\n\n"

    text += "<blockquote>💵 <b>Баланс</b>\n"
    text += f"💳 С балансом: <b>{users_with_balance:,}</b> ({balance_percent:.1f}%)\n"
    text += f"📉 Средний: <b>{avg_balance_usdt:,.2f} USDT</b></blockquote>\n\n"

    text += "<blockquote>🔗 <b>Источники</b>\n"
    text += f"👥 По реф. ссылке: <b>{with_referral:,}</b> ({referral_percent:.1f}%)\n"
    text += f"🔍 Напрямую: <b>{without_referral:,}</b></blockquote>"

    return text


@router.callback_query(F.data == AdminCallback.STATS_USERS)
async def callback_stats_users(callback: CallbackQuery) -> None:
    """Статистика пользователей."""
    if not await _check_admin(callback):
        return

    text = await get_users_stats("all")
    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_section_keyboard("users", "all"),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== СТАТИСТИКА: ЗАКАЗЫ ====================

async def get_orders_stats(
    period: str,
    date_from: str = None,
    date_to: str = None,
) -> str:
    """Статистика по заказам."""
    if period == "custom" and date_from and date_to:
        period_start, period_end = parse_custom_dates(date_from, date_to)
    else:
        period_start = get_period_start(period)
        period_end = None
    period_name = get_period_name(period, date_from, date_to)

    async with async_session_factory() as session:
        if period_start and period_end:
            order_filter = and_(Order.created_at >= period_start, Order.created_at <= period_end)
        elif period_start:
            order_filter = Order.created_at >= period_start
        else:
            order_filter = True

        # Всего заказов
        total_orders = (await session.execute(
            select(func.count(Order.id)).where(order_filter)
        )).scalar() or 0

        # Уникальных покупателей
        unique_buyers = (await session.execute(
            select(func.count(func.distinct(Order.user_id))).where(order_filter)
        )).scalar() or 0

        # Среднее количество заказов на покупателя
        avg_orders_per_user = (total_orders / unique_buyers) if unique_buyers > 0 else 0

        # По товарам
        stars_orders = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.product_type == "stars"))
        )).scalar() or 0
        stars_sold = (await session.execute(
            select(func.sum(Order.quantity)).where(and_(
                order_filter, Order.product_type == "stars", Order.status == "completed"
            ))
        )).scalar() or 0

        premium_orders = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.product_type == "premium"))
        )).scalar() or 0
        premium_sold = (await session.execute(
            select(func.sum(Order.quantity)).where(and_(
                order_filter, Order.product_type == "premium", Order.status == "completed"
            ))
        )).scalar() or 0

        # По статусам
        completed = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.status == "completed"))
        )).scalar() or 0
        cancelled = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.status == "cancelled"))
        )).scalar() or 0
        processing = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.status == "processing"))
        )).scalar() or 0
        pending = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.status == "pending"))
        )).scalar() or 0
        failed = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.status == "failed"))
        )).scalar() or 0

        # По способам оплаты
        payment_methods = (await session.execute(
            select(Order.payment_provider, func.count(Order.id).label("cnt"))
            .where(order_filter)
            .group_by(Order.payment_provider)
            .order_by(desc("cnt"))
        )).fetchall()

    text = f"📦 <b>Заказы {period_name}</b>\n\n"

    text += "<blockquote>📊 <b>Общее</b>\n"
    text += f"📋 Всего: <b>{total_orders:,}</b>\n"
    text += f"👥 Покупателей: <b>{unique_buyers:,}</b>\n"
    text += f"📈 На покупателя: <b>{avg_orders_per_user:.1f}</b></blockquote>\n\n"

    text += "<blockquote>🛒 <b>Товары</b>\n"
    text += f"⭐ Stars: <b>{stars_orders:,}</b> заказов, <b>{stars_sold:,}</b> продано\n"
    text += f"👑 Premium: <b>{premium_orders:,}</b> заказов, <b>{premium_sold:,}</b> мес.</blockquote>\n\n"

    text += "<blockquote>📋 <b>Статусы</b>\n"
    text += f"✅ Успешные: <b>{completed:,}</b>\n"
    text += f"❌ Ошибки: <b>{failed:,}</b>\n"
    text += f"🚫 Отменённые: <b>{cancelled:,}</b>\n"
    text += f"⏳ В процессе: <b>{processing:,}</b>\n"
    text += f"🕐 Ожидают: <b>{pending:,}</b></blockquote>\n\n"

    text += "<blockquote>💳 <b>Оплата</b>\n"
    payment_names = {
        "balance": "💰 Баланс",
        "ton": "💎 TON",
        "usdt": "💵 USDT",
        "cryptobot": "🤖 CryptoBot",
        "platega": "🏦 СБП / Platega",
        "lava": "🌋 Lava / СБП",
    }
    for i, (provider, cnt) in enumerate(payment_methods):
        name = payment_names.get(provider, provider)
        text += f"{name}: <b>{cnt:,}</b>\n"
    text = text.rstrip("\n") + "</blockquote>"

    return text


@router.callback_query(F.data == AdminCallback.STATS_ORDERS)
async def callback_stats_orders(callback: CallbackQuery) -> None:
    """Статистика заказов."""
    if not await _check_admin(callback):
        return

    text = await get_orders_stats("all")
    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_section_keyboard("orders", "all"),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== СТАТИСТИКА: ФИНАНСЫ ====================

async def get_finance_stats(
    period: str,
    date_from: str = None,
    date_to: str = None,
) -> str:
    """Статистика по финансам."""
    from src.services.bot_settings_service import get_bot_settings

    # Получаем настройки
    settings = await get_bot_settings()
    STAR_PRICE_USDT = Decimal(settings.get("star_price_usdt", "0.02"))
    STAR_COST_USDT = Decimal(settings.get("star_cost_usdt", "0.015"))
    PREMIUM_COSTS = {
        3: Decimal(settings.get("premium_cost_3m", "6.00")),
        6: Decimal(settings.get("premium_cost_6m", "10.00")),
        12: Decimal(settings.get("premium_cost_12m", "18.00")),
    }

    if period == "custom" and date_from and date_to:
        period_start, period_end = parse_custom_dates(date_from, date_to)
    else:
        period_start = get_period_start(period)
        period_end = None
    period_name = get_period_name(period, date_from, date_to)

    async with async_session_factory() as session:
        # Формируем фильтры
        if period_start and period_end:
            date_filter = lambda field: and_(field >= period_start, field <= period_end)
        elif period_start:
            date_filter = lambda field: field >= period_start
        else:
            date_filter = lambda field: True

        order_filter = and_(
            date_filter(Order.created_at),
            Order.status == "completed"
        )
        ledger_filter = date_filter(BalanceLedger.created_at)

        # Валовой доход (выручка)
        gross_revenue = (await session.execute(
            select(func.sum(Order.price_usdt)).where(order_filter)
        )).scalar() or Decimal("0")

        # Доход по товарам
        stars_revenue = (await session.execute(
            select(func.sum(Order.price_usdt)).where(and_(order_filter, Order.product_type == "stars"))
        )).scalar() or Decimal("0")
        premium_revenue = (await session.execute(
            select(func.sum(Order.price_usdt)).where(and_(order_filter, Order.product_type == "premium"))
        )).scalar() or Decimal("0")

        # Количество проданных товаров для расчёта себестоимости
        stars_sold = (await session.execute(
            select(func.sum(Order.quantity)).where(and_(order_filter, Order.product_type == "stars"))
        )).scalar() or 0

        # Premium по месяцам
        premium_3m_sold = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.product_type == "premium", Order.quantity == 3))
        )).scalar() or 0
        premium_6m_sold = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.product_type == "premium", Order.quantity == 6))
        )).scalar() or 0
        premium_12m_sold = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.product_type == "premium", Order.quantity == 12))
        )).scalar() or 0

        # Себестоимость
        stars_cost = Decimal(stars_sold) * STAR_COST_USDT
        premium_cost = (
            premium_3m_sold * PREMIUM_COSTS[3] +
            premium_6m_sold * PREMIUM_COSTS[6] +
            premium_12m_sold * PREMIUM_COSTS[12]
        )
        total_cost = stars_cost + premium_cost

        # Реферальные выплаты (в Stars -> USDT)
        referral_filter = date_filter(ReferralEarning.created_at)
        referral_payouts_stars = (await session.execute(
            select(func.sum(ReferralEarning.amount_stars)).where(referral_filter)
        )).scalar() or Decimal("0")
        referral_payouts_usdt = referral_payouts_stars * STAR_PRICE_USDT

        # Чистая прибыль = Выручка - Себестоимость - Реферальные выплаты
        net_profit = gross_revenue - total_cost - referral_payouts_usdt

        # Конверсия: пользователи с покупками / всего пользователей
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        paying_users = (await session.execute(
            select(func.count(func.distinct(Order.user_id))).where(Order.status == "completed")
        )).scalar() or 0
        conversion_rate = (paying_users / total_users * 100) if total_users > 0 else 0

        # LTV (средний доход на платящего пользователя)
        ltv = (gross_revenue / paying_users) if paying_users > 0 else Decimal("0")

        # Балансы пользователей
        total_balance_usdt = (await session.execute(
            select(func.sum(User.balance_usdt))
        )).scalar() or Decimal("0")
        total_balance_stars = (await session.execute(
            select(func.sum(User.balance_stars))
        )).scalar() or Decimal("0")
        total_referral_balance = (await session.execute(
            select(func.sum(User.referral_balance))
        )).scalar() or Decimal("0")

        # Средний чек
        completed_count = (await session.execute(
            select(func.count(Order.id)).where(order_filter)
        )).scalar() or 0
        avg_check = (gross_revenue / completed_count) if completed_count > 0 else Decimal("0")

    text = f"💰 <b>Финансы {period_name}</b>\n\n"

    text += "<blockquote>📈 <b>Доходы</b>\n"
    text += f"💵 Выручка: <b>{gross_revenue:,.2f} USDT</b>\n"
    text += f"⭐ Stars: <b>{stars_revenue:,.2f} USDT</b>\n"
    text += f"👑 Premium: <b>{premium_revenue:,.2f} USDT</b></blockquote>\n\n"

    text += "<blockquote>📉 <b>Расходы</b>\n"
    text += f"🏭 Себестоимость: <b>-{total_cost:,.2f} USDT</b>\n"
    text += f"👥 Реф. выплаты: <b>-{referral_payouts_usdt:,.2f} USDT</b></blockquote>\n\n"

    text += "<blockquote>💎 <b>Прибыль</b>\n"
    text += f"💰 Чистая: <b>{net_profit:,.2f} USDT</b></blockquote>\n\n"

    text += "<blockquote>📊 <b>Показатели</b>\n"
    text += f"👤 Пользователей: <b>{total_users:,}</b>\n"
    text += f"💳 Платящих: <b>{paying_users:,}</b>\n"
    text += f"📈 Конверсия: <b>{conversion_rate:.1f}%</b>\n"
    text += f"🧾 Средний чек: <b>{avg_check:,.2f} USDT</b>\n"
    text += f"💰 Доход на клиента: <b>{ltv:,.2f} USDT</b></blockquote>\n\n"

    text += "<blockquote>💼 <b>Балансы</b>\n"
    text += f"💵 USDT: <b>{total_balance_usdt:,.2f}</b>\n"
    text += f"⭐ Stars: <b>{total_balance_stars:,.0f}</b>\n"
    text += f"👥 Реферальный: <b>{total_referral_balance:,.2f} USDT</b></blockquote>"

    return text


@router.callback_query(F.data == AdminCallback.STATS_FINANCE)
async def callback_stats_finance(callback: CallbackQuery) -> None:
    """Статистика финансов."""
    if not await _check_admin(callback):
        return

    text = await get_finance_stats("all")
    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_section_keyboard("finance", "all"),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== СТАТИСТИКА: РЕФЕРАЛЫ ====================

async def get_referrals_stats(
    period: str,
    date_from: str = None,
    date_to: str = None,
) -> str:
    """Статистика по рефералам."""
    STAR_PRICE_USDT = Decimal("0.02")  # Курс конвертации звёзд в USDT

    if period == "custom" and date_from and date_to:
        period_start, period_end = parse_custom_dates(date_from, date_to)
    else:
        period_start = get_period_start(period)
        period_end = None
    period_name = get_period_name(period, date_from, date_to)

    async with async_session_factory() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0

        # Пользователи с рефералами и без
        with_referral = (await session.execute(
            select(func.count(User.id)).where(User.referrer_code.isnot(None))
        )).scalar() or 0
        without_referral = total_users - with_referral
        referral_percent = (with_referral / total_users * 100) if total_users > 0 else 0

        # Фильтр для выплат
        if period_start and period_end:
            earning_filter = and_(
                ReferralEarning.created_at >= period_start,
                ReferralEarning.created_at <= period_end
            )
        elif period_start:
            earning_filter = ReferralEarning.created_at >= period_start
        else:
            earning_filter = True

        # По уровням: пользователи и доход
        level1_users = (await session.execute(
            select(func.count(func.distinct(ReferralEarning.referee_id))).where(and_(
                earning_filter, ReferralEarning.level == 1
            ))
        )).scalar() or 0
        level1_stars = (await session.execute(
            select(func.sum(ReferralEarning.amount_stars)).where(and_(
                earning_filter, ReferralEarning.level == 1
            ))
        )).scalar() or Decimal("0")
        level1_usdt = level1_stars * STAR_PRICE_USDT

        level2_users = (await session.execute(
            select(func.count(func.distinct(ReferralEarning.referee_id))).where(and_(
                earning_filter, ReferralEarning.level == 2
            ))
        )).scalar() or 0
        level2_stars = (await session.execute(
            select(func.sum(ReferralEarning.amount_stars)).where(and_(
                earning_filter, ReferralEarning.level == 2
            ))
        )).scalar() or Decimal("0")
        level2_usdt = level2_stars * STAR_PRICE_USDT

        level3_users = (await session.execute(
            select(func.count(func.distinct(ReferralEarning.referee_id))).where(and_(
                earning_filter, ReferralEarning.level == 3
            ))
        )).scalar() or 0
        level3_stars = (await session.execute(
            select(func.sum(ReferralEarning.amount_stars)).where(and_(
                earning_filter, ReferralEarning.level == 3
            ))
        )).scalar() or Decimal("0")
        level3_usdt = level3_stars * STAR_PRICE_USDT

        # Всего выплачено
        total_payouts_stars = (await session.execute(
            select(func.sum(ReferralEarning.amount_stars)).where(earning_filter)
        )).scalar() or Decimal("0")
        total_payouts_usdt = total_payouts_stars * STAR_PRICE_USDT

        # Средний доход реферера
        unique_referrers = (await session.execute(
            select(func.count(func.distinct(ReferralEarning.referrer_id))).where(earning_filter)
        )).scalar() or 0
        avg_referrer_usdt = (total_payouts_usdt / unique_referrers) if unique_referrers > 0 else Decimal("0")

    text = f"👥 <b>Рефералы {period_name}</b>\n\n"

    text += "<blockquote>📊 <b>Общее</b>\n"
    text += f"✅ С рефералами: <b>{with_referral:,}</b> ({referral_percent:.1f}%)\n"
    text += f"➖ Без рефералов: <b>{without_referral:,}</b></blockquote>\n\n"

    text += "<blockquote>📶 <b>Уровни</b>\n"
    text += f"1️⃣ Уровень: <b>{level1_users:,}</b> чел., <b>{level1_usdt:,.2f} USDT</b>\n"
    text += f"2️⃣ Уровень: <b>{level2_users:,}</b> чел., <b>{level2_usdt:,.2f} USDT</b>\n"
    text += f"3️⃣ Уровень: <b>{level3_users:,}</b> чел., <b>{level3_usdt:,.2f} USDT</b></blockquote>\n\n"

    text += "<blockquote>💰 <b>Выплаты</b>\n"
    text += f"💵 Всего: <b>{total_payouts_usdt:,.2f} USDT</b>\n"
    text += f"📊 Средняя: <b>{avg_referrer_usdt:,.2f} USDT</b></blockquote>"

    return text


@router.callback_query(F.data == AdminCallback.STATS_REFERRALS)
async def callback_stats_referrals(callback: CallbackQuery) -> None:
    """Статистика рефералов."""
    if not await _check_admin(callback):
        return

    text = await get_referrals_stats("all")
    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_section_keyboard("referrals", "all"),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== СТАТИСТИКА: ЧЕКИ ====================

async def get_checks_stats(
    period: str,
    date_from: str = None,
    date_to: str = None,
) -> str:
    """Статистика по чекам."""
    if period == "custom" and date_from and date_to:
        period_start, period_end = parse_custom_dates(date_from, date_to)
    else:
        period_start = get_period_start(period)
        period_end = None
    period_name = get_period_name(period, date_from, date_to)

    async with async_session_factory() as session:
        if period_start and period_end:
            check_filter = and_(Check.created_at >= period_start, Check.created_at <= period_end)
            activation_filter = and_(CheckActivation.created_at >= period_start, CheckActivation.created_at <= period_end)
        elif period_start:
            check_filter = Check.created_at >= period_start
            activation_filter = CheckActivation.created_at >= period_start
        else:
            check_filter = True
            activation_filter = True

        # Общая статистика
        total_checks = (await session.execute(
            select(func.count(Check.id)).where(check_filter)
        )).scalar() or 0

        total_activations = (await session.execute(
            select(func.count(CheckActivation.id)).where(activation_filter)
        )).scalar() or 0

        checks_with_activations = (await session.execute(
            select(func.count(func.distinct(CheckActivation.check_id))).where(activation_filter)
        )).scalar() or 0

        activation_rate = (checks_with_activations / total_checks * 100) if total_checks > 0 else 0

        # Stars чеки
        stars_checks = (await session.execute(
            select(func.count(Check.id)).where(and_(check_filter, Check.amount_premium_months == 0))
        )).scalar() or 0
        stars_issued = (await session.execute(
            select(func.sum(CheckActivation.amount_received))
            .join(Check)
            .where(and_(activation_filter, Check.amount_premium_months == 0))
        )).scalar() or Decimal("0")

        # Premium чеки
        premium_checks = (await session.execute(
            select(func.count(Check.id)).where(and_(check_filter, Check.amount_premium_months > 0))
        )).scalar() or 0
        premium_activations = (await session.execute(
            select(func.count(CheckActivation.id))
            .join(Check)
            .where(and_(activation_filter, Check.amount_premium_months > 0))
        )).scalar() or 0

    text = f"🎫 <b>Чеки {period_name}</b>\n\n"

    text += "<blockquote>📊 <b>Общее</b>\n"
    text += f"📝 Создано: <b>{total_checks:,}</b>\n"
    text += f"✅ Активировано: <b>{checks_with_activations:,}</b>\n"
    text += f"🔄 Активаций: <b>{total_activations:,}</b>\n"
    text += f"📈 Процент: <b>{activation_rate:.1f}%</b></blockquote>\n\n"

    text += "<blockquote>⭐ <b>Stars чеки</b>\n"
    text += f"📝 Создано: <b>{stars_checks:,}</b>\n"
    text += f"💫 Выдано: <b>{stars_issued:,.0f}</b> Stars</blockquote>\n\n"

    text += "<blockquote>👑 <b>Premium чеки</b>\n"
    text += f"📝 Создано: <b>{premium_checks:,}</b>\n"
    text += f"✅ Активаций: <b>{premium_activations:,}</b></blockquote>"

    return text


@router.callback_query(F.data == AdminCallback.STATS_CHECKS)
async def callback_stats_checks(callback: CallbackQuery) -> None:
    """Статистика чеков."""
    if not await _check_admin(callback):
        return

    text = await get_checks_stats("all")
    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_section_keyboard("checks", "all"),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== СТАТИСТИКА: ПРОМОКОДЫ ====================

async def get_promo_stats(
    period: str,
    date_from: str = None,
    date_to: str = None,
) -> str:
    """Статистика по промокодам."""
    if period == "custom" and date_from and date_to:
        period_start, period_end = parse_custom_dates(date_from, date_to)
    else:
        period_start = get_period_start(period)
        period_end = None
    period_name = get_period_name(period, date_from, date_to)

    async with async_session_factory() as session:
        if period_start and period_end:
            promo_filter = and_(PromoCode.created_at >= period_start, PromoCode.created_at <= period_end)
            use_filter = and_(PromoUse.created_at >= period_start, PromoUse.created_at <= period_end)
        elif period_start:
            promo_filter = PromoCode.created_at >= period_start
            use_filter = PromoUse.created_at >= period_start
        else:
            promo_filter = True
            use_filter = True

        # Общая статистика
        total_promos = (await session.execute(
            select(func.count(PromoCode.id)).where(promo_filter)
        )).scalar() or 0

        total_uses = (await session.execute(
            select(func.count(PromoUse.id)).where(use_filter)
        )).scalar() or 0

        active_promos = (await session.execute(
            select(func.count(PromoCode.id)).where(and_(promo_filter, PromoCode.is_active == True))
        )).scalar() or 0

        # По типу бонуса (stars - если bonus_stars > 0)
        stars_promos = (await session.execute(
            select(func.count(PromoCode.id)).where(and_(promo_filter, PromoCode.bonus_stars > 0))
        )).scalar() or 0

        # Суммы бонусов
        total_bonus_applied = (await session.execute(
            select(func.sum(PromoUse.bonus_applied)).where(use_filter)
        )).scalar() or Decimal("0")

        # Эффективность
        used_promos = (await session.execute(
            select(func.count(func.distinct(PromoUse.promo_id))).where(use_filter)
        )).scalar() or 0
        effectiveness = (used_promos / total_promos * 100) if total_promos > 0 else 0

        # Среднее использований на промокод
        avg_uses = (total_uses / used_promos) if used_promos > 0 else 0

    text = f"🎁 <b>Промокоды {period_name}</b>\n\n"

    text += "<blockquote>📊 <b>Общее</b>\n"
    text += f"📝 Создано: <b>{total_promos:,}</b>\n"
    text += f"🔄 Использований: <b>{total_uses:,}</b>\n"
    text += f"✅ Активных: <b>{active_promos:,}</b></blockquote>\n\n"

    text += "<blockquote>📦 <b>Типы</b>\n"
    text += f"⭐ Stars бонус: <b>{stars_promos:,}</b></blockquote>\n\n"

    text += "<blockquote>📈 <b>Результаты</b>\n"
    text += f"✅ Использовано: <b>{used_promos:,}</b> из {total_promos:,}\n"
    text += f"📊 Процент: <b>{effectiveness:.1f}%</b>\n"
    text += f"🔢 На промокод: <b>{avg_uses:.1f}</b>\n"
    text += f"💫 Выдано: <b>{total_bonus_applied:,.0f}</b> Stars</blockquote>"

    return text


@router.callback_query(F.data == AdminCallback.STATS_PROMO)
async def callback_stats_promo(callback: CallbackQuery) -> None:
    """Статистика промокодов."""
    if not await _check_admin(callback):
        return

    text = await get_promo_stats("all")
    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_section_keyboard("promo", "all"),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== СТАТИСТИКА: ТЕХНИЧЕСКАЯ ====================

async def get_tech_stats(
    period: str,
    date_from: str = None,
    date_to: str = None,
) -> str:
    """Техническая статистика."""
    import time
    import platform
    from src.core.queue import get_order_queue

    if period == "custom" and date_from and date_to:
        period_start, period_end = parse_custom_dates(date_from, date_to)
    else:
        period_start = get_period_start(period)
        period_end = None
    period_name = get_period_name(period, date_from, date_to)

    async with async_session_factory() as session:
        if period_start and period_end:
            order_filter = and_(Order.created_at >= period_start, Order.created_at <= period_end)
        elif period_start:
            order_filter = Order.created_at >= period_start
        else:
            order_filter = True

        # Ошибки
        total_errors = (await session.execute(
            select(func.count(Order.id)).where(and_(order_filter, Order.status == "failed"))
        )).scalar() or 0

        # Типы ошибок (по error_message)
        error_types = (await session.execute(
            select(Order.error_message, func.count(Order.id).label("cnt"))
            .where(and_(order_filter, Order.status == "failed", Order.error_message.isnot(None)))
            .group_by(Order.error_message)
            .order_by(desc("cnt"))
            .limit(5)
        )).fetchall()

        # Производительность: среднее время заказа (от создания до завершения)
        avg_time_result = (await session.execute(
            select(func.avg(
                func.extract('epoch', Order.completed_at) - func.extract('epoch', Order.created_at)
            )).where(and_(
                order_filter,
                Order.status == "completed",
                Order.completed_at.isnot(None)
            ))
        )).scalar()
        avg_order_time = avg_time_result if avg_time_result else 0

        # Заказы в очереди БД (pending + processing)
        db_pending = (await session.execute(
            select(func.count(Order.id)).where(Order.status == "pending")
        )).scalar() or 0
        db_processing = (await session.execute(
            select(func.count(Order.id)).where(Order.status == "processing")
        )).scalar() or 0

        # Всего заказов за период
        total_orders = (await session.execute(
            select(func.count(Order.id)).where(order_filter)
        )).scalar() or 0

        # Процент ошибок
        error_rate = (total_errors / total_orders * 100) if total_orders > 0 else 0

    # Очередь Redis
    try:
        queue = get_order_queue()
        queue_size = await queue.size()
        queue_status = "✅ Подключена"
    except Exception as e:
        queue_size = 0
        queue_status = f"❌ Ошибка: {str(e)[:20]}"

    # Системная информация
    try:
        import psutil

        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
        cpu_percent = process.cpu_percent()
        uptime_seconds = time.time() - process.create_time()
        uptime_hours = int(uptime_seconds // 3600)
        uptime_mins = int((uptime_seconds % 3600) // 60)
    except (ImportError, OSError):
        memory_mb = 0
        cpu_percent = 0
        uptime_hours = 0
        uptime_mins = 0

    text = f"⚙️ <b>Техническая {period_name}</b>\n\n"

    text += "<blockquote>🖥️ <b>Система</b>\n"
    text += f"⏱️ Аптайм: <b>{uptime_hours}ч {uptime_mins}м</b>\n"
    text += f"💾 RAM: <b>{memory_mb:.1f} MB</b>\n"
    text += f"⚡ CPU: <b>{cpu_percent:.1f}%</b>\n"
    text += f"🐍 Python: <b>{platform.python_version()}</b></blockquote>\n\n"

    text += "<blockquote>📬 <b>Очередь</b>\n"
    text += f"🔗 Redis: {queue_status}\n"
    text += f"📋 В очереди: <b>{queue_size:,}</b>\n"
    text += f"🕐 Ожидают: <b>{db_pending:,}</b>\n"
    text += f"⏳ В процессе: <b>{db_processing:,}</b></blockquote>\n\n"

    text += "<blockquote>❌ <b>Ошибки</b>\n"
    text += f"📊 Всего: <b>{total_errors:,}</b>\n"
    text += f"📈 Процент: <b>{error_rate:.2f}%</b></blockquote>\n"

    if error_types:
        text += "\n<blockquote>📋 <b>Топ ошибок</b>\n"
        for i, (error_msg, cnt) in enumerate(error_types):
            msg = error_msg[:25] + "..." if len(error_msg) > 25 else error_msg
            text += f"🔴 {msg}: <b>{cnt}</b>\n"
        text = text.rstrip("\n") + "</blockquote>\n"

    text += "\n<blockquote>⏱️ <b>Скорость</b>\n"
    if avg_order_time > 0:
        minutes = int(avg_order_time // 60)
        seconds = int(avg_order_time % 60)
        text += f"🚀 Время заказа: <b>{minutes}м {seconds}с</b></blockquote>"
    else:
        text += f"🚀 Время заказа: <b>—</b></blockquote>"

    return text


@router.callback_query(F.data == AdminCallback.STATS_TECH)
async def callback_stats_tech(callback: CallbackQuery) -> None:
    """Техническая статистика."""
    if not await _check_admin(callback):
        return

    text = await get_tech_stats("all")
    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_section_keyboard("tech", "all"),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== ОБРАБОТКА ПЕРИОДОВ ====================

@router.callback_query(F.data.regexp(r"^admin:stats:(users|orders|finance|referrals|checks|promo|tech):(24h|7d|30d|all)$"))
async def callback_stats_period(callback: CallbackQuery) -> None:
    """Статистика раздела за выбранный период."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    section = parts[2]
    period = parts[3]

    stats_functions = {
        "users": get_users_stats,
        "orders": get_orders_stats,
        "finance": get_finance_stats,
        "referrals": get_referrals_stats,
        "checks": get_checks_stats,
        "promo": get_promo_stats,
        "tech": get_tech_stats,
    }

    stats_func = stats_functions.get(section)
    if not stats_func:
        await callback.answer("Неизвестный раздел", show_alert=True)
        return

    text = await stats_func(period)
    await callback.message.edit_text(
        text=text,
        reply_markup=get_stats_section_keyboard(section, period),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== ОСТАЛЬНЫЕ РАЗДЕЛЫ ====================

@router.callback_query(F.data == AdminCallback.BACK)
async def callback_admin_back(callback: CallbackQuery) -> None:
    """Назад в главное меню админки."""
    if not await _check_admin(callback):
        return

    await callback.message.edit_text(
        text="🔐 <b>Админ-панель</b>\n\nВыберите раздел:",
        reply_markup=get_admin_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== КАСТОМНЫЙ ПЕРИОД (КАЛЕНДАРЬ) ====================

@router.callback_query(F.data.regexp(r"^admin:stats:(users|orders|finance|referrals|checks|promo|tech):custom$"))
async def callback_stats_custom_period(callback: CallbackQuery, state: FSMContext) -> None:
    """Начать выбор кастомного периода."""
    if not await _check_admin(callback):
        return

    section = callback.data.split(":")[2]
    today = date.today()

    await state.update_data(stats_section=section, custom_date_from=None, custom_date_to=None)
    await state.set_state(AdminStates.stats_selecting_from)

    await callback.message.edit_text(
        text="📅 <b>Выбор периода</b>\n\nВыберите <b>начальную дату</b>:",
        reply_markup=get_calendar_keyboard(today.year, today.month, context=f"stats_from:{section}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cal:(prev|next):\d+:\d+:stats_(from|to):"))
async def callback_calendar_navigate(callback: CallbackQuery, state: FSMContext) -> None:
    """Навигация по календарю."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    direction = parts[1]
    year = int(parts[2])
    month = int(parts[3])
    context = parts[4] + ":" + parts[5]  # stats_from:section or stats_to:section

    await callback.message.edit_reply_markup(
        reply_markup=get_calendar_keyboard(year, month, context=context)
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cal:day:\d+:\d+:\d+:stats_from:"))
async def callback_calendar_select_from(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор начальной даты."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    year = int(parts[2])
    month = int(parts[3])
    day = int(parts[4])
    section = parts[6]

    selected_date = date(year, month, day)
    date_str = selected_date.strftime("%d.%m.%Y")

    await state.update_data(custom_date_from=date_str, stats_section=section)
    await state.set_state(AdminStates.stats_selecting_to)

    today = date.today()
    await callback.message.edit_text(
        text=f"📅 <b>Выбор периода</b>\n\n<b>С:</b> {date_str}\n\nВыберите <b>конечную дату</b>:",
        reply_markup=get_calendar_keyboard(
            today.year, today.month,
            context=f"stats_to:{section}",
            min_date=selected_date
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cal:day:\d+:\d+:\d+:stats_to:"))
async def callback_calendar_select_to(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор конечной даты и показ статистики."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    year = int(parts[2])
    month = int(parts[3])
    day = int(parts[4])
    section = parts[6]

    selected_date = date(year, month, day)
    date_to = selected_date.strftime("%d.%m.%Y")

    data = await state.get_data()
    date_from = data.get("custom_date_from")

    await state.clear()

    stats_functions = {
        "users": get_users_stats,
        "orders": get_orders_stats,
        "finance": get_finance_stats,
        "referrals": get_referrals_stats,
        "checks": get_checks_stats,
        "promo": get_promo_stats,
        "tech": get_tech_stats,
    }

    stats_func = stats_functions.get(section)
    if stats_func:
        text = await stats_func("custom", date_from, date_to)
        await callback.message.edit_text(
            text=text,
            reply_markup=get_stats_section_keyboard(section, "custom", date_from, date_to),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cal:today:stats_(from|to):"))
async def callback_calendar_today(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор сегодняшней даты."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    context_type = parts[2]  # stats_from or stats_to
    section = parts[3]

    today = date.today()

    if context_type == "stats_from":
        # Имитируем выбор сегодня как начальной даты
        date_str = today.strftime("%d.%m.%Y")
        await state.update_data(custom_date_from=date_str, stats_section=section)
        await state.set_state(AdminStates.stats_selecting_to)

        await callback.message.edit_text(
            text=f"📅 <b>Выбор периода</b>\n\n<b>С:</b> {date_str}\n\nВыберите <b>конечную дату</b>:",
            reply_markup=get_calendar_keyboard(
                today.year, today.month,
                context=f"stats_to:{section}",
                min_date=today
            ),
            parse_mode="HTML",
        )
    else:
        # Выбор сегодня как конечной даты
        date_to = today.strftime("%d.%m.%Y")
        data = await state.get_data()
        date_from = data.get("custom_date_from")
        section = data.get("stats_section", section)

        await state.clear()

        stats_functions = {
            "users": get_users_stats,
            "orders": get_orders_stats,
            "finance": get_finance_stats,
            "referrals": get_referrals_stats,
            "checks": get_checks_stats,
            "promo": get_promo_stats,
            "tech": get_tech_stats,
        }

        stats_func = stats_functions.get(section)
        if stats_func:
            text = await stats_func("custom", date_from, date_to)
            await callback.message.edit_text(
                text=text,
                reply_markup=get_stats_section_keyboard(section, "custom", date_from, date_to),
                parse_mode="HTML",
            )

    await callback.answer()


@router.callback_query(F.data.regexp(r"^cal:cancel:stats_(from|to):"))
async def callback_calendar_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена выбора периода."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    section = parts[3]

    await state.clear()

    # Возвращаемся к статистике раздела
    stats_functions = {
        "users": get_users_stats,
        "orders": get_orders_stats,
        "finance": get_finance_stats,
        "referrals": get_referrals_stats,
        "checks": get_checks_stats,
        "promo": get_promo_stats,
        "tech": get_tech_stats,
    }

    stats_func = stats_functions.get(section)
    if stats_func:
        text = await stats_func("all")
        await callback.message.edit_text(
            text=text,
            reply_markup=get_stats_section_keyboard(section, "all"),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "cal:ignore")
async def callback_calendar_ignore(callback: CallbackQuery) -> None:
    """Игнорировать нажатие на пустую ячейку."""
    await callback.answer()


# ==================== УПРАВЛЕНИЕ ЛОГАМИ ====================

@router.callback_query(F.data == AdminCallback.LOGS)
async def callback_logs_menu(callback: CallbackQuery) -> None:
    """Меню настроек логирования."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        settings = await service.get_settings()

    enabled = settings.get("enabled", True)
    group_id = settings.get("group_id")

    enabled_topics = sum(1 for t in settings.get("topics", {}).values() if t.get("enabled", True))
    total_topics = len(settings.get("topics", {}))

    enabled_events = sum(1 for e in settings.get("events", {}).values() if e)
    total_events = len(settings.get("events", {}))

    text = "📋 <b>Настройки логирования</b>\n\n"
    text += f"<b>Статус:</b> {'✅ Включено' if enabled else '❌ Выключено'}\n"
    text += f"<b>Группа:</b> <code>{group_id}</code>\n\n"
    text += f"<b>Топики:</b> {enabled_topics}/{total_topics} активно\n"
    text += f"<b>События:</b> {enabled_events}/{total_events} активно"

    await callback.message.edit_text(
        text=text,
        reply_markup=get_logs_menu_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.LOGS_TOGGLE)
async def callback_logs_toggle(callback: CallbackQuery) -> None:
    """Включить/выключить логирование."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        settings = await service.get_settings()
        new_enabled = not settings.get("enabled", True)
        await service.toggle_logging(new_enabled)
        await session.commit()
        settings["enabled"] = new_enabled

    invalidate_log_settings_cache()
    tg_logger.invalidate_cache()

    await callback.message.edit_reply_markup(
        reply_markup=get_logs_menu_keyboard(settings)
    )
    status = "включено" if new_enabled else "выключено"
    await callback.answer(f"Логирование {status}")


@router.callback_query(F.data == AdminCallback.LOGS_BACK)
async def callback_logs_back(callback: CallbackQuery) -> None:
    """Назад к меню логов."""
    await callback_logs_menu(callback)


@router.callback_query(F.data == AdminCallback.LOGS_GROUP)
async def callback_logs_group(callback: CallbackQuery, state: FSMContext) -> None:
    """Настройка группы для логов."""
    if not await _check_admin(callback):
        return

    await state.set_state(AdminStates.waiting_log_group_id)

    await callback.message.edit_text(
        text="🔗 <b>Настройка группы для логов</b>\n\n"
             "Отправьте ID группы (начинается с -100...).\n\n"
             "Чтобы узнать ID, добавьте бота @getmyid_bot в группу.",
        reply_markup=get_logs_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.LOGS_CANCEL)
async def callback_logs_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена ввода."""
    await state.clear()
    await callback_logs_menu(callback)


@router.message(AdminStates.waiting_log_group_id)
async def process_log_group_id(message: Message, state: FSMContext) -> None:
    """Обработка ввода ID группы."""
    try:
        group_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "Неверный формат. Отправьте число (например: -1001234567890)",
            reply_markup=get_logs_cancel_keyboard(),
        )
        return

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        await service.set_group_id(group_id)
        await session.commit()

    invalidate_log_settings_cache()
    tg_logger.invalidate_cache()

    await state.clear()
    await message.answer(
        f"✅ Группа для логов установлена: <code>{group_id}</code>\n\n"
        "Вернитесь в /admin для дальнейших настроек.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == AdminCallback.LOGS_TOPICS)
async def callback_logs_topics(callback: CallbackQuery) -> None:
    """Управление топиками."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        settings = await service.get_settings()

    await callback.message.edit_text(
        text="📂 <b>Управление топиками</b>\n\n"
             "• Нажмите на название — вкл/выкл логирование\n"
             "• Нажмите на <code>#ID</code> — изменить ID топика",
        reply_markup=get_logs_topics_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:logs:topic:toggle:"))
async def callback_logs_topic_toggle(callback: CallbackQuery) -> None:
    """Переключение топика."""
    if not await _check_admin(callback):
        return

    topic_key = callback.data.split(":")[-1]

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        settings = await service.get_settings()

        current = settings.get("topics", {}).get(topic_key, {}).get("enabled", True)
        await service.toggle_topic(topic_key, not current)
        await session.commit()

        settings = await service.get_settings()

    invalidate_log_settings_cache()
    tg_logger.invalidate_cache()

    await callback.message.edit_reply_markup(
        reply_markup=get_logs_topics_keyboard(settings)
    )
    topic_name = settings.get("topics", {}).get(topic_key, {}).get("name", topic_key)
    status = "включён" if not current else "выключен"
    await callback.answer(f"{topic_name}: {status}")


@router.callback_query(F.data.regexp(r"^admin:logs:topic:edit:"))
async def callback_logs_topic_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Редактирование ID топика."""
    if not await _check_admin(callback):
        return

    topic_key = callback.data.split(":")[-1]

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        settings = await service.get_settings()

    topic_data = settings.get("topics", {}).get(topic_key, {})
    topic_name = topic_data.get("name", topic_key)
    current_id = topic_data.get("id", "не задан")

    await state.set_state(AdminStates.waiting_topic_id)
    await state.update_data(editing_topic_key=topic_key)

    await callback.message.edit_text(
        text=f"📂 <b>Изменение ID топика</b>\n\n"
             f"<b>Топик:</b> {topic_name}\n"
             f"<b>Текущий ID:</b> <code>{current_id}</code>\n\n"
             f"Отправьте новый ID топика (число).\n\n"
             f"<i>ID топика можно найти в ссылке на сообщение в топике.</i>",
        reply_markup=get_logs_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_topic_id)
async def process_topic_id(message: Message, state: FSMContext) -> None:
    """Обработка ввода ID топика."""
    try:
        topic_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "Неверный формат. Отправьте число.",
            reply_markup=get_logs_cancel_keyboard(),
        )
        return

    data = await state.get_data()
    topic_key = data.get("editing_topic_key")

    if not topic_key:
        await state.clear()
        await message.answer("Ошибка. Попробуйте снова через /admin")
        return

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        await service.set_topic_id(topic_key, topic_id)
        await session.commit()
        settings = await service.get_settings()

    invalidate_log_settings_cache()
    tg_logger.invalidate_cache()

    topic_name = settings.get("topics", {}).get(topic_key, {}).get("name", topic_key)

    await state.clear()
    await message.answer(
        f"✅ ID топика <b>{topic_name}</b> установлен: <code>{topic_id}</code>\n\n"
        "Вернитесь в /admin для дальнейших настроек.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == AdminCallback.LOGS_EVENTS)
async def callback_logs_events(callback: CallbackQuery) -> None:
    """Управление событиями."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        settings = await service.get_settings()

    await callback.message.edit_text(
        text="📋 <b>Управление событиями</b>\n\n"
             "Нажмите на событие, чтобы включить/выключить его:",
        reply_markup=get_logs_events_keyboard(settings, page=0),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:logs:events:page:\d+$"))
async def callback_logs_events_page(callback: CallbackQuery) -> None:
    """Переключение страницы событий."""
    if not await _check_admin(callback):
        return

    page = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        settings = await service.get_settings()

    await callback.message.edit_reply_markup(
        reply_markup=get_logs_events_keyboard(settings, page=page)
    )
    await callback.answer()


@router.callback_query(F.data == "admin:logs:events:nop")
async def callback_logs_events_nop(callback: CallbackQuery) -> None:
    """Игнорировать нажатие на номер страницы."""
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:logs:event:"))
async def callback_logs_event_toggle(callback: CallbackQuery) -> None:
    """Переключение события."""
    if not await _check_admin(callback):
        return

    event_key = callback.data.split(":")[-1]

    async with async_session_factory() as session:
        service = LogSettingsService(session)
        settings = await service.get_settings()

        current = settings.get("events", {}).get(event_key, True)
        await service.toggle_event(event_key, not current)
        await session.commit()

        settings = await service.get_settings()

    invalidate_log_settings_cache()
    tg_logger.invalidate_cache()

    # Определяем текущую страницу (упрощённо - оставляем на той же)
    await callback.message.edit_reply_markup(
        reply_markup=get_logs_events_keyboard(settings, page=0)
    )
    status = "включено" if not current else "выключено"
    await callback.answer(f"Событие {status}")


# ==================== УПРАВЛЕНИЕ ПРОМОКОДАМИ ====================

@router.callback_query(F.data == AdminCallback.PROMO)
async def callback_promo_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню управления промокодами."""
    if not await _check_admin(callback):
        return

    await state.clear()

    async with async_session_factory() as session:
        user_service = UserService(session)
        promos = await user_service.get_all_promo_codes()

    active_count = sum(1 for p in promos if p.is_active)
    total_uses = sum(p.current_uses for p in promos)

    text = (
        "🎁 <b>Управление промокодами</b>\n\n"
        f"<blockquote>📊 Всего: <b>{len(promos)}</b>\n"
        f"✅ Активных: <b>{active_count}</b>\n"
        f"👤 Использований: <b>{total_uses}</b></blockquote>"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_promo_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.PROMO_BACK)
async def callback_promo_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Назад к меню промокодов."""
    await callback_promo_menu(callback, state)


@router.callback_query(F.data == AdminCallback.PROMO_CREATE)
async def callback_promo_create(callback: CallbackQuery, state: FSMContext) -> None:
    """Начать создание промокода."""
    if not await _check_admin(callback):
        return

    await state.set_state(AdminStates.promo_waiting_code)
    # Сохраняем ID сообщения бота для последующего редактирования
    await state.update_data(promo_data={}, promo_bot_message_id=callback.message.message_id)

    await callback.message.edit_text(
        text=(
            "🎁 <b>Создание промокода</b>\n\n"
            "Введите код или отправьте <code>*</code> для автогенерации (3-20 символов):"
        ),
        reply_markup=get_promo_code_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.promo_waiting_code)
async def process_promo_code_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода кода промокода."""
    import secrets

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    bot_message_id = data.get("promo_bot_message_id")

    code = message.text.strip().upper()

    # Автогенерация
    if code == "*":
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))

    if len(code) < 3 or len(code) > 20:
        if bot_message_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=bot_message_id,
                    text="🎁 <b>Создание промокода</b>\n\n"
                         "❌ Код должен быть от 3 до 20 символов.\n\n"
                         "Введите код снова:",
                    reply_markup=get_promo_code_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    # Проверяем существование
    async with async_session_factory() as session:
        user_service = UserService(session)
        existing = await user_service.get_promo_code(code)

    if existing:
        if bot_message_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=bot_message_id,
                    text=f"🎁 <b>Создание промокода</b>\n\n"
                         f"❌ Промокод <code>{code}</code> уже существует.\n\n"
                         "Введите другой код:",
                    reply_markup=get_promo_code_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    promo_data = data.get("promo_data", {})
    promo_data["code"] = code
    await state.update_data(promo_data=promo_data)
    await state.set_state(None)

    if bot_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text="🎁 <b>Тип бонуса</b>\n\n"
                     f"<blockquote>📝 Код: <b><code>{code}</code></b></blockquote>\n\n"
                     "Выберите тип бонуса:",
                reply_markup=get_promo_type_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data == AdminCallback.PROMO_BACK_TO_CODE)
async def callback_promo_back_to_code(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к вводу кода."""
    if not await _check_admin(callback):
        return

    await state.set_state(AdminStates.promo_waiting_code)
    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data.pop("code", None)
    await state.update_data(promo_data=promo_data)

    await callback.message.edit_text(
        text=(
            "🎁 <b>Создание промокода</b>\n\n"
            "Введите код или отправьте <code>*</code> для автогенерации (3-20 символов):"
        ),
        reply_markup=get_promo_code_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.PROMO_TYPE_STARS)
async def callback_promo_type_stars(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбран бонус Звёзды."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data["type"] = "stars"
    await state.update_data(promo_data=promo_data)
    await state.set_state(AdminStates.promo_waiting_bonus)

    await callback.message.edit_text(
        text=(
            f"⭐ <b>Количество звёзд</b>\n\n"
            f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
            f"⭐ Тип: <b>Звёзды</b></blockquote>\n\n"
            f"Введите количество звёзд для бонуса:"
        ),
        reply_markup=get_promo_bonus_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.PROMO_TYPE_USDT)
async def callback_promo_type_usdt(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбран бонус USDT."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data["type"] = "usdt"
    await state.update_data(promo_data=promo_data)
    await state.set_state(AdminStates.promo_waiting_bonus)

    await callback.message.edit_text(
        text=(
            f"💵 <b>Сумма USDT</b>\n\n"
            f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
            f"💵 Тип: <b>USDT</b></blockquote>\n\n"
            f"Введите сумму USDT для бонуса:"
        ),
        reply_markup=get_promo_bonus_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.PROMO_TYPE_PREMIUM)
async def callback_promo_type_premium(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбран бонус Premium."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data["type"] = "premium"
    await state.update_data(promo_data=promo_data)
    await state.set_state(None)  # Не ждём ввода, кнопки с выбором

    await callback.message.edit_text(
        text=(
            f"👑 <b>Количество месяцев Premium</b>\n\n"
            f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
            f"👑 Тип: <b>Premium</b></blockquote>\n\n"
            f"Выберите количество месяцев:"
        ),
        reply_markup=get_promo_premium_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:promo:premium:\d+$"))
async def callback_promo_premium_months(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбрано количество месяцев Premium."""
    if not await _check_admin(callback):
        return

    months = int(callback.data.split(":")[-1])
    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data["bonus_premium"] = months
    await state.update_data(promo_data=promo_data)
    await state.set_state(AdminStates.promo_waiting_limit)

    await callback.message.edit_text(
        text=(
            f"👤 <b>Лимит активаций</b>\n\n"
            f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
            f"👑 Бонус: <b>+{months} мес. Premium</b></blockquote>\n\n"
            f"Введите лимит использований:"
        ),
        reply_markup=get_promo_limit_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.PROMO_BACK_TO_TYPE)
async def callback_promo_back_to_type(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к выбору типа бонуса."""
    if not await _check_admin(callback):
        return

    await state.set_state(None)
    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data.pop("type", None)
    promo_data.pop("bonus_stars", None)
    promo_data.pop("bonus_usdt", None)
    promo_data.pop("bonus_premium", None)
    await state.update_data(promo_data=promo_data)

    await callback.message.edit_text(
        text="🎁 <b>Тип бонуса</b>\n\n"
             f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b></blockquote>\n\n"
             "Выберите тип бонуса:",
        reply_markup=get_promo_type_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


def _get_promo_bonus_emoji(promo_data: dict) -> str:
    """Получить emoji для типа бонуса промокода."""
    promo_type = promo_data.get("type", "stars")
    if promo_type == "stars":
        return "⭐"
    elif promo_type == "usdt":
        return "💵"
    elif promo_type == "premium":
        return "👑"
    return "🎁"


def _format_promo_bonus(promo_data: dict) -> str:
    """Форматировать текст бонуса промокода (без emoji)."""
    promo_type = promo_data.get("type", "stars")
    if promo_type == "stars":
        return f"+{promo_data.get('bonus_stars', 0)} звёзд"
    elif promo_type == "usdt":
        return f"+{promo_data.get('bonus_usdt', 0)} USDT"
    elif promo_type == "premium":
        return f"+{promo_data.get('bonus_premium', 0)} мес. Premium"
    return "без бонуса"


@router.message(AdminStates.promo_waiting_bonus)
async def process_promo_bonus_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода бонуса (Stars или USDT)."""
    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    bot_message_id = data.get("promo_bot_message_id")
    promo_type = promo_data.get("type", "stars")

    try:
        if promo_type == "usdt":
            value = float(message.text.strip().replace(",", "."))
            if value <= 0:
                raise ValueError("Должно быть положительным")
            promo_data["bonus_usdt"] = value
            bonus_emoji = "💵"
            bonus_text = f"+{value} USDT"
            type_text = "USDT"
            type_emoji = "💵"
            title = "💵 <b>Сумма USDT</b>"
            input_prompt = "Введите сумму USDT для бонуса:"
        else:  # stars
            value = int(message.text.strip())
            if value <= 0:
                raise ValueError("Должно быть положительным")
            promo_data["bonus_stars"] = value
            bonus_emoji = "⭐"
            bonus_text = f"+{value} звёзд"
            type_text = "Звёзды"
            type_emoji = "⭐"
            title = "⭐ <b>Количество звёзд</b>"
            input_prompt = "Введите количество звёзд для бонуса:"
    except ValueError:
        error_msg = "❌ Введите положительное число"
        if bot_message_id:
            try:
                type_text = "USDT" if promo_type == "usdt" else "Звёзды"
                title = "💵 <b>Сумма USDT</b>" if promo_type == "usdt" else "⭐ <b>Количество звёзд</b>"
                type_emoji = "💵" if promo_type == "usdt" else "⭐"
                input_prompt = "Введите сумму USDT для бонуса:" if promo_type == "usdt" else "Введите количество звёзд для бонуса:"
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=bot_message_id,
                    text=f"{title}\n\n"
                         f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
                         f"{type_emoji} Тип: <b>{type_text}</b></blockquote>\n\n"
                         f"{error_msg}\n{input_prompt}",
                    reply_markup=get_promo_bonus_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    await state.update_data(promo_data=promo_data)
    await state.set_state(AdminStates.promo_waiting_limit)

    if bot_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text=f"👤 <b>Лимит активаций</b>\n\n"
                     f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
                     f"{bonus_emoji} Бонус: <b>{bonus_text}</b></blockquote>\n\n"
                     f"Введите лимит использований:",
                reply_markup=get_promo_limit_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data == AdminCallback.PROMO_BACK_TO_BONUS)
async def callback_promo_back_to_bonus(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к вводу бонуса."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_type = promo_data.get("type", "stars")

    # Очищаем бонус
    promo_data.pop("bonus_stars", None)
    promo_data.pop("bonus_usdt", None)
    promo_data.pop("bonus_premium", None)
    promo_data.pop("max_uses", None)
    await state.update_data(promo_data=promo_data)

    if promo_type == "premium":
        await state.set_state(None)
        await callback.message.edit_text(
            text=(
                f"👑 <b>Количество месяцев Premium</b>\n\n"
                f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
                f"👑 Тип: <b>Premium</b></blockquote>\n\n"
                f"Выберите количество месяцев:"
            ),
            reply_markup=get_promo_premium_keyboard(),
            parse_mode="HTML",
        )
    elif promo_type == "usdt":
        await state.set_state(AdminStates.promo_waiting_bonus)
        await callback.message.edit_text(
            text=(
                f"💵 <b>Сумма USDT</b>\n\n"
                f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
                f"💵 Тип: <b>USDT</b></blockquote>\n\n"
                f"Введите сумму USDT для бонуса:"
            ),
            reply_markup=get_promo_bonus_keyboard(),
            parse_mode="HTML",
        )
    else:  # stars
        await state.set_state(AdminStates.promo_waiting_bonus)
        await callback.message.edit_text(
            text=(
                f"⭐ <b>Количество звёзд</b>\n\n"
                f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
                f"⭐ Тип: <b>Звёзды</b></blockquote>\n\n"
                f"Введите количество звёзд для бонуса:"
            ),
            reply_markup=get_promo_bonus_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.PROMO_SKIP_LIMIT)
async def callback_promo_skip_limit(callback: CallbackQuery, state: FSMContext) -> None:
    """Пропустить лимит (без лимита)."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data["max_uses"] = None
    await state.update_data(promo_data=promo_data)
    await state.set_state(AdminStates.promo_waiting_expires)

    await callback.message.edit_text(
        f"⏰ <b>Срок действия</b>\n\n"
        f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
        f"{_get_promo_bonus_emoji(promo_data)} Бонус: <b>{_format_promo_bonus(promo_data)}</b>\n"
        f"👤 Лимит: <b>♾️ Без лимита</b></blockquote>\n\n"
        f"Введите срок действия в днях:",
        reply_markup=get_promo_expires_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.promo_waiting_limit)
async def process_promo_limit_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода лимита использований."""
    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    bot_message_id = data.get("promo_bot_message_id")
    promo_data = data.get("promo_data", {})

    try:
        value = int(message.text.strip())
        if value <= 0:
            raise ValueError()
    except ValueError:
        if bot_message_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=bot_message_id,
                    text=f"👤 <b>Лимит активаций</b>\n\n"
                         f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
                         f"{_get_promo_bonus_emoji(promo_data)} Бонус: <b>{_format_promo_bonus(promo_data)}</b></blockquote>\n\n"
                         f"❌ Введите положительное целое число:",
                    reply_markup=get_promo_limit_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    promo_data["max_uses"] = value
    await state.update_data(promo_data=promo_data)
    await state.set_state(AdminStates.promo_waiting_expires)

    if bot_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text=f"⏰ <b>Срок действия</b>\n\n"
                     f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
                     f"{_get_promo_bonus_emoji(promo_data)} Бонус: <b>{_format_promo_bonus(promo_data)}</b>\n"
                     f"👤 Лимит: <b>{value} активаций</b></blockquote>\n\n"
                     f"Введите срок действия в днях:",
                reply_markup=get_promo_expires_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data == AdminCallback.PROMO_BACK_TO_LIMIT)
async def callback_promo_back_to_limit(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к вводу лимита."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data.pop("max_uses", None)
    promo_data.pop("expires_at", None)
    promo_data.pop("expires_days", None)
    await state.update_data(promo_data=promo_data)
    await state.set_state(AdminStates.promo_waiting_limit)

    await callback.message.edit_text(
        text=f"👤 <b>Лимит активаций</b>\n\n"
             f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
             f"{_get_promo_bonus_emoji(promo_data)} Бонус: <b>{_format_promo_bonus(promo_data)}</b></blockquote>\n\n"
             f"Введите лимит использований:",
        reply_markup=get_promo_limit_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.PROMO_SKIP_EXPIRES)
async def callback_promo_skip_expires(callback: CallbackQuery, state: FSMContext) -> None:
    """Пропустить срок действия (бессрочно)."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data["expires_at"] = None
    await state.update_data(promo_data=promo_data)

    # Показать подтверждение
    await _show_promo_confirmation(callback.message, promo_data, edit=True)
    await callback.answer()


@router.message(AdminStates.promo_waiting_expires)
async def process_promo_expires_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода срока действия."""
    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    bot_message_id = data.get("promo_bot_message_id")
    promo_data = data.get("promo_data", {})

    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError()
    except ValueError:
        if bot_message_id:
            limit_text = f"{promo_data.get('max_uses')} активаций" if promo_data.get("max_uses") else "♾️ Без лимита"
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=bot_message_id,
                    text=f"⏰ <b>Срок действия</b>\n\n"
                         f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
                         f"{_get_promo_bonus_emoji(promo_data)} Бонус: <b>{_format_promo_bonus(promo_data)}</b>\n"
                         f"👤 Лимит: <b>{limit_text}</b></blockquote>\n\n"
                         f"❌ Введите положительное целое число (дни):",
                    reply_markup=get_promo_expires_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    promo_data = data.get("promo_data", {})
    promo_data["expires_at"] = datetime.utcnow() + timedelta(days=days)
    promo_data["expires_days"] = days
    await state.update_data(promo_data=promo_data)

    # Показать подтверждение (редактируем сообщение бота)
    if bot_message_id:
        await _show_promo_confirmation_by_id(message.bot, message.chat.id, bot_message_id, promo_data)
    else:
        await _show_promo_confirmation(message, promo_data, edit=False)


@router.callback_query(F.data == AdminCallback.PROMO_BACK_TO_EXPIRES)
async def callback_promo_back_to_expires(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к вводу срока действия."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})
    promo_data.pop("expires_at", None)
    promo_data.pop("expires_days", None)
    await state.update_data(promo_data=promo_data)
    await state.set_state(AdminStates.promo_waiting_expires)

    limit_text = f"{promo_data.get('max_uses')} активаций" if promo_data.get("max_uses") else "♾️ Без лимита"

    await callback.message.edit_text(
        text=f"⏰ <b>Срок действия</b>\n\n"
             f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
             f"{_get_promo_bonus_emoji(promo_data)} Бонус: <b>{_format_promo_bonus(promo_data)}</b>\n"
             f"👤 Лимит: <b>{limit_text}</b></blockquote>\n\n"
             f"Введите срок действия в днях:",
        reply_markup=get_promo_expires_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


def _build_promo_confirmation_text(promo_data: dict) -> str:
    """Построить текст подтверждения промокода."""
    bonus_emoji = _get_promo_bonus_emoji(promo_data)
    bonus_text = _format_promo_bonus(promo_data)
    limit_text = f"{promo_data.get('max_uses')} активаций" if promo_data.get("max_uses") else "♾️ Без лимита"

    if promo_data.get("expires_at"):
        expires_text = f"{promo_data.get('expires_days')} дней"
    else:
        expires_text = "♾️ Бессрочно"

    return (
        "✅ <b>Подтверждение</b>\n\n"
        f"<blockquote>📝 Код: <b><code>{promo_data.get('code')}</code></b>\n"
        f"{bonus_emoji} Бонус: <b>{bonus_text}</b>\n"
        f"👤 Лимит: <b>{limit_text}</b>\n"
        f"⏰ Срок: <b>{expires_text}</b></blockquote>\n\n"
        "Создать промокод?"
    )


async def _show_promo_confirmation_by_id(bot, chat_id: int, message_id: int, promo_data: dict) -> None:
    """Показать подтверждение создания промокода по ID сообщения."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_build_promo_confirmation_text(promo_data),
            reply_markup=get_promo_confirm_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _show_promo_confirmation(message, promo_data: dict, edit: bool = False) -> None:
    """Показать подтверждение создания промокода."""
    text = _build_promo_confirmation_text(promo_data)

    if edit:
        await message.edit_text(
            text=text,
            reply_markup=get_promo_confirm_keyboard(),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            text=text,
            reply_markup=get_promo_confirm_keyboard(),
            parse_mode="HTML",
        )


@router.callback_query(F.data == AdminCallback.PROMO_CONFIRM)
async def callback_promo_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтвердить создание промокода."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    promo_data = data.get("promo_data", {})

    async with async_session_factory() as session:
        user_service = UserService(session)
        promo, msg_key = await user_service.create_promo_code(
            code=promo_data.get("code"),
            bonus_stars=Decimal(str(promo_data.get("bonus_stars", 0))),
            bonus_usdt=Decimal(str(promo_data.get("bonus_usdt", 0))),
            bonus_premium=promo_data.get("bonus_premium", 0),
            max_uses=promo_data.get("max_uses"),
            expires_at=promo_data.get("expires_at"),
        )
        await session.commit()

    await state.clear()

    if promo:
        # Формируем текст бонуса (emoji в начале строки)
        if promo.bonus_stars > 0:
            bonus_emoji = "⭐"
            bonus_text = f"+{promo.bonus_stars:,.0f} звёзд"
        elif promo.bonus_usdt > 0:
            bonus_emoji = "💵"
            bonus_text = f"+{promo.bonus_usdt:,.2f} USDT"
        elif promo.bonus_premium > 0:
            bonus_emoji = "👑"
            bonus_text = f"+{promo.bonus_premium} мес. Premium"
        else:
            bonus_emoji = "🎁"
            bonus_text = "без бонуса"

        limit_text = f"{promo.current_uses}/{promo.max_uses}" if promo.max_uses else f"{promo.current_uses}/♾️"
        expires_text = "♾️ Бессрочно"
        if promo.expires_at:
            expires_text = to_moscow(promo.expires_at).strftime("%d.%m.%Y %H:%M")
        created_text = to_moscow(promo.created_at).strftime("%d.%m.%Y %H:%M")

        await callback.message.edit_text(
            f"🎁 <b>Промокод создан</b>\n\n"
            f"<blockquote>📝 Код: <b><code>{promo.code}</code></b>\n"
            f"✅ Статус: <b>Активен</b>\n"
            f"{bonus_emoji} Бонус: <b>{bonus_text}</b>\n"
            f"👤 Использований: <b>{limit_text}</b>\n"
            f"⏰ Действует до: <b>{expires_text}</b>\n"
            f"📅 Создан: <b>{created_text}</b></blockquote>",
            reply_markup=get_promo_back_keyboard(),
            parse_mode="HTML",
        )
    else:
        error_msgs = {
            "already_exists": "Промокод уже существует",
            "invalid": "Некорректный код",
        }
        await callback.message.edit_text(
            f"❌ Ошибка: {error_msgs.get(msg_key, 'Неизвестная ошибка')}",
            reply_markup=get_promo_back_keyboard(),
            parse_mode="HTML",
        )

    await callback.answer()


@router.callback_query(F.data == AdminCallback.PROMO_CANCEL)
async def callback_promo_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена создания промокода."""
    await state.clear()
    await callback_promo_menu(callback, state)


def _build_promo_list_text(total_count: int, page: int, page_size: int = 5) -> str:
    """Построить текст для списка промокодов."""
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    return (
        "📋 <b>Список промокодов</b>\n\n"
        f"<blockquote>Всего промокодов: <b>{total_count}</b>\n"
        f"Страница <b>{page + 1} из {total_pages}</b></blockquote>"
    )


@router.callback_query(F.data == AdminCallback.PROMO_LIST)
async def callback_promo_list(callback: CallbackQuery) -> None:
    """Список промокодов."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        promos = await user_service.get_all_promo_codes()

    if not promos:
        await callback.message.edit_text(
            "📋 <b>Список промокодов</b>\n\n"
            "Промокодов пока нет.",
            reply_markup=get_promo_empty_list_keyboard(),
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            _build_promo_list_text(len(promos), page=0),
            reply_markup=get_promo_list_keyboard(promos, page=0),
            parse_mode="HTML",
        )

    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:promo:page:\d+$"))
async def callback_promo_list_page(callback: CallbackQuery) -> None:
    """Пагинация списка промокодов."""
    if not await _check_admin(callback):
        return

    page = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        promos = await user_service.get_all_promo_codes()

    await callback.message.edit_text(
        _build_promo_list_text(len(promos), page=page),
        reply_markup=get_promo_list_keyboard(promos, page=page),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:promo:nop")
async def callback_promo_nop(callback: CallbackQuery) -> None:
    """Игнорировать нажатие на номер страницы."""
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:promo:view:\d+$"))
async def callback_promo_view(callback: CallbackQuery) -> None:
    """Просмотр промокода."""
    if not await _check_admin(callback):
        return

    promo_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        promo = await user_service.get_promo_by_id(promo_id)

    if not promo:
        await callback.answer("Промокод не найден", show_alert=True)
        return

    # Определяем статус и тип бонуса
    if promo.is_active:
        status_emoji = "✅"
        status_text = "Активен"
    else:
        status_emoji = "❌"
        status_text = "Неактивен"

    # Определяем тип бонуса (emoji в начале строки)
    if promo.bonus_stars > 0:
        bonus_emoji = "⭐"
        bonus_text = f"+{promo.bonus_stars:,.0f} звёзд"
    elif promo.bonus_usdt > 0:
        bonus_emoji = "💵"
        bonus_text = f"+{promo.bonus_usdt:,.2f} USDT"
    elif promo.bonus_premium > 0:
        bonus_emoji = "👑"
        bonus_text = f"+{promo.bonus_premium} мес. Premium"
    else:
        bonus_emoji = "🎁"
        bonus_text = "без бонуса"

    limit_text = f"{promo.current_uses}/{promo.max_uses}" if promo.max_uses else f"{promo.current_uses}/♾️"

    if promo.expires_at:
        expires_text = to_moscow(promo.expires_at).strftime("%d.%m.%Y %H:%M")
        if promo.expires_at < datetime.utcnow():
            expires_text += " (истёк)"
    else:
        expires_text = "♾️ Бессрочно"

    text = (
        f"🎁 <b>Промокод</b>\n\n"
        f"<blockquote>📝 Код: <b><code>{promo.code}</code></b>\n"
        f"{status_emoji} Статус: <b>{status_text}</b>\n"
        f"{bonus_emoji} Бонус: <b>{bonus_text}</b>\n"
        f"👤 Использований: <b>{limit_text}</b>\n"
        f"⏰ Действует до: <b>{expires_text}</b>\n"
        f"📅 Создан: <b>{to_moscow(promo.created_at).strftime('%d.%m.%Y %H:%M')}</b></blockquote>"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_promo_detail_keyboard(promo.id, promo.is_active),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:promo:toggle:\d+$"))
async def callback_promo_toggle(callback: CallbackQuery) -> None:
    """Переключить активность промокода."""
    if not await _check_admin(callback):
        return

    promo_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        success = await user_service.toggle_promo_code(promo_id)
        await session.commit()

        promo = await user_service.get_promo_by_id(promo_id)

    if success and promo:
        status = "активирован" if promo.is_active else "деактивирован"
        await callback.answer(f"Промокод {status}")

        # Обновляем просмотр
        await callback_promo_view(callback)
    else:
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.regexp(r"^admin:promo:delete:confirm:\d+$"))
async def callback_promo_delete_confirm(callback: CallbackQuery) -> None:
    """Показать подтверждение удаления промокода."""
    if not await _check_admin(callback):
        return

    promo_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        promo = await user_service.get_promo_by_id(promo_id)

    if not promo:
        await callback.answer("Промокод не найден", show_alert=True)
        return

    await callback.message.edit_text(
        f"🗑️ <b>Удаление промокода</b>\n\n"
        f"Вы уверены, что хотите удалить промокод <code>{promo.code}</code>?",
        reply_markup=get_promo_delete_confirm_keyboard(promo_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:promo:delete:\d+$"))
async def callback_promo_delete(callback: CallbackQuery) -> None:
    """Удалить промокод."""
    if not await _check_admin(callback):
        return

    promo_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        success = await user_service.delete_promo_code(promo_id)
        await session.commit()

    if success:
        await callback.answer("Промокод удалён")
        await callback_promo_list(callback)
    else:
        await callback.answer("Ошибка удаления", show_alert=True)


# ==================== НАСТРОЙКИ БОТА ====================

# Названия настроек для отображения
SETTING_NAMES = {
    "star_price_usdt": "Цена 1 звезды (USDT)",
    "min_stars": "Минимум звёзд",
    "max_stars": "Максимум звёзд",
    "premium_price_3m": "Цена Premium 3 мес (USDT)",
    "premium_price_6m": "Цена Premium 6 мес (USDT)",
    "premium_price_12m": "Цена Premium 12 мес (USDT)",
    "star_cost_usdt": "Себестоимость 1 звезды (USDT)",
    "premium_cost_3m": "Себестоимость Premium 3 мес (USDT)",
    "premium_cost_6m": "Себестоимость Premium 6 мес (USDT)",
    "premium_cost_12m": "Себестоимость Premium 12 мес (USDT)",
    "referral_percent_level1": "Реферал уровень 1 (%)",
    "referral_percent_level2": "Реферал уровень 2 (%)",
    "referral_percent_level3": "Реферал уровень 3 (%)",
    "payment_fee_cryptobot": "Комиссия CryptoBot (%)",
    "payment_fee_ton": "Комиссия TON (%)",
    "payment_fee_platega": "Комиссия Platega (%)",
    "payment_fee_lava": "Комиссия Lava (%)",
    "cryptobot_token": "Токен CryptoBot",
    "ton_wallet_address": "Адрес TON кошелька",
    "platega_merchant_id": "Platega MerchantId",
    "platega_secret": "Platega Secret",
    "platega_poll_interval_seconds": "Автопроверка Platega (сек)",
    "lava_shop_id": "Lava Shop ID",
    "lava_secret_key": "Lava Secret Key",
    "lava_additional_key": "Lava Additional Key",
    "lava_poll_interval_seconds": "Автопроверка Lava (сек)",
    "support_username": "Username поддержки",
    "news_channel_url": "Ссылка на новостной канал",
    "required_subscription_channel": "Канал обязательной подписки",
    "required_subscription_url": "Ссылка обязательной подписки",
}


@router.callback_query(F.data == AdminCallback.SETTINGS)
async def callback_settings_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню настроек."""
    if not await _check_admin(callback):
        return

    await state.clear()

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    await callback.message.edit_text(
        text="⚙️ <b>Настройки бота</b>\n\nВыберите раздел:",
        reply_markup=get_settings_menu_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_BACK)
async def callback_settings_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Назад в меню настроек."""
    await callback_settings_menu(callback, state)


@router.callback_query(F.data == AdminCallback.SETTINGS_STARS)
async def callback_settings_stars(callback: CallbackQuery) -> None:
    """Настройки звёзд."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    star_cost = settings.get("star_cost_usdt", "0.015")
    star_price = settings.get("star_price_usdt", "0.02")
    min_stars = settings.get("min_stars", 50)
    max_stars = settings.get("max_stars", 10000)

    await callback.message.edit_text(
        text=(
            "⭐ <b>Настройки звёзд</b>\n\n"
            "<blockquote>"
            f"🏭 Себестоимость: <b>{star_cost} USDT</b>\n"
            f"💵 Цена в боте: <b>{star_price} USDT</b>\n"
            f"📉 Минимум: <b>{min_stars}</b>\n"
            f"📈 Максимум: <b>{max_stars:,}</b>"
            "</blockquote>\n\n"
            "Нажмите на параметр для изменения:"
        ),
        reply_markup=get_settings_stars_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_PREMIUM)
async def callback_settings_premium(callback: CallbackQuery) -> None:
    """Настройки Premium."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    # Prices in bot
    price_3m = settings.get("premium_price_3m", "8.99")
    price_6m = settings.get("premium_price_6m", "15.99")
    price_12m = settings.get("premium_price_12m", "28.99")

    # Costs
    cost_3m = settings.get("premium_cost_3m", "6.00")
    cost_6m = settings.get("premium_cost_6m", "10.00")
    cost_12m = settings.get("premium_cost_12m", "18.00")

    await callback.message.edit_text(
        text=(
            "👑 <b>Настройки Premium</b>\n\n"
            "<blockquote>"
            "<b>📅 3 месяца</b>\n"
            f"🏭 Себестоимость: <b>{cost_3m} USDT</b>\n"
            f"💵 Цена в боте: <b>${price_3m}</b>\n\n"
            "<b>📅 6 месяцев</b>\n"
            f"🏭 Себестоимость: <b>{cost_6m} USDT</b>\n"
            f"💵 Цена в боте: <b>${price_6m}</b>\n\n"
            "<b>📅 12 месяцев</b>\n"
            f"🏭 Себестоимость: <b>{cost_12m} USDT</b>\n"
            f"💵 Цена в боте: <b>${price_12m}</b>"
            "</blockquote>\n\n"
            "Нажмите для изменения:"
        ),
        reply_markup=get_settings_premium_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_PREMIUM_COST)
async def callback_settings_premium_cost(callback: CallbackQuery) -> None:
    """Выбор периода для изменения себестоимости Premium."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    cost_3m = settings.get("premium_cost_3m", "6.00")
    cost_6m = settings.get("premium_cost_6m", "10.00")
    cost_12m = settings.get("premium_cost_12m", "18.00")

    await callback.message.edit_text(
        text=(
            "🏭 <b>Себестоимость Premium</b>\n\n"
            "<blockquote>"
            f"📅 3 месяца: <b>{cost_3m} USDT</b>\n"
            f"📅 6 месяцев: <b>{cost_6m} USDT</b>\n"
            f"📅 12 месяцев: <b>{cost_12m} USDT</b>"
            "</blockquote>\n\n"
            "Выберите период:"
        ),
        reply_markup=get_settings_premium_months_keyboard("cost"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_PREMIUM_PRICE)
async def callback_settings_premium_price(callback: CallbackQuery) -> None:
    """Выбор периода для изменения цены Premium."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    price_3m = settings.get("premium_price_3m", "8.99")
    price_6m = settings.get("premium_price_6m", "15.99")
    price_12m = settings.get("premium_price_12m", "28.99")

    await callback.message.edit_text(
        text=(
            "💵 <b>Цена Premium в боте</b>\n\n"
            "<blockquote>"
            f"📅 3 месяца: <b>${price_3m}</b>\n"
            f"📅 6 месяцев: <b>${price_6m}</b>\n"
            f"📅 12 месяцев: <b>${price_12m}</b>"
            "</blockquote>\n\n"
            "Выберите период:"
        ),
        reply_markup=get_settings_premium_months_keyboard("price"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_COST)
async def callback_settings_cost(callback: CallbackQuery) -> None:
    """Настройки себестоимости."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    star_cost = settings.get("star_cost_usdt", "0.015")
    premium_cost_3m = settings.get("premium_cost_3m", "6.00")
    premium_cost_6m = settings.get("premium_cost_6m", "10.00")
    premium_cost_12m = settings.get("premium_cost_12m", "18.00")

    await callback.message.edit_text(
        text=(
            "🏭 <b>Себестоимость товаров</b>\n\n"
            f"⭐ Звезда: <b>{star_cost} USDT</b>\n"
            f"👑 Premium 3 мес: <b>{premium_cost_3m} USDT</b>\n"
            f"👑 Premium 6 мес: <b>{premium_cost_6m} USDT</b>\n"
            f"👑 Premium 12 мес: <b>{premium_cost_12m} USDT</b>\n\n"
            "Нажмите для изменения:"
        ),
        reply_markup=get_settings_cost_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_REFERRAL)
async def callback_settings_referral(callback: CallbackQuery) -> None:
    """Настройки реферальной системы."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    lvl1 = format_percent(settings.get("referral_percent_level1", "5"))
    lvl2 = format_percent(settings.get("referral_percent_level2", "3"))
    lvl3 = format_percent(settings.get("referral_percent_level3", "1"))

    await callback.message.edit_text(
        text=(
            "👥 <b>Реферальная система</b>\n\n"
            "<blockquote>"
            "📖 <b>Описание уровней:</b>\n"
            "🥇 <b>Уровень 1</b> — ваш прямой реферал\n"
            "🥈 <b>Уровень 2</b> — реферал вашего реферала\n"
            "🥉 <b>Уровень 3</b> — реферал реферала реферала"
            "</blockquote>\n\n"
            "<blockquote>"
            "💰 <b>Текущие проценты:</b>\n"
            f"🥇 Уровень 1: <b>{lvl1}%</b>\n"
            f"🥈 Уровень 2: <b>{lvl2}%</b>\n"
            f"🥉 Уровень 3: <b>{lvl3}%</b>"
            "</blockquote>\n\n"
            "Нажмите на уровень для изменения:"
        ),
        reply_markup=get_settings_referral_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_SUPPORT)
async def callback_settings_support(callback: CallbackQuery) -> None:
    """Настройки поддержки."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    support_username = settings.get("support_username", "support")
    news_channel_url = settings.get("news_channel_url") or "не задана"

    await callback.message.edit_text(
        text=(
            "🔗 <b>Ссылки</b>\n\n"
            "<blockquote>"
            f"📩 Поддержка: <b>@{support_username}</b>\n"
            f"📰 Новостной канал: <b>{news_channel_url}</b>"
            "</blockquote>\n\n"
            "Нажмите кнопку ниже чтобы изменить:"
        ),
        reply_markup=get_settings_support_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_SUBSCRIPTION)
async def callback_settings_subscription(callback: CallbackQuery) -> None:
    """Настройки обязательной подписки."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()
        channels = await _get_subscription_channels(session)

    required_subscription_channel = settings.get("required_subscription_channel") or "не задан"
    required_subscription_url = settings.get("required_subscription_url") or "не задана"

    try:
        bot_info = await callback.bot.get_me()
        bot_username = bot_info.username
    except Exception as e:
        logger.error(f"Failed to get bot username for subscription settings: {e}")
        bot_username = None

    await callback.message.edit_text(
        text=(
            "🔒 <b>ОП — обязательная подписка</b>\n\n"
            "<blockquote>"
            f"Канал для проверки: <b>{required_subscription_channel}</b>\n"
            f"Ссылка для кнопки: <b>{required_subscription_url}</b>"
            "</blockquote>\n\n"
            "Нажмите канал из списка, чтобы сделать его активным для ОП. "
            "Бот должен быть админом канала, иначе Telegram не даст проверить подписку пользователя."
        ),
        reply_markup=get_settings_subscription_keyboard(settings, bot_username, channels),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:subscription:select:"))
async def callback_settings_subscription_select(callback: CallbackQuery) -> None:
    """Выбрать канал обязательной подписки из каналов, где бот уже админ."""
    if not await _check_admin(callback):
        return

    try:
        channel_pk = int(callback.data.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный канал", show_alert=True)
        return

    async with async_session_factory() as session:
        result = await session.execute(
            select(BotChannel).where(
                BotChannel.id == channel_pk,
                BotChannel.is_active == True,
            )
        )
        channel = result.scalar_one_or_none()
        if not channel:
            await callback.answer("Канал не найден или бот уже не админ", show_alert=True)
            return

        service = BotSettingsService(session, admin_id=callback.from_user.id)
        settings = await service.get_settings()
        old_channel = settings.get("required_subscription_channel", "")
        old_url = str(settings.get("required_subscription_url") or "")
        same_channel = str(old_channel) == str(channel.channel_id)
        has_invite_url = "t.me/+" in old_url or "t.me/joinchat/" in old_url

        if same_channel and has_invite_url:
            subscription_url = old_url
        else:
            try:
                subscription_url = await _make_subscription_url(callback.bot, channel)
            except Exception as e:
                logger.error(f"Failed to create subscription invite link for channel {channel.channel_id}: {e}")
                await callback.answer(
                    "Не удалось создать ссылку. Проверьте, что бот админ канала и может приглашать пользователей.",
                    show_alert=True,
                )
                return

        await service.set_setting(
            "required_subscription_channel",
            str(channel.channel_id),
            old_value=old_channel,
        )

        await service.set_setting(
            "required_subscription_url",
            subscription_url,
            old_value=old_url,
        )

        await session.commit()

    invalidate_bot_settings_cache()
    await callback.answer("Канал обязательной подписки выбран")
    await callback_settings_subscription(callback)


@router.callback_query(F.data == AdminCallback.SETTINGS_MEDIA)
async def callback_settings_media(callback: CallbackQuery, state: FSMContext) -> None:
    """Настройки фото пользовательских меню."""
    if not await _check_admin(callback):
        return

    await state.clear()

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    media = get_menu_media(settings)
    await callback.message.edit_text(
        text=(
            "🖼 <b>Медиа меню</b>\n\n"
            "Выберите меню и отправьте фото. Если фото не задано, пользователи увидят обычный текст."
        ),
        reply_markup=get_settings_media_keyboard(media, MENU_MEDIA_ITEMS),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:media:set:"))
async def callback_settings_media_set(callback: CallbackQuery, state: FSMContext) -> None:
    """Начать загрузку фото для меню."""
    if not await _check_admin(callback):
        return

    menu_key = callback.data.rsplit(":", 1)[-1]
    if menu_key not in MENU_MEDIA_ITEMS:
        await callback.answer("Меню не найдено", show_alert=True)
        return

    await state.set_state(AdminStates.settings_waiting_media_photo)
    await state.update_data(
        media_menu_key=menu_key,
        bot_message_id=callback.message.message_id,
        section="media",
    )

    await callback.message.edit_text(
        text=f"Отправьте фото для меню: <b>{MENU_MEDIA_ITEMS[menu_key]}</b>",
        reply_markup=get_settings_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.settings_waiting_media_photo)
async def message_settings_media_photo(message: Message, state: FSMContext) -> None:
    """Сохранить фото для пользовательского меню."""
    if not await check_admin_message(message):
        return

    data = await state.get_data()
    menu_key = data.get("media_menu_key")
    bot_message_id = data.get("bot_message_id")

    try:
        await message.delete()
    except Exception:
        pass

    if menu_key not in MENU_MEDIA_ITEMS:
        await state.clear()
        return

    if not message.photo:
        if bot_message_id:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text="Нужно отправить именно фото.",
                reply_markup=get_settings_cancel_keyboard(),
                parse_mode="HTML",
            )
        return

    photo_file_id = message.photo[-1].file_id

    async with async_session_factory() as session:
        service = BotSettingsService(session, admin_id=message.from_user.id)
        settings = await service.get_settings()
        media = get_menu_media(settings).copy()
        media[menu_key] = photo_file_id
        settings["menu_media"] = media
        await service.save_settings(settings)
        await session.commit()

    invalidate_bot_settings_cache()
    await state.clear()

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    if bot_message_id:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=f"✅ Фото для меню <b>{MENU_MEDIA_ITEMS[menu_key]}</b> сохранено.",
            reply_markup=get_settings_media_keyboard(get_menu_media(settings), MENU_MEDIA_ITEMS),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:settings:media:remove:"))
async def callback_settings_media_remove(callback: CallbackQuery, state: FSMContext) -> None:
    """Удалить фото пользовательского меню."""
    if not await _check_admin(callback):
        return

    menu_key = callback.data.rsplit(":", 1)[-1]
    if menu_key not in MENU_MEDIA_ITEMS:
        await callback.answer("Меню не найдено", show_alert=True)
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session, admin_id=callback.from_user.id)
        settings = await service.get_settings()
        media = get_menu_media(settings).copy()
        media.pop(menu_key, None)
        settings["menu_media"] = media
        await service.save_settings(settings)
        await session.commit()

    invalidate_bot_settings_cache()
    await state.clear()

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    await callback.message.edit_text(
        text=f"Фото для меню <b>{MENU_MEDIA_ITEMS[menu_key]}</b> удалено.",
        reply_markup=get_settings_media_keyboard(get_menu_media(settings), MENU_MEDIA_ITEMS),
        parse_mode="HTML",
    )
    await callback.answer("Удалено")


# Алиас для обратной совместимости
format_percent = _format_percent


@router.callback_query(F.data == AdminCallback.SETTINGS_PAYMENTS)
async def callback_settings_payments(callback: CallbackQuery) -> None:
    """Настройки способов оплаты."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    fee_cryptobot = format_percent(settings.get("payment_fee_cryptobot", "3"))
    fee_ton = format_percent(settings.get("payment_fee_ton", "0"))
    fee_platega = format_percent(settings.get("payment_fee_platega", "8"))
    fee_lava = format_percent(settings.get("payment_fee_lava", "3.4"))
    lava_enabled = str(settings.get("lava_enabled", "false")).lower() in (
        "true", "1", "yes", "on"
    )
    token = settings.get("cryptobot_token", "")
    wallet = settings.get("ton_wallet_address", "")

    # Форматируем отображение токена и кошелька
    if token:
        token_display = token[:10] + "..." + token[-5:] if len(token) > 20 else token
    else:
        token_display = "❌ не установлен"

    if wallet:
        wallet_display = wallet[:10] + "..." + wallet[-6:]
    else:
        wallet_display = "❌ не установлен"

    await callback.message.edit_text(
        text=(
            "💳 <b>Способы оплаты</b>\n\n"
            "<blockquote>"
            "<b>🤖 CryptoBot</b>\n"
            f"🔑 Токен: <b>{token_display}</b>\n"
            f"📊 Комиссия: <b>{fee_cryptobot}%</b>\n\n"
            "<b>💎 TON</b>\n"
            f"📬 Кошелёк: <b>{wallet_display}</b>\n"
            f"📊 Комиссия: <b>{fee_ton}%</b>\n\n"
            "<b>🏦 Platega</b>\n"
            f"📊 Комиссия: <b>{fee_platega}%</b>\n\n"
            "<b>🌋 Lava / СБП</b>\n"
            f"Статус: <b>{'включена' if lava_enabled else 'выключена'}</b>\n"
            f"📊 Комиссия: <b>{fee_lava}%</b>"
            "</blockquote>\n\n"
            "Выберите способ оплаты для настройки:"
        ),
        reply_markup=get_settings_payments_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_PAYMENT_CRYPTOBOT)
async def callback_settings_cryptobot(callback: CallbackQuery) -> None:
    """Настройки CryptoBot."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    token = settings.get("cryptobot_token", "")
    fee = format_percent(settings.get("payment_fee_cryptobot", "3"))

    # Скрываем токен частично
    if token:
        token_display = token[:10] + "..." + token[-5:] if len(token) > 20 else token[:5] + "..."
    else:
        token_display = "не установлен"

    await callback.message.edit_text(
        text=(
            "🤖 <b>Настройки CryptoBot</b>\n\n"
            "<blockquote>"
            f"🔑 Токен: <b>{token_display}</b>\n"
            f"📊 Комиссия: <b>{fee}%</b>"
            "</blockquote>\n\n"
            "Нажмите для изменения:"
        ),
        reply_markup=get_settings_cryptobot_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_PAYMENT_TON)
async def callback_settings_ton(callback: CallbackQuery) -> None:
    """Настройки TON."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    wallet = settings.get("ton_wallet_address", "")
    fee = format_percent(settings.get("payment_fee_ton", "0"))

    # Скрываем адрес частично
    if wallet:
        wallet_display = wallet[:10] + "..." + wallet[-6:]
    else:
        wallet_display = "не установлен"

    await callback.message.edit_text(
        text=(
            "💎 <b>Настройки TON</b>\n\n"
            "<blockquote>"
            f"📬 Адрес: <b>{wallet_display}</b>\n"
            f"📊 Комиссия: <b>{fee}%</b>"
            "</blockquote>\n\n"
            "Нажмите для изменения:"
        ),
        reply_markup=get_settings_ton_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.SETTINGS_PAYMENT_PLATEGA)
async def callback_settings_platega(callback: CallbackQuery) -> None:
    """Настройки Platega."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    enabled = str(settings.get("platega_enabled", "false")).lower() in ("true", "1", "yes", "on")
    merchant = settings.get("platega_merchant_id", "")
    secret = settings.get("platega_secret", "")
    poll = settings.get("platega_poll_interval_seconds", "5")
    fee = format_percent(settings.get("payment_fee_platega", "8"))

    merchant_display = (merchant[:8] + "..." + merchant[-4:]) if len(merchant) > 14 else (merchant if merchant else "не установлен")
    secret_display = (secret[:8] + "..." + secret[-4:]) if len(secret) > 14 else (secret[:4] + "..." if secret else "не установлен")
    status = "включен" if enabled else "выключен"

    await callback.message.edit_text(
        text=(
            "🏦 <b>Настройки Platega</b>\n\n"
            "<blockquote>"
            f"Статус: <b>{status}</b>\n"
            f"MerchantId: <b>{merchant_display}</b>\n"
            f"Secret: <b>{secret_display}</b>\n"
            f"Комиссия: <b>{fee}%</b>\n"
            f"Автопроверка: <b>{poll} сек.</b>\n"
            "Время платежа: <b>30 мин.</b>"
            "</blockquote>\n\n"
            "Нажмите для изменения:"
        ),
        reply_markup=get_settings_platega_keyboard(enabled),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:platega:toggle")
async def callback_settings_platega_toggle(callback: CallbackQuery) -> None:
    """Быстро включить или выключить Platega."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session, admin_id=callback.from_user.id)
        settings = await service.get_settings()
        enabled = str(settings.get("platega_enabled", "false")).lower() in ("true", "1", "yes", "on")
        await service.set_setting("platega_enabled", "false" if enabled else "true", old_value=settings.get("platega_enabled", "false"))
        await session.commit()

    invalidate_bot_settings_cache()
    await callback_settings_platega(callback)


def _mask_lava_shop_id(value: str) -> str:
    if not value:
        return "не установлен"
    return f"{value[:8]}...{value[-4:]}" if len(value) > 14 else value


def _mask_lava_key(value: str) -> str:
    return "установлен" if value else "не установлен"


async def _render_lava_settings(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()

    enabled = str(settings.get("lava_enabled", "false")).lower() in (
        "true", "1", "yes", "on"
    )
    shop_id = settings.get("lava_shop_id", "") or ""
    secret_key = settings.get("lava_secret_key", "") or ""
    additional_key = settings.get("lava_additional_key", "") or ""
    poll = settings.get("lava_poll_interval_seconds", "5")
    fee = format_percent(settings.get("payment_fee_lava", "3.4"))
    await callback.message.edit_text(
        text=(
            "🌋 <b>Настройки Lava / СБП</b>\n\n"
            "<blockquote>"
            f"Статус: <b>{'включена' if enabled else 'выключена'}</b>\n"
            f"Shop ID: <b>{_mask_lava_shop_id(shop_id)}</b>\n"
            f"Secret Key: <b>{_mask_lava_key(secret_key)}</b>\n"
            f"Additional Key: <b>{_mask_lava_key(additional_key)}</b>\n"
            f"Комиссия: <b>{fee}%</b>\n"
            f"Автопроверка: <b>{poll} сек.</b>\n"
            "Метод оплаты: <b>только СБП</b>\n"
            "Время счёта: <b>30 мин.</b>"
            "</blockquote>\n\n"
            "Для включения заполните Shop ID и Secret Key."
        ),
        reply_markup=get_settings_lava_keyboard(enabled),
        parse_mode="HTML",
    )


@router.callback_query(F.data == AdminCallback.SETTINGS_PAYMENT_LAVA)
async def callback_settings_lava(callback: CallbackQuery) -> None:
    """Настройки Lava Business API."""
    if not await _check_admin(callback):
        return
    await _render_lava_settings(callback)
    await callback.answer()


@router.callback_query(F.data == "admin:settings:lava:toggle")
async def callback_settings_lava_toggle(callback: CallbackQuery) -> None:
    """Включить Lava после заполнения обязательных ключей."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()
    enabled = str(settings.get("lava_enabled", "false")).lower() in (
        "true", "1", "yes", "on"
    )

    if not enabled:
        if not settings.get("lava_shop_id") or not settings.get("lava_secret_key"):
            await callback.answer("Сначала заполните Shop ID и Secret Key", show_alert=True)
            return

    async with async_session_factory() as session:
        service = BotSettingsService(session, admin_id=callback.from_user.id)
        await service.set_setting(
            "lava_enabled",
            "false" if enabled else "true",
            old_value=settings.get("lava_enabled", "false"),
        )
        await session.commit()
    invalidate_bot_settings_cache()
    await _render_lava_settings(callback)
    await callback.answer("Lava выключена" if enabled else "Lava включена")


@router.callback_query(F.data.startswith("admin:settings:edit:"))
async def callback_settings_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Начать редактирование настройки."""
    if not await _check_admin(callback):
        return

    setting_key = callback.data.replace("admin:settings:edit:", "")
    setting_name = SETTING_NAMES.get(setting_key, setting_key)

    async with async_session_factory() as session:
        service = BotSettingsService(session)
        settings = await service.get_settings()
        current_value = settings.get(setting_key, "")

    # Определяем секцию для возврата
    if "star" in setting_key or setting_key in ("min_stars", "max_stars"):
        section = "stars"
    elif "premium_cost" in setting_key:
        section = "premium_cost"
    elif "premium_price" in setting_key:
        section = "premium_price"
    elif "premium" in setting_key:
        section = "premium"
    elif "referral" in setting_key:
        section = "referral"
    elif setting_key == "cryptobot_token" or setting_key == "payment_fee_cryptobot":
        section = "cryptobot"
    elif setting_key == "ton_wallet_address" or setting_key == "payment_fee_ton":
        section = "ton"
    elif setting_key.startswith("platega_") or setting_key == "payment_fee_platega":
        section = "platega"
    elif setting_key.startswith("lava_") or setting_key == "payment_fee_lava":
        section = "lava"
    elif setting_key in (
        "support_username",
        "news_channel_url",
    ):
        section = "support"
    elif setting_key in (
        "required_subscription_channel",
        "required_subscription_url",
    ):
        section = "subscription"
    else:
        section = "settings"

    await state.set_state(AdminStates.settings_waiting_value)
    await state.update_data(
        setting_key=setting_key,
        setting_name=setting_name,
        section=section,
        bot_message_id=callback.message.message_id,
    )

    # Формируем текст с блоком текущих значений
    if section == "stars":
        star_cost = settings.get("star_cost_usdt", "0.015")
        star_price = settings.get("star_price_usdt", "0.02")
        min_stars = settings.get("min_stars", 50)
        max_stars = settings.get("max_stars", 10000)
        values_block = (
            "<blockquote>"
            f"🏭 Себестоимость: <b>{star_cost} USDT</b>\n"
            f"💵 Цена в боте: <b>{star_price} USDT</b>\n"
            f"📉 Минимум: <b>{min_stars}</b>\n"
            f"📈 Максимум: <b>{int(max_stars):,}</b>"
            "</blockquote>\n\n"
        )
    elif section == "premium":
        if "cost" in setting_key:
            cost_3m = settings.get("premium_cost_3m", "6.00")
            cost_6m = settings.get("premium_cost_6m", "10.00")
            cost_12m = settings.get("premium_cost_12m", "18.00")
            values_block = (
                "<blockquote>"
                f"📅 3 месяца: <b>{cost_3m} USDT</b>\n"
                f"📅 6 месяцев: <b>{cost_6m} USDT</b>\n"
                f"📅 12 месяцев: <b>{cost_12m} USDT</b>"
                "</blockquote>\n\n"
            )
        else:
            price_3m = settings.get("premium_price_3m", "8.99")
            price_6m = settings.get("premium_price_6m", "15.99")
            price_12m = settings.get("premium_price_12m", "28.99")
            values_block = (
                "<blockquote>"
                f"📅 3 месяца: <b>${price_3m}</b>\n"
                f"📅 6 месяцев: <b>${price_6m}</b>\n"
                f"📅 12 месяцев: <b>${price_12m}</b>"
                "</blockquote>\n\n"
            )
    elif section == "referral":
        lvl1 = format_percent(settings.get("referral_percent_level1", "5"))
        lvl2 = format_percent(settings.get("referral_percent_level2", "3"))
        lvl3 = format_percent(settings.get("referral_percent_level3", "1"))
        values_block = (
            "<blockquote>"
            "📖 <b>Описание уровней:</b>\n"
            "🥇 <b>Уровень 1</b> — ваш прямой реферал\n"
            "🥈 <b>Уровень 2</b> — реферал вашего реферала\n"
            "🥉 <b>Уровень 3</b> — реферал реферала реферала"
            "</blockquote>\n\n"
            "<blockquote>"
            "💰 <b>Текущие проценты:</b>\n"
            f"🥇 Уровень 1: <b>{lvl1}%</b>\n"
            f"🥈 Уровень 2: <b>{lvl2}%</b>\n"
            f"🥉 Уровень 3: <b>{lvl3}%</b>"
            "</blockquote>\n\n"
        )
    elif section == "cryptobot":
        token = settings.get("cryptobot_token", "")
        fee = format_percent(settings.get("payment_fee_cryptobot", "3"))
        token_display = (token[:10] + "..." + token[-5:] if len(token) > 20 else token[:5] + "...") if token else "не установлен"
        values_block = (
            "<blockquote>"
            f"🔑 Токен: <b>{token_display}</b>\n"
            f"📊 Комиссия: <b>{fee}%</b>"
            "</blockquote>\n\n"
        )
    elif section == "ton":
        wallet = settings.get("ton_wallet_address", "")
        fee = format_percent(settings.get("payment_fee_ton", "0"))
        wallet_display = (wallet[:10] + "..." + wallet[-6:]) if wallet else "не установлен"
        values_block = (
            "<blockquote>"
            f"📬 Адрес: <b>{wallet_display}</b>\n"
            f"📊 Комиссия: <b>{fee}%</b>"
            "</blockquote>\n\n"
        )
    elif section == "platega":
        if setting_key == "platega_merchant_id":
            current_display = (
                current_value[:8] + "..." + current_value[-4:]
                if len(str(current_value)) > 14
                else current_value or "не установлен"
            )
        elif setting_key == "platega_secret":
            current_display = (
                current_value[:8] + "..." + current_value[-4:]
                if len(str(current_value)) > 14
                else "***" if current_value else "не установлен"
            )
        elif setting_key == "platega_poll_interval_seconds":
            current_display = f"{current_value or '5'} сек."
        elif setting_key == "payment_fee_platega":
            current_display = f"{format_percent(current_value or '8')}%"
        else:
            current_display = current_value or "не установлено"
        values_block = f"Текущее значение: <b>{current_display}</b>\n\n"
    elif section == "lava":
        if setting_key == "lava_shop_id":
            current_display = _mask_lava_shop_id(str(current_value))
        elif setting_key in ("lava_secret_key", "lava_additional_key"):
            current_display = _mask_lava_key(str(current_value))
        elif setting_key == "lava_poll_interval_seconds":
            current_display = f"{current_value or '5'} сек."
        elif setting_key == "payment_fee_lava":
            current_display = f"{format_percent(current_value or '3.4')}%"
        else:
            current_display = current_value or "не установлено"
        clear_hint = (
            "Чтобы очистить необязательный Additional Key, введите -\n\n"
            if setting_key == "lava_additional_key"
            else ""
        )
        values_block = (
            f"Текущее значение: <b>{current_display}</b>\n\n"
            f"{clear_hint}"
        )
    elif section == "support":
        support_username = settings.get("support_username", "support")
        news_channel_url = settings.get("news_channel_url") or "не задана"
        if setting_key == "news_channel_url":
            prompt = "Введите ссылку на канал, @username или username:"
        else:
            prompt = "Введите username без @:"
        values_block = (
            "<blockquote>"
            f"📩 Поддержка: <b>@{support_username}</b>\n"
            f"📰 Новостной канал: <b>{news_channel_url}</b>"
            "</blockquote>\n\n"
            f"{prompt}"
            "\n\n"
        )
    elif section == "subscription":
        required_subscription_channel = settings.get("required_subscription_channel") or "не задан"
        required_subscription_url = settings.get("required_subscription_url") or "не задана"
        if setting_key == "required_subscription_channel":
            prompt = "Введите @username, username или числовой ID канала. Чтобы отключить, введите -"
        else:
            prompt = "Введите ссылку для кнопки подписки, @username или username. Чтобы очистить, введите -"
        values_block = (
            "<blockquote>"
            f"🔒 Канал для проверки: <b>{required_subscription_channel}</b>\n"
            f"🔗 Ссылка для кнопки: <b>{required_subscription_url}</b>"
            "</blockquote>\n\n"
            f"{prompt}"
            "\n\n"
        )
    else:
        values_block = f"Текущее значение: <code>{current_value}</code>\n\n"

    await callback.message.edit_text(
        text=(
            f"✏️ <b>Изменение: {setting_name}</b>\n\n"
            f"{values_block}"
            "Введите новое значение:"
        ),
        reply_markup=get_settings_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.settings_waiting_value)
async def process_settings_value(message: Message, state: FSMContext) -> None:
    """Обработка нового значения настройки."""
    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    setting_key = data.get("setting_key")
    setting_name = data.get("setting_name")
    bot_message_id = data.get("bot_message_id")

    new_value = message.text.strip()

    # Получаем текущие настройки для бизнес-валидации
    async with async_session_factory() as session:
        service = BotSettingsService(session)
        current_settings = await service.get_settings()

    # Валидация в зависимости от типа настройки
    is_valid = True
    error_msg = ""

    if setting_key in ("min_stars", "max_stars"):
        try:
            int_val = int(new_value)
            if int_val <= 0:
                raise ValueError("Значение должно быть положительным")

            # Бизнес-валидация: min_stars < max_stars
            if setting_key == "min_stars":
                max_stars = int(current_settings.get("max_stars", 10000))
                if int_val >= max_stars:
                    raise ValueError(f"Минимум должен быть меньше максимума ({max_stars})")
            elif setting_key == "max_stars":
                min_stars = int(current_settings.get("min_stars", 50))
                if int_val <= min_stars:
                    raise ValueError(f"Максимум должен быть больше минимума ({min_stars})")

            new_value = str(int_val)
        except ValueError as e:
            is_valid = False
            error_msg = str(e) if str(e) else "Введите целое положительное число"

    elif setting_key.startswith("referral_percent"):
        try:
            float_val = float(new_value.replace(",", "."))
            if float_val < 0 or float_val > 100:
                raise ValueError("Введите число от 0 до 100")

            # Бизнес-валидация: сумма реферальных процентов ≤ 100%
            lvl1 = float(current_settings.get("referral_percent_level1", "5"))
            lvl2 = float(current_settings.get("referral_percent_level2", "3"))
            lvl3 = float(current_settings.get("referral_percent_level3", "1"))

            # Обновляем нужный уровень
            if setting_key == "referral_percent_level1":
                lvl1 = float_val
            elif setting_key == "referral_percent_level2":
                lvl2 = float_val
            elif setting_key == "referral_percent_level3":
                lvl3 = float_val

            total = lvl1 + lvl2 + lvl3
            if total > 100:
                raise ValueError(f"Сумма процентов ({total:.1f}%) превышает 100%")

            new_value = str(float_val)
        except ValueError as e:
            is_valid = False
            error_msg = str(e) if str(e) else "Введите число от 0 до 100"

    elif setting_key.startswith("payment_fee"):
        try:
            float_val = float(new_value.replace(",", "."))
            if float_val < 0 or float_val > 100:
                raise ValueError()
            new_value = str(float_val)
        except ValueError:
            is_valid = False
            error_msg = "Введите число от 0 до 100"

    elif "price" in setting_key or "cost" in setting_key:
        try:
            float_val = float(new_value.replace(",", "."))
            if float_val <= 0:
                raise ValueError("Значение должно быть больше 0")

            # Бизнес-валидация: price > cost
            if "star_" in setting_key:
                # Для звёзд
                if setting_key == "star_price_usdt":
                    cost = float(current_settings.get("star_cost_usdt", "0.015"))
                    if float_val <= cost:
                        raise ValueError(f"Цена должна быть больше себестоимости ({cost})")
                elif setting_key == "star_cost_usdt":
                    price = float(current_settings.get("star_price_usdt", "0.02"))
                    if float_val >= price:
                        raise ValueError(f"Себестоимость должна быть меньше цены ({price})")
                new_value = f"{float_val:.4f}".rstrip('0').rstrip('.')
                if '.' not in new_value:
                    new_value += ".0"
            else:
                # Для Premium
                period_map = {
                    "premium_price_3m": "premium_cost_3m",
                    "premium_price_6m": "premium_cost_6m",
                    "premium_price_12m": "premium_cost_12m",
                    "premium_cost_3m": "premium_price_3m",
                    "premium_cost_6m": "premium_price_6m",
                    "premium_cost_12m": "premium_price_12m",
                }
                if setting_key in period_map:
                    related_key = period_map[setting_key]
                    related_val = float(current_settings.get(related_key, "0"))
                    if "price" in setting_key:
                        if float_val <= related_val:
                            raise ValueError(f"Цена должна быть больше себестоимости ({related_val})")
                    else:
                        if float_val >= related_val:
                            raise ValueError(f"Себестоимость должна быть меньше цены ({related_val})")
                new_value = f"{float_val:.2f}"
        except ValueError as e:
            is_valid = False
            error_msg = str(e) if str(e) else "Введите положительное число"

    elif setting_key == "cryptobot_token":
        # Токен CryptoBot - формат: число:строка (минимум 10 символов после :)
        if not new_value:
            is_valid = False
            error_msg = "Токен не может быть пустым"
        elif not re.match(r'^\d+:[A-Za-z0-9_-]{10,}$', new_value):
            is_valid = False
            error_msg = "Неверный формат токена. Формат: 123456:ABCdef..."

    elif setting_key == "ton_wallet_address":
        # TON адрес - должен начинаться с EQ или UQ и иметь длину 48 символов (base64)
        if not new_value:
            is_valid = False
            error_msg = "Адрес не может быть пустым"
        elif not re.match(r'^(EQ|UQ)[A-Za-z0-9_-]{46}$', new_value):
            is_valid = False
            error_msg = "Неверный формат адреса. Адрес должен начинаться с EQ или UQ и содержать 48 символов"

    elif setting_key in ("platega_merchant_id", "platega_secret"):
        if not new_value:
            is_valid = False
            error_msg = "Значение не может быть пустым"

    elif setting_key == "lava_shop_id":
        try:
            UUID(new_value)
            new_value = new_value.lower()
        except (ValueError, AttributeError):
            is_valid = False
            error_msg = "Shop ID должен быть корректным UUID из кабинета Lava"

    elif setting_key == "lava_secret_key":
        if not new_value:
            is_valid = False
            error_msg = "Secret Key не может быть пустым"

    elif setting_key == "lava_additional_key":
        if new_value == "-":
            new_value = ""

    elif setting_key == "lava_poll_interval_seconds":
        try:
            interval = int(new_value)
            if interval < 3 or interval > 60:
                raise ValueError
            new_value = str(interval)
        except ValueError:
            is_valid = False
            error_msg = "Интервал должен быть от 3 до 60 секунд"

    elif setting_key == "platega_poll_interval_seconds":
        try:
            interval = int(new_value)
            if interval < 3 or interval > 60:
                raise ValueError
            new_value = str(interval)
        except ValueError:
            is_valid = False
            error_msg = "Интервал должен быть от 3 до 60 секунд"

    elif setting_key == "support_username":
        # Убираем @ если есть
        new_value = new_value.lstrip("@")
        # Проверка формата username
        if not new_value or len(new_value) < 3 or len(new_value) > 32:
            is_valid = False
            error_msg = "Username должен быть от 3 до 32 символов"
        elif not new_value.replace("_", "").isalnum():
            is_valid = False
            error_msg = "Username может содержать только буквы, цифры и _"

    elif setting_key == "news_channel_url":
        new_value = new_value.strip()
        if new_value.startswith("@"):
            username = new_value[1:]
            if not username or not username.replace("_", "").isalnum():
                is_valid = False
                error_msg = "Username канала может содержать только буквы, цифры и _"
            else:
                new_value = f"https://t.me/{username}"
        elif re.match(r"^[A-Za-z0-9_]{3,32}$", new_value):
            new_value = f"https://t.me/{new_value}"
        elif not re.match(r"^https?://(t\.me|telegram\.me)/[A-Za-z0-9_+/-]+$", new_value):
            is_valid = False
            error_msg = "Введите ссылку вида https://t.me/channel или @channel"

    elif setting_key == "required_subscription_channel":
        new_value = new_value.strip()
        if new_value in ("", "-", "нет", "off", "disable"):
            new_value = ""
        elif re.match(r"^-?\d+$", new_value):
            pass
        elif new_value.startswith("@"):
            username = new_value[1:]
            if not re.match(r"^[A-Za-z0-9_]{5,32}$", username):
                is_valid = False
                error_msg = "Введите @username канала от 5 до 32 символов или числовой ID"
            else:
                new_value = f"@{username}"
        elif re.match(r"^[A-Za-z0-9_]{5,32}$", new_value):
            new_value = f"@{new_value}"
        else:
            match = re.match(r"^https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})/?$", new_value)
            if match:
                new_value = f"@{match.group(1)}"
            else:
                is_valid = False
                error_msg = "Для проверки подписки нужен @username, username или числовой ID канала"

    elif setting_key == "required_subscription_url":
        new_value = new_value.strip()
        if new_value in ("", "-", "нет", "off", "disable"):
            new_value = ""
        elif new_value.startswith("@"):
            username = new_value[1:]
            if not re.match(r"^[A-Za-z0-9_]{5,32}$", username):
                is_valid = False
                error_msg = "Username канала может содержать только буквы, цифры и _"
            else:
                new_value = f"https://t.me/{username}"
        elif re.match(r"^[A-Za-z0-9_]{5,32}$", new_value):
            new_value = f"https://t.me/{new_value}"
        elif not re.match(r"^https?://(t\.me|telegram\.me)/[A-Za-z0-9_+/-]+$", new_value):
            is_valid = False
            error_msg = "Введите ссылку вида https://t.me/channel, invite-ссылку или @channel"

    if not is_valid:
        if bot_message_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=bot_message_id,
                    text=(
                        f"✏️ <b>Изменение: {setting_name}</b>\n\n"
                        f"{error_msg}\n\n"
                        "Введите новое значение:"
                    ),
                    reply_markup=get_settings_cancel_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    # Сохраняем новое значение с аудит-логированием
    admin_id = message.from_user.id
    old_value = current_settings.get(setting_key, "")
    async with async_session_factory() as session:
        service = BotSettingsService(session, admin_id=admin_id)
        success = await service.set_setting(setting_key, new_value, old_value=old_value)
        if success and setting_key in ("lava_shop_id", "lava_secret_key"):
            await service.set_setting(
                "lava_enabled",
                "false",
                old_value=current_settings.get("lava_enabled", "false"),
            )
        await session.commit()

    # Сбрасываем кэш настроек
    invalidate_bot_settings_cache()

    await state.clear()

    if success:
        # Получаем обновленные настройки
        async with async_session_factory() as session:
            service = BotSettingsService(session)
            settings = await service.get_settings()

        # Определяем, к какому разделу вернуться
        if "star" in setting_key or setting_key in ("min_stars", "max_stars"):
            # Возвращаемся к настройкам звёзд
            star_cost = settings.get("star_cost_usdt", "0.015")
            star_price = settings.get("star_price_usdt", "0.02")
            min_stars = settings.get("min_stars", 50)
            max_stars = settings.get("max_stars", 10000)

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "⭐ <b>Настройки звёзд</b>\n\n"
                            f"✅ {setting_name} изменено на <b>{new_value}</b>\n\n"
                            "<blockquote>"
                            f"🏭 Себестоимость: <b>{star_cost} USDT</b>\n"
                            f"💵 Цена в боте: <b>{star_price} USDT</b>\n"
                            f"📉 Минимум: <b>{min_stars}</b>\n"
                            f"📈 Максимум: <b>{int(max_stars):,}</b>"
                            "</blockquote>\n\n"
                            "Нажмите на параметр для изменения:"
                        ),
                        reply_markup=get_settings_stars_keyboard(settings),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif "premium" in setting_key:
            # Возвращаемся к настройкам Premium
            price_3m = settings.get("premium_price_3m", "8.99")
            price_6m = settings.get("premium_price_6m", "15.99")
            price_12m = settings.get("premium_price_12m", "28.99")
            cost_3m = settings.get("premium_cost_3m", "6.00")
            cost_6m = settings.get("premium_cost_6m", "10.00")
            cost_12m = settings.get("premium_cost_12m", "18.00")

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "👑 <b>Настройки Premium</b>\n\n"
                            f"✅ {setting_name} изменено на <b>{new_value}</b>\n\n"
                            "<blockquote>"
                            "<b>📅 3 месяца</b>\n"
                            f"🏭 Себестоимость: <b>{cost_3m} USDT</b>\n"
                            f"💵 Цена в боте: <b>${price_3m}</b>\n\n"
                            "<b>📅 6 месяцев</b>\n"
                            f"🏭 Себестоимость: <b>{cost_6m} USDT</b>\n"
                            f"💵 Цена в боте: <b>${price_6m}</b>\n\n"
                            "<b>📅 12 месяцев</b>\n"
                            f"🏭 Себестоимость: <b>{cost_12m} USDT</b>\n"
                            f"💵 Цена в боте: <b>${price_12m}</b>"
                            "</blockquote>\n\n"
                            "Нажмите для изменения:"
                        ),
                        reply_markup=get_settings_premium_keyboard(settings),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif "referral" in setting_key:
            lvl1 = format_percent(settings.get("referral_percent_level1", "5"))
            lvl2 = format_percent(settings.get("referral_percent_level2", "3"))
            lvl3 = format_percent(settings.get("referral_percent_level3", "1"))

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "👥 <b>Реферальная система</b>\n\n"
                            f"✅ {setting_name} изменено на <b>{format_percent(new_value)}%</b>\n\n"
                            "<blockquote>"
                            "📖 <b>Описание уровней:</b>\n"
                            "🥇 <b>Уровень 1</b> — ваш прямой реферал\n"
                            "🥈 <b>Уровень 2</b> — реферал вашего реферала\n"
                            "🥉 <b>Уровень 3</b> — реферал реферала реферала"
                            "</blockquote>\n\n"
                            "<blockquote>"
                            "💰 <b>Текущие проценты:</b>\n"
                            f"🥇 Уровень 1: <b>{lvl1}%</b>\n"
                            f"🥈 Уровень 2: <b>{lvl2}%</b>\n"
                            f"🥉 Уровень 3: <b>{lvl3}%</b>"
                            "</blockquote>\n\n"
                            "Нажмите на уровень для изменения:"
                        ),
                        reply_markup=get_settings_referral_keyboard(settings),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif setting_key == "cryptobot_token" or setting_key == "payment_fee_cryptobot":
            token = settings.get("cryptobot_token", "")
            fee = format_percent(settings.get("payment_fee_cryptobot", "3"))
            token_display = (token[:10] + "..." + token[-5:] if len(token) > 20 else token[:5] + "...") if token else "не установлен"

            # Формируем отображение нового значения
            if setting_key == "cryptobot_token":
                new_display = new_value[:10] + "..." + new_value[-5:] if len(new_value) > 20 else new_value[:5] + "..."
            else:
                new_display = f"{format_percent(new_value)}%"

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "🤖 <b>Настройки CryptoBot</b>\n\n"
                            f"✅ {setting_name} изменено на <b>{new_display}</b>\n\n"
                            "<blockquote>"
                            f"🔑 Токен: <b>{token_display}</b>\n"
                            f"📊 Комиссия: <b>{fee}%</b>"
                            "</blockquote>\n\n"
                            "Нажмите для изменения:"
                        ),
                        reply_markup=get_settings_cryptobot_keyboard(),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif setting_key == "ton_wallet_address" or setting_key == "payment_fee_ton":
            wallet = settings.get("ton_wallet_address", "")
            fee = format_percent(settings.get("payment_fee_ton", "0"))
            wallet_display = (wallet[:10] + "..." + wallet[-6:]) if wallet else "не установлен"

            # Формируем отображение нового значения
            if setting_key == "ton_wallet_address":
                new_display = new_value[:10] + "..." + new_value[-6:]
            else:
                new_display = f"{format_percent(new_value)}%"

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "💎 <b>Настройки TON</b>\n\n"
                            f"✅ {setting_name} изменено на <b>{new_display}</b>\n\n"
                            "<blockquote>"
                            f"📬 Адрес: <b>{wallet_display}</b>\n"
                            f"📊 Комиссия: <b>{fee}%</b>"
                            "</blockquote>\n\n"
                            "Нажмите для изменения:"
                        ),
                        reply_markup=get_settings_ton_keyboard(),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif setting_key.startswith("platega_") or setting_key == "payment_fee_platega":
            enabled = str(settings.get("platega_enabled", "false")).lower() in ("true", "1", "yes", "on")
            merchant = settings.get("platega_merchant_id", "")
            secret = settings.get("platega_secret", "")
            poll = settings.get("platega_poll_interval_seconds", "5")
            fee = format_percent(settings.get("payment_fee_platega", "8"))
            merchant_display = (merchant[:8] + "..." + merchant[-4:]) if len(merchant) > 14 else (merchant or "не установлен")
            secret_display = (secret[:8] + "..." + secret[-4:]) if len(secret) > 14 else ("***" if secret else "не установлен")

            if setting_key == "platega_merchant_id":
                new_display = merchant_display
            elif setting_key == "platega_secret":
                new_display = secret_display
            elif setting_key == "platega_poll_interval_seconds":
                new_display = f"{new_value} сек."
            elif setting_key == "payment_fee_platega":
                new_display = f"{format_percent(new_value)}%"
            else:
                new_display = new_value

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "🏦 <b>Настройки Platega</b>\n\n"
                            f"✅ {setting_name} изменено на <b>{new_display}</b>\n\n"
                            "<blockquote>"
                            f"Статус: <b>{'включен' if enabled else 'выключен'}</b>\n"
                            f"MerchantId: <b>{merchant_display}</b>\n"
                            f"Secret: <b>{secret_display}</b>\n"
                            f"Комиссия: <b>{fee}%</b>\n"
                            f"Автопроверка: <b>{poll} сек.</b>\n"
                            "Время платежа: <b>30 мин.</b>"
                            "</blockquote>\n\n"
                            "Нажмите кнопку ниже, чтобы изменить настройку:"
                        ),
                        reply_markup=get_settings_platega_keyboard(enabled),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif setting_key.startswith("lava_") or setting_key == "payment_fee_lava":
            enabled = str(settings.get("lava_enabled", "false")).lower() in (
                "true", "1", "yes", "on"
            )
            shop_id = settings.get("lava_shop_id", "") or ""
            secret_key = settings.get("lava_secret_key", "") or ""
            additional_key = settings.get("lava_additional_key", "") or ""
            poll = settings.get("lava_poll_interval_seconds", "5")
            fee = format_percent(settings.get("payment_fee_lava", "3.4"))

            if setting_key == "lava_shop_id":
                new_display = _mask_lava_shop_id(shop_id)
            elif setting_key in ("lava_secret_key", "lava_additional_key"):
                new_display = _mask_lava_key(new_value)
            elif setting_key == "lava_poll_interval_seconds":
                new_display = f"{new_value} сек."
            elif setting_key == "payment_fee_lava":
                new_display = f"{format_percent(new_value)}%"
            else:
                new_display = new_value

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "🌋 <b>Настройки Lava / СБП</b>\n\n"
                            f"✅ {setting_name} изменено на <b>{new_display}</b>\n\n"
                            "<blockquote>"
                            f"Статус: <b>{'включена' if enabled else 'выключена'}</b>\n"
                            f"Shop ID: <b>{_mask_lava_shop_id(shop_id)}</b>\n"
                            f"Secret Key: <b>{_mask_lava_key(secret_key)}</b>\n"
                            f"Additional Key: <b>{_mask_lava_key(additional_key)}</b>\n"
                            f"Комиссия: <b>{fee}%</b>\n"
                            f"Автопроверка: <b>{poll} сек.</b>\n"
                            "Метод оплаты: <b>только СБП</b>\n"
                            "Время счёта: <b>30 мин.</b>"
                            "</blockquote>\n\n"
                            "После изменения Shop ID или Secret Key Lava нужно включить заново."
                        ),
                        reply_markup=get_settings_lava_keyboard(enabled),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif setting_key in (
            "support_username",
            "news_channel_url",
        ):
            support_username = settings.get("support_username", "support")
            news_channel_url = settings.get("news_channel_url") or "не задана"
            if setting_key == "support_username":
                success_text = f"✅ Username поддержки изменён на <b>@{new_value}</b>"
            else:
                success_text = f"✅ Новостной канал изменён на <b>{new_value}</b>"

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "🔗 <b>Ссылки</b>\n\n"
                            f"{success_text}\n\n"
                            "<blockquote>"
                            f"📩 Поддержка: <b>@{support_username}</b>\n"
                            f"📰 Новостной канал: <b>{news_channel_url}</b>"
                            "</blockquote>\n\n"
                            "Нажмите кнопку ниже чтобы изменить:"
                        ),
                        reply_markup=get_settings_support_keyboard(settings),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif setting_key in (
            "required_subscription_channel",
            "required_subscription_url",
        ):
            required_subscription_channel = settings.get("required_subscription_channel") or "не задан"
            required_subscription_url = settings.get("required_subscription_url") or "не задана"
            if setting_key == "required_subscription_channel":
                success_text = f"✅ Канал обязательной подписки изменён на <b>{new_value or 'отключено'}</b>"
            else:
                success_text = f"✅ Ссылка обязательной подписки изменена на <b>{new_value or 'очищена'}</b>"

            try:
                bot_info = await message.bot.get_me()
                bot_username = bot_info.username
            except Exception as e:
                logger.error(f"Failed to get bot username for subscription settings: {e}")
                bot_username = None

            async with async_session_factory() as session:
                channels = await _get_subscription_channels(session)

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=(
                            "🔒 <b>ОП — обязательная подписка</b>\n\n"
                            f"{success_text}\n\n"
                            "<blockquote>"
                            f"Канал для проверки: <b>{required_subscription_channel}</b>\n"
                            f"Ссылка для кнопки: <b>{required_subscription_url}</b>"
                            "</blockquote>\n\n"
                            "Нажмите канал из списка, чтобы сделать его активным для ОП. "
                            "Бот должен быть админом канала, иначе Telegram не даст проверить подписку пользователя."
                        ),
                        reply_markup=get_settings_subscription_keyboard(settings, bot_username, channels),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    logger.info(f"Admin changed setting {setting_key} to {new_value}")


@router.callback_query(F.data == AdminCallback.SETTINGS_CANCEL)
async def callback_settings_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена редактирования настройки."""
    data = await state.get_data()
    section = data.get("section", "settings")
    await state.clear()

    # Возврат в предыдущее меню в зависимости от секции
    if section == "stars":
        await callback_settings_stars(callback)
    elif section == "premium_cost":
        await callback_settings_premium_cost(callback)
    elif section == "premium_price":
        await callback_settings_premium_price(callback)
    elif section == "premium":
        await callback_settings_premium(callback)
    elif section == "referral":
        await callback_settings_referral(callback)
    elif section == "support":
        await callback_settings_support(callback)
    elif section == "subscription":
        await callback_settings_subscription(callback)
    elif section == "media":
        await callback_settings_media(callback, state)
    elif section == "cryptobot":
        await callback_settings_cryptobot(callback)
    elif section == "ton":
        await callback_settings_ton(callback)
    elif section == "platega":
        await callback_settings_platega(callback)
    elif section == "lava":
        await callback_settings_lava(callback)
    else:
        await callback_settings_menu(callback, state)


# ==================== УТИЛИТЫ ====================

# Алиас для обратной совместимости
_check_admin = check_admin
