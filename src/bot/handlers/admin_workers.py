"""
Handlers для просмотра статуса воркеров (автонастройка).

Режим автонастройки:
- Один воркер с concurrent = количество активных Fragment аккаунтов
- Автоперезапуск при падении
- Автоматическая реконфигурация при изменении аккаунтов
"""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from src.bot.handlers.admin_utils import check_admin
from src.bot.keyboards.admin import (
    AdminCallback,
    get_settings_menu_keyboard,
    get_workers_status_keyboard,
)
from src.db.session import async_session_factory
from src.services.fragment_account_service import FragmentAccountService
from src.workers.supervisor import get_supervisor
from src.services.bot_settings_service import get_bot_settings

logger = logging.getLogger(__name__)

# Алиас для обратной совместимости
_check_admin = check_admin

router = Router(name="admin_workers")


def format_supervisor_status(status: dict, active_accounts_count: int = 0) -> str:
    """Форматировать статус супервизора и воркера."""
    supervisor_info = status.get("supervisor", {})
    worker_info = status.get("worker", {})
    pool_info = status.get("pool", {})

    # Статус супервизора
    supervisor_running = supervisor_info.get("is_running", False)
    restarts = supervisor_info.get("restarts", 0)
    reconfigurations = supervisor_info.get("reconfigurations", 0)

    # Статус воркера
    worker_running = worker_info.get("is_running", False)
    max_concurrent = worker_info.get("max_concurrent", 0)
    active_orders = worker_info.get("active_orders", 0)
    orders_processed = worker_info.get("orders_processed", 0)
    orders_succeeded = worker_info.get("orders_succeeded", 0)
    orders_failed = worker_info.get("orders_failed", 0)

    # Информация о пуле аккаунтов (используем реальное количество из БД)
    pool_size = active_accounts_count
    accounts = pool_info.get("accounts", {})

    # Форматируем статусы (иконка отдельно от текста)
    supervisor_icon = "🟢" if supervisor_running else "🔴"
    supervisor_status_text = "Работает" if supervisor_running else "Остановлен"
    worker_icon = "🟢" if worker_running else "🔴"
    worker_status_text = "Работает" if worker_running else "Остановлен"

    text = (
        f"<b>🔧 Воркеры</b>\n\n"
        f"<blockquote>"
        f"⚙️ Режим: <b>Автонастройка</b>\n"
        f"💎 Активных аккаунтов: <b>{pool_size}</b>\n"
        f"⚡ Параллельность: <b>{max_concurrent}</b>"
        f"</blockquote>\n\n"
    )

    # Статус супервизора
    text += (
        f"<blockquote>"
        f"<b>🛡️ Супервизор</b>\n"
        f"{supervisor_icon} Статус: <b>{supervisor_status_text}</b>\n"
        f"🔄 Перезапусков: <b>{restarts}</b>\n"
        f"🔧 Реконфигураций: <b>{reconfigurations}</b>"
        f"</blockquote>\n\n"
    )

    # Статус воркера
    text += (
        f"<blockquote>"
        f"<b>📊 Воркер</b>\n"
        f"{worker_icon} Статус: <b>{worker_status_text}</b>\n"
        f"📦 В обработке: <b>{active_orders}</b>\n"
        f"✅ Выполнено: <b>{orders_succeeded}</b>\n"
        f"❌ Ошибок: <b>{orders_failed}</b>\n"
        f"📈 Всего: <b>{orders_processed}</b>"
        f"</blockquote>\n\n"
    )

    # Информация по аккаунтам
    if accounts:
        text += (
            f"<blockquote>"
            f"<b>🔥 Аккаунты (в работе)</b>\n"
        )
        acc_lines = []
        for acc_id, acc_info in accounts.items():
            warmth = acc_info.get("warmth", {})
            cb_state = acc_info.get("circuit_breaker_state", "unknown")

            if warmth:
                score = warmth.get("score", 0)
                success_rate = warmth.get("success_rate", 0)
                total = warmth.get("total_transactions", 0)

                # Иконка состояния circuit breaker
                cb_icon = "🟢" if cb_state == "closed" else ("🟡" if cb_state == "half_open" else "🔴")

                acc_lines.append(f"{cb_icon} #{acc_id}: score=<b>{score}</b>, success=<b>{success_rate:.0%}</b>, tx=<b>{total}</b>")
            else:
                acc_lines.append(f"⚪ #{acc_id}: нет данных")
        text += "\n".join(acc_lines)
        text += "</blockquote>\n"
    elif pool_size > 0:
        text += "✨ <i>Аккаунты готовы к работе</i>\n"
        text += "📊 <i>Статистика появится после первых заказов</i>\n"
    else:
        text += "⚠️ <i>Нет активных аккаунтов</i>\n"
        text += "💡 <i>Добавьте аккаунт в разделе Fragment</i>\n"

    return text


def format_no_supervisor() -> str:
    """Сообщение когда супервизор не запущен."""
    return (
        "<b>🔧 Воркеры</b>\n\n"
        "<blockquote>"
        "🔴 Статус: <b>Супервизор не запущен</b>\n\n"
        "ℹ️ Воркер автоматически запускается при старте бота.\n"
        "Если вы видите это сообщение, возможно произошла ошибка при запуске."
        "</blockquote>\n\n"
        "<blockquote>"
        "🔍 <b>Проверьте:</b>\n"
        "• 💎 Есть ли активные Fragment аккаунты\n"
        "• 📋 Логи бота на наличие ошибок"
        "</blockquote>"
    )


# ==================== HANDLERS ====================

@router.callback_query(F.data == AdminCallback.WORKERS)
async def show_workers(callback: CallbackQuery):
    """Показать статус воркеров."""
    if not await _check_admin(callback):
        return

    supervisor = get_supervisor()

    # Получаем реальное количество активных аккаунтов из БД
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        active_accounts = await service.get_all_active_accounts()
        active_accounts_count = len(active_accounts)

    if not supervisor or not supervisor.is_running:
        text = format_no_supervisor()
    else:
        status = supervisor.get_status()
        text = format_supervisor_status(status, active_accounts_count)

    await callback.message.edit_text(
        text,
        reply_markup=get_workers_status_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == AdminCallback.WORKERS_REFRESH)
async def refresh_workers(callback: CallbackQuery):
    """Обновить статус воркеров."""
    if not await _check_admin(callback):
        return

    supervisor = get_supervisor()

    # Получаем реальное количество активных аккаунтов из БД
    async with async_session_factory() as session:
        service = FragmentAccountService(session)
        active_accounts = await service.get_all_active_accounts()
        active_accounts_count = len(active_accounts)

    if not supervisor or not supervisor.is_running:
        text = format_no_supervisor()
    else:
        status = supervisor.get_status()
        text = format_supervisor_status(status, active_accounts_count)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=get_workers_status_keyboard(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await callback.answer("Обновлено")


@router.callback_query(F.data == AdminCallback.WORKERS_BACK)
async def back_to_settings(callback: CallbackQuery):
    """Вернуться в меню настроек."""
    if not await _check_admin(callback):
        return

    settings = await get_bot_settings()

    await callback.message.edit_text(
        "<b>⚙️ Настройки</b>\n\nВыберите раздел:",
        reply_markup=get_settings_menu_keyboard(settings),
        parse_mode="HTML",
    )
    await callback.answer()
