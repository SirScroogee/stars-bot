"""
Точка входа для Telegram бота.
"""
import asyncio
import logging
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

# Импорт роутеров
from src.bot.handlers import admin, admin_admins, admin_broadcast, admin_fragment, admin_orders, admin_users, admin_workers, bot_admin, checks, deposit, inline, menu, premium, profile, stars, start

# Импорт middleware
from src.bot.middlewares.ban_check import BanCheckMiddleware

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)

# Локальный прокси для запуска из РФ. Перед деплоем на сервер держать None.
# Если нужно запустить локально через прокси, временно заменить на "http://127.0.0.1:10808".
LOCAL_TELEGRAM_PROXY: str | None = None


async def main() -> None:
    """Запуск бота."""
    config = get_config()

    # Инициализация очереди
    redis_queue = None
    supervisor = None

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

    # Создаём и запускаем WorkerSupervisor (автонастройка воркеров)
    try:
        supervisor = WorkerSupervisor()
        set_supervisor(supervisor)
        await supervisor.start()
        logger.info("WorkerSupervisor started")
    except Exception as e:
        logger.error(f"Failed to start WorkerSupervisor: {e}")
        # Продолжаем без supervisor'а — заказы будут накапливаться в очереди

    # Создаём бота с фильтрацией исходящих сообщений
    session = (
        AiohttpSession(proxy=LOCAL_TELEGRAM_PROXY)
        if LOCAL_TELEGRAM_PROXY
        else AiohttpSession()
    )
    bot = SafeBot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session
    )

    # Устанавливаем бота для логгера
    TelegramLogger.set_bot(bot)

    # Устанавливаем бота для уведомлений пользователям
    set_notification_bot(bot)

    # Устанавливаем callbacks для уведомлений о результатах заказов
    if supervisor:
        supervisor.set_order_callbacks(
            on_completed=notify_order_completed,
            on_failed=notify_order_failed,
        )
        logger.info("Order notification callbacks set")

    # Создаём диспетчер
    dp = Dispatcher()

    # Регистрируем middleware для проверки бана
    dp.message.middleware(BanCheckMiddleware())
    dp.callback_query.middleware(BanCheckMiddleware())
    dp.inline_query.middleware(BanCheckMiddleware())

    # Регистрируем глобальный обработчик ошибок
    @dp.error()
    async def global_error_handler(event: ErrorEvent) -> bool:
        """
        Глобальный обработчик ошибок.
        Логирует ошибки и отправляет уведомление в Telegram.
        """
        exception = event.exception
        update = event.update

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

        # Возвращаем True чтобы aiogram не логировал ошибку повторно
        return True

    # Регистрируем роутеры
    dp.include_router(bot_admin.router)  # Отслеживание добавления бота в каналы
    dp.include_router(start.router)
    dp.include_router(admin.router)
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
    dp.include_router(menu.router)
    dp.include_router(inline.router)

    # Запускаем бота
    logger.info("Starting bot...")

    try:
        # Удаляем webhook если был
        await bot.delete_webhook(drop_pending_updates=True)

        # Логируем запуск
        await tg_logger.log_bot_started()

        # Запускаем polling
        await dp.start_polling(bot)
    finally:
        # Graceful shutdown
        logger.info("Shutting down...")

        # Останавливаем WorkerSupervisor
        if supervisor:
            await supervisor.stop()
            logger.info("WorkerSupervisor stopped")

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
