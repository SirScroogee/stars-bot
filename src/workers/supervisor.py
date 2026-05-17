"""
WorkerSupervisor — автоматическое управление воркерами.

Функции:
- Автоматический расчёт concurrent на основе количества активных Fragment аккаунтов
- Мониторинг состояния воркера
- Автоперезапуск при падении
- Пересоздание при изменении количества аккаунтов
"""
import asyncio
import logging
import time
from typing import Optional

from src.db.session import async_session_factory
from src.services.fragment_account_service import FragmentAccountService
from src.workers.order_worker import (
    OrderWorker,
    get_order_queue,
    set_order_worker,
)

logger = logging.getLogger(__name__)


class WorkerSupervisor:
    """
    Супервизор для автоматического управления воркерами.

    Гарантирует:
    - Один воркер с concurrent = количество активных аккаунтов
    - Автоперезапуск при падении
    - Пересоздание при изменении конфигурации
    """

    def __init__(
        self,
        check_interval: float = 5.0,
        restart_delay: float = 2.0,
        max_restart_attempts: int = 5,
        restart_window: float = 300.0,  # 5 минут
    ):
        """
        Args:
            check_interval: Интервал проверки состояния воркера (секунды)
            restart_delay: Задержка перед перезапуском (секунды)
            max_restart_attempts: Макс. количество перезапусков за restart_window
            restart_window: Окно для подсчёта перезапусков (секунды)
        """
        self._check_interval = check_interval
        self._restart_delay = restart_delay
        self._max_restart_attempts = max_restart_attempts
        self._restart_window = restart_window

        self._worker: Optional[OrderWorker] = None
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        # Трекинг перезапусков
        self._restart_timestamps: list[float] = []

        # Callback для уведомлений о заказах
        self._on_order_completed = None
        self._on_order_failed = None

        # Статистика
        self._stats = {
            "started_at": None,
            "restarts": 0,
            "reconfigurations": 0,
            "last_restart_at": None,
            "last_reconfigure_at": None,
        }

    def set_order_callbacks(
        self,
        on_completed=None,
        on_failed=None,
    ) -> None:
        """Установить callback'и для уведомлений о заказах."""
        self._on_order_completed = on_completed
        self._on_order_failed = on_failed

        # Если воркер уже запущен, обновляем его callbacks
        if self._worker:
            self._worker.set_callbacks(
                on_completed=on_completed,
                on_failed=on_failed,
            )

    async def _get_active_accounts_count(self) -> int:
        """Получить количество активных Fragment аккаунтов."""
        async with async_session_factory() as session:
            service = FragmentAccountService(session)
            accounts = await service.get_all_active_accounts()
            return len(accounts)

    async def start(self) -> None:
        """Запустить супервизор и воркер."""
        if self._running:
            logger.warning("[Supervisor] Already running")
            return

        logger.info("[Supervisor] Starting with auto-configuration...")

        self._running = True
        self._stats["started_at"] = time.time()

        # Создаём и запускаем воркер
        await self._create_and_start_worker()

        # Запускаем мониторинг
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        logger.info("[Supervisor] Started successfully")

    async def stop(self, wait_for_active: bool = True) -> None:
        """Остановить супервизор и воркер."""
        if not self._running:
            return

        logger.info("[Supervisor] Stopping...")

        self._running = False

        # Останавливаем мониторинг
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # Останавливаем воркер
        if self._worker:
            await self._worker.stop(wait_for_active=wait_for_active)
            self._worker = None
            set_order_worker(None)

        logger.info("[Supervisor] Stopped")

    async def reconfigure(self) -> None:
        """
        Пересоздать воркер с новой конфигурацией.

        Вызывается при изменении количества активных аккаунтов.
        """
        if not self._running:
            logger.warning("[Supervisor] Not running, cannot reconfigure")
            return

        logger.info("[Supervisor] Reconfiguring worker...")

        # Получаем новое количество аккаунтов
        new_count = await self._get_active_accounts_count()
        current_concurrent = self._worker._max_concurrent_orders if self._worker else 0

        # Если количество не изменилось, ничего не делаем
        if new_count == current_concurrent and self._worker and self._worker.is_running:
            logger.info(f"[Supervisor] No change needed (concurrent={new_count})")
            return

        logger.info(f"[Supervisor] Account count changed: {current_concurrent} -> {new_count}")

        # Останавливаем текущий воркер
        if self._worker:
            logger.info("[Supervisor] Stopping current worker...")
            await self._worker.stop(wait_for_active=True, timeout=30.0)
            self._worker = None

        # Создаём новый воркер
        await self._create_and_start_worker()

        self._stats["reconfigurations"] += 1
        self._stats["last_reconfigure_at"] = time.time()

        logger.info("[Supervisor] Reconfiguration complete")

    async def _create_and_start_worker(self) -> None:
        """Создать и запустить воркер с автоконфигурацией."""
        # Получаем количество активных аккаунтов
        active_count = await self._get_active_accounts_count()

        if active_count == 0:
            logger.warning("[Supervisor] No active Fragment accounts, worker not started")
            logger.warning("[Supervisor] Orders will queue until an account is activated")
            return

        logger.info(f"[Supervisor] Found {active_count} active Fragment accounts")
        logger.info(f"[Supervisor] Creating worker with concurrent={active_count}")

        # Создаём воркер
        self._worker = OrderWorker(
            queue=get_order_queue(),
            max_concurrent_orders=active_count,
            worker_id="auto-worker",
        )

        # Устанавливаем callbacks
        if self._on_order_completed or self._on_order_failed:
            self._worker.set_callbacks(
                on_completed=self._on_order_completed,
                on_failed=self._on_order_failed,
            )

        # Запускаем
        await self._worker.start()

        # Регистрируем глобально для get_order_worker()
        set_order_worker(self._worker)

        logger.info(f"[Supervisor] Worker started (concurrent={active_count})")

    async def _monitor_loop(self) -> None:
        """Цикл мониторинга состояния воркера."""
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)

                if not self._running:
                    break

                await self._check_worker_health()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[Supervisor] Error in monitor loop: {e}")

    async def _check_worker_health(self) -> None:
        """Проверить состояние воркера и перезапустить при необходимости."""
        # Проверяем, нужен ли воркер
        active_count = await self._get_active_accounts_count()

        if active_count == 0:
            # Нет аккаунтов - воркер не нужен
            if self._worker and self._worker.is_running:
                logger.info("[Supervisor] No active accounts, stopping worker")
                await self._worker.stop(wait_for_active=True)
                self._worker = None
            return

        # Есть аккаунты, проверяем воркер
        if self._worker is None or not self._worker.is_running:
            logger.warning("[Supervisor] Worker is not running! Attempting restart...")

            # Проверяем лимит перезапусков
            if self._is_restart_limit_exceeded():
                logger.error(
                    f"[Supervisor] Restart limit exceeded "
                    f"({self._max_restart_attempts} restarts in {self._restart_window}s). "
                    f"Pausing for 60 seconds..."
                )
                await asyncio.sleep(60)
                self._restart_timestamps.clear()

            # Перезапускаем
            await self._restart_worker()

    def _is_restart_limit_exceeded(self) -> bool:
        """Проверить, не превышен ли лимит перезапусков."""
        now = time.time()

        # Удаляем старые timestamps
        self._restart_timestamps = [
            ts for ts in self._restart_timestamps
            if now - ts < self._restart_window
        ]

        return len(self._restart_timestamps) >= self._max_restart_attempts

    async def _restart_worker(self) -> None:
        """Перезапустить воркер."""
        logger.info(f"[Supervisor] Restarting worker in {self._restart_delay}s...")

        await asyncio.sleep(self._restart_delay)

        # Останавливаем старый воркер если есть
        if self._worker:
            try:
                await self._worker.stop(wait_for_active=False, timeout=5.0)
            except Exception as e:
                logger.warning(f"[Supervisor] Error stopping old worker: {e}")
            self._worker = None

        # Создаём новый
        await self._create_and_start_worker()

        # Записываем статистику
        self._restart_timestamps.append(time.time())
        self._stats["restarts"] += 1
        self._stats["last_restart_at"] = time.time()

        if self._worker:
            logger.info("[Supervisor] Worker restarted successfully")
        else:
            logger.warning("[Supervisor] Worker restart failed (no active accounts?)")

    def get_status(self) -> dict:
        """Получить статус супервизора и воркера."""
        worker_stats = {}
        pool_stats = {}

        if self._worker:
            worker_stats = {
                "is_running": self._worker.is_running,
                "max_concurrent": self._worker._max_concurrent_orders,
                "active_orders": len(self._worker._active_orders),
                "orders_processed": self._worker._stats.get("orders_processed", 0),
                "orders_succeeded": self._worker._stats.get("orders_succeeded", 0),
                "orders_failed": self._worker._stats.get("orders_failed", 0),
            }
            pool_stats = self._worker.get_pool_stats()

        return {
            "supervisor": {
                "is_running": self._running,
                "started_at": self._stats["started_at"],
                "restarts": self._stats["restarts"],
                "reconfigurations": self._stats["reconfigurations"],
                "last_restart_at": self._stats["last_restart_at"],
                "last_reconfigure_at": self._stats["last_reconfigure_at"],
            },
            "worker": worker_stats,
            "pool": pool_stats,
        }

    @property
    def worker(self) -> Optional[OrderWorker]:
        """Получить текущий воркер."""
        return self._worker

    @property
    def is_running(self) -> bool:
        """Проверить, запущен ли супервизор."""
        return self._running


# Глобальный экземпляр супервизора
_supervisor: Optional[WorkerSupervisor] = None


def get_supervisor() -> Optional[WorkerSupervisor]:
    """Получить глобальный экземпляр супервизора."""
    return _supervisor


def set_supervisor(supervisor: WorkerSupervisor) -> None:
    """Установить глобальный экземпляр супервизора."""
    global _supervisor
    _supervisor = supervisor


async def create_and_start_supervisor() -> WorkerSupervisor:
    """Создать и запустить супервизор."""
    global _supervisor

    supervisor = WorkerSupervisor()
    await supervisor.start()

    _supervisor = supervisor

    return supervisor


async def stop_supervisor() -> None:
    """Остановить супервизор."""
    global _supervisor

    if _supervisor:
        await _supervisor.stop()
        _supervisor = None


async def trigger_reconfigure() -> None:
    """
    Триггер пересоздания воркера.

    Вызывается при изменении количества активных аккаунтов.
    """
    if _supervisor and _supervisor.is_running:
        await _supervisor.reconfigure()
    else:
        logger.warning("[Supervisor] Cannot reconfigure: supervisor not running")
