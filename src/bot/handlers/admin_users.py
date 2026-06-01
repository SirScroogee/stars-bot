"""
Handlers для управления пользователями в админ-панели.

Функционал:
- Поиск пользователя по ID/username
- Просмотр информации о пользователе
- Бан/разбан
- Назначение/снятие прав админа
- Просмотр заказов пользователя
- Управление балансами
- Управление рефералами
- Отправка сообщений пользователю
"""
import logging
from datetime import datetime
from decimal import Decimal

from aiogram import F, Router, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload

from src.bot.handlers.admin_utils import (
    check_admin,
    check_admin_message,
    to_moscow_time,
)
from src.bot.keyboards.admin import (
    AdminCallback,
    get_admin_menu_keyboard,
    get_users_menu_keyboard,
    get_users_cancel_keyboard,
    get_user_detail_keyboard,
    get_users_recent_keyboard,
    get_user_orders_keyboard,
    get_user_balances_keyboard,
    get_user_balance_cancel_keyboard,
    get_user_balance_confirm_keyboard,
    get_user_referrals_keyboard,
    get_user_referrals_cancel_keyboard,
    get_user_referrals_confirm_keyboard,
    get_user_referrals_list_keyboard,
    get_user_message_cancel_keyboard,
    get_user_message_confirm_keyboard,
    get_user_message_back_keyboard,
)
from src.bot.middlewares.ban_check import invalidate_ban_cache
from src.db.models import User, Order, BalanceLedger
from src.db.session import async_session_factory
from src.services.user_service import UserService
from src.services.telegram_logger import tg_logger

logger = logging.getLogger(__name__)

router = Router(name="admin_users")

# Алиасы для обратной совместимости
_check_admin = check_admin
_check_admin_message = check_admin_message
_to_moscow_time = to_moscow_time


class UsersStates(StatesGroup):
    """Состояния для управления пользователями."""
    searching = State()  # Ожидание ввода ID/username для поиска
    balance_input = State()  # Ожидание ввода нового значения баланса
    referrer_input = State()  # Ожидание ввода нового реферера
    message_input = State()  # Ожидание ввода сообщения
    message_confirm = State()  # Подтверждение отправки сообщения


async def _update_user_from_telegram(bot: Bot, user_id: int) -> tuple[User | None, bool]:
    """
    Обновить информацию о пользователе из Telegram API.
    Получает актуальный username и сохраняет в БД.

    Returns:
        tuple: (user, bot_blocked) - пользователь и флаг блокировки бота
    """
    try:
        # Получаем актуальную информацию из Telegram
        chat = await bot.get_chat(user_id)

        async with async_session_factory() as session:
            user_service = UserService(session)
            user = await user_service.get_user(user_id)

            if not user:
                return None, False

            # Обновляем username если изменился
            if chat.username != user.username:
                logger.info(f"User {user_id} username changed: {user.username} -> {chat.username}")
                user.username = chat.username
                await session.commit()
                await session.refresh(user)

            return user, False  # Бот не заблокирован

    except Exception as e:
        # Если не удалось получить данные - бот заблокирован пользователем
        logger.debug(f"Could not update user {user_id} from Telegram: {e}")

        # Возвращаем данные из БД
        async with async_session_factory() as session:
            user_service = UserService(session)
            user = await user_service.get_user(user_id)
            return user, True  # Бот заблокирован


async def _get_total_users() -> int:
    """Получить общее количество пользователей."""
    async with async_session_factory() as session:
        result = await session.execute(select(func.count(User.id)))
        return result.scalar() or 0


async def _get_recent_users(limit: int = 50) -> list[User]:
    """Получить последних зарегистрированных пользователей."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).order_by(desc(User.created_at)).limit(limit)
        )
        return list(result.scalars().all())


async def _get_user_stats(user_id: int, user_referral_code: str) -> dict:
    """Получить статистику пользователя."""
    async with async_session_factory() as session:
        # Количество заказов
        orders_result = await session.execute(
            select(func.count(Order.id)).where(Order.user_id == user_id)
        )
        total_orders = orders_result.scalar() or 0

        # Успешные заказы
        success_result = await session.execute(
            select(func.count(Order.id)).where(
                Order.user_id == user_id,
                Order.status == "completed"
            )
        )
        success_orders = success_result.scalar() or 0

        # Сумма звёзд (только для product_type="stars")
        stars_result = await session.execute(
            select(func.sum(Order.quantity)).where(
                Order.user_id == user_id,
                Order.status == "completed",
                Order.product_type == "stars"
            )
        )
        total_stars = stars_result.scalar() or 0

        # Потрачено на звёзды
        stars_spent_result = await session.execute(
            select(func.sum(Order.price_usdt)).where(
                Order.user_id == user_id,
                Order.status == "completed",
                Order.product_type == "stars"
            )
        )
        stars_spent = stars_spent_result.scalar() or 0

        # Сумма месяцев Premium
        premium_result = await session.execute(
            select(func.sum(Order.quantity)).where(
                Order.user_id == user_id,
                Order.status == "completed",
                Order.product_type == "premium"
            )
        )
        total_premium = premium_result.scalar() or 0

        # Потрачено на Premium
        premium_spent_result = await session.execute(
            select(func.sum(Order.price_usdt)).where(
                Order.user_id == user_id,
                Order.status == "completed",
                Order.product_type == "premium"
            )
        )
        premium_spent = premium_spent_result.scalar() or 0

        # Сумма потрачено всего
        total_spent = (float(stars_spent) if stars_spent else 0) + (float(premium_spent) if premium_spent else 0)

        # Статистика рефералов
        # Количество рефералов (пользователи, которые использовали реф. код этого пользователя)
        referrals_count_result = await session.execute(
            select(func.count(User.id)).where(User.referrer_code == user_referral_code)
        )
        referrals_count = referrals_count_result.scalar() or 0

        # Сколько потратили рефералы
        referrals_spent_result = await session.execute(
            select(func.sum(Order.price_usdt)).where(
                Order.user_id.in_(
                    select(User.id).where(User.referrer_code == user_referral_code)
                ),
                Order.status == "completed"
            )
        )
        referrals_spent = referrals_spent_result.scalar() or 0

        return {
            "total_orders": total_orders,
            "success_orders": success_orders,
            "total_stars": total_stars,
            "stars_spent": float(stars_spent) if stars_spent else 0,
            "total_premium": total_premium,
            "premium_spent": float(premium_spent) if premium_spent else 0,
            "total_spent": total_spent,
            "referrals_count": referrals_count,
            "referrals_spent": float(referrals_spent) if referrals_spent else 0,
        }


async def _get_user_orders(user_id: int, limit: int = 50) -> list[Order]:
    """Получить заказы пользователя."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(desc(Order.created_at))
            .limit(limit)
        )
        return list(result.scalars().all())


def _format_user_info(user: User, stats: dict, bot_blocked: bool = False) -> str:
    """Форматировать информацию о пользователе."""
    # Объединённый статус (приоритет: забанен > бот заблокирован > админ > активен)
    if user.is_banned:
        status = "🚫 Заблокирован"
    elif bot_blocked:
        status = "🤖 Бот заблокирован"
    elif user.is_admin:
        status = "👑 Админ"
    else:
        status = "✅ Активен"

    username = f"@{user.username}" if user.username else "—"

    # Даты
    created = user.created_at.strftime("%d.%m.%Y %H:%M") if user.created_at else "—"
    updated = user.updated_at.strftime("%d.%m.%Y %H:%M") if user.updated_at else "—"

    # Статистика заказов
    success_rate = 0
    if stats["total_orders"] > 0:
        success_rate = stats["success_orders"] / stats["total_orders"] * 100

    text = "👤 <b>Информация о пользователе</b>\n\n"

    # Основная информация
    # Маппинг языков
    languages = {"ru": "Русский", "en": "English"}
    lang_display = languages.get(user.language_code, user.language_code)

    text += "<blockquote>📋 <b>Основное</b>\n"
    text += f"🆔 ID: <b><code>{user.id}</code></b>\n"
    text += f"👤 Username: <b>{username}</b>\n"
    text += f"📌 Статус: <b>{status}</b>\n"
    text += f"🌐 Язык: <b>{lang_display}</b></blockquote>\n\n"

    # Балансы
    text += "<blockquote>💰 <b>Балансы</b>\n"
    text += f"💵 <b>{float(user.balance_usdt):.2f}</b> USDT\n"
    text += f"⭐ <b>{user.balance_stars:.0f}</b> звёзд\n"
    text += f"👑 <b>{user.balance_premium_months}</b> мес. Premium\n"
    text += f"🎁 <b>{float(user.referral_balance):.2f}</b> USDT реф.</blockquote>\n\n"

    # Реферальная система
    text += "<blockquote>👥 <b>Рефералы</b>\n"
    text += f"🎫 Код: <b><code>{user.referral_code or '—'}</code></b>\n"
    text += f"👤 Приглашено: <b>{stats['referrals_count']}</b>\n"
    text += f"💵 Потратили: <b>{stats['referrals_spent']:.2f}</b> USDT\n"
    text += f"💰 Заработано: <b>{float(user.total_referral_earnings):.2f}</b> USDT</blockquote>\n\n"

    # Статистика заказов
    text += "<blockquote>📊 <b>Заказы</b>\n"
    if stats["total_orders"] == 0:
        text += "Заказов нет</blockquote>\n\n"
    else:
        text += f"📦 Всего: <b>{stats['total_orders']}</b>\n"
        text += f"✅ Успешных: <b>{stats['success_orders']}</b> ({success_rate:.0f}%)\n"
        text += f"⭐ Куплено: <b>{stats['total_stars']:,}</b> звёзд ({stats['stars_spent']:.2f} USDT)\n"
        text += f"👑 Куплено: <b>{stats['total_premium']}</b> мес. Premium ({stats['premium_spent']:.2f} USDT)\n"
        text += f"💵 Потрачено: <b>{stats['total_spent']:.2f}</b> USDT</blockquote>\n\n"

    # Даты
    text += "<blockquote>📅 <b>Даты</b>\n"
    text += f"📥 Регистрация: <b>{created}</b>\n"
    text += f"🔄 Обновление: <b>{updated}</b></blockquote>"

    return text


# ==================== ГЛАВНОЕ МЕНЮ ПОЛЬЗОВАТЕЛЕЙ ====================


@router.callback_query(F.data == AdminCallback.MANAGE_USERS)
async def callback_users_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню управления пользователями."""
    if not await _check_admin(callback):
        return

    await state.clear()

    total_users = await _get_total_users()

    text = (
        "👥 <b>Управление пользователями</b>\n\n"
        f"Всего пользователей: <b>{total_users:,}</b>\n\n"
        "Выберите действие:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_users_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:users:nop")
async def callback_users_nop(callback: CallbackQuery) -> None:
    """Пустой callback."""
    await callback.answer()


# ==================== ПОИСК ПОЛЬЗОВАТЕЛЯ ====================


@router.callback_query(F.data == AdminCallback.USERS_SEARCH)
async def callback_users_search(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало поиска пользователя."""
    if not await _check_admin(callback):
        return

    await state.set_state(UsersStates.searching)
    # Сохраняем ID сообщения бота для редактирования
    await state.update_data(bot_message_id=callback.message.message_id)

    text = (
        "🔍 <b>Поиск пользователя</b>\n\n"
        "Отправьте:\n"
        "• <b>Telegram ID</b> (число)\n"
        "• <b>@username</b>\n"
        "• или <b>реферальный код</b>\n\n"
        "<i>Пользователь должен быть зарегистрирован в боте.</i>"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_users_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(UsersStates.searching)
async def message_users_search(message: Message, state: FSMContext) -> None:
    """Поиск пользователя по ID/username."""
    if not await _check_admin_message(message):
        return

    input_text = message.text.strip()

    # Получаем ID сообщения бота
    state_data = await state.get_data()
    bot_message_id = state_data.get("bot_message_id")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = None

        # Определяем, это ID, username или реферальный код
        if input_text.startswith("@"):
            username = input_text[1:]
            user = await user_service.get_user_by_username(username)
        elif input_text.isdigit():
            user_id = int(input_text)
            user = await user_service.get_user(user_id)
        else:
            # Пробуем как username без @
            user = await user_service.get_user_by_username(input_text)

            # Если не нашли - пробуем как реферальный код
            if not user:
                result = await session.execute(
                    select(User).where(User.referral_code == input_text)
                )
                user = result.scalar_one_or_none()

        if not user:
            # Редактируем сообщение бота
            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text="❌ Пользователь не найден.\n\n"
                             "Проверьте ID или username и попробуйте снова.",
                        reply_markup=get_users_cancel_keyboard(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    pass
            await message.answer(
                "❌ Пользователь не найден.\n\n"
                "Проверьте ID или username и попробуйте снова.",
                reply_markup=get_users_cancel_keyboard(),
            )
            return

    await state.clear()

    # Получаем статистику
    stats = await _get_user_stats(user.id, user.referral_code)
    text = _format_user_info(user, stats)

    # Редактируем сообщение бота
    if bot_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text=text,
                reply_markup=get_user_detail_keyboard(user.id, user.is_banned, user.is_admin),
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    await message.answer(
        text=text,
        reply_markup=get_user_detail_keyboard(user.id, user.is_banned, user.is_admin),
        parse_mode="HTML",
    )


# ==================== ПРОСМОТР ПОЛЬЗОВАТЕЛЯ ====================


@router.callback_query(F.data.regexp(r"^admin:users:view:\d+$"))
async def callback_user_view(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Просмотр информации о пользователе."""
    if not await _check_admin(callback):
        return

    await state.clear()
    user_id = int(callback.data.split(":")[-1])

    # Обновляем информацию о пользователе из Telegram (username и т.д.)
    user, bot_blocked = await _update_user_from_telegram(bot, user_id)

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    stats = await _get_user_stats(user.id, user.referral_code)
    text = _format_user_info(user, stats, bot_blocked)

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_detail_keyboard(user.id, user.is_banned, user.is_admin),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== ПОСЛЕДНИЕ РЕГИСТРАЦИИ ====================


@router.callback_query(F.data == AdminCallback.USERS_RECENT)
async def callback_users_recent(callback: CallbackQuery) -> None:
    """Последние зарегистрированные пользователи."""
    if not await _check_admin(callback):
        return

    users = await _get_recent_users()

    text = (
        "🕐 <b>Последние регистрации</b>\n\n"
        f"Показаны последние {len(users)} пользователей:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_users_recent_keyboard(users),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:users:recent:page:\d+$"))
async def callback_users_recent_page(callback: CallbackQuery) -> None:
    """Пагинация последних регистраций."""
    if not await _check_admin(callback):
        return

    page = int(callback.data.split(":")[-1])
    users = await _get_recent_users()

    await callback.message.edit_reply_markup(
        reply_markup=get_users_recent_keyboard(users, page=page),
    )
    await callback.answer()


# ==================== БАН/РАЗБАН ====================


@router.callback_query(F.data.regexp(r"^admin:users:ban:\d+$"))
async def callback_user_ban(callback: CallbackQuery) -> None:
    """Забанить пользователя."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    # Нельзя забанить себя
    if user_id == callback.from_user.id:
        await callback.answer("Нельзя забанить самого себя!", show_alert=True)
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        user.is_banned = True
        await session.commit()

        # Инвалидируем кэш бан-статуса
        invalidate_ban_cache(user_id)

        logger.info(f"User {user_id} banned by admin {callback.from_user.id}")

        # Логируем в Telegram
        await tg_logger.log_user_banned(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            user_id=user_id,
            user_username=user.username,
        )

    await callback.answer("🚫 Пользователь забанен", show_alert=True)

    # Обновляем карточку
    stats = await _get_user_stats(user.id, user.referral_code)
    text = _format_user_info(user, stats)

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_detail_keyboard(user.id, user.is_banned, user.is_admin),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:users:unban:\d+$"))
async def callback_user_unban(callback: CallbackQuery) -> None:
    """Разбанить пользователя."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        user.is_banned = False
        await session.commit()

        # Инвалидируем кэш бан-статуса
        invalidate_ban_cache(user_id)

        logger.info(f"User {user_id} unbanned by admin {callback.from_user.id}")

        # Логируем в Telegram
        await tg_logger.log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action="Разбан пользователя",
            details=f"@{user.username}" if user.username else f"ID: {user_id}",
        )

    await callback.answer("✅ Пользователь разбанен", show_alert=True)

    # Обновляем карточку
    stats = await _get_user_stats(user.id, user.referral_code)
    text = _format_user_info(user, stats)

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_detail_keyboard(user.id, user.is_banned, user.is_admin),
        parse_mode="HTML",
    )


# ==================== ПРАВА АДМИНА ====================


@router.callback_query(F.data.regexp(r"^admin:users:promote:\d+$"))
async def callback_user_promote(callback: CallbackQuery) -> None:
    """Сделать пользователя админом."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        user.is_admin = True
        await session.commit()

        logger.info(f"User {user_id} promoted to admin by {callback.from_user.id}")

        # Логируем в Telegram
        await tg_logger.log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action="Назначен администратор",
            details=f"@{user.username}" if user.username else f"ID: {user_id}",
        )

    await callback.answer("👑 Пользователь назначен админом", show_alert=True)

    # Обновляем карточку
    stats = await _get_user_stats(user.id, user.referral_code)
    text = _format_user_info(user, stats)

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_detail_keyboard(user.id, user.is_banned, user.is_admin),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:users:demote:\d+$"))
async def callback_user_demote(callback: CallbackQuery) -> None:
    """Снять права админа."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    # Нельзя снять права с себя
    if user_id == callback.from_user.id:
        await callback.answer("Нельзя снять права админа с самого себя!", show_alert=True)
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        user.is_admin = False
        await session.commit()

        logger.info(f"User {user_id} demoted from admin by {callback.from_user.id}")

        # Логируем в Telegram
        await tg_logger.log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action="Сняты права администратора",
            details=f"@{user.username}" if user.username else f"ID: {user_id}",
        )

    await callback.answer("Права админа сняты", show_alert=True)

    # Обновляем карточку
    stats = await _get_user_stats(user.id, user.referral_code)
    text = _format_user_info(user, stats)

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_detail_keyboard(user.id, user.is_banned, user.is_admin),
        parse_mode="HTML",
    )


# ==================== ЗАКАЗЫ ПОЛЬЗОВАТЕЛЯ ====================


@router.callback_query(F.data.regexp(r"^admin:users:orders:\d+$"))
async def callback_user_orders(callback: CallbackQuery) -> None:
    """Заказы пользователя."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    orders = await _get_user_orders(user.id)

    name = user.username or user.first_name or f"ID: {user.id}"

    username_display = f"@{user.username}" if user.username else name
    if not orders:
        text = f"📦 <b>Заказы пользователя {username_display}</b>\n\nЗаказов нет"
    else:
        text = f"📦 <b>Заказы пользователя {username_display}</b>\n\nВсего заказов: <b>{len(orders)}</b>"

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_orders_keyboard(user.id, orders),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:users:orders:\d+:page:\d+$"))
async def callback_user_orders_page(callback: CallbackQuery) -> None:
    """Пагинация заказов пользователя."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    user_id = int(parts[3])
    page = int(parts[5])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    orders = await _get_user_orders(user.id)

    await callback.message.edit_reply_markup(
        reply_markup=get_user_orders_keyboard(user.id, orders, page=page),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:users:order:\d+$"))
async def callback_user_order_detail(callback: CallbackQuery) -> None:
    """Детали заказа."""
    if not await _check_admin(callback):
        return

    order_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        # Загружаем заказ вместе с платежом и транзакциями
        result = await session.execute(
            select(Order)
            .options(selectinload(Order.payment), selectinload(Order.transactions))
            .where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()

        if not order:
            await callback.answer("Заказ не найден", show_alert=True)
            return

        # Получаем пользователя (user_id = telegram id)
        user_service = UserService(session)
        user = await user_service.get_user(order.user_id)

        # Сохраняем данные платежа
        payment_data = order.payment

        # Ищем ID платежа пользователя из транзакций
        user_payment_id = None
        user_payment_type = None  # "cryptobot" или "ton"
        for tx in order.transactions:
            if tx.external_id:
                if tx.external_id.startswith("cryptobot"):
                    # external_id: "cryptobot_stars:123456" или "cryptobot_premium:123456"
                    parts = tx.external_id.split(":")
                    if len(parts) == 2:
                        user_payment_id = parts[1]
                        user_payment_type = "cryptobot"
                        break
                elif tx.external_id.startswith("ton_"):
                    # external_id: "ton_stars:event_id" или "ton_premium:event_id"
                    parts = tx.external_id.split(":")
                    if len(parts) == 2:
                        user_payment_id = parts[1]
                        user_payment_type = "ton"
                        break

        # Фоллбэк для старых заказов без привязки transaction -> order
        # Ищем транзакцию по user_id и времени создания заказа
        if not user_payment_id and order.payment_provider in ("cryptobot", "ton"):
            from src.db.models import Transaction as TxModel
            fallback_result = await session.execute(
                select(TxModel)
                .where(
                    TxModel.user_id == order.user_id,
                    TxModel.external_id.isnot(None),
                    TxModel.created_at >= order.created_at - timedelta(minutes=5),
                    TxModel.created_at <= order.created_at + timedelta(minutes=5),
                )
                .order_by(TxModel.created_at.desc())
            )
            fallback_tx = fallback_result.scalar_one_or_none()
            if fallback_tx and fallback_tx.external_id:
                if fallback_tx.external_id.startswith("cryptobot"):
                    parts = fallback_tx.external_id.split(":")
                    if len(parts) == 2:
                        user_payment_id = parts[1]
                        user_payment_type = "cryptobot"
                elif fallback_tx.external_id.startswith("ton_"):
                    parts = fallback_tx.external_id.split(":")
                    if len(parts) == 2:
                        user_payment_id = parts[1]
                        user_payment_type = "ton"

    product_types = {
        "stars": "⭐ Звёзды",
        "premium": "👑 Premium",
    }

    payment_providers = {
        "balance": "💎 С баланса",
        "ton": "💎 TON",
        "cryptobot": "🤖 CryptoBot",
    }

    # Названия статусов без эмодзи
    status_labels = {
        "pending": "Ожидает",
        "processing": "Обрабатывается",
        "completed": "Выполнен",
        "failed": "Ошибка",
        "cancelled": "Отменён",
    }

    # Эмодзи для статусов
    status_icons = {
        "pending": "⏳",
        "processing": "🔄",
        "completed": "✅",
        "failed": "❌",
        "cancelled": "🚫",
    }

    status_label = status_labels.get(order.status, order.status)
    status_icon = status_icons.get(order.status, "📌")
    product = product_types.get(order.product_type, order.product_type)
    payment_method = payment_providers.get(order.payment_provider, order.payment_provider)

    # Конвертируем время в московское
    created_msk = _to_moscow_time(order.created_at)
    completed_msk = _to_moscow_time(order.completed_at) if order.completed_at else None

    created = created_msk.strftime("%d.%m.%Y %H:%M:%S") if created_msk else "—"
    completed = completed_msk.strftime("%d.%m.%Y %H:%M:%S") if completed_msk else "—"
    qty_text = f"{order.quantity:,} ⭐" if order.product_type == "stars" else f"{order.quantity} мес"

    # Формируем username получателя
    recipient = order.recipient_username
    if recipient and not recipient.startswith("@"):
        recipient = f"@{recipient}"

    # Определяем, это вывод с баланса
    is_balance_withdrawal = order.payment_provider == "balance"

    text = f"📦 <b>Заказ #{order.id} (#{order.order_key})</b>\n\n"

    # Основная информация о заказе
    text += "<blockquote>📋 <b>Информация</b>\n"
    text += f"🏷️ Тип: <b>{product}</b>\n"
    text += f"📊 Количество: <b>{qty_text}</b>\n"
    text += f"👤 Получатель: <b>{recipient}</b></blockquote>\n\n"

    # Оплата пользователя
    text += "<blockquote>💳 <b>Оплата</b>\n"
    text += f"🏦 Способ: <b>{payment_method}</b>\n"
    if not is_balance_withdrawal:
        net_amount = float(order.price_usdt)
        # Для CryptoBot показываем комиссию
        if order.payment_provider == "cryptobot":
            fee_percent = 3
            amount_with_fee = net_amount * 1.03
            fee_amount = amount_with_fee - net_amount
            text += f"💵 К оплате: <b>{amount_with_fee:.2f} USDT</b>\n"
            text += f"📊 Комиссия: <b>{fee_amount:.2f} USDT</b> ({fee_percent}%)\n"
            text += f"💰 Зачислено: <b>{net_amount:.2f} USDT</b>\n"
        else:
            text += f"💵 Сумма: <b>{net_amount:.2f} USDT</b>\n"
        # ID платежа пользователя
        if user_payment_id:
            if user_payment_type == "cryptobot":
                text += f"🆔 Invoice ID: <b><code>#IV{user_payment_id}</code></b>"
            elif user_payment_type == "ton":
                text += f"🆔 Event ID: <b><code>{user_payment_id}</code></b>"
        elif payment_data and payment_data.provider_tx_id:
            text += f"🆔 ID: <b><code>{payment_data.provider_tx_id}</code></b>"
    else:
        text += f"💎 Списано с баланса"
    text += "</blockquote>\n\n"

    # Транзакция Fragment (покупка звёзд/премиума)
    if order.fragment_tx_id or order.fragment_ton_spent:
        text += "<blockquote>🔗 <b>Транзакция Fragment</b>\n"
        if order.fragment_tx_id:
            tx_url = f"https://tonscan.org/tx/{order.fragment_tx_id}"
            text += f"📝 TX Hash: <a href=\"{tx_url}\"><b>{order.fragment_tx_id}</b></a>\n"
        if order.fragment_ton_spent:
            text += f"💎 Потрачено: <b>{float(order.fragment_ton_spent):.4f} TON</b>"
        text += "</blockquote>\n\n"

    # Статус заказа
    text += "<blockquote>📌 <b>Статус заказа</b>\n"
    text += f"{status_icon} Статус: <b>{status_label}</b>\n"
    text += f"📥 Создан: <b>{created}</b>\n"
    text += f"✅ Завершён: <b>{completed}</b>"
    if order.error_message:
        text += f"\n❌ Ошибка: {order.error_message}"
    text += "</blockquote>"

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ К заказам",
                    callback_data=f"admin:users:orders:{user.id if user else 0}",
                ),
            ],
        ]
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await callback.answer()


# ==================== ОТМЕНА ====================


@router.callback_query(F.data == AdminCallback.USERS_CANCEL)
async def callback_users_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена поиска."""
    await state.clear()
    await callback_users_menu(callback, state)


@router.callback_query(F.data == AdminCallback.USERS_BACK)
async def callback_users_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Назад в меню пользователей."""
    await callback_users_menu(callback, state)


# ==================== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЕМ ====================


@router.callback_query(F.data.regexp(r"^admin:users:manage:\d+$"))
async def callback_user_manage(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Меню управления пользователем (редирект на просмотр)."""
    if not await _check_admin(callback):
        return

    await state.clear()
    user_id = int(callback.data.split(":")[-1])

    # Обновляем информацию о пользователе из Telegram
    user, bot_blocked = await _update_user_from_telegram(bot, user_id)

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    # Показываем полную информацию о пользователе
    stats = await _get_user_stats(user.id, user.referral_code)
    text = _format_user_info(user, stats, bot_blocked)

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_detail_keyboard(user.id, user.is_banned, user.is_admin),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== БАЛАНСЫ ====================


BALANCE_TYPES = {
    "usdt": {"field": "balance_usdt", "name": "USDT", "icon": "💵", "decimals": 2},
    "stars": {"field": "balance_stars", "name": "Звёзды", "icon": "⭐", "decimals": 0},
    "premium": {"field": "balance_premium_months", "name": "Premium", "icon": "👑", "decimals": 0, "suffix": " мес"},
    "referral": {"field": "referral_balance", "name": "Реферальный", "icon": "🎁", "decimals": 2, "suffix": " USDT"},
}


@router.callback_query(F.data.regexp(r"^admin:users:balances:\d+$"))
async def callback_user_balances(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню балансов пользователя."""
    if not await _check_admin(callback):
        return

    await state.clear()
    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    text = (
        f"💰 <b>Балансы пользователя</b>\n\n"
        f"<blockquote>"
        f"👤 Пользователь: <b>{username}</b>\n\n"
        f"💵 USDT: <b>{float(user.balance_usdt):.2f}</b>\n"
        f"⭐ Звёзды: <b>{int(user.balance_stars):,}</b>\n"
        f"👑 Premium: <b>{user.balance_premium_months} мес</b>\n"
        f"🎁 Реферальный: <b>{float(user.referral_balance):.2f} USDT</b>"
        f"</blockquote>\n\n"
        f"Выберите баланс для изменения:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_balances_keyboard(user.id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:users:balance:(usdt|stars|premium|referral):\d+$"))
async def callback_user_balance_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало редактирования баланса."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    balance_type = parts[3]
    user_id = int(parts[4])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    balance_info = BALANCE_TYPES[balance_type]
    current_value = getattr(user, balance_info["field"])
    suffix = balance_info.get("suffix", "")

    if balance_info["decimals"] == 0:
        current_display = f"{int(current_value):,}{suffix}"
    else:
        current_display = f"{float(current_value):.{balance_info['decimals']}f}{suffix}"

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    await state.set_state(UsersStates.balance_input)
    await state.update_data(
        user_id=user_id,
        balance_type=balance_type,
        current_value=str(current_value),
        bot_message_id=callback.message.message_id,
    )

    text = (
        f"{balance_info['icon']} <b>Изменение баланса {balance_info['name']}</b>\n\n"
        f"<blockquote>"
        f"👤 Пользователь: <b>{username}</b>\n"
        f"{balance_info['icon']} Текущий баланс: <b>{current_display}</b>"
        f"</blockquote>\n\n"
        f"Введите новое значение:\n"
        f"• Число — установить точное значение\n"
        f"• <code>+100</code> — добавить к текущему\n"
        f"• <code>-50</code> — вычесть от текущего"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_balance_cancel_keyboard(user_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(UsersStates.balance_input)
async def process_balance_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода нового значения баланса."""
    if not await _check_admin_message(message):
        return

    data = await state.get_data()
    user_id = data.get("user_id")
    balance_type = data.get("balance_type")
    current_value = Decimal(data.get("current_value", "0"))
    bot_message_id = data.get("bot_message_id")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    input_text = message.text.strip().replace(",", ".")
    balance_info = BALANCE_TYPES[balance_type]

    # Парсим значение
    try:
        if input_text.startswith("+"):
            change = Decimal(input_text[1:])
            new_value = current_value + change
        elif input_text.startswith("-"):
            change = Decimal(input_text[1:])
            new_value = current_value - change
        else:
            new_value = Decimal(input_text)

        # Проверка на отрицательное значение
        if new_value < 0:
            raise ValueError("Баланс не может быть отрицательным")

        # Для Premium и звёзд - только целые числа
        if balance_type in ("stars", "premium"):
            new_value = Decimal(int(new_value))

    except Exception as e:
        # Ошибка парсинга
        async with async_session_factory() as session:
            user_service = UserService(session)
            user = await user_service.get_user(user_id)

        username = f"@{user.username}" if user and user.username else f"ID: {user_id}"
        suffix = balance_info.get("suffix", "")

        if balance_info["decimals"] == 0:
            current_display = f"{int(current_value):,}{suffix}"
        else:
            current_display = f"{float(current_value):.{balance_info['decimals']}f}{suffix}"

        error_text = (
            f"{balance_info['icon']} <b>Изменение баланса {balance_info['name']}</b>\n\n"
            f"<blockquote>"
            f"👤 Пользователь: <b>{username}</b>\n"
            f"{balance_info['icon']} Текущий баланс: <b>{current_display}</b>"
            f"</blockquote>\n\n"
            f"❌ <b>Ошибка:</b> Введите корректное число\n\n"
            f"Формат:\n"
            f"• Число — установить точное значение\n"
            f"• <code>+100</code> — добавить к текущему\n"
            f"• <code>-50</code> — вычесть от текущего"
        )

        if bot_message_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=bot_message_id,
                    text=error_text,
                    reply_markup=get_user_balance_cancel_keyboard(user_id),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    # Сохраняем для подтверждения
    await state.update_data(new_value=str(new_value))

    # Получаем пользователя для отображения
    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

    username = f"@{user.username}" if user and user.username else f"ID: {user_id}"
    suffix = balance_info.get("suffix", "")

    if balance_info["decimals"] == 0:
        old_display = f"{int(current_value):,}{suffix}"
        new_display = f"{int(new_value):,}{suffix}"
    else:
        old_display = f"{float(current_value):.{balance_info['decimals']}f}{suffix}"
        new_display = f"{float(new_value):.{balance_info['decimals']}f}{suffix}"

    change = new_value - current_value
    if change >= 0:
        change_display = f"+{int(change):,}" if balance_info["decimals"] == 0 else f"+{float(change):.{balance_info['decimals']}f}"
    else:
        change_display = f"{int(change):,}" if balance_info["decimals"] == 0 else f"{float(change):.{balance_info['decimals']}f}"

    confirm_text = (
        f"⚠️ <b>Подтверждение изменения</b>\n\n"
        f"<blockquote>"
        f"👤 Пользователь: <b>{username}</b>\n"
        f"{balance_info['icon']} Баланс: <b>{balance_info['name']}</b>\n\n"
        f"📊 Было: <b>{old_display}</b>\n"
        f"📊 Станет: <b>{new_display}</b>\n"
        f"📈 Изменение: <b>{change_display}{suffix}</b>"
        f"</blockquote>\n\n"
        f"Подтвердить изменение?"
    )

    if bot_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text=confirm_text,
                reply_markup=get_user_balance_confirm_keyboard(user_id),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^admin:users:balance:confirm:\d+$"))
async def callback_user_balance_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение изменения баланса."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    user_id = data.get("user_id")
    balance_type = data.get("balance_type")
    old_value = Decimal(data.get("current_value", "0"))
    new_value = Decimal(data.get("new_value", "0"))

    if not user_id or not balance_type:
        await callback.answer("Ошибка: данные не найдены", show_alert=True)
        await state.clear()
        return

    balance_info = BALANCE_TYPES[balance_type]

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            await state.clear()
            return

        # Сохраняем текущие балансы для ledger
        balance_stars_before = user.balance_stars
        balance_usdt_before = user.balance_usdt
        balance_premium_before = user.balance_premium_months

        # Обновляем баланс
        if balance_type == "premium":
            setattr(user, balance_info["field"], int(new_value))
        else:
            setattr(user, balance_info["field"], new_value)

        # Создаём запись в ledger (для аудита)
        # Определяем изменение
        change = new_value - old_value

        # Суммы изменений по типам
        amount_usdt = Decimal("0")
        amount_stars = Decimal("0")
        amount_premium = 0

        if balance_type == "usdt":
            amount_usdt = change
        elif balance_type == "referral":
            amount_usdt = change  # referral_balance в USDT
        elif balance_type == "stars":
            amount_stars = change
        elif balance_type == "premium":
            amount_premium = int(new_value) - int(old_value)

        # Определяем операцию (debit/credit)
        operation = "credit" if change >= 0 else "debit"

        admin_username = callback.from_user.username or str(callback.from_user.id)
        ledger_entry = BalanceLedger(
            user_id=user_id,
            operation=operation,
            amount_stars=abs(amount_stars),
            amount_usdt=abs(amount_usdt),
            amount_premium=abs(amount_premium),
            balance_stars_before=balance_stars_before,
            balance_stars_after=user.balance_stars,
            balance_usdt_before=balance_usdt_before,
            balance_usdt_after=user.balance_usdt,
            balance_premium_before=balance_premium_before,
            balance_premium_after=user.balance_premium_months,
            description=f"Изменение администратором @{admin_username}: {balance_info['name']}",
        )
        session.add(ledger_entry)

        await session.commit()

        username = f"@{user.username}" if user.username else f"ID: {user.id}"

        # Логирование
        logger.info(
            f"Admin {callback.from_user.id} changed {balance_type} balance for user {user_id}: "
            f"{old_value} -> {new_value}"
        )

        # Лог в Telegram
        suffix = balance_info.get("suffix", "")
        if balance_info["decimals"] == 0:
            old_display = f"{int(old_value):,}{suffix}"
            new_display = f"{int(new_value):,}{suffix}"
        else:
            old_display = f"{float(old_value):.{balance_info['decimals']}f}{suffix}"
            new_display = f"{float(new_value):.{balance_info['decimals']}f}{suffix}"

        await tg_logger.log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action=f"Изменение баланса {balance_info['name']}",
            details=f"Пользователь: {username}\nБыло: {old_display} → Стало: {new_display}",
        )

    await state.clear()
    await callback.answer("✅ Баланс изменён", show_alert=True)

    # Возвращаемся к балансам
    await callback_user_balances(callback, state)


# ==================== РЕФЕРАЛЫ ====================


async def _get_referrer_info(referrer_code: str) -> dict | None:
    """Получить информацию о реферере по коду."""
    if not referrer_code:
        return None

    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.referral_code == referrer_code)
        )
        referrer = result.scalar_one_or_none()

        if referrer:
            return {
                "id": referrer.id,
                "username": referrer.username,
                "referral_code": referrer.referral_code,
            }
    return None


async def _get_user_referrals(referral_code: str) -> list[dict]:
    """Получить список рефералов пользователя."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User)
            .where(User.referrer_code == referral_code)
            .order_by(desc(User.created_at))
        )
        referrals = result.scalars().all()

        referral_list = []
        for ref in referrals:
            # Считаем сколько потратил реферал
            spent_result = await session.execute(
                select(func.sum(Order.price_usdt)).where(
                    Order.user_id == ref.id,
                    Order.status == "completed"
                )
            )
            spent = spent_result.scalar() or 0

            referral_list.append({
                "id": ref.id,
                "username": ref.username,
                "spent": float(spent),
                "created_at": ref.created_at,
            })

        return referral_list


@router.callback_query(F.data.regexp(r"^admin:users:referrals:\d+$"))
async def callback_user_referrals(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню рефералов пользователя."""
    if not await _check_admin(callback):
        return

    await state.clear()
    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    # Информация о реферере
    referrer_info = await _get_referrer_info(user.referrer_code)
    if referrer_info:
        referrer_display = f"@{referrer_info['username']}" if referrer_info['username'] else f"ID: {referrer_info['id']}"
    else:
        referrer_display = "Нет"

    # Получаем рефералов
    referrals = await _get_user_referrals(user.referral_code)
    total_spent_by_referrals = sum(r["spent"] for r in referrals)

    text = (
        f"👥 <b>Рефералы пользователя</b>\n\n"
        f"<blockquote>"
        f"👤 Пользователь: <b>{username}</b>\n\n"
        f"🎫 Его реф. код: <b><code>{user.referral_code}</code></b>\n"
        f"👤 Пригласил: <b>{referrer_display}</b>\n"
        f"👥 Приглашено им: <b>{len(referrals)}</b>\n"
        f"💵 Потратили рефералы: <b>{total_spent_by_referrals:.2f} USDT</b>\n"
        f"💰 Заработано всего: <b>{float(user.total_referral_earnings):.2f} USDT</b>"
        f"</blockquote>"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_referrals_keyboard(user.id, has_referrer=bool(user.referrer_code)),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:users:referrals:change:\d+$"))
async def callback_user_referrals_change(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало изменения реферера."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    await state.set_state(UsersStates.referrer_input)
    await state.update_data(
        user_id=user_id,
        bot_message_id=callback.message.message_id,
    )

    text = (
        f"✏️ <b>Изменение реферера</b>\n\n"
        f"<blockquote>"
        f"👤 Пользователь: <b>{username}</b>\n"
        f"🆔 ID: <b><code>{user.id}</code></b>"
        f"</blockquote>\n\n"
        f"Введите ID или @username нового реферера:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_referrals_cancel_keyboard(user_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(UsersStates.referrer_input)
async def process_referrer_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода нового реферера."""
    if not await _check_admin_message(message):
        return

    data = await state.get_data()
    user_id = data.get("user_id")
    bot_message_id = data.get("bot_message_id")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    input_text = message.text.strip()

    async with async_session_factory() as session:
        user_service = UserService(session)

        # Получаем редактируемого пользователя
        user = await user_service.get_user(user_id)
        if not user:
            await state.clear()
            return

        # Ищем нового реферера
        if input_text.startswith("@"):
            referrer = await user_service.get_user_by_username(input_text[1:])
        elif input_text.isdigit():
            referrer = await user_service.get_user(int(input_text))
        else:
            referrer = await user_service.get_user_by_username(input_text)

        username = f"@{user.username}" if user.username else f"ID: {user.id}"

        if not referrer:
            error_text = (
                f"✏️ <b>Изменение реферера</b>\n\n"
                f"<blockquote>"
                f"👤 Пользователь: <b>{username}</b>"
                f"</blockquote>\n\n"
                f"❌ <b>Ошибка:</b> Пользователь не найден\n\n"
                f"Введите ID или @username нового реферера:"
            )

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=error_text,
                        reply_markup=get_user_referrals_cancel_keyboard(user_id),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

        # Проверка: нельзя быть своим реферером
        if referrer.id == user_id:
            error_text = (
                f"✏️ <b>Изменение реферера</b>\n\n"
                f"<blockquote>"
                f"👤 Пользователь: <b>{username}</b>"
                f"</blockquote>\n\n"
                f"❌ <b>Ошибка:</b> Пользователь не может быть своим реферером\n\n"
                f"Введите ID или @username нового реферера:"
            )

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=error_text,
                        reply_markup=get_user_referrals_cancel_keyboard(user_id),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

        # Проверка на циклическую зависимость:
        # Нельзя назначить реферером того, кто уже является рефералом этого пользователя
        if referrer.referrer_code == user.referral_code:
            error_text = (
                f"✏️ <b>Изменение реферера</b>\n\n"
                f"<blockquote>"
                f"👤 Пользователь: <b>{username}</b>"
                f"</blockquote>\n\n"
                f"❌ <b>Ошибка:</b> Этот пользователь уже является рефералом\n"
                f"<i>Нельзя создавать циклические реферальные связи</i>\n\n"
                f"Введите ID или @username нового реферера:"
            )

            if bot_message_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=bot_message_id,
                        text=error_text,
                        reply_markup=get_user_referrals_cancel_keyboard(user_id),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

        # Устанавливаем нового реферера
        old_referrer_code = user.referrer_code
        user.referrer_code = referrer.referral_code
        await session.commit()

        referrer_username = f"@{referrer.username}" if referrer.username else f"ID: {referrer.id}"

        # Логирование
        logger.info(
            f"Admin {message.from_user.id} changed referrer for user {user_id}: "
            f"{old_referrer_code} -> {referrer.referral_code}"
        )

        await tg_logger.log_admin_action(
            admin_id=message.from_user.id,
            admin_username=message.from_user.username,
            action="Изменение реферера",
            details=f"Пользователь: {username}\nНовый реферер: {referrer_username}",
        )

    await state.clear()

    # Показываем успех и возвращаемся
    success_text = (
        f"✅ <b>Реферер изменён</b>\n\n"
        f"<blockquote>"
        f"👤 Пользователь: <b>{username}</b>\n"
        f"👤 Новый реферер: <b>{referrer_username}</b>"
        f"</blockquote>"
    )

    if bot_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text=success_text,
                reply_markup=get_user_referrals_keyboard(user_id, has_referrer=True),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^admin:users:referrals:reset:\d+$"))
async def callback_user_referrals_reset(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение сброса реферера."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    # Информация о текущем реферере
    referrer_info = await _get_referrer_info(user.referrer_code)
    if referrer_info:
        referrer_display = f"@{referrer_info['username']}" if referrer_info['username'] else f"ID: {referrer_info['id']}"
    else:
        referrer_display = user.referrer_code or "Нет"

    text = (
        f"⚠️ <b>Сброс реферера</b>\n\n"
        f"<blockquote>"
        f"👤 Пользователь: <b>{username}</b>\n"
        f"👤 Текущий реферер: <b>{referrer_display}</b>"
        f"</blockquote>\n\n"
        f"Вы уверены, что хотите сбросить реферера?"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_referrals_confirm_keyboard(user_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:users:referrals:reset:confirm:\d+$"))
async def callback_user_referrals_reset_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение сброса реферера."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        username = f"@{user.username}" if user.username else f"ID: {user.id}"
        old_referrer_code = user.referrer_code

        # Сбрасываем реферера
        user.referrer_code = None
        await session.commit()

        # Логирование
        logger.info(f"Admin {callback.from_user.id} reset referrer for user {user_id}")

        await tg_logger.log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action="Сброс реферера",
            details=f"Пользователь: {username}\nБывший реферер: {old_referrer_code}",
        )

    await callback.answer("✅ Реферер сброшен", show_alert=True)

    # Возвращаемся к рефералам
    await callback_user_referrals(callback, state)


@router.callback_query(F.data.regexp(r"^admin:users:referrals:list:\d+$"))
async def callback_user_referrals_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Список рефералов пользователя."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"
    referrals = await _get_user_referrals(user.referral_code)

    if not referrals:
        text = (
            f"📋 <b>Рефералы пользователя {username}</b>\n\n"
            f"<i>Пользователь никого не пригласил</i>"
        )
    else:
        text = (
            f"📋 <b>Рефералы пользователя {username}</b>\n\n"
            f"Всего приглашено: <b>{len(referrals)}</b>"
        )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_referrals_list_keyboard(user_id, referrals),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:users:referrals:list:\d+:page:\d+$"))
async def callback_user_referrals_list_page(callback: CallbackQuery) -> None:
    """Пагинация списка рефералов."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    user_id = int(parts[4])
    page = int(parts[6])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    referrals = await _get_user_referrals(user.referral_code)

    await callback.message.edit_reply_markup(
        reply_markup=get_user_referrals_list_keyboard(user_id, referrals, page=page),
    )
    await callback.answer()


# ==================== ОТПРАВКА СООБЩЕНИЯ ====================


@router.callback_query(F.data.regexp(r"^admin:users:message:\d+$"))
async def callback_user_message(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало отправки сообщения пользователю."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    await state.set_state(UsersStates.message_input)
    await state.update_data(
        user_id=user_id,
        bot_message_id=callback.message.message_id,
    )

    text = (
        f"✉️ <b>Отправка сообщения</b>\n\n"
        f"<blockquote>"
        f"👤 Получатель: <b>{username}</b>\n"
        f"🆔 ID: <b><code>{user.id}</code></b>"
        f"</blockquote>\n\n"
        f"Введите текст сообщения:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_message_cancel_keyboard(user_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(UsersStates.message_input)
async def process_message_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода сообщения - показываем подтверждение."""
    if not await _check_admin_message(message):
        return

    data = await state.get_data()
    user_id = data.get("user_id")
    bot_message_id = data.get("bot_message_id")

    # Удаляем сообщение админа
    try:
        await message.delete()
    except Exception:
        pass

    message_text = message.text.strip()

    if not message_text:
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await state.clear()
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    # Сохраняем текст сообщения и переходим к подтверждению
    await state.update_data(message_text=message_text)
    await state.set_state(UsersStates.message_confirm)

    confirm_text = (
        f"📨 <b>Подтверждение отправки</b>\n\n"
        f"<blockquote>"
        f"👤 Получатель: <b>{username}</b>\n"
        f"🆔 ID: <b><code>{user.id}</code></b>"
        f"</blockquote>\n\n"
        f"<b>Текст сообщения:</b>\n"
        f"<blockquote>{message_text}</blockquote>\n\n"
        f"Отправить сообщение?"
    )

    if bot_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text=confirm_text,
                reply_markup=get_user_message_confirm_keyboard(user_id),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^admin:users:message:confirm:\d+$"))
async def callback_message_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Подтверждение и отправка сообщения пользователю."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    message_text = data.get("message_text", "")

    if not message_text:
        await callback.answer("Текст сообщения не найден", show_alert=True)
        await state.clear()
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            await state.clear()
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    # Отправляем сообщение пользователю
    try:
        await bot.send_message(
            chat_id=user_id,
            text=message_text,
            parse_mode="HTML",
        )

        success = True
        error_msg = None

    except Exception as e:
        success = False
        error_msg = str(e)
        logger.error(f"Failed to send message to user {user_id}: {e}")

    await state.clear()

    if success:
        # Логирование
        logger.info(f"Admin {callback.from_user.id} sent message to user {user_id}")

        await tg_logger.log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action="Отправка сообщения пользователю",
            details=f"Получатель: {username}\nТекст: {message_text[:100]}{'...' if len(message_text) > 100 else ''}",
        )

        result_text = (
            f"✅ <b>Сообщение отправлено</b>\n\n"
            f"<blockquote>"
            f"👤 Получатель: <b>{username}</b>\n"
            f"🆔 ID: <b><code>{user.id}</code></b>\n"
            f"📝 Текст: {message_text[:200]}{'...' if len(message_text) > 200 else ''}"
            f"</blockquote>"
        )
    else:
        result_text = (
            f"❌ <b>Ошибка отправки</b>\n\n"
            f"<blockquote>"
            f"👤 Получатель: <b>{username}</b>\n"
            f"🆔 ID: <b><code>{user.id}</code></b>\n"
            f"❌ Ошибка: {error_msg}"
            f"</blockquote>\n\n"
            f"<i>Возможно, пользователь заблокировал бота.</i>"
        )

    await callback.message.edit_text(
        text=result_text,
        reply_markup=get_user_message_back_keyboard(user_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:users:message:edit:\d+$"))
async def callback_message_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к редактированию сообщения."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            await state.clear()
            return

    username = f"@{user.username}" if user.username else f"ID: {user.id}"

    # Возвращаемся к вводу сообщения
    await state.set_state(UsersStates.message_input)
    await state.update_data(
        user_id=user_id,
        bot_message_id=callback.message.message_id,
    )

    text = (
        f"✉️ <b>Отправка сообщения</b>\n\n"
        f"<blockquote>"
        f"👤 Получатель: <b>{username}</b>\n"
        f"🆔 ID: <b><code>{user.id}</code></b>"
        f"</blockquote>\n\n"
        f"Введите новый текст сообщения:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_user_message_cancel_keyboard(user_id),
        parse_mode="HTML",
    )
    await callback.answer()
