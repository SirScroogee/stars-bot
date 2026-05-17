"""
Handlers для управления администраторами бота.
"""
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func

from src.bot.handlers.admin_utils import (
    check_admin,
    check_admin_message,
    to_moscow_time,
)
from src.bot.keyboards.admin import (
    AdminCallback,
    get_admin_menu_keyboard,
    get_admins_keyboard,
    get_admins_cancel_keyboard,
    get_admin_remove_confirm_keyboard,
)
from src.db.models import User, Order
from src.db.session import async_session_factory
from src.services.user_service import UserService
from src.services.telegram_logger import tg_logger

logger = logging.getLogger(__name__)

router = Router(name="admin_admins")

# Алиасы для обратной совместимости
_check_admin = check_admin
_check_admin_message = check_admin_message


def _to_moscow(dt: datetime) -> str:
    """Конвертировать datetime в московское время и отформатировать."""
    if dt is None:
        return "—"
    moscow_time = to_moscow_time(dt)
    return moscow_time.strftime("%d.%m.%Y %H:%M")


class AdminsStates(StatesGroup):
    """Состояния для управления админами."""
    waiting_user_id = State()  # Ожидание ID или username нового админа


async def _get_all_admins() -> list[User]:
    """Получить всех админов."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.is_admin == True).order_by(User.created_at)
        )
        return list(result.scalars().all())


# ==================== ГЛАВНОЕ МЕНЮ АДМИНОВ ====================


@router.callback_query(F.data == AdminCallback.ADMINS)
async def callback_admins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Список администраторов."""
    if not await _check_admin(callback):
        return

    await state.clear()

    admins = await _get_all_admins()

    text = (
        "👑 <b>Управление администраторами</b>\n\n"
        f"<blockquote>Всего админов: <b>{len(admins)}</b></blockquote>\n\n"
        "Выберите админа для просмотра или удаления:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_admins_keyboard(admins),
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== ПРОСМОТР АДМИНА ====================


@router.callback_query(F.data.regexp(r"^admin:admins:view:\d+$"))
async def callback_admin_view(callback: CallbackQuery) -> None:
    """Просмотр информации об администраторе."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        # Статистика заказов этого админа (если он покупал)
        orders_result = await session.execute(
            select(func.count(Order.id)).where(Order.user_id == user.id)
        )
        total_orders = orders_result.scalar() or 0

    # Форматируем информацию
    username = f"@{user.username}" if user.username else "—"
    created = _to_moscow(user.created_at)
    updated = _to_moscow(user.updated_at)

    text = (
        f"👑 <b>Администратор</b>\n\n"
        f"<blockquote>"
        f"🆔 ID: <b><code>{user.id}</code></b>\n"
        f"👤 Username: <b>{username}</b>\n"
        f"🌐 Язык: <b>{user.language_code or '—'}</b>"
        f"</blockquote>\n\n"
        f"<blockquote>"
        f"📅 Регистрация: <b>{created}</b>\n"
        f"🔄 Обновление: <b>{updated}</b>\n"
        f"📦 Заказов: <b>{total_orders}</b>"
        f"</blockquote>"
    )

    # Нельзя удалить себя
    can_remove = user_id != callback.from_user.id

    buttons = []
    if can_remove:
        buttons.append([
            InlineKeyboardButton(
                text="🗑 Снять права админа",
                callback_data=f"admin:admins:remove:{user_id}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.ADMINS,
        ),
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


# ==================== ДОБАВЛЕНИЕ АДМИНА ====================


@router.callback_query(F.data == AdminCallback.ADMINS_ADD)
async def callback_admins_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало добавления админа."""
    if not await _check_admin(callback):
        return

    await state.set_state(AdminsStates.waiting_user_id)

    text = (
        "➕ <b>Добавление администратора</b>\n\n"
        "Отправьте:\n"
        "• <b>Telegram ID</b> пользователя (число)\n"
        "• или <b>@username</b>\n\n"
        "<i>Пользователь должен быть зарегистрирован в боте.</i>"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_admins_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminsStates.waiting_user_id)
async def message_admins_add(message: Message, state: FSMContext) -> None:
    """Получение ID/username нового админа."""
    if not await _check_admin_message(message):
        return

    input_text = message.text.strip()

    async with async_session_factory() as session:
        user_service = UserService(session)

        # Определяем, это ID или username
        if input_text.startswith("@"):
            username = input_text[1:]  # Убираем @
            user = await user_service.get_user_by_username(username)
        elif input_text.isdigit():
            user_id = int(input_text)
            user = await user_service.get_user(user_id)
        else:
            await message.answer(
                "❌ Неверный формат.\n\n"
                "Отправьте Telegram ID (число) или @username.",
                reply_markup=get_admins_cancel_keyboard(),
            )
            return

        if not user:
            await message.answer(
                "❌ Пользователь не найден.\n\n"
                "Убедитесь, что пользователь зарегистрирован в боте.",
                reply_markup=get_admins_cancel_keyboard(),
            )
            return

        if user.is_admin:
            await message.answer(
                f"ℹ️ Пользователь <b>{user.username or user.id}</b> уже является админом.",
                reply_markup=get_admins_cancel_keyboard(),
                parse_mode="HTML",
            )
            return

        # Назначаем админом
        user.is_admin = True
        await session.commit()

        logger.info(f"User {user.id} promoted to admin by {message.from_user.id}")

        # Логируем в Telegram
        await tg_logger.log_admin_action(
            admin_id=message.from_user.id,
            admin_username=message.from_user.username,
            action="Назначен администратор",
            details=f"@{user.username}" if user.username else f"ID: {user.id}",
        )

    await state.clear()

    name = user.username or f"ID: {user.id}"
    await message.answer(
        f"✅ Пользователь <b>{name}</b> назначен администратором!",
        parse_mode="HTML",
    )

    # Показываем обновлённый список
    admins = await _get_all_admins()
    await message.answer(
        "👑 <b>Управление администраторами</b>\n\n"
        f"Всего админов: <b>{len(admins)}</b>",
        reply_markup=get_admins_keyboard(admins),
        parse_mode="HTML",
    )


# ==================== УДАЛЕНИЕ АДМИНА ====================


@router.callback_query(F.data.regexp(r"^admin:admins:remove:\d+$"))
async def callback_admins_remove(callback: CallbackQuery) -> None:
    """Подтверждение удаления админа."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    # Нельзя удалить себя
    if user_id == callback.from_user.id:
        await callback.answer("Нельзя удалить самого себя!", show_alert=True)
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

    name = user.username or f"ID: {user.id}"

    await callback.message.edit_text(
        f"🗑 <b>Удаление администратора</b>\n\n"
        f"Вы уверены, что хотите снять права админа с пользователя <b>{name}</b>?",
        reply_markup=get_admin_remove_confirm_keyboard(user_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:admins:remove:confirm:\d+$"))
async def callback_admins_remove_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждённое удаление админа."""
    if not await _check_admin(callback):
        return

    user_id = int(callback.data.split(":")[-1])

    # Нельзя удалить себя
    if user_id == callback.from_user.id:
        await callback.answer("Нельзя удалить самого себя!", show_alert=True)
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        user = await user_service.get_user(user_id)

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        name = user.username or f"ID: {user.id}"

        # Снимаем права
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

    await callback.answer(f"Пользователь {name} больше не админ", show_alert=True)

    # Показываем обновлённый список
    await callback_admins_menu(callback, state)


# ==================== ОТМЕНА ====================


@router.callback_query(F.data == AdminCallback.ADMINS_CANCEL)
async def callback_admins_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена добавления админа."""
    await state.clear()
    await callback_admins_menu(callback, state)


@router.callback_query(F.data == AdminCallback.ADMINS_BACK)
async def callback_admins_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Назад в меню админов."""
    await callback_admins_menu(callback, state)
