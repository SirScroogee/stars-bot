"""
Handlers для управления заказами в админ-панели.
"""
import logging
from datetime import datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, LinkPreviewOptions
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.bot.handlers.admin_utils import check_admin, to_moscow_time
from src.bot.keyboards.admin import (
    AdminCallback,
    get_admin_menu_keyboard,
    get_orders_menu_keyboard,
    get_orders_list_keyboard,
    get_order_detail_keyboard,
    get_orders_stats_keyboard,
    get_orders_cancel_keyboard,
    get_order_confirm_action_keyboard,
    get_orders_problems_keyboard,
)
from src.db.models import Order
from src.db.session import async_session_factory
from src.core.queue import get_order_queue
from src.services.order_service import OrderService
from src.services.user_service import UserService
from src.workers.order_worker import get_order_worker

logger = logging.getLogger(__name__)

router = Router(name="admin_orders")

# Алиасы для обратной совместимости
_check_admin = check_admin
_to_moscow_time = to_moscow_time


async def _enqueue_order_for_retry(order_id: int) -> None:
    worker = get_order_worker()
    if worker and worker.is_running:
        await worker.enqueue_order(order_id)
    else:
        await get_order_queue().enqueue(order_id)


class OrdersStates(StatesGroup):
    """Состояния для работы с заказами."""
    search = State()


# ==================== ГЛАВНОЕ МЕНЮ ЗАКАЗОВ ====================


@router.callback_query(F.data == AdminCallback.MANAGE_ORDERS)
async def callback_orders_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню управления заказами."""
    if not await _check_admin(callback):
        return

    await state.clear()

    async with async_session_factory() as session:
        order_service = OrderService(session)
        stats = await order_service.get_orders_stats("all")
        problems = await order_service.get_problem_orders()

    text = "📦 <b>Управление заказами</b>\n\n"

    # Блок общей статистики
    text += (
        f"<blockquote>"
        f"📊 Всего заказов: <b>{stats['total']:,}</b>\n"
        f"✅ Выполнено: <b>{stats['completed']:,}</b>\n"
        f"⏳ В ожидании: <b>{stats['pending']:,}</b>\n"
        f"🔄 Обработка: <b>{stats['processing']:,}</b>\n"
        f"❌ Ошибки: <b>{stats['failed']:,}</b>"
        f"</blockquote>\n\n"
    )

    # Блок финансов
    text += (
        f"<blockquote>"
        f"💰 Выручка: <b>${stats['total_revenue_usdt']:.2f}</b>\n"
        f"⭐ Продано звёзд: <b>{stats['total_stars_sold']:,}</b>\n"
        f"💸 Потрачено TON: <b>{stats['total_ton_spent']:.2f}</b>\n"
        f"📈 Успешность: <b>{stats['success_rate']:.1f}%</b>"
        f"</blockquote>\n\n"
    )

    # Проблемные заказы
    if problems:
        failed_count = sum(1 for o in problems if o.status == "failed")
        stuck_count = len(problems) - failed_count
        text += (
            f"⚠️ <b>Проблемные заказы: {len(problems)}</b>\n"
            f"<blockquote>"
            f"❌ Ошибки: <b>{failed_count}</b>\n"
            f"🔄 Зависли: <b>{stuck_count}</b>"
            f"</blockquote>"
        )
    else:
        text += "✅ <b>Проблемных заказов нет</b>"

    await callback.message.edit_text(
        text=text,
        reply_markup=get_orders_menu_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == AdminCallback.ORDERS)
async def callback_orders_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в меню заказов."""
    await callback_orders_menu(callback, state)


# ==================== СПИСОК ЗАКАЗОВ ====================


@router.callback_query(F.data == AdminCallback.ORDERS_LIST)
async def callback_orders_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Список всех заказов."""
    if not await _check_admin(callback):
        return

    await state.clear()
    await _show_orders_list(callback, filter_type="all", page=0)


@router.callback_query(F.data.regexp(r"^admin:orders:filter:(all|pending|processing|completed|failed)$"))
async def callback_orders_filter(callback: CallbackQuery) -> None:
    """Фильтр списка заказов."""
    if not await _check_admin(callback):
        return

    filter_type = callback.data.split(":")[-1]
    await _show_orders_list(callback, filter_type=filter_type, page=0)


@router.callback_query(F.data.regexp(r"^admin:orders:page:\d+:(all|pending|processing|completed|failed)$"))
async def callback_orders_page(callback: CallbackQuery) -> None:
    """Пагинация списка заказов."""
    if not await _check_admin(callback):
        return

    parts = callback.data.split(":")
    page = int(parts[3])
    filter_type = parts[4]
    await _show_orders_list(callback, filter_type=filter_type, page=page)


@router.callback_query(F.data == "admin:orders:nop")
async def callback_orders_nop(callback: CallbackQuery) -> None:
    """Пустой callback для индикатора страницы."""
    await callback.answer()


async def _show_orders_list(callback: CallbackQuery, filter_type: str, page: int) -> None:
    """Показать список заказов."""
    async with async_session_factory() as session:
        order_service = OrderService(session)

        # Получаем заказы
        if filter_type == "all":
            orders = await order_service.get_all_orders(limit=200)
        else:
            orders = await order_service.get_all_orders(status_filter=filter_type, limit=200)

        total_count = len(orders)

    filter_names = {
        "all": "Все",
        "pending": "⏳ В ожидании",
        "processing": "🔄 Обработка",
        "completed": "✅ Выполнено",
        "failed": "❌ Ошибки",
    }

    text = (
        f"📦 <b>Список заказов</b>\n\n"
        f"<blockquote>"
        f"Фильтр: <b>{filter_names.get(filter_type, filter_type)}</b>\n"
        f"Найдено: <b>{total_count:,}</b>"
        f"</blockquote>"
    )

    if not orders:
        text += "\n\n<i>Заказов не найдено</i>"

    await callback.message.edit_text(
        text=text,
        reply_markup=get_orders_list_keyboard(orders, page=page, current_filter=filter_type),
        parse_mode="HTML",
    )


# ==================== ПРОСМОТР ЗАКАЗА ====================


@router.callback_query(F.data.regexp(r"^admin:orders:view:\d+$"))
async def callback_order_view(callback: CallbackQuery) -> None:
    """Просмотр детальной информации о заказе."""
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

        # Получаем информацию о пользователе
        user_service = UserService(session)
        user = await user_service.get_user(order.user_id)

        # Сохраняем данные платежа
        payment_data = order.payment

        # Ищем ID платежа пользователя из транзакций
        user_payment_id = None
        user_payment_type = None
        for tx in order.transactions:
            if tx.external_id:
                if tx.external_id.startswith("cryptobot"):
                    parts = tx.external_id.split(":")
                    if len(parts) == 2:
                        user_payment_id = parts[1]
                        user_payment_type = "cryptobot"
                        break
                elif tx.external_id.startswith("ton_"):
                    parts = tx.external_id.split(":")
                    if len(parts) == 2:
                        user_payment_id = parts[1]
                        user_payment_type = "ton"
                        break

        # Фоллбэк для старых заказов
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

    status_labels = {
        "pending": "Ожидает",
        "processing": "Обрабатывается",
        "completed": "Выполнен",
        "failed": "Ошибка",
        "cancelled": "Отменён",
        "refunded": "Возврат",
    }

    status_icons = {
        "pending": "⏳",
        "processing": "🔄",
        "completed": "✅",
        "failed": "❌",
        "cancelled": "🚫",
        "refunded": "💸",
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

    await callback.message.edit_text(
        text=text,
        reply_markup=get_order_detail_keyboard(order_id, order.status),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ==================== ПЕРЕХОД К ПОЛЬЗОВАТЕЛЮ ====================


@router.callback_query(F.data.regexp(r"^admin:orders:user:\d+$"))
async def callback_order_user(callback: CallbackQuery) -> None:
    """Переход к карточке пользователя заказа."""
    if not await _check_admin(callback):
        return

    order_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        order_service = OrderService(session)
        order = await order_service.get_order(order_id)

        if not order:
            await callback.answer("Заказ не найден", show_alert=True)
            return

    # Перенаправляем на карточку пользователя
    callback.data = f"admin:users:view:{order.user_id}"

    # Импортируем тут, чтобы избежать циклического импорта
    from src.bot.handlers.admin_users import callback_user_view
    await callback_user_view(callback)


# ==================== ПОИСК ЗАКАЗОВ ====================


@router.callback_query(F.data == AdminCallback.ORDERS_SEARCH)
async def callback_orders_search(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало поиска заказа."""
    if not await _check_admin(callback):
        return

    await state.set_state(OrdersStates.search)

    text = (
        "🔍 <b>Поиск заказа</b>\n\n"
        "<blockquote>"
        "Введите для поиска:\n"
        "• <b>Код заказа</b> (например: ABC12345)\n"
        "• <b>Username получателя</b> (например: durov)\n"
        "• <b>User ID</b> (например: 123456789)"
        "</blockquote>"
    )

    msg = await callback.message.edit_text(
        text=text,
        reply_markup=get_orders_cancel_keyboard(),
        parse_mode="HTML",
    )
    await state.update_data(bot_message_id=msg.message_id, bot_chat_id=callback.message.chat.id)


@router.message(OrdersStates.search)
async def message_orders_search(message: Message, state: FSMContext) -> None:
    """Обработка поискового запроса."""
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    bot_chat_id = data.get("bot_chat_id")

    query = message.text.strip()

    # Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    async with async_session_factory() as session:
        order_service = OrderService(session)
        orders = await order_service.search_orders(query)

    await state.clear()

    if not orders:
        text = (
            f"🔍 <b>Поиск: {query}</b>\n\n"
            "<i>Заказов не найдено</i>"
        )
        await message.bot.edit_message_text(
            chat_id=bot_chat_id,
            message_id=bot_message_id,
            text=text,
            reply_markup=get_orders_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    text = (
        f"🔍 <b>Поиск: {query}</b>\n\n"
        f"<blockquote>"
        f"Найдено: <b>{len(orders)}</b>"
        f"</blockquote>"
    )

    await message.bot.edit_message_text(
        chat_id=bot_chat_id,
        message_id=bot_message_id,
        text=text,
        reply_markup=get_orders_list_keyboard(orders, current_filter="all"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == AdminCallback.ORDERS_CANCEL)
async def callback_orders_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена поиска."""
    await state.clear()
    await callback_orders_menu(callback, state)


# ==================== ПРОБЛЕМНЫЕ ЗАКАЗЫ ====================


@router.callback_query(F.data == AdminCallback.ORDERS_PROBLEMS)
async def callback_orders_problems(callback: CallbackQuery) -> None:
    """Список проблемных заказов."""
    if not await _check_admin(callback):
        return

    await _show_problems_list(callback, page=0)


@router.callback_query(F.data.regexp(r"^admin:orders:problems:page:\d+$"))
async def callback_orders_problems_page(callback: CallbackQuery) -> None:
    """Пагинация проблемных заказов."""
    if not await _check_admin(callback):
        return

    page = int(callback.data.split(":")[-1])
    await _show_problems_list(callback, page=page)


async def _show_problems_list(callback: CallbackQuery, page: int) -> None:
    """Показать список проблемных заказов."""
    async with async_session_factory() as session:
        order_service = OrderService(session)
        orders = await order_service.get_problem_orders()

    failed_count = sum(1 for o in orders if o.status == "failed")
    stuck_count = len(orders) - failed_count

    text = (
        "⚠️ <b>Проблемные заказы</b>\n\n"
        f"<blockquote>"
        f"❌ Ошибки: <b>{failed_count}</b>\n"
        f"🔄 Зависли (>10 мин): <b>{stuck_count}</b>\n"
        f"📊 Всего: <b>{len(orders)}</b>"
        f"</blockquote>"
    )

    if not orders:
        text += "\n\n✅ <i>Проблемных заказов нет</i>"

    await callback.message.edit_text(
        text=text,
        reply_markup=get_orders_problems_keyboard(orders, page=page),
        parse_mode="HTML",
    )


# ==================== ДЕЙСТВИЯ С ЗАКАЗАМИ ====================


@router.callback_query(F.data.regexp(r"^admin:orders:retry:\d+$"))
async def callback_order_retry(callback: CallbackQuery) -> None:
    """Запрос на повторную обработку заказа."""
    if not await _check_admin(callback):
        return

    order_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        order_service = OrderService(session)
        order = await order_service.get_order(order_id)

        if not order:
            await callback.answer("Заказ не найден", show_alert=True)
            return

        if order.status not in ("failed", "processing"):
            await callback.answer("Можно повторить только неудачные заказы", show_alert=True)
            return

    text = (
        f"🔄 <b>Повторить заказ #{order_id}?</b>\n\n"
        f"<blockquote>"
        f"Получатель: <b>@{order.recipient_username}</b>\n"
        f"Количество: <b>{order.quantity:,}</b>\n"
        f"Ошибка: <code>{order.error_message[:100] if order.error_message else '—'}</code>"
        f"</blockquote>\n\n"
        "Заказ будет возвращён в очередь на обработку."
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_order_confirm_action_keyboard(order_id, "retry"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:orders:retry:confirm:\d+$"))
async def callback_order_retry_confirm(callback: CallbackQuery) -> None:
    """Подтверждение повторной обработки заказа."""
    if not await _check_admin(callback):
        return

    order_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        order_service = OrderService(session)
        success = await order_service.retry_order(order_id)
        await session.commit()

        if success:
            await _enqueue_order_for_retry(order_id)
            await callback.answer("✅ Заказ возвращён в очередь", show_alert=True)
            logger.info(f"Order {order_id} retried by admin {callback.from_user.id}")
        else:
            await callback.answer("❌ Не удалось повторить заказ", show_alert=True)

    # Возвращаемся к списку заказов
    callback.data = f"admin:orders:view:{order_id}"
    await callback_order_view(callback)


@router.callback_query(F.data == "admin:orders:retry:all")
async def callback_orders_retry_all(callback: CallbackQuery) -> None:
    """Запрос на повторную обработку всех неудачных заказов."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        order_service = OrderService(session)
        failed_orders = await order_service.get_failed_orders(limit=10000)

    if not failed_orders:
        await callback.answer("Нет неудачных заказов", show_alert=True)
        return

    text = (
        f"🔄 <b>Повторить все неудачные заказы?</b>\n\n"
        f"<blockquote>"
        f"Количество: <b>{len(failed_orders)}</b>"
        f"</blockquote>\n\n"
        "Все заказы будут возвращены в очередь на обработку."
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_order_confirm_action_keyboard(0, "retry_all"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:orders:retry_all:confirm:0")
async def callback_orders_retry_all_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение повторной обработки всех неудачных заказов."""
    if not await _check_admin(callback):
        return

    async with async_session_factory() as session:
        order_service = OrderService(session)
        failed_orders = await order_service.get_failed_orders(limit=10000)
        retry_order_ids = [
            order.id for order in failed_orders
            if order.payment_provider != "balance"
        ]
        count = await order_service.retry_all_failed()
        await session.commit()

    for order_id in retry_order_ids:
        await _enqueue_order_for_retry(order_id)

    await callback.answer(f"✅ Повторено заказов: {count}", show_alert=True)
    logger.info(f"All failed orders retried by admin {callback.from_user.id}: {count}")

    # Возвращаемся к проблемным заказам
    await callback_orders_problems(callback)


@router.callback_query(F.data.regexp(r"^admin:orders:cancel:\d+$"))
async def callback_order_cancel(callback: CallbackQuery) -> None:
    """Запрос на отмену заказа."""
    if not await _check_admin(callback):
        return

    order_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        order_service = OrderService(session)
        order = await order_service.get_order(order_id)

        if not order:
            await callback.answer("Заказ не найден", show_alert=True)
            return

        if order.status != "pending":
            await callback.answer("Можно отменить только заказы в ожидании", show_alert=True)
            return

    text = (
        f"❌ <b>Отменить заказ #{order_id}?</b>\n\n"
        f"<blockquote>"
        f"Получатель: <b>@{order.recipient_username}</b>\n"
        f"Количество: <b>{order.quantity:,}</b>\n"
        f"Сумма: <b>${order.price_usdt:.2f}</b>"
        f"</blockquote>\n\n"
        "⚠️ Деньги НЕ будут автоматически возвращены!"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_order_confirm_action_keyboard(order_id, "do_cancel"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:orders:do_cancel:confirm:\d+$"))
async def callback_order_cancel_confirm(callback: CallbackQuery) -> None:
    """Подтверждение отмены заказа."""
    if not await _check_admin(callback):
        return

    order_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        order_service = OrderService(session)
        success = await order_service.set_cancelled(order_id)
        await session.commit()

        if success:
            await callback.answer("✅ Заказ отменён", show_alert=True)
            logger.info(f"Order {order_id} cancelled by admin {callback.from_user.id}")
        else:
            await callback.answer("❌ Не удалось отменить заказ", show_alert=True)

    # Возвращаемся к просмотру заказа
    callback.data = f"admin:orders:view:{order_id}"
    await callback_order_view(callback)


@router.callback_query(F.data.regexp(r"^admin:orders:refund:\d+$"))
async def callback_order_refund(callback: CallbackQuery) -> None:
    """Запрос на возврат средств."""
    if not await _check_admin(callback):
        return

    order_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        order_service = OrderService(session)
        order = await order_service.get_order(order_id)

        if not order:
            await callback.answer("Заказ не найден", show_alert=True)
            return

        if order.status not in ("completed", "failed"):
            await callback.answer("Можно вернуть только выполненные или неудачные заказы", show_alert=True)
            return

    text = (
        f"💸 <b>Вернуть деньги за заказ #{order_id}?</b>\n\n"
        f"<blockquote>"
        f"Получатель: <b>@{order.recipient_username}</b>\n"
        f"Количество: <b>{order.quantity:,}</b>\n"
        f"Сумма: <b>${order.price_usdt:.2f}</b>"
        f"</blockquote>\n\n"
        "⚠️ Заказ будет помечен как «возврат». "
        "Фактический возврат средств нужно выполнить вручную!"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_order_confirm_action_keyboard(order_id, "do_refund"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin:orders:do_refund:confirm:\d+$"))
async def callback_order_refund_confirm(callback: CallbackQuery) -> None:
    """Подтверждение возврата средств."""
    if not await _check_admin(callback):
        return

    order_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        order_service = OrderService(session)
        success = await order_service.set_refunded(order_id)
        await session.commit()

        if success:
            await callback.answer("✅ Заказ помечен как возврат", show_alert=True)
            logger.info(f"Order {order_id} refunded by admin {callback.from_user.id}")
        else:
            await callback.answer("❌ Не удалось оформить возврат", show_alert=True)

    # Возвращаемся к просмотру заказа
    callback.data = f"admin:orders:view:{order_id}"
    await callback_order_view(callback)


# ==================== СТАТИСТИКА ====================


@router.callback_query(F.data == AdminCallback.ORDERS_STATS)
async def callback_orders_stats(callback: CallbackQuery) -> None:
    """Статистика заказов."""
    if not await _check_admin(callback):
        return

    await _show_orders_stats(callback, period="all")


@router.callback_query(F.data.regexp(r"^admin:orders:stats:(24h|7d|30d|all)$"))
async def callback_orders_stats_period(callback: CallbackQuery) -> None:
    """Статистика за выбранный период."""
    if not await _check_admin(callback):
        return

    period = callback.data.split(":")[-1]
    await _show_orders_stats(callback, period=period)


async def _show_orders_stats(callback: CallbackQuery, period: str) -> None:
    """Показать статистику заказов."""
    async with async_session_factory() as session:
        order_service = OrderService(session)
        stats = await order_service.get_orders_stats(period)

    period_names = {
        "24h": "за 24 часа",
        "7d": "за 7 дней",
        "30d": "за 30 дней",
        "all": "за всё время",
    }

    text = (
        f"📊 <b>Статистика заказов</b>\n"
        f"<i>{period_names.get(period, period)}</i>\n\n"
    )

    # Основная статистика
    text += (
        f"<blockquote>"
        f"📦 Всего заказов: <b>{stats['total']:,}</b>\n"
        f"✅ Выполнено: <b>{stats['completed']:,}</b>\n"
        f"⏳ В ожидании: <b>{stats['pending']:,}</b>\n"
        f"🔄 Обработка: <b>{stats['processing']:,}</b>\n"
        f"❌ Ошибки: <b>{stats['failed']:,}</b>\n"
        f"🚫 Отменено: <b>{stats['cancelled']:,}</b>\n"
        f"💸 Возвраты: <b>{stats['refunded']:,}</b>"
        f"</blockquote>\n\n"
    )

    # Финансы
    text += (
        f"<blockquote>"
        f"💰 Выручка: <b>${stats['total_revenue_usdt']:.2f}</b>\n"
        f"⭐ Продано звёзд: <b>{stats['total_stars_sold']:,}</b>\n"
        f"💸 Потрачено TON: <b>{stats['total_ton_spent']:.4f}</b>\n"
        f"📈 Средний чек: <b>${stats['avg_order_usdt']:.2f}</b>\n"
        f"✅ Успешность: <b>{stats['success_rate']:.1f}%</b>"
        f"</blockquote>"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=get_orders_stats_keyboard(period),
        parse_mode="HTML",
    )
