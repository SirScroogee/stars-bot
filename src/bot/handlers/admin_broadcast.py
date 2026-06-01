"""
Handlers для рассылки сообщений пользователям.

Поддерживает:
- Текстовые сообщения с HTML-форматированием
- Фото с подписью или без
- Стикеры
- Предпросмотр перед отправкой
- Прогресс отправки с визуальным прогресс-баром
- Подробная статистика после завершения
"""
import asyncio
import logging
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, func

from src.bot.handlers.admin_utils import (
    check_admin,
    check_admin_message,
    to_moscow_time,
    MOSCOW_TZ,
)
from src.bot.keyboards.admin import (
    AdminCallback,
    get_admin_menu_keyboard,
    get_broadcast_menu_keyboard,
    get_broadcast_cancel_keyboard,
    get_broadcast_confirm_keyboard,
)
from src.db.models import User
from src.db.session import async_session_factory
from src.services.user_service import UserService
from src.services.telegram_logger import tg_logger

logger = logging.getLogger(__name__)

router = Router(name="admin_broadcast")

# Алиасы для обратной совместимости
_check_admin = check_admin
_check_admin_message = check_admin_message


def _create_progress_bar(current: int, total: int, length: int = 20) -> str:
    """Создать визуальный прогресс-бар."""
    if total == 0:
        return "░" * length

    percent = current / total
    filled = int(length * percent)
    empty = length - filled

    bar = "█" * filled + "░" * empty
    return bar


def _format_duration(seconds: float) -> str:
    """Форматировать длительность в человекочитаемый формат."""
    if seconds < 60:
        return f"{seconds:.1f} сек"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes} мин {secs} сек"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours} ч {minutes} мин"


def _format_time(dt: datetime) -> str:
    """Форматировать время в московском часовом поясе."""
    moscow_time = dt.astimezone(MOSCOW_TZ)
    return moscow_time.strftime("%H:%M:%S")


class BroadcastStates(StatesGroup):
    """Состояния для рассылки."""
    waiting_text = State()  # Ожидание текстового сообщения
    waiting_photo = State()  # Ожидание фото (с текстом или без)
    waiting_sticker = State()  # Ожидание стикера
    confirm = State()  # Подтверждение рассылки


async def _get_users_count() -> int:
    """Получить количество пользователей для рассылки."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(func.count(User.id)).where(User.is_banned == False)
        )
        return result.scalar() or 0


async def _get_all_user_ids() -> list[int]:
    """Получить все ID пользователей для рассылки."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User.id).where(User.is_banned == False)
        )
        return [row[0] for row in result.fetchall()]


# ==================== ГЛАВНОЕ МЕНЮ РАССЫЛКИ ====================


@router.callback_query(F.data == AdminCallback.BROADCAST)
async def callback_broadcast_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню рассылки."""
    if not await _check_admin(callback):
        return

    await state.clear()

    users_count = await _get_users_count()

    text = (
        "📢 <b>Рассылка сообщений</b>\n\n"
        "Выберите тип сообщения для рассылки:\n\n"
        "<b>Поддерживаемое форматирование:</b>\n"
        "• <code>&lt;b&gt;жирный&lt;/b&gt;</code> → <b>жирный</b>\n"
        "• <code>&lt;i&gt;курсив&lt;/i&gt;</code> → <i>курсив</i>\n"
        "• <code>&lt;u&gt;подчёркнутый&lt;/u&gt;</code> → <u>подчёркнутый</u>\n"
        "• <code>&lt;s&gt;зачёркнутый&lt;/s&gt;</code> → <s>зачёркнутый</s>\n"
        "• <code>&lt;code&gt;моноширинный&lt;/code&gt;</code> → <code>моноширинный</code>\n"
        "• <code>&lt;a href=\"URL\"&gt;ссылка&lt;/a&gt;</code> → ссылка\n\n"
        f"👥 Получателей: <b>{users_count:,}</b> пользователей"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_broadcast_menu_keyboard(users_count),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast:nop")
async def callback_broadcast_nop(callback: CallbackQuery) -> None:
    """Пустой callback."""
    await callback.answer()


# ==================== ТЕКСТОВАЯ РАССЫЛКА ====================


@router.callback_query(F.data == AdminCallback.BROADCAST_TEXT)
async def callback_broadcast_text(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало текстовой рассылки."""
    if not await _check_admin(callback):
        return

    await state.set_state(BroadcastStates.waiting_text)

    text = (
        "📝 <b>Текстовая рассылка</b>\n\n"
        "Отправьте текст сообщения для рассылки.\n\n"
        "Можете использовать HTML-форматирование:\n"
        "• <code>&lt;b&gt;жирный&lt;/b&gt;</code>\n"
        "• <code>&lt;i&gt;курсив&lt;/i&gt;</code>\n"
        "• <code>&lt;u&gt;подчёркнутый&lt;/u&gt;</code>\n"
        "• <code>&lt;a href=\"URL\"&gt;текст ссылки&lt;/a&gt;</code>\n\n"
        "Или просто отформатируйте текст в Telegram и отправьте."
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_broadcast_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_text)
async def message_broadcast_text(message: Message, state: FSMContext) -> None:
    """Получение текста для рассылки."""
    if not await _check_admin_message(message):
        return

    if not message.text:
        await message.answer(
            "❌ Пожалуйста, отправьте <b>текстовое сообщение</b>.\n\n"
            "Если хотите отправить стикер или фото, вернитесь назад и выберите нужный тип рассылки.",
            reply_markup=get_broadcast_cancel_keyboard(),
            parse_mode="HTML",
        )
        return

    # Сохраняем данные
    await state.update_data(
        broadcast_type="text",
        text=message.html_text,  # Сохраняем с HTML-форматированием
        entities=message.entities,
    )
    await state.set_state(BroadcastStates.confirm)

    # Показываем предпросмотр
    await message.answer(
        "👁️ <b>Предпросмотр рассылки:</b>",
        parse_mode="HTML",
    )

    # Отправляем сам текст как его увидят пользователи
    await message.answer(
        text=message.html_text,
        parse_mode="HTML",
    )

    # Сообщение с кнопками подтверждения
    users_count = await _get_users_count()
    await message.answer(
        text=(
            f"📊 <b>Информация о рассылке:</b>\n\n"
            f"📝 Тип: Текстовое сообщение\n"
            f"👥 Получателей: <b>{users_count:,}</b>\n\n"
            f"Подтвердите отправку рассылки:"
        ),
        reply_markup=get_broadcast_confirm_keyboard(),
        parse_mode="HTML",
    )


# ==================== ФОТО РАССЫЛКА ====================


@router.callback_query(F.data == AdminCallback.BROADCAST_PHOTO)
async def callback_broadcast_photo(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало рассылки с фото."""
    if not await _check_admin(callback):
        return

    await state.set_state(BroadcastStates.waiting_photo)

    text = (
        "🖼️ <b>Рассылка с фото</b>\n\n"
        "Отправьте фотографию.\n\n"
        "Вы можете:\n"
        "• Отправить фото <b>без подписи</b>\n"
        "• Отправить фото <b>с подписью</b> (caption)\n\n"
        "Подпись поддерживает HTML-форматирование."
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_broadcast_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_photo, F.photo)
async def message_broadcast_photo(message: Message, state: FSMContext) -> None:
    """Получение фото для рассылки."""
    if not await _check_admin_message(message):
        return

    # Берём фото в лучшем качестве (последнее в списке)
    photo = message.photo[-1]

    # Сохраняем данные
    await state.update_data(
        broadcast_type="photo",
        photo_id=photo.file_id,
        caption=message.html_text if message.caption else None,
        caption_entities=message.caption_entities,
    )
    await state.set_state(BroadcastStates.confirm)

    # Показываем предпросмотр
    await message.answer(
        "👁️ <b>Предпросмотр рассылки:</b>",
        parse_mode="HTML",
    )

    # Отправляем фото как его увидят пользователи
    await message.answer_photo(
        photo=photo.file_id,
        caption=message.html_text if message.caption else None,
        parse_mode="HTML",
    )

    # Сообщение с кнопками подтверждения
    users_count = await _get_users_count()
    caption_info = "с подписью" if message.caption else "без подписи"
    await message.answer(
        text=(
            f"📊 <b>Информация о рассылке:</b>\n\n"
            f"🖼️ Тип: Фото {caption_info}\n"
            f"👥 Получателей: <b>{users_count:,}</b>\n\n"
            f"Подтвердите отправку рассылки:"
        ),
        reply_markup=get_broadcast_confirm_keyboard(),
        parse_mode="HTML",
    )


@router.message(BroadcastStates.waiting_photo)
async def message_broadcast_photo_invalid(message: Message) -> None:
    """Неверный формат - ожидаем фото."""
    await message.answer(
        "❌ Пожалуйста, отправьте <b>фотографию</b>.\n\n"
        "Если хотите отправить только текст, вернитесь назад и выберите «Текстовое сообщение».",
        reply_markup=get_broadcast_cancel_keyboard(),
        parse_mode="HTML",
    )


# ==================== СТИКЕР РАССЫЛКА ====================


@router.callback_query(F.data == AdminCallback.BROADCAST_STICKER)
async def callback_broadcast_sticker(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало рассылки со стикером."""
    if not await _check_admin(callback):
        return

    await state.set_state(BroadcastStates.waiting_sticker)

    text = (
        "🏷️ <b>Рассылка со стикером</b>\n\n"
        "Отправьте стикер, который нужно разослать пользователям."
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_broadcast_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_sticker, F.sticker)
async def message_broadcast_sticker(message: Message, state: FSMContext) -> None:
    """Получение стикера для рассылки."""
    if not await _check_admin_message(message):
        return

    await state.update_data(
        broadcast_type="sticker",
        sticker_id=message.sticker.file_id,
    )
    await state.set_state(BroadcastStates.confirm)

    await message.answer(
        "👁️ <b>Предпросмотр рассылки:</b>",
        parse_mode="HTML",
    )
    await message.answer_sticker(sticker=message.sticker.file_id)

    users_count = await _get_users_count()
    await message.answer(
        text=(
            f"📊 <b>Информация о рассылке:</b>\n\n"
            f"🏷️ Тип: Стикер\n"
            f"👥 Получателей: <b>{users_count:,}</b>\n\n"
            f"Подтвердите отправку рассылки:"
        ),
        reply_markup=get_broadcast_confirm_keyboard(),
        parse_mode="HTML",
    )


@router.message(BroadcastStates.waiting_sticker)
async def message_broadcast_sticker_invalid(message: Message) -> None:
    """Неверный формат - ожидаем стикер."""
    await message.answer(
        "❌ Пожалуйста, отправьте <b>стикер</b>.\n\n"
        "Если хотите отправить текст или фото, вернитесь назад и выберите нужный тип рассылки.",
        reply_markup=get_broadcast_cancel_keyboard(),
        parse_mode="HTML",
    )


# ==================== ПОДТВЕРЖДЕНИЕ И ОТПРАВКА ====================


@router.callback_query(F.data == AdminCallback.BROADCAST_CONFIRM)
async def callback_broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение и начало рассылки."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    broadcast_type = data.get("broadcast_type")

    if not broadcast_type:
        await callback.answer("Ошибка: данные рассылки не найдены", show_alert=True)
        await state.clear()
        return

    await callback.answer("🚀 Рассылка запущена!")

    # Получаем список пользователей
    user_ids = await _get_all_user_ids()
    total = len(user_ids)

    if total == 0:
        await callback.message.edit_text(
            "❌ Нет пользователей для рассылки",
            reply_markup=get_admin_menu_keyboard(),
        )
        await state.clear()
        return

    # Время начала
    start_time = time.time()
    start_datetime = datetime.now(MOSCOW_TZ)

    # Начинаем рассылку
    progress_bar = _create_progress_bar(0, total)
    await callback.message.edit_text(
        f"🚀 <b>Рассылка в процессе...</b>\n\n"
        f"<code>{progress_bar}</code> 0%\n\n"
        f"📊 Прогресс: <b>0</b> / {total:,}\n"
        f"✅ Успешно: 0\n"
        f"🚫 Заблокировали: 0\n"
        f"❌ Ошибок: 0\n\n"
        f"⏳ Осталось: ~{_format_duration(total * 0.05)}",
        parse_mode="HTML",
    )

    bot = callback.bot
    success = 0
    blocked = 0  # Пользователи, заблокировавшие бота
    other_errors = 0  # Другие ошибки
    last_update_time = time.time()

    for i, user_id in enumerate(user_ids):
        try:
            if broadcast_type == "text":
                await bot.send_message(
                    chat_id=user_id,
                    text=data.get("text"),
                    parse_mode="HTML",
                )
            elif broadcast_type == "photo":
                await bot.send_photo(
                    chat_id=user_id,
                    photo=data.get("photo_id"),
                    caption=data.get("caption"),
                    parse_mode="HTML",
                )
            elif broadcast_type == "sticker":
                await bot.send_sticker(
                    chat_id=user_id,
                    sticker=data.get("sticker_id"),
                )
            success += 1
        except Exception as e:
            error_msg = str(e).lower()
            # Проверяем, заблокировал ли пользователь бота
            if "blocked" in error_msg or "deactivated" in error_msg or "kicked" in error_msg:
                blocked += 1
            else:
                other_errors += 1
            logger.debug(f"Broadcast failed for user {user_id}: {e}")

        # Обновляем прогресс каждые 20 пользователей или каждые 2 секунды
        current_time = time.time()
        should_update = (
            (i + 1) % 20 == 0 or
            i == total - 1 or
            current_time - last_update_time >= 2
        )

        if should_update:
            last_update_time = current_time
            done = i + 1
            remaining = total - done
            percent = int(done / total * 100)
            progress_bar = _create_progress_bar(done, total)

            # Расчёт ETA
            elapsed = current_time - start_time
            if done > 0:
                speed = done / elapsed  # сообщений в секунду
                eta_seconds = remaining / speed if speed > 0 else 0
                eta_text = _format_duration(eta_seconds)
            else:
                eta_text = "расчёт..."

            try:
                await callback.message.edit_text(
                    f"🚀 <b>Рассылка в процессе...</b>\n\n"
                    f"<code>{progress_bar}</code> {percent}%\n\n"
                    f"📊 Прогресс: <b>{done:,}</b> / {total:,}\n"
                    f"✅ Успешно: {success:,}\n"
                    f"🚫 Заблокировали: {blocked:,}\n"
                    f"❌ Ошибок: {other_errors:,}\n\n"
                    f"⏳ Осталось: ~{eta_text}",
                    parse_mode="HTML",
                )
            except Exception:
                pass  # Игнорируем ошибки редактирования (слишком частые запросы)

        # Небольшая задержка чтобы не превысить лимиты API
        await asyncio.sleep(0.05)

    # Время окончания
    end_time = time.time()
    end_datetime = datetime.now(MOSCOW_TZ)
    duration = end_time - start_time
    speed = total / duration if duration > 0 else 0
    delivery_rate = (success / total * 100) if total > 0 else 0

    # Тип рассылки для отображения
    type_display_map = {
        "text": "📝 Текст",
        "photo": "🖼️ Фото",
        "sticker": "🏷️ Стикер",
    }
    type_display = type_display_map.get(broadcast_type, broadcast_type)

    # Финальное сообщение с подробной статистикой
    await callback.message.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"<blockquote><b>📊 Статистика</b>\n"
        f"👥 Всего пользователей: <b>{total:,}</b>\n"
        f"✅ Успешно доставлено: <b>{success:,}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked:,}</b>\n"
        f"❌ Другие ошибки: <b>{other_errors:,}</b>\n"
        f"📈 Доставляемость: <b>{delivery_rate:.1f}%</b></blockquote>\n\n"
        f"<blockquote><b>⏱️ Время</b>\n"
        f"🕐 Начало: <b>{_format_time(start_datetime)}</b>\n"
        f"🕐 Окончание: <b>{_format_time(end_datetime)}</b>\n"
        f"⏳ Длительность: <b>{_format_duration(duration)}</b>\n"
        f"⚡ Скорость: <b>{speed:.1f}</b> сообщ./сек</blockquote>\n\n"
        f"<blockquote><b>📋 Детали</b>\n"
        f"📨 Тип: <b>{type_display}</b>\n"
        f"👤 Админ: <b>@{callback.from_user.username or callback.from_user.id}</b></blockquote>",
        reply_markup=get_admin_menu_keyboard(),
        parse_mode="HTML",
    )

    logger.info(
        f"Broadcast completed by admin {callback.from_user.id}: "
        f"total={total}, success={success}, blocked={blocked}, errors={other_errors}, "
        f"duration={duration:.1f}s"
    )

    # Логируем в Telegram
    await tg_logger.log_admin_action(
        admin_id=callback.from_user.id,
        admin_username=callback.from_user.username,
        action="Рассылка завершена",
        details=(
            f"Тип: {type_display}\n"
            f"Всего: {total:,}, успешно: {success:,}\n"
            f"Заблокировали: {blocked:,}, ошибок: {other_errors:,}\n"
            f"Время: {_format_duration(duration)}"
        ),
    )

    await state.clear()


# ==================== ОТМЕНА ====================


@router.callback_query(F.data == AdminCallback.BROADCAST_CANCEL)
async def callback_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена рассылки."""
    await state.clear()
    await callback_broadcast_menu(callback, state)


@router.callback_query(F.data == AdminCallback.BROADCAST_BACK)
async def callback_broadcast_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Назад в меню рассылки."""
    await callback_broadcast_menu(callback, state)
