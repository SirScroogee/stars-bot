"""
Точка входа для Telegram бота.
"""
import asyncio
import logging
import os
import sys
import traceback

from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

from src.config import get_config
from src.bot.safe_bot import SafeBot
from src.db.session import dispose_engine
from src.services.telegram_logger import tg_logger, TelegramLogger
from src.bot.callback_utils import is_stale_callback_error, safe_callback_answer
from src.locales import get_user_locale, t
from src.services.order_notification_service import (
    set_notification_bot,
    notify_order_completed,
    notify_order_failed,
)

# Queue и Supervisor
from src.core.queue import RedisQueue, set_order_queue, InMemoryQueue
from src.workers.supervisor import (
    WorkerSupervisor,
    set_supervisor,
    get_supervisor,
)
from src.workers.platega_poller import PlategaPaymentPoller
from src.workers.lava_poller import LavaPaymentPoller
from src.workers.giveaway_worker import GiveawayWorker
from src.workers.order_monitor import OrderMonitor

# Импорт роутеров
from src.bot.handlers import admin, admin_admins, admin_broadcast, admin_fragment, admin_gifts, admin_giveaways, admin_orders, admin_users, admin_workers, bot_admin, checks, deposit, giveaways, inline, menu, premium, profile, stars, start

# Импорт middleware
from src.bot.middlewares.ban_check import BanCheckMiddleware
from src.bot.middlewares.giveaway_activity import GiveawayActivityMiddleware
from src.bot.middlewares.subscription_check import SubscriptionCheckMiddleware

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)

# Optional Telegram API proxy. Set TELEGRAM_PROXY locally if Telegram is unavailable directly.
LOCAL_TELEGRAM_PROXY: str | None = os.getenv("TELEGRAM_PROXY")


def register_user_middlewares(dp: Dispatcher) -> None:
    """Register user gates before activity tracking for every incoming action."""
    observers = (dp.message, dp.callback_query, dp.inline_query)

    for observer in observers:
        observer.outer_middleware(BanCheckMiddleware())
    for observer in observers:
        observer.outer_middleware(SubscriptionCheckMiddleware())
    for observer in observers:
        observer.outer_middleware(GiveawayActivityMiddleware())


async def main() -> None:
    """Запуск бота."""
    config = get_config()

    # Инициализация очереди
    redis_queue = None
    supervisor = None
    platega_poller = None
    lava_poller = None
    giveaway_worker = None
    order_monitor = None

    if config.redis_url:
        try:
            # Используем Redis для продакшена
            redis_queue = RedisQueue(config.redis_url)
            await redis_queue.connect()
            set_order_queue(redis_queue)
            logger.info("Redis queue initialized")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            logger.warning("Falling back to in-memory queue")
            set_order_queue(InMemoryQueue())
    else:
        # In-memory очередь для разработки
        set_order_queue(InMemoryQueue())
        logger.info("Using in-memory queue (no REDIS_URL configured)")

    # Initialize Telegram clients before workers so recovered orders can always
    # produce user and administrator notifications during startup.
    session = (
        AiohttpSession(proxy=LOCAL_TELEGRAM_PROXY, timeout=15)
        if LOCAL_TELEGRAM_PROXY
        else AiohttpSession(timeout=15)
    )
    bot = SafeBot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    TelegramLogger.set_bot(bot)
    set_notification_bot(bot)

    # Создаём и запускаем WorkerSupervisor (автонастройка воркеров)
    try:
        supervisor = WorkerSupervisor()
        set_supervisor(supervisor)
        supervisor.set_order_callbacks(
            on_completed=notify_order_completed,
            on_failed=notify_order_failed,
        )
        await supervisor.start()
        logger.info("WorkerSupervisor started with order notification callbacks")
    except Exception as e:
        logger.error(f"Failed to start WorkerSupervisor: {e}")
        # Продолжаем без supervisor'а — заказы будут накапливаться в очереди

    order_monitor = OrderMonitor()
    await order_monitor.start()

    platega_poller = PlategaPaymentPoller(bot)
    await platega_poller.start()

    lava_poller = LavaPaymentPoller(bot)
    await lava_poller.start()

    giveaway_worker = GiveawayWorker(bot)
    await giveaway_worker.start()

    # Создаём диспетчер
    dp = Dispatcher()

    # Ban/subscription gates run before giveaway activity tracking.
    register_user_middlewares(dp)

    # Регистрируем глобальный обработчик ошибок
    @dp.error()
    async def global_error_handler(event: ErrorEvent) -> bool:
        """
        Глобальный обработчик ошибок.
        Логирует ошибки и отправляет уведомление в Telegram.
        """
        exception = event.exception
        update = event.update

        if is_stale_callback_error(exception):
            callback = update.callback_query if update else None
            callback_user_id = callback.from_user.id if callback and callback.from_user else None
            logger.info("Ignoring expired callback query for user %s", callback_user_id)
            if callback and callback.message:
                try:
                    lang = get_user_locale(callback.from_user.language_code)
                    await callback.message.answer(t("common.callback_expired", lang))
                except Exception as recovery_error:
                    logger.debug("Could not send callback expiry recovery message: %s", recovery_error)
            return True

        # Получаем информацию о пользователе
        user_id = None
        user_info = "Unknown"
        if update:
            if update.message and update.message.from_user:
                user_id = update.message.from_user.id
                user_info = f"@{update.message.from_user.username or user_id}"
            elif update.callback_query and update.callback_query.from_user:
                user_id = update.callback_query.from_user.id
                user_info = f"@{update.callback_query.from_user.username or user_id}"
            elif update.inline_query and update.inline_query.from_user:
                user_id = update.inline_query.from_user.id
                user_info = f"@{update.inline_query.from_user.username or user_id}"

        # Логируем ошибку
        error_text = f"{type(exception).__name__}: {exception}"
        tb = traceback.format_exc()
        logger.error(f"Unhandled error for user {user_info}: {error_text}\n{tb}")

        # Отправляем уведомление в Telegram лог-канал
        try:
            await tg_logger.log_error(
                error_type=type(exception).__name__,
                error_message=str(exception),
                user_id=user_id,
                details=tb[:1000] if len(tb) > 1000 else tb,  # Ограничиваем размер traceback
            )
        except Exception as e:
            logger.error(f"Failed to send error to Telegram logger: {e}")

        callback = update.callback_query if update else None
        if callback:
            try:
                lang = get_user_locale(callback.from_user.language_code)
                await safe_callback_answer(
                    callback,
                    t("common.error", lang),
                    show_alert=True,
                )
            except Exception as callback_error:
                logger.debug("Could not close failed callback query: %s", callback_error)

        # Возвращаем True чтобы aiogram не логировал ошибку повторно
        return True

    # Регистрируем роутеры
    dp.include_router(bot_admin.router)  # Отслеживание добавления бота в каналы
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(admin_gifts.router)  # Отправка Telegram Gifts администраторами
    dp.include_router(admin_giveaways.router)
    dp.include_router(admin_admins.router)  # Управление админами
    dp.include_router(admin_broadcast.router)  # Рассылка сообщений
    dp.include_router(admin_fragment.router)  # Управление Fragment аккаунтами
    dp.include_router(admin_orders.router)  # Управление заказами
    dp.include_router(admin_users.router)  # Управление пользователями
    dp.include_router(admin_workers.router)  # Управление воркерами
    dp.include_router(stars.router)
    dp.include_router(premium.router)
    dp.include_router(deposit.router)
    dp.include_router(checks.router)
    dp.include_router(profile.router)
    dp.include_router(giveaways.router)
    dp.include_router(menu.router)
    dp.include_router(inline.router)

    # Запускаем бота
    logger.info("Starting bot...")

    try:
        # Удаляем webhook если был
        # Preserve successful_payment updates across short restarts. Gift top-up
        # payments are idempotent in the database, while dropping them could charge
        # an administrator without continuing the paid Gift operation.
        await bot.delete_webhook(drop_pending_updates=False)

        # Логируем запуск
        await tg_logger.log_bot_started()

        # Запускаем polling
        await dp.start_polling(bot)
    finally:
        # Graceful shutdown
        logger.info("Shutting down...")

        # Останавливаем WorkerSupervisor
        if platega_poller:
            await platega_poller.stop()
            logger.info("PlategaPaymentPoller stopped")

        if lava_poller:
            await lava_poller.stop()
            logger.info("LavaPaymentPoller stopped")

        if giveaway_worker:
            await giveaway_worker.stop()
            logger.info("GiveawayWorker stopped")

        if order_monitor:
            await order_monitor.stop()
            logger.info("OrderMonitor stopped")

        if supervisor:
            await supervisor.stop()
            logger.info("WorkerSupervisor stopped")

        try:
            from src.services.recipient_service import clear_client_cache

            await clear_client_cache()
            logger.info("Recipient Fragment clients closed")
        except Exception as e:
            logger.error(f"Error closing recipient Fragment clients: {e}")

        # Закрываем Redis соединение
        if redis_queue:
            await redis_queue.close()
            logger.info("Redis queue closed")

        # Закрываем пул соединений БД
        try:
            await dispose_engine()
            logger.info("Database engine disposed")
        except Exception as e:
            logger.error(f"Error disposing database engine: {e}")

        # Логируем остановку
        await tg_logger.log_bot_stopped()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
