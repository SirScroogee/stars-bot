"""
Handlers для управления Fragment аккаунтами в админ-панели.
"""
import asyncio
import html
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from src.api_clients.fragment import FragmentClient
from src.api_clients.fragment.config import FragmentConfig
from src.api_clients.fragment.exceptions import (
    FragmentAccessDeniedError,
    FragmentAPIError,
    RecipientNotFoundError,
    SessionExpiredError,
)
from src.bot.handlers.admin_utils import check_admin
from src.bot.keyboards.admin import (
    AdminCallback,
    get_admin_menu_keyboard,
    get_fragment_menu_keyboard,
    get_fragment_list_keyboard,
    get_fragment_detail_keyboard,
    get_fragment_payment_method_keyboard,
    get_fragment_edit_keyboard,
    get_fragment_priority_keyboard,
    get_fragment_confirm_delete_keyboard,
    get_fragment_cancel_keyboard,
    get_fragment_add_step_keyboard,
    get_fragment_confirm_keyboard,
)
from src.db.models import FragmentAccount, FragmentAccountStatus
from src.db.session import async_session_factory
from src.services.fragment_account_service import FragmentAccountService
from src.services.user_service import UserService
from src.workers.supervisor import trigger_reconfigure

logger = logging.getLogger(__name__)

router = Router(name="admin_fragment")

# Кэширование: не проверять аккаунт если прошло меньше этого времени
CACHE_TTL = timedelta(minutes=5)
PREFLIGHT_STARS_QUANTITY = 50


class FragmentStates(StatesGroup):
    """Состояния для добавления/редактирования Fragment аккаунтов."""
    # Добавление нового аккаунта
    add_name = State()
    add_tonapi_key = State()
    add_mnemonic = State()
    add_fragment_hash = State()
    add_stel_token = State()
    add_stel_ssid = State()
    add_stel_dt = State()
    add_stel_ton_token = State()
    add_confirm = State()

    # Редактирование
    edit_field = State()

    # Обновление сессии
    session_stel_token = State()
    session_stel_ssid = State()
    session_stel_dt = State()
    session_stel_ton_token = State()


# Алиас для обратной совместимости
_check_admin = check_admin


# ==================== ГЛАВНОЕ МЕНЮ FRAGMENT ====================


@router.callback_query(F.data == AdminCallback.FRAGMENT)
async def callback_fragment_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню управления Fragment аккаунтами."""
    if not await _check_admin(callback):
        return

    await state.clear()

    # Проверяем, нужно ли автообновление
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        accounts_to_check = await service.get_all_accounts()
        needs_update = any(_needs_refresh(acc.last_session_check) for acc in accounts_to_check)

    # Если нужно обновление - показываем индикатор
    if needs_update:
        try:
            await callback.message.edit_text(
                "💎 <b>Fragment аккаунты</b>\n\n"
                "<i>🔄 Обновление данных...</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        # Автообновляем все аккаунты с устаревшим кэшем
        await _auto_refresh_all_accounts()

    # Получаем актуальные данные
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        stats = await service.get_stats()
        accounts = await service.get_active_accounts()

    # Формируем текст
    text = f"💎 <b>Fragment аккаунты</b>\n\n"

    # Блок статистики аккаунтов
    text += (
        f"<blockquote>"
        f"📊 Всего: <b>{stats['total']}</b>\n"
        f"✅ Активных: <b>{stats['active']}</b>\n"
        f"🔴 Сессия истекла: <b>{stats['session_expired']}</b>\n"
        f"💰 Низкий баланс: <b>{stats['low_balance']}</b>\n"
        f"⏸️ Отключено: <b>{stats['disabled']}</b>"
        f"</blockquote>\n\n"
    )

    # Блок статистики заказов
    text += (
        f"<blockquote>"
        f"📦 Всего заказов: <b>{stats['total_orders']:,}</b>\n"
        f"✅ Успешных: <b>{stats['successful_orders']:,}</b>\n"
        f"💸 Потрачено TON: <b>{stats['total_ton_spent']:.2f}</b>"
        f"</blockquote>\n\n"
    )

    # Активные аккаунты
    if accounts:
        text += "📋 <b>Активные аккаунты:</b>\n"
        for acc in accounts:
            balance_str = f"{acc.ton_balance:.2f}" if acc.ton_balance else "0.00"

            text += (
                f"<blockquote>"
                f"💎 <b>{acc.name}</b>\n"
                f"💰 Баланс: <b>{balance_str} TON</b>\n"
                f"📦 Заказов: <b>{acc.total_orders:,}</b> "
                f"(✅ <b>{acc.successful_orders:,}</b>)\n"
                f"💸 Потрачено: <b>{acc.total_ton_spent:.2f} TON</b>"
                f"</blockquote>\n"
            )
    else:
        text += "<i>Нет активных аккаунтов</i>"

    await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_menu_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == AdminCallback.FRAGMENT_BACK)
async def callback_fragment_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Назад в меню Fragment."""
    await callback_fragment_menu(callback, state)


# ==================== СПИСОК АККАУНТОВ ====================


@router.callback_query(F.data == AdminCallback.FRAGMENT_LIST)
async def callback_fragment_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Список всех Fragment аккаунтов."""
    if not await _check_admin(callback):
        return

    await state.clear()

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        accounts = await service.get_all_accounts()

    if not accounts:
        text = (
            "💎 <b>Fragment аккаунты</b>\n\n"
            "<i>Нет добавленных аккаунтов.</i>\n"
            "Нажмите «➕ Добавить аккаунт» чтобы добавить первый."
        )
    else:
        text = (
            "💎 <b>Fragment аккаунты</b>\n\n"
            "<blockquote>"
            "✅ — <b>активен</b>\n"
            "🔴 — <b>сессия истекла</b>\n"
            "💰 — <b>низкий баланс</b>\n"
            "⏸️ — <b>отключён</b>"
            "</blockquote>\n\n"
            "<b>Выберите аккаунт для просмотра:</b>"
        )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_list_keyboard(accounts),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:fragment:page:\d+$"))
async def callback_fragment_page(callback: CallbackQuery) -> None:
    """Пагинация списка аккаунтов."""
    if not await _check_admin(callback):
        return

    page = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        accounts = await service.get_all_accounts()

    await callback.message.edit_reply_markup(
        reply_markup=get_fragment_list_keyboard(accounts, page=page),
    )


@router.callback_query(F.data == "admin:fragment:nop")
async def callback_fragment_nop(callback: CallbackQuery) -> None:
    """Пустой callback для индикатора страницы."""
    await callback.answer()


# ==================== ПРОСМОТР АККАУНТА ====================


@router.callback_query(F.data.regexp(r"^admin:fragment:view:\d+$"))
async def callback_fragment_view(callback: CallbackQuery) -> None:
    """Просмотр детальной информации об аккаунте."""
    if not await _check_admin(callback):
        return

    account_id = int(callback.data.split(":")[-1])

    # Проверяем, нужно ли автообновление
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        account = await service.get_account(account_id)

        if not account:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        needs_update = _needs_refresh(account.last_session_check)

    # Если нужно обновление - показываем индикатор
    if needs_update:
        try:
            await callback.message.edit_text(
                f"💎 <b>{account.name}</b>\n\n"
                "<i>🔄 Обновление данных...</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        # Автообновляем аккаунт
        await _auto_refresh_account(account_id)

        # Перечитываем данные после обновления
        async with async_session_factory() as session:
            service = FragmentAccountService(session)
            account = await service.get_account(account_id)

    status_names = {
        "active": "✅ Активен",
        "session_expired": "🔴 Сессия истекла",
        "low_balance": "💰 Низкий баланс",
        "disabled": "⏸️ Отключён",
    }

    # Приоритет словами
    priority_names = {
        0: "🔥 Высокий",
        1: "📊 Средний",
        2: "📉 Низкий",
    }
    priority_str = priority_names.get(account.priority, f"📊 {account.priority}")

    balance_str = f"{account.ton_balance:.4f} TON" if account.ton_balance else "—"
    last_check = account.last_session_check.strftime("%d.%m.%Y %H:%M") if account.last_session_check else "никогда"
    wallet = account.wallet_address[:20] + "..." if account.wallet_address and len(account.wallet_address) > 20 else (account.wallet_address or "—")
    payment_method = account.payment_method or "ton"
    payment_str = "USDT TON" if payment_method == "usdt_ton" else "TON"

    text = (
        f"💎 <b>Информация об аккаунте</b>\n\n"
        f"<blockquote>"
        f"📝 Имя: <b>{account.name}</b>\n"
        f"📊 Статус: <b>{status_names.get(account.status, account.status)}</b>\n"
        f"⚡ Приоритет: <b>{priority_str}</b>\n"
        f"💳 Способ оплаты: <b>{payment_str}</b>"
        f"</blockquote>\n\n"
        f"<blockquote>"
        f"👛 Кошелёк: <code>{wallet}</code>\n"
        f"💰 Баланс: <b>{balance_str}</b>"
        f"</blockquote>\n\n"
        f"<blockquote>"
        f"📦 Заказов: <b>{account.total_orders:,}</b>\n"
        f"✅ Успешных: <b>{account.successful_orders:,}</b>\n"
        f"💸 Потрачено: <b>{account.total_ton_spent:.4f} TON</b>"
        f"</blockquote>\n\n"
        f"<blockquote>"
        f"🕐 Последняя проверка: <b>{last_check}</b>\n"
        f"📅 Создан: <b>{account.created_at.strftime('%d.%m.%Y %H:%M')}</b>"
        f"</blockquote>"
    )

    if account.error_message:
        text += f"\n\n⚠️ <b>Ошибка:</b> {account.error_message}"

    await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_detail_keyboard(account_id, account.is_active),
        parse_mode="HTML",
    )


# ==================== СПОСОБ ОПЛАТЫ ====================


@router.callback_query(F.data.regexp(r"^admin:fragment:payment:\d+$"))
async def callback_fragment_payment(callback: CallbackQuery) -> None:
    """Меню выбора способа оплаты Fragment аккаунта."""
    if not await _check_admin(callback):
        return

    account_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        account = await service.get_account(account_id)

        if not account:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

    current_method = account.payment_method or "ton"
    current_text = "USDT TON" if current_method == "usdt_ton" else "TON"

    text = (
        "💳 <b>Способ оплаты Fragment</b>\n\n"
        f"Аккаунт: <b>{html.escape(account.name)}</b>\n"
        f"Текущий способ: <b>{current_text}</b>\n\n"
        "Выберите способ оплаты для Stars и Premium:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_payment_method_keyboard(account_id, current_method),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:fragment:payment:set:\d+:(ton|usdt_ton)$"))
async def callback_fragment_payment_set(callback: CallbackQuery) -> None:
    """Сохранить способ оплаты Fragment аккаунта."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    account_id = int(parts[-2])
    payment_method = parts[-1]

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        account = await service.update_account(account_id, payment_method=payment_method)
        await session.commit()

        if not account:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

    await trigger_reconfigure()
    method_text = "USDT TON" if payment_method == "usdt_ton" else "TON"
    await callback.answer(f"✅ Способ оплаты: {method_text}", show_alert=True)

    view_callback = callback.model_copy(update={"data": f"admin:fragment:view:{account_id}"})
    await callback_fragment_view(view_callback)


# ==================== TOGGLE АКТИВНОСТИ ====================


@router.callback_query(F.data.regexp(r"^admin:fragment:toggle:\d+$"))
async def callback_fragment_toggle(callback: CallbackQuery) -> None:
    """Включить/выключить аккаунт."""
    if not await _check_admin(callback):
        return

    account_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        new_state = await service.toggle_account(account_id)
        await session.commit()

        if new_state is None:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

    # Пересоздаём воркер (изменилось кол-во активных аккаунтов)
    await trigger_reconfigure()

    status = "включён" if new_state else "выключен"
    await callback.answer(f"Аккаунт {status}", show_alert=True)

    # Обновляем карточку
    await callback_fragment_view(callback)


# ==================== УДАЛЕНИЕ ====================


@router.callback_query(F.data.regexp(r"^admin:fragment:delete:\d+$"))
async def callback_fragment_delete(callback: CallbackQuery) -> None:
    """Подтверждение удаления аккаунта."""
    if not await _check_admin(callback):
        return

    account_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        account = await service.get_account(account_id)

        if not account:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

    text = (
        f"🗑️ <b>Удаление аккаунта</b>\n\n"
        f"Вы уверены, что хотите удалить аккаунт <b>{account.name}</b>?\n\n"
        f"⚠️ Это действие необратимо!"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_confirm_delete_keyboard(account_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:fragment:delete:confirm:\d+$"))
async def callback_fragment_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждённое удаление аккаунта."""
    if not await _check_admin(callback):
        return

    account_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        deleted = await service.delete_account(account_id)
        await session.commit()

        if not deleted:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

    # Пересоздаём воркер (изменилось кол-во активных аккаунтов)
    await trigger_reconfigure()

    await callback.answer("Аккаунт удалён", show_alert=True)

    # Возвращаемся к списку
    await callback_fragment_list(callback, state)


# ==================== РЕДАКТИРОВАНИЕ ====================


@router.callback_query(F.data.regexp(r"^admin:fragment:edit:\d+$"))
async def callback_fragment_edit_menu(callback: CallbackQuery) -> None:
    """Меню редактирования аккаунта."""
    if not await _check_admin(callback):
        return

    account_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        account = await service.get_account(account_id)

        if not account:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

    text = (
        f"✏️ <b>Редактирование: {account.name}</b>\n\n"
        "Выберите поле для изменения:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_edit_keyboard(account_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:fragment:edit:\d+:priority$"))
async def callback_fragment_edit_priority(callback: CallbackQuery) -> None:
    """Выбор приоритета аккаунта."""
    if not await _check_admin(callback):
        return

    account_id = int(callback.data.split(":")[3])

    text = (
        "⚡ <b>Выбор приоритета</b>\n\n"
        "<blockquote>"
        "🔥 <b>Высокий</b> — используется первым\n"
        "📊 <b>Средний</b> — обычный приоритет\n"
        "📉 <b>Низкий</b> — используется последним"
        "</blockquote>\n\n"
        "Выберите приоритет:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_priority_keyboard(account_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:fragment:priority:\d+:\d+$"))
async def callback_fragment_set_priority(callback: CallbackQuery) -> None:
    """Установка приоритета аккаунта."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    account_id = int(parts[3])
    priority = int(parts[4])

    priority_names = {0: "Высокий", 1: "Средний", 2: "Низкий"}

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        await service.update_account(account_id, priority=priority)
        await session.commit()

    await callback.answer(f"✅ Приоритет изменён: {priority_names.get(priority, priority)}", show_alert=True)

    view_callback = callback.model_copy(update={"data": f"admin:fragment:view:{account_id}"})
    await callback_fragment_view(view_callback)


@router.callback_query(F.data.regexp(r"^admin:fragment:edit:\d+:(name|tonapi|mnemonic|hash)$"))
async def callback_fragment_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало редактирования конкретного поля."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    account_id = int(parts[3])
    field = parts[4]

    await state.update_data(edit_account_id=account_id, edit_field=field)
    await state.set_state(FragmentStates.edit_field)

    field_names = {
        "name": "имя аккаунта",
        "tonapi": "TONAPI Key",
        "mnemonic": "мнемонику (24 слова через пробел)",
        "hash": "резервный Fragment Hash (или - чтобы очистить)",
    }

    text = (
        f"✏️ <b>Редактирование</b>\n\n"
        f"Введите новое значение для поля <b>{field_names.get(field, field)}</b>:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_cancel_keyboard(account_id, "edit"),
        parse_mode="HTML",
    )


@router.message(FragmentStates.edit_field)
async def message_fragment_edit_field(message: Message, state: FSMContext) -> None:
    """Обработка нового значения поля."""
    data = await state.get_data()
    account_id = data.get("edit_account_id")
    field = data.get("edit_field")
    value = message.text.strip()

    if not account_id or not field:
        await state.clear()
        return

    async with async_session_factory() as session:
        service = FragmentAccountService(session)

        try:
            if field == "name":
                await service.update_account(account_id, name=value)
            elif field == "tonapi":
                await service.update_account(account_id, tonapi_key=value)
            elif field == "mnemonic":
                mnemonic = value.split()
                if len(mnemonic) != 24:
                    await message.answer(
                        "❌ Мнемоника должна содержать 24 слова. Попробуйте снова:",
                        reply_markup=get_fragment_cancel_keyboard(account_id, "edit"),
                    )
                    return
                await service.update_account(account_id, mnemonic=mnemonic)
            elif field == "hash":
                await service.update_account(account_id, fragment_hash="" if value == "-" else value)
            await session.commit()
            await message.answer("✅ Значение обновлено!")

        except ValueError as e:
            await message.answer(
                f"❌ Неверный формат: {e}\nПопробуйте снова:",
                reply_markup=get_fragment_cancel_keyboard(account_id, "edit"),
            )
            return
        except Exception as e:
            logger.error(f"Error updating fragment account: {e}")
            await message.answer(
                f"❌ Ошибка: {e}",
                reply_markup=get_fragment_cancel_keyboard(account_id, "edit"),
            )
            return

    await state.clear()

    # Удаляем сообщение с вводом для безопасности (особенно мнемоника)
    try:
        await message.delete()
    except Exception:
        pass

    # Показываем карточку аккаунта
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        account = await service.get_account(account_id)

    if account:
        # Отправляем новое сообщение с карточкой
        await message.answer(
            text=f"💎 <b>{account.name}</b>\n\n"
                 f"<b>Статус:</b> {account.status}\n"
                 f"<b>Активен:</b> {'✅' if account.is_active else '❌'}\n\n"
                 f"Значение успешно обновлено!",
            reply_markup=get_fragment_detail_keyboard(account_id, account.is_active),
            parse_mode="HTML",
        )


# ==================== ОБНОВЛЕНИЕ СЕССИИ ====================


@router.callback_query(F.data.regexp(r"^admin:fragment:session:\d+$"))
async def callback_fragment_session(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало обновления сессионных токенов."""
    if not await _check_admin(callback):
        return

    account_id = int(callback.data.split(":")[-1])

    await state.update_data(session_account_id=account_id)
    await state.set_state(FragmentStates.session_stel_token)

    text = (
        "🔑 <b>Обновление сессии Fragment</b>\n\n"
        "Введите <b>STEL_TOKEN</b>:\n\n"
        "<i>Это значение cookie stel_token из браузера после авторизации на fragment.com</i>"
    )

    msg = await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_cancel_keyboard(account_id, "view"),
        parse_mode="HTML",
    )
    await state.update_data(bot_message_id=msg.message_id, bot_chat_id=callback.message.chat.id)


@router.message(FragmentStates.session_stel_token)
async def message_session_stel_token(message: Message, state: FSMContext) -> None:
    """Получение STEL_TOKEN."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")
    account_id = data.get("session_account_id")

    # Удаляем сообщение для безопасности
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(stel_token=message.text.strip())
    await state.set_state(FragmentStates.session_stel_ssid)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "🔑 <b>Обновление сессии Fragment</b>\n\n"
            "Введите <b>STEL_SSID</b>:\n\n"
            "<i>Это значение cookie stel_ssid из браузера</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(account_id, "view"),
        parse_mode="HTML",
    )


@router.message(FragmentStates.session_stel_ssid)
async def message_session_stel_ssid(message: Message, state: FSMContext) -> None:
    """Получение STEL_SSID."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")
    account_id = data.get("session_account_id")

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(stel_ssid=message.text.strip())
    await state.set_state(FragmentStates.session_stel_dt)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "🔑 <b>Обновление сессии Fragment</b>\n\n"
            "Введите <b>STEL_DT</b>:\n\n"
            "<i>Это значение cookie stel_dt из браузера. Обычно: -300</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(account_id, "view"),
        parse_mode="HTML",
    )


@router.message(FragmentStates.session_stel_dt)
async def message_session_stel_dt(message: Message, state: FSMContext) -> None:
    """Получение STEL_DT."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")
    account_id = data.get("session_account_id")

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(stel_dt=message.text.strip() or "-300")
    await state.set_state(FragmentStates.session_stel_ton_token)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "🔑 <b>Обновление сессии Fragment</b>\n\n"
            "Введите <b>STEL_TON_TOKEN</b>:\n\n"
            "<i>Это значение cookie stel_ton_token из браузера</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(account_id, "view"),
        parse_mode="HTML",
    )


@router.message(FragmentStates.session_stel_ton_token)
async def message_session_stel_ton_token(message: Message, state: FSMContext) -> None:
    """Получение STEL_TON_TOKEN и сохранение сессии."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")
    account_id = data.get("session_account_id")
    stel_token = data.get("stel_token")
    stel_ssid = data.get("stel_ssid")
    stel_dt = data.get("stel_dt", "-300")
    stel_ton_token = message.text.strip()

    try:
        await message.delete()
    except Exception:
        pass

    if not account_id:
        await state.clear()
        return
    preflight_username = message.from_user.username
    if not preflight_username:
        await message.bot.edit_message_text(
            chat_id=bot_chat_id,
            message_id=bot_message_id,
            text=(
                "❌ <b>Не удалось проверить сессию</b>\n\n"
                "Для проверки cookies у админа должен быть Telegram username."
            ),
            reply_markup=get_fragment_cancel_keyboard(account_id, "view"),
            parse_mode="HTML",
        )
        return

    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        success = await service.update_session_tokens(
            account_id=account_id,
            stel_token=stel_token,
            stel_ssid=stel_ssid,
            stel_ton_token=stel_ton_token,
            stel_dt=stel_dt,
        )
        await session.commit()

        account = await service.get_account(account_id)

    await state.clear()

    check_result = {"success": False, "error": "Аккаунт не найден"}
    if success:
        check_result = await _check_single_account(account_id, preflight_username=preflight_username)
        if check_result.get("success"):
            # Пересоздаём воркер только после успешной проверки cookies.
            await trigger_reconfigure()
        else:
            async with async_session_factory() as check_session:
                check_service = FragmentAccountService(check_session)
                if check_result.get("session_expired"):
                    await check_service.mark_session_expired(
                        account_id,
                        check_result.get("error") or "Fragment session expired",
                    )
                else:
                    await check_service.set_status(
                        account_id,
                        FragmentAccountStatus.DISABLED,
                        check_result.get("error") or "Fragment preflight failed",
                    )
                await check_session.commit()

    if success and account and check_result.get("success"):
        await message.bot.edit_message_text(
            chat_id=bot_chat_id,
            message_id=bot_message_id,
            text=(
                f"✅ <b>Сессия обновлена!</b>\n\n"
                f"<blockquote>"
                f"Аккаунт: <b>{account.name}</b>\n"
                f"Статус: <b>✅ Активен</b>"
                f"</blockquote>"
            ),
            reply_markup=get_fragment_detail_keyboard(account_id, account.is_active),
            parse_mode="HTML",
        )
    else:
        await message.bot.edit_message_text(
            chat_id=bot_chat_id,
            message_id=bot_message_id,
            text=(
                "❌ <b>Сессия сохранена, но проверка не пройдена</b>\n\n"
                f"<blockquote>{html.escape(str(check_result.get('error') or 'Неизвестная ошибка'))}</blockquote>"
            ),
            reply_markup=get_fragment_detail_keyboard(account_id, False),
            parse_mode="HTML",
        )


# ==================== ДОБАВЛЕНИЕ АККАУНТА ====================


@router.callback_query(F.data == AdminCallback.FRAGMENT_ADD)
async def callback_fragment_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало добавления нового аккаунта."""
    if not await _check_admin(callback):
        return

    await state.clear()
    await state.set_state(FragmentStates.add_name)

    text = (
        "➕ <b>Добавление Fragment аккаунта</b>\n\n"
        "<b>Шаг 1/8:</b> Введите <b>имя</b> аккаунта:\n\n"
        "<i>Это понятное название для вас, например: «Основной кошелёк» или «Резервный»</i>"
    )

    msg = await callback.message.edit_text(
        text=text,
        reply_markup=get_fragment_cancel_keyboard(),
        parse_mode="HTML",
    )
    await state.update_data(bot_message_id=msg.message_id, bot_chat_id=callback.message.chat.id)


@router.message(FragmentStates.add_name)
async def message_add_name(message: Message, state: FSMContext) -> None:
    """Получение имени аккаунта."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    name = message.text.strip()
    if len(name) < 2 or len(name) > 100:
        await message.bot.edit_message_text(
            chat_id=bot_chat_id,
            message_id=bot_message_id,
            text="❌ Имя должно быть от 2 до 100 символов. Попробуйте снова:",
            reply_markup=get_fragment_cancel_keyboard(),
        )
        return

    await state.update_data(add_name=name)
    await state.set_state(FragmentStates.add_tonapi_key)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "➕ <b>Добавление Fragment аккаунта</b>\n\n"
            "<b>Шаг 2/8:</b> Введите <b>TONAPI Key</b>:\n\n"
            "<i>Получите ключ на https://tonconsole.com → API Keys</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.message(FragmentStates.add_tonapi_key)
async def message_add_tonapi_key(message: Message, state: FSMContext) -> None:
    """Получение TONAPI Key."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(add_tonapi_key=message.text.strip())
    await state.set_state(FragmentStates.add_mnemonic)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "➕ <b>Добавление Fragment аккаунта</b>\n\n"
            "<b>Шаг 3/8:</b> Введите <b>мнемонику</b> кошелька:\n\n"
            "<i>24 слова через пробел. Это секретная фраза восстановления кошелька TON.</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.message(FragmentStates.add_mnemonic)
async def message_add_mnemonic(message: Message, state: FSMContext) -> None:
    """Получение мнемоники."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    mnemonic = message.text.strip().split()

    # Удаляем сразу для безопасности
    try:
        await message.delete()
    except Exception:
        pass

    if len(mnemonic) != 24:
        await message.bot.edit_message_text(
            chat_id=bot_chat_id,
            message_id=bot_message_id,
            text=f"❌ Мнемоника должна содержать 24 слова (получено: {len(mnemonic)}). Попробуйте снова:",
            reply_markup=get_fragment_cancel_keyboard(),
        )
        return

    await state.update_data(add_mnemonic=mnemonic)
    await state.set_state(FragmentStates.add_fragment_hash)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "➕ <b>Добавление Fragment аккаунта</b>\n\n"
            "<b>Шаг 4/8:</b> Введите <b>Fragment Hash</b>:\n\n"
            "<i>Это резервный hash из URL /api?hash=... или со страницы Fragment. "
            "Если не знаете его, отправьте «-».</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.message(FragmentStates.add_fragment_hash)
async def message_add_fragment_hash(message: Message, state: FSMContext) -> None:
    """Получение резервного Fragment Hash."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    try:
        await message.delete()
    except Exception:
        pass

    value = message.text.strip()
    await state.update_data(add_fragment_hash="" if value == "-" else value)
    await state.set_state(FragmentStates.add_stel_token)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "➕ <b>Добавление Fragment аккаунта</b>\n\n"
            "<b>Шаг 5/8:</b> Введите <b>STEL_TOKEN</b>:\n\n"
            "<i>Откройте DevTools (F12) → Application → Cookies → fragment.com\n"
            "Скопируйте значение cookie «stel_token»</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.message(FragmentStates.add_stel_token)
async def message_add_stel_token(message: Message, state: FSMContext) -> None:
    """Получение STEL_TOKEN."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(add_stel_token=message.text.strip())
    await state.set_state(FragmentStates.add_stel_ssid)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "➕ <b>Добавление Fragment аккаунта</b>\n\n"
            "<b>Шаг 6/8:</b> Введите <b>STEL_SSID</b>:\n\n"
            "<i>Скопируйте значение cookie «stel_ssid» из того же места</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.message(FragmentStates.add_stel_ssid)
async def message_add_stel_ssid(message: Message, state: FSMContext) -> None:
    """Получение STEL_SSID."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(add_stel_ssid=message.text.strip())
    await state.set_state(FragmentStates.add_stel_dt)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "➕ <b>Добавление Fragment аккаунта</b>\n\n"
            "<b>Шаг 7/8:</b> Введите <b>STEL_DT</b>:\n\n"
            "<i>Скопируйте значение cookie «stel_dt». Обычно: -300</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.message(FragmentStates.add_stel_dt)
async def message_add_stel_dt(message: Message, state: FSMContext) -> None:
    """Получение STEL_DT."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(add_stel_dt=message.text.strip() or "-300")
    await state.set_state(FragmentStates.add_stel_ton_token)

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=(
            "➕ <b>Добавление Fragment аккаунта</b>\n\n"
            "<b>Шаг 8/8:</b> Введите <b>STEL_TON_TOKEN</b>:\n\n"
            "<i>Скопируйте значение cookie «stel_ton_token»</i>"
        ),
        reply_markup=get_fragment_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.message(FragmentStates.add_stel_ton_token)
async def message_add_stel_ton_token(message: Message, state: FSMContext) -> None:
    """Получение STEL_TON_TOKEN и подтверждение."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(add_stel_ton_token=message.text.strip())
    await state.set_state(FragmentStates.add_confirm)

    data = await state.get_data()

    text = (
        "➕ <b>Подтверждение добавления</b>\n\n"
        f"<blockquote>"
        f"Имя: <b>{data.get('add_name')}</b>\n"
        f"TONAPI Key: <b>{data.get('add_tonapi_key', '')[:20]}...</b>\n"
        f"Мнемоника: <b>{data.get('add_mnemonic', [''])[0]}...</b> (24 слова)\n"
        f"Fragment Hash: <b>{'резервный указан' if data.get('add_fragment_hash') else 'автоматически'}</b>\n"
        f"Токены сессии: <b>✅ Заполнены</b>"
        f"</blockquote>\n\n"
        "Всё верно? Подтвердите добавление:"
    )

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=text,
        reply_markup=get_fragment_confirm_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == AdminCallback.FRAGMENT_CONFIRM)
async def callback_fragment_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение и сохранение нового аккаунта."""
    if not await _check_admin(callback):
        return

    data = await state.get_data()
    preflight_username = callback.from_user.username
    if not preflight_username:
        await callback.answer(
            "❌ Для проверки cookies у админа должен быть Telegram username.",
            show_alert=True,
        )
        return

    async with async_session_factory() as session:
        service = FragmentAccountService(session)

        try:
            account = await service.create_account(
                name=data.get("add_name"),
                tonapi_key=data.get("add_tonapi_key"),
                mnemonic=data.get("add_mnemonic"),
                fragment_hash=data.get("add_fragment_hash", ""),
                stel_token=data.get("add_stel_token"),
                stel_ssid=data.get("add_stel_ssid"),
                stel_ton_token=data.get("add_stel_ton_token"),
                stel_dt=data.get("add_stel_dt", "-300"),
            )
            await session.commit()

            check_result = await _check_single_account(account.id, preflight_username=preflight_username)
            if check_result.get("success"):
                # Пересоздаём воркер только после успешной проверки cookies.
                await trigger_reconfigure()
                await callback.answer("✅ Аккаунт добавлен и проверен!", show_alert=True)
            else:
                async with async_session_factory() as check_session:
                    check_service = FragmentAccountService(check_session)
                    if check_result.get("session_expired"):
                        await check_service.mark_session_expired(
                            account.id,
                            check_result.get("error") or "Fragment session expired",
                        )
                    else:
                        await check_service.set_status(
                            account.id,
                            FragmentAccountStatus.DISABLED,
                            check_result.get("error") or "Fragment preflight failed",
                        )
                    await check_session.commit()
                await callback.answer("⚠️ Аккаунт сохранён, но проверка не пройдена", show_alert=True)
            logger.info(f"Fragment account created: {account.id} by admin {callback.from_user.id}")

        except Exception as e:
            logger.error(f"Error creating fragment account: {e}")
            await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
            await state.clear()
            return

    await state.clear()

    # Показываем созданный аккаунт
    status_text = "✅ Активен" if check_result.get("success") else "🔴 Не активен"
    error_text = ""
    if not check_result.get("success"):
        error_text = (
            "\n\n<b>Ошибка проверки:</b>\n"
            f"<blockquote>{html.escape(str(check_result.get('error') or 'Неизвестная ошибка'))}</blockquote>"
        )

    await callback.message.edit_text(
        text=(
            f"✅ <b>Аккаунт создан!</b>\n\n"
            f"<b>Имя:</b> {account.name}\n"
            f"<b>ID:</b> {account.id}\n"
            f"<b>Статус:</b> {status_text}"
            f"{error_text}"
        ),
        reply_markup=get_fragment_detail_keyboard(account.id, bool(check_result.get("success"))),
        parse_mode="HTML",
    )


@router.callback_query(F.data == AdminCallback.FRAGMENT_CANCEL)
async def callback_fragment_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена добавления/редактирования."""
    data = await state.get_data()
    edit_account_id = data.get("edit_account_id")
    session_account_id = data.get("session_account_id")

    await state.clear()

    # Если редактировали аккаунт — возвращаемся к меню редактирования
    if edit_account_id:
        edit_callback = callback.model_copy(update={"data": f"admin:fragment:edit:{edit_account_id}"})
        await callback_fragment_edit_menu(edit_callback)
    # Если обновляли сессию — возвращаемся к просмотру аккаунта
    elif session_account_id:
        view_callback = callback.model_copy(update={"data": f"admin:fragment:view:{session_account_id}"})
        await callback_fragment_view(view_callback)
    # Иначе — в главное меню
    else:
        await callback_fragment_menu(callback, state)


# ==================== ПРОВЕРКА АККАУНТА (внутренняя) ====================


def _format_fragment_auth_error(error: Exception) -> str:
    """Понятное описание ошибки авторизации Fragment."""
    if isinstance(error, SessionExpiredError):
        method = getattr(error, "method", "unknown")
        return (
            "Сессия Fragment истекла. "
            f"Метод: {method}. "
            "Обновите cookies из свежей browser-сессии."
        )
    if isinstance(error, FragmentAccessDeniedError):
        method = getattr(error, "method", "unknown")
        return (
            "Fragment отклонил покупочный запрос Access denied. "
            f"Метод: {method}. "
            "Это может быть не истекшая сессия, а несовпадение кошелька, IP/прокси, "
            "ограничение аккаунта Fragment или изменение антифрод-проверок."
        )
    return str(error)


async def _check_single_account(account_id: int, preflight_username: str | None = None) -> dict:
    """
    Проверить один аккаунт Fragment.

    Проверяет:
    1. Подключение к TON API (получение баланса)
    2. Валидность сессии Fragment (тестовый запрос)

    Returns:
        dict с результатами проверки
    """
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        account_data = await service.get_account_data(account_id)

        if not account_data:
            return {"success": False, "error": "Аккаунт не найден"}

        result = {
            "account_id": account_id,
            "success": False,
            "balance": 0.0,
            "error": None,
            "session_valid": False,
            "wallet_valid": False,
            "session_expired": False,
        }

        client = None
        try:
            # Создаём клиент
            config = FragmentConfig.from_account_data(account_data)
            client = FragmentClient(config)

            # 1. Проверяем баланс кошелька (тест TON API)
            try:
                balance = await client.get_balance()
                result["balance"] = balance
                result["wallet_valid"] = True

                # Обновляем баланс в БД
                await service.update_balance(account_id, Decimal(str(balance)))

                # Сохраняем адрес кошелька если ещё не сохранён
                wallet_address = await client.get_wallet_address()
                if wallet_address:
                    await service.update_account(account_id, wallet_address=wallet_address)
            except Exception as e:
                logger.warning(f"Account {account_id}: wallet check failed: {e}")
                result["error"] = f"Ошибка кошелька: {str(e)[:50]}"

            # 2. Проверяем сессию Fragment.
            # Если есть username админа, делаем сильную проверку через initBuyStarsRequest
            # без оплаты. Поиск получателя сам по себе не гарантирует валидность cookies.
            try:
                if preflight_username:
                    await client.preflight_stars_purchase(
                        preflight_username,
                        quantity=PREFLIGHT_STARS_QUANTITY,
                    )
                else:
                    await client.search_stars_recipient("telegram", use_cache=False)
                result["session_valid"] = True
            except SessionExpiredError as e:
                error = _format_fragment_auth_error(e)
                logger.warning(f"Account {account_id}: {error}")
                result["error"] = error
                result["session_expired"] = True
                await service.mark_session_expired(account_id, error)
            except FragmentAccessDeniedError as e:
                error = _format_fragment_auth_error(e)
                logger.warning(f"Account {account_id}: {error}")
                result["error"] = error
            except RecipientNotFoundError as e:
                logger.warning(f"Account {account_id}: preflight recipient failed: {e}")
                result["error"] = (
                    f"Тестовый username @{preflight_username or 'telegram'} не подходит "
                    "для проверки Stars. Укажите аккаунт Telegram с username, который может получать Stars."
                )
            except Exception as e:
                logger.warning(f"Account {account_id}: session check failed: {e}")
                # Не помечаем как истёкшую, может быть временная ошибка
                if not result["error"]:
                    result["error"] = f"Ошибка сессии: {str(e)[:50]}"

            # 3. Определяем итоговый статус
            if result["wallet_valid"] and result["session_valid"]:
                result["success"] = True
                # Если аккаунт был неактивен, активируем его
                account = await service.get_account(account_id)
                if account and account.status == FragmentAccountStatus.SESSION_EXPIRED.value:
                    await service.set_status(account_id, FragmentAccountStatus.ACTIVE)
                    account.is_active = True

            # Обновляем время последней проверки
            account = await service.get_account(account_id)
            if account:
                account.last_session_check = datetime.utcnow()

            await session.commit()

        except Exception as e:
            logger.exception(f"Account {account_id}: check failed: {e}")
            result["error"] = str(e)[:100]
        finally:
            if client:
                await client.close()

        return result


def _needs_refresh(last_check: datetime | None) -> bool:
    """Проверить, нужно ли обновлять данные аккаунта."""
    if last_check is None:
        return True
    return datetime.utcnow() - last_check > CACHE_TTL


async def _auto_refresh_account(account_id: int) -> dict | None:
    """
    Автоматически обновить данные аккаунта если кэш устарел.

    Returns:
        dict с результатами проверки или None если кэш актуален
    """
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        account = await service.get_account(account_id)

        if not account:
            return None

        if not _needs_refresh(account.last_session_check):
            # Кэш актуален, не нужно обновлять
            return None

    # Кэш устарел, обновляем
    return await _check_single_account(account_id)


async def _auto_refresh_all_accounts() -> int:
    """
    Автоматически обновить все аккаунты с устаревшим кэшем.

    Returns:
        Количество обновлённых аккаунтов
    """
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        accounts = await service.get_all_accounts()

    refreshed = 0
    for account in accounts:
        if _needs_refresh(account.last_session_check):
            await _check_single_account(account.id)
            refreshed += 1
            # Небольшая задержка чтобы не перегружать API
            await asyncio.sleep(0.3)

    return refreshed
