"""
OrderWorker — обработчик очереди заказов.

Этот воркер:
- Берёт заказы из очереди
- Выбирает лучший Fragment аккаунт из БД (с warmth tracking)
- Выполняет оплату через PaymentService
- Обновляет статусы через OrderService
- Обновляет статистику аккаунта
- Управляет retry при временных ошибках

ВАЖНО:
- Один заказ = один результат (идемпотентность)
- Без двойных списаний
- Восстановление после падений
- Переиспользование клиентов (connection pooling)
- Умная балансировка аккаунтов
- Параллельная обработка (max_concurrent_orders)
- Поддержка нескольких воркеров (worker_id)
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from src.api_clients.fragment import FragmentClient
from src.api_clients.fragment.config import FragmentConfig
from src.core.queue import OrderQueue, QueueItem, get_order_queue, RedisQueue
from src.db.models import Order, OrderStatus, User, FragmentAccountStatus
from src.db.session import async_session_factory
from src.services.fragment_account_service import FragmentAccountService, FragmentAccountData
from src.services.order_service import OrderService
from src.services.payment_service import PaymentErrorType, PaymentService
from src.services.telegram_logger import tg_logger

logger = logging.getLogger(__name__)


@dataclass
class AccountWarmth:
    """
    Метрики "прогретости" аккаунта для умной балансировки.

    Чем выше warmth_score, тем лучше аккаунт подходит для следующего заказа.
    """
    account_id: int

    # Статистика за сессию
    success_count: int = 0
    failure_count: int = 0
    total_orders: int = 0

    # Временные метки
    last_used_at: float = 0.0  # timestamp последнего использования
    last_success_at: float = 0.0  # timestamp последнего успеха

    # Circuit breaker статус (из клиента)
    is_circuit_open: bool = False

    # Текущая нагрузка
    active_orders: int = 0

    @property
    def success_rate(self) -> float:
        """Процент успешных заказов (0.0 - 1.0)."""
        if self.total_orders == 0:
            return 1.0  # Новый аккаунт - даём шанс
        return self.success_count / self.total_orders

    @property
    def warmth_score(self) -> float:
        """
        Вычислить score для выбора аккаунта.

        Учитывает:
        - success_rate (важнее всего)
        - recency (давно использованные получают бонус)
        - active_orders (меньше нагрузка = лучше)
        - circuit breaker статус
        """
        if self.is_circuit_open:
            return -1000.0  # Аккаунт временно недоступен

        score = 0.0

        # 1. Success rate (40% веса) - от 0 до 40
        score += self.success_rate * 40

        # 2. Recency bonus (30% веса) - давно неиспользованные лучше
        # Чем больше прошло времени, тем выше бонус (до 30 очков за 5 минут простоя)
        now = time.time()
        idle_seconds = now - self.last_used_at if self.last_used_at > 0 else 300
        recency_bonus = min(idle_seconds / 300 * 30, 30)  # max 30 очков за 5 минут
        score += recency_bonus

        # 3. Load penalty (20% веса) - меньше активных заказов = лучше
        load_penalty = self.active_orders * 10  # -10 за каждый активный заказ
        score -= load_penalty

        # 4. Experience bonus (10% веса) - опытные аккаунты надёжнее
        # Но только если success_rate хороший
        if self.success_rate >= 0.8 and self.total_orders >= 5:
            score += 10

        return score


class OrderWorker:
    """
    Воркер для обработки заказов из очереди.

    Гарантии:
    - Идемпотентность: заказ в статусе COMPLETED не обрабатывается повторно
    - Retry: временные ошибки возвращают заказ в очередь с backoff
    - Восстановление: застрявшие заказы возвращаются в очередь при старте
    - Динамический выбор аккаунта: берётся лучший активный аккаунт из БД
    - Connection pooling: переиспользование клиентов для эффективности
    - Warmth tracking: умная балансировка нагрузки между аккаунтами
    - Concurrent processing: параллельная обработка нескольких заказов
    """

    def __init__(
        self,
        queue: OrderQueue | None = None,
        poll_interval: float = 0.1,  # Уменьшено для concurrent режима
        retry_delay_base: float = 5.0,
        insufficient_funds_retry_delay: float = 300.0,
        maintenance_interval: float = 300.0,
        fallback_config: FragmentConfig | None = None,
        max_pool_idle_time: float = 300.0,  # 5 минут
        max_concurrent_orders: int = 1,  # Параллельная обработка
        worker_id: str | None = None,  # ID для логирования
        order_timeout: float = 300.0,  # Таймаут обработки заказа (5 минут)
    ):
        """
        Args:
            queue: Очередь заказов (по умолчанию глобальная)
            poll_interval: Интервал опроса очереди (секунды)
            retry_delay_base: Базовая задержка retry (секунды)
            fallback_config: Fallback конфиг из .env (если нет аккаунтов в БД)
            max_pool_idle_time: Макс. время простоя клиента в пуле перед закрытием
            max_concurrent_orders: Макс. количество заказов обрабатываемых параллельно
            worker_id: Уникальный ID воркера для логирования (автогенерируется если не указан)
            order_timeout: Максимальное время обработки одного заказа (секунды)
        """
        self._queue = queue or get_order_queue()
        self._poll_interval = poll_interval
        self._retry_delay_base = retry_delay_base
        self._insufficient_funds_retry_delay = insufficient_funds_retry_delay
        self._maintenance_interval = maintenance_interval
        self._fallback_config = fallback_config
        self._max_pool_idle_time = max_pool_idle_time
        self._max_concurrent_orders = max_concurrent_orders
        self._worker_id = worker_id or f"worker-{id(self) % 10000:04d}"
        self._order_timeout = order_timeout

        self._running = False
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None

        # ===== CONCURRENT PROCESSING =====
        self._semaphore: asyncio.Semaphore | None = None
        self._active_tasks: set[asyncio.Task] = set()
        self._active_orders: set[int] = set()  # order_id которые сейчас в обработке

        # ===== CLIENT POOL =====
        # Пул клиентов: account_id -> (FragmentClient, last_used_timestamp)
        self._client_pool: dict[int, tuple[FragmentClient, float]] = {}
        self._pool_lock = asyncio.Lock()

        # ===== ACCOUNT TRANSACTION LOCKS =====
        # Семафор на аккаунт: гарантирует что один аккаунт выполняет одну транзакцию
        # Это критично для TON кошелька (seqno должен быть уникальным)
        self._account_locks: dict[int, asyncio.Semaphore] = {}
        self._account_locks_lock = asyncio.Lock()  # Lock для создания account locks

        # ===== ACCOUNT WARMTH TRACKING =====
        # Метрики прогретости: account_id -> AccountWarmth
        self._account_warmth: dict[int, AccountWarmth] = {}

        # Callback для уведомлений (опционально)
        self._on_order_completed: Callable[[Order], Awaitable[None]] | None = None
        self._on_order_failed: Callable[[Order, str], Awaitable[None]] | None = None

        # Статистика воркера
        self._stats = {
            "orders_processed": 0,
            "orders_succeeded": 0,
            "orders_failed": 0,
            "started_at": None,
        }

    def set_callbacks(
        self,
        on_completed: Callable[[Order], Awaitable[None]] | None = None,
        on_failed: Callable[[Order, str], Awaitable[None]] | None = None,
    ) -> None:
        """Установить callback'и для уведомлений о результатах."""
        self._on_order_completed = on_completed
        self._on_order_failed = on_failed

    async def start(self) -> None:
        """Запустить воркер."""
        if self._running:
            logger.warning(f"[{self._worker_id}] OrderWorker is already running")
            return

        logger.info(
            f"[{self._worker_id}] Starting OrderWorker "
            f"(max_concurrent={self._max_concurrent_orders})..."
        )

        # Инициализируем семафор для ограничения параллельности
        self._semaphore = asyncio.Semaphore(self._max_concurrent_orders)

        # Восстанавливаем застрявшие заказы
        await self._recover_stuck_orders()

        self._running = True
        self._stats["started_at"] = time.time()
        self._task = asyncio.create_task(self._run_loop())
        self._cleanup_task = asyncio.create_task(self._pool_cleanup_loop())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

        logger.info(f"[{self._worker_id}] OrderWorker started")

    async def stop(self, wait_for_active: bool = True, timeout: float = 30.0) -> None:
        """
        Остановить воркер.

        Args:
            wait_for_active: Ждать завершения активных заказов
            timeout: Максимальное время ожидания активных заказов
        """
        if not self._running:
            return

        logger.info(f"[{self._worker_id}] Stopping OrderWorker...")

        self._running = False

        # Ждём завершения активных задач
        if wait_for_active and self._active_tasks:
            logger.info(
                f"[{self._worker_id}] Waiting for {len(self._active_tasks)} active orders..."
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._active_tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[{self._worker_id}] Timeout waiting for active orders, cancelling..."
                )
                for task in self._active_tasks:
                    task.cancel()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass

        # Закрываем все клиенты в пуле
        await self._close_all_clients()

        logger.info(
            f"[{self._worker_id}] OrderWorker stopped. "
            f"Stats: processed={self._stats['orders_processed']}, "
            f"succeeded={self._stats['orders_succeeded']}, "
            f"failed={self._stats['orders_failed']}"
        )

    async def _close_all_clients(self) -> None:
        """Закрыть все клиенты в пуле."""
        async with self._pool_lock:
            for account_id, (client, _) in self._client_pool.items():
                try:
                    await client.close()
                    logger.debug(f"Closed pooled client for account {account_id}")
                except Exception as e:
                    logger.warning(f"Error closing client for account {account_id}: {e}")
            self._client_pool.clear()
            logger.info("All pooled clients closed")

    async def _pool_cleanup_loop(self) -> None:
        """Периодически очищать неиспользуемые клиенты из пула."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Проверяем каждую минуту
                await self._cleanup_idle_clients()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in pool cleanup loop: {e}")

    async def _cleanup_idle_clients(self) -> None:
        """Закрыть клиенты, которые простаивают дольше max_pool_idle_time."""
        now = time.time()
        to_remove = []

        async with self._pool_lock:
            for account_id, (client, last_used) in self._client_pool.items():
                idle_time = now - last_used
                if idle_time > self._max_pool_idle_time:
                    to_remove.append(account_id)

            for account_id in to_remove:
                client, _ = self._client_pool.pop(account_id)
                try:
                    await client.close()
                    logger.info(f"Closed idle client for account {account_id}")
                except Exception as e:
                    logger.warning(f"Error closing idle client {account_id}: {e}")

    async def _ack_if_redis(self, order_id: int) -> None:
        if isinstance(self._queue, RedisQueue):
            await self._queue.ack(order_id)

    async def _maintenance_loop(self) -> None:
        """Periodically recover stuck orders and requeue retryable failed orders."""
        while self._running:
            try:
                await asyncio.sleep(self._maintenance_interval)
                if not self._running:
                    break

                await self._recover_stuck_orders(
                    recover_redis_processing=False,
                    recover_pending=False,
                )
                await self._recover_retryable_failed_orders()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in order maintenance loop: {e}")

    async def _get_fragment_account(self) -> tuple[Optional[FragmentAccountData], Optional[int]]:
        """
        Получить лучший активный Fragment аккаунт из БД с учётом warmth.

        Алгоритм:
        1. Получаем все активные аккаунты из БД
        2. Применяем warmth scoring для каждого
        3. Выбираем аккаунт с лучшим score

        Returns:
            (FragmentAccountData, account_id) или (None, None) если нет аккаунтов
        """
        async with async_session_factory() as session:
            service = FragmentAccountService(session)
            accounts = await service.get_all_active_accounts()

            if not accounts:
                return None, None

            # Если один аккаунт - возвращаем его
            if len(accounts) == 1:
                return accounts[0], accounts[0].id

            # Выбираем лучший по warmth score
            best_account = None
            best_score = float('-inf')

            for account in accounts:
                warmth = self._get_or_create_warmth(account.id)

                # Обновляем circuit breaker статус из клиента (если есть в пуле)
                if account.id in self._client_pool:
                    client, _ = self._client_pool[account.id]
                    warmth.is_circuit_open = client._circuit_breaker.is_open

                score = warmth.warmth_score
                logger.debug(
                    f"Account {account.id} ({account.name}): score={score:.2f}, "
                    f"success_rate={warmth.success_rate:.2%}, active={warmth.active_orders}"
                )

                if score > best_score:
                    best_score = score
                    best_account = account

            if best_account:
                logger.info(
                    f"Selected account {best_account.id} ({best_account.name}) "
                    f"with score {best_score:.2f}"
                )
                return best_account, best_account.id

        return None, None

    def _get_or_create_warmth(self, account_id: int) -> AccountWarmth:
        """Получить или создать метрики warmth для аккаунта."""
        if account_id not in self._account_warmth:
            self._account_warmth[account_id] = AccountWarmth(account_id=account_id)
        return self._account_warmth[account_id]

    async def _get_account_lock(self, account_id: int) -> asyncio.Semaphore:
        """
        Получить lock для аккаунта (создаёт если не существует).

        Гарантирует что один аккаунт выполняет только одну транзакцию
        в момент времени (важно для TON seqno).
        """
        async with self._account_locks_lock:
            if account_id not in self._account_locks:
                # Semaphore(1) = только один holder в момент времени
                self._account_locks[account_id] = asyncio.Semaphore(1)
            return self._account_locks[account_id]

    async def _get_or_create_client(
        self,
        account_data: FragmentAccountData,
    ) -> FragmentClient:
        """
        Получить клиент из пула или создать новый.

        Connection pooling: переиспользуем клиенты для эффективности.
        """
        account_id = account_data.id

        async with self._pool_lock:
            if account_id in self._client_pool:
                client, _ = self._client_pool[account_id]
                # Обновляем timestamp использования
                self._client_pool[account_id] = (client, time.time())
                logger.debug(f"Reusing pooled client for account {account_id}")
                return client

            # Создаём новый клиент
            config = FragmentConfig.from_account_data(account_data)
            client = FragmentClient(config)
            self._client_pool[account_id] = (client, time.time())
            logger.info(f"Created new client for account {account_id}, pool size: {len(self._client_pool)}")
            return client

    async def _remove_client_from_pool(self, account_id: int) -> None:
        """Удалить клиент из пула (например, при истёкшей сессии)."""
        async with self._pool_lock:
            if account_id in self._client_pool:
                client, _ = self._client_pool.pop(account_id)
                try:
                    await client.close()
                except Exception as e:
                    logger.warning(f"Error closing removed client {account_id}: {e}")
                logger.info(f"Removed client {account_id} from pool")

    def _update_warmth_on_order_start(self, account_id: int) -> None:
        """Обновить warmth при начале обработки заказа."""
        warmth = self._get_or_create_warmth(account_id)
        warmth.active_orders += 1
        warmth.last_used_at = time.time()

    def _update_warmth_on_order_complete(self, account_id: int, success: bool) -> None:
        """Обновить warmth после завершения заказа."""
        warmth = self._get_or_create_warmth(account_id)
        warmth.active_orders = max(0, warmth.active_orders - 1)
        warmth.total_orders += 1

        if success:
            warmth.success_count += 1
            warmth.last_success_at = time.time()
        else:
            warmth.failure_count += 1

    async def _update_account_stats(
        self,
        account_id: int,
        success: bool,
        ton_spent: Decimal = Decimal("0"),
    ) -> None:
        """Обновить статистику аккаунта после заказа."""
        async with async_session_factory() as session:
            service = FragmentAccountService(session)
            await service.increment_order_stats(account_id, success, ton_spent)
            await session.commit()

    async def _mark_account_session_expired(self, account_id: int, error: str) -> None:
        """Пометить аккаунт как имеющий истёкшую сессию."""
        async with async_session_factory() as session:
            service = FragmentAccountService(session)
            account = await service.get_account(account_id)
            should_notify = bool(account and account.status != FragmentAccountStatus.SESSION_EXPIRED.value)
            account_name = account.name if account else f"Account #{account_id}"
            await service.mark_session_expired(account_id, error)
            await session.commit()
            logger.warning(f"Fragment account {account_id} marked as session_expired")

        if should_notify:
            await tg_logger.log_fragment_session_expired(
                account_id=account_id,
                account_name=account_name,
                error_message=error,
            )

    async def _run_loop(self) -> None:
        """
        Основной цикл обработки очереди.

        Использует семафор для ограничения параллельности.
        Каждый заказ обрабатывается в отдельной задаче.
        """
        while self._running:
            try:
                # Ждём свободный слот для обработки
                await self._semaphore.acquire()

                # Проверяем, что воркер ещё работает
                if not self._running:
                    self._semaphore.release()
                    break

                # Получаем заказ из очереди
                item = await self._queue.dequeue(timeout=self._poll_interval)

                if item is None:
                    self._semaphore.release()
                    continue

                # Проверяем, не обрабатывается ли уже этот заказ
                if item.order_id in self._active_orders:
                    logger.warning(
                        f"[{self._worker_id}] Order {item.order_id} already being processed, skipping"
                    )
                    self._semaphore.release()
                    continue

                # Добавляем в активные
                self._active_orders.add(item.order_id)

                # Создаём задачу для обработки заказа
                task = asyncio.create_task(
                    self._process_order_with_cleanup(item),
                    name=f"order-{item.order_id}",
                )
                self._active_tasks.add(task)

                # Callback для очистки после завершения
                task.add_done_callback(lambda t: self._on_task_done(t))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[{self._worker_id}] Error in OrderWorker loop: {e}")
                # Освобождаем семафор при ошибке
                try:
                    self._semaphore.release()
                except ValueError:
                    pass
                await asyncio.sleep(self._poll_interval)

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Callback при завершении задачи обработки заказа."""
        self._active_tasks.discard(task)
        # Семафор освобождается в _process_order_with_cleanup

    async def _process_order_with_cleanup(self, item: QueueItem) -> None:
        """
        Обёртка для обработки заказа с гарантированной очисткой.

        Гарантирует:
        - Освобождение семафора
        - Удаление из active_orders
        - Обновление статистики
        - Таймаут на обработку
        """
        try:
            # Добавляем таймаут на обработку заказа
            await asyncio.wait_for(
                self._process_order(item),
                timeout=self._order_timeout
            )
            self._stats["orders_processed"] += 1
        except asyncio.TimeoutError:
            logger.error(
                f"[{self._worker_id}] Order {item.order_id} processing timed out after {self._order_timeout}s"
            )
            self._stats["orders_processed"] += 1
            self._stats["orders_failed"] += 1
            # Возвращаем заказ в очередь для повторной обработки
            if item.can_retry:
                delay = self._retry_delay_base * (2 ** item.retry_count)
                await self._queue.requeue(item, delay=delay)
                logger.warning(f"[{self._worker_id}] Order {item.order_id} requeued after timeout")
        except Exception as e:
            logger.exception(
                f"[{self._worker_id}] Unhandled error processing order {item.order_id}: {e}"
            )
            self._stats["orders_processed"] += 1
            self._stats["orders_failed"] += 1
        finally:
            # Удаляем из активных
            self._active_orders.discard(item.order_id)
            # Освобождаем семафор
            self._semaphore.release()

    async def _process_order(self, item: QueueItem) -> None:
        """
        Обработать один заказ из очереди.

        Args:
            item: Элемент очереди с order_id
        """
        order_id = item.order_id

        logger.info(f"[{self._worker_id}] Processing order {order_id} (retry={item.retry_count})")

        # Получаем аккаунт для выполнения заказа (с учётом warmth)
        account_data, account_id = await self._get_fragment_account()
        use_pool = True  # Флаг: использовать пул клиентов

        if account_data is None:
            # Пробуем fallback на .env config
            if self._fallback_config:
                logger.warning(f"[{self._worker_id}] No active Fragment accounts in DB, using fallback config")
                fragment_client = FragmentClient(self._fallback_config)
                account_id = None
                use_pool = False  # Fallback клиент не в пуле
            else:
                logger.error(f"[{self._worker_id}] No Fragment accounts available for order {order_id}")
                # Возвращаем в очередь с задержкой
                delay = self._retry_delay_base * (2 ** item.retry_count)
                await self._queue.requeue(item, delay=delay)
                logger.warning(f"[{self._worker_id}] Order {order_id} requeued, waiting for available account")
                return
        else:
            # Используем пул клиентов
            fragment_client = await self._get_or_create_client(account_data)
            logger.info(
                f"[{self._worker_id}] Using Fragment account {account_id} "
                f"({account_data.name}) for order {order_id}"
            )

            # Обновляем warmth при старте
            self._update_warmth_on_order_start(account_id)

        payment_service = PaymentService(fragment_client)

        async with async_session_factory() as session:
            order_service = OrderService(session)

            # 1. Получаем заказ
            order = await order_service.get_order(order_id)

            if order is None:
                logger.error(f"Order {order_id} not found")
                await self._ack_if_redis(order_id)
                # Уменьшаем active_orders если использовали пул
                if account_id:
                    self._update_warmth_on_order_complete(account_id, success=False)
                if not use_pool:
                    await fragment_client.close()
                return

            # 2. Проверяем идемпотентность
            if order.status == OrderStatus.COMPLETED.value:
                logger.info(f"Order {order_id} already completed, skipping")
                await self._ack_if_redis(order_id)
                if account_id:
                    self._update_warmth_on_order_complete(account_id, success=True)
                if not use_pool:
                    await fragment_client.close()
                return

            if order.status == OrderStatus.FAILED.value:
                logger.info(f"Order {order_id} already failed, skipping")
                await self._ack_if_redis(order_id)
                if account_id:
                    self._update_warmth_on_order_complete(account_id, success=False)
                if not use_pool:
                    await fragment_client.close()
                return

            if order.status == OrderStatus.CANCELLED.value:
                logger.info(f"Order {order_id} cancelled, skipping")
                await self._ack_if_redis(order_id)
                if account_id:
                    self._update_warmth_on_order_complete(account_id, success=False)
                if not use_pool:
                    await fragment_client.close()
                return

            if order.status == OrderStatus.REFUNDED.value:
                logger.info(f"Order {order_id} refunded, skipping")
                await self._ack_if_redis(order_id)
                if account_id:
                    self._update_warmth_on_order_complete(account_id, success=False)
                if not use_pool:
                    await fragment_client.close()
                return

            # 3. Устанавливаем статус PROCESSING
            if not await order_service.set_processing(order_id):
                logger.warning(f"Order {order_id} cannot be set to PROCESSING")
                await self._ack_if_redis(order_id)
                if account_id:
                    self._update_warmth_on_order_complete(account_id, success=False)
                if not use_pool:
                    await fragment_client.close()
                return

            await session.commit()

            # 4. Используем username из заказа
            recipient_username = order.recipient_username

            # 5. Выполняем оплату С БЛОКИРОВКОЙ АККАУНТА
            # Блокировка гарантирует что один аккаунт выполняет одну транзакцию
            # (важно для TON seqno - sequence number должен быть уникальным)
            account_lock = await self._get_account_lock(account_id) if account_id else None

            if account_lock:
                async with account_lock:
                    logger.debug(
                        f"[{self._worker_id}] Acquired lock for account {account_id}, "
                        f"executing order {order_id}"
                    )
                    result = await payment_service.execute_order(
                        product_type=order.product_type,
                        recipient_username=recipient_username,
                        quantity=order.quantity,
                        show_sender=False,
                    )
            else:
                # Fallback без lock (нет account_id)
                result = await payment_service.execute_order(
                    product_type=order.product_type,
                    recipient_username=recipient_username,
                    quantity=order.quantity,
                    show_sender=False,
                )

            # НЕ закрываем клиент - он остаётся в пуле для переиспользования
            # Если fallback (не в пуле) - закрываем
            if not use_pool:
                await fragment_client.close()

            # 6. Обрабатываем результат
            if result.success:
                # Успех: записываем транзакцию
                tx_hash = result.transactions[0].tx_hash if result.transactions else ""
                ton_spent = Decimal(str(sum(tx.amount for tx in result.transactions))) if result.transactions else Decimal("0")

                await order_service.set_completed(order_id, tx_hash, ton_spent)
                await session.commit()

                # Обновляем статистику аккаунта (БД + warmth)
                if account_id:
                    await self._update_account_stats(account_id, success=True, ton_spent=ton_spent)
                    self._update_warmth_on_order_complete(account_id, success=True)

                # Подтверждаем обработку в Redis (удаляем из processing set)
                if isinstance(self._queue, RedisQueue):
                    await self._queue.ack(order_id)

                logger.info(f"Order {order_id} completed successfully: {tx_hash}")

                # Получаем данные пользователя для логирования
                from sqlalchemy import select
                user_result = await session.execute(
                    select(User).where(User.id == order.user_id)
                )
                user = user_result.scalar_one_or_none()

                # Логируем в Telegram
                await tg_logger.log_order_completed(
                    order_id=order_id,
                    user_id=order.user_id,
                    username=user.username if user else None,
                    product_type=order.product_type,
                    quantity=order.quantity,
                    recipient=order.recipient_username,
                )

                if self._on_order_completed:
                    try:
                        # Обновляем объект заказа
                        order = await order_service.get_order(order_id)
                        await self._on_order_completed(order)
                    except Exception as callback_err:
                        logger.exception(f"Error in on_order_completed callback for order {order_id}: {callback_err}")

            elif result.error_type == PaymentErrorType.SESSION_EXPIRED:
                # Сессия истекла - помечаем аккаунт и пробуем другой
                if account_id:
                    await self._mark_account_session_expired(account_id, result.error_message or "Session expired")
                    await self._update_account_stats(account_id, success=False)
                    self._update_warmth_on_order_complete(account_id, success=False)
                    # Удаляем клиент из пула - сессия больше не валидна
                    await self._remove_client_from_pool(account_id)

                # Возвращаем заказ в очередь (попробуем другим аккаунтом)
                await order_service.return_to_pending(order_id)
                await session.commit()

                delay = self._retry_delay_base
                await self._queue.requeue(item, delay=delay)

                logger.warning(
                    f"Order {order_id} will be retried with different account: {result.error_message}"
                )

            elif result.error_type == PaymentErrorType.INSUFFICIENT_FUNDS:
                await order_service.return_to_pending(order_id)
                await session.commit()

                if account_id:
                    await self._update_account_stats(account_id, success=False)
                    self._update_warmth_on_order_complete(account_id, success=False)

                item.retry_count = 0
                await self._queue.requeue(item, delay=self._insufficient_funds_retry_delay)

                logger.warning(
                    f"Order {order_id} delayed for insufficient funds retry in "
                    f"{self._insufficient_funds_retry_delay}s: {result.error_message}"
                )

            elif result.should_retry and item.can_retry:
                # Временная ошибка: возвращаем в очередь
                await order_service.return_to_pending(order_id)
                await session.commit()

                # Обновляем статистику (неуспешная попытка)
                if account_id:
                    await self._update_account_stats(account_id, success=False)
                    self._update_warmth_on_order_complete(account_id, success=False)

                delay = self._retry_delay_base * (2 ** item.retry_count)
                await self._queue.requeue(item, delay=delay)

                logger.warning(
                    f"Order {order_id} will be retried in {delay}s: {result.error_message}"
                )

            else:
                # Постоянная ошибка или исчерпаны retry
                error_msg = result.error_message or "Unknown error"
                await order_service.set_failed(order_id, error_msg)

                # Обновляем статистику аккаунта (БД + warmth)
                if account_id:
                    await self._update_account_stats(account_id, success=False)
                    self._update_warmth_on_order_complete(account_id, success=False)

                # Возвращаем деньги на баланс если оплата была с баланса
                from sqlalchemy import select
                if order.payment_provider == "balance":
                    user_result = await session.execute(
                        select(User).where(User.id == order.user_id).with_for_update()
                    )
                    user = user_result.scalar_one_or_none()
                    if user:
                        user.balance_usdt += order.price_usdt
                        logger.info(f"Refunded {order.price_usdt} USDT to user {order.user_id} for failed order {order_id}")
                else:
                    user_result = await session.execute(
                        select(User).where(User.id == order.user_id)
                    )
                    user = user_result.scalar_one_or_none()

                await session.commit()

                # Подтверждаем обработку в Redis (удаляем из processing set)
                if isinstance(self._queue, RedisQueue):
                    await self._queue.ack(order_id)

                logger.error(f"Order {order_id} failed: {error_msg}")

                # Логируем в Telegram (и в ошибки, и в заказы)
                await tg_logger.log_order_failed(
                    order_id=order_id,
                    user_id=order.user_id,
                    username=user.username if user else None,
                    reason=error_msg,
                )
                await tg_logger.log_order_error(
                    order_id=order_id,
                    error_message=error_msg,
                    user_id=order.user_id,
                    username=user.username if user else None,
                )

                if self._on_order_failed:
                    try:
                        order = await order_service.get_order(order_id)
                        await self._on_order_failed(order, error_msg)
                    except Exception as callback_err:
                        logger.exception(f"Error in on_order_failed callback for order {order_id}: {callback_err}")

    async def _recover_stuck_orders(
        self,
        recover_redis_processing: bool = True,
        recover_pending: bool = True,
    ) -> None:
        """
        Восстановить застрявшие заказы при старте.

        Заказы в статусе PROCESSING, которые не завершились,
        возвращаются в очередь.

        Также восстанавливает элементы из Redis processing set,
        которые были прерваны при crash.
        """
        logger.info("Recovering stuck orders...")

        # 1. Восстановление из Redis processing set (если используется Redis)
        if recover_redis_processing and isinstance(self._queue, RedisQueue):
            processing_items = await self._queue.get_processing_items()
            for item in processing_items:
                logger.warning(f"Recovering order {item.order_id} from Redis processing set")
                # Перемещаем обратно в очередь без увеличения retry_count
                # (requeue увеличивает, поэтому вручную)
                await self._queue.ack(item.order_id)
                await self._queue.enqueue(item.order_id, max_retries=item.max_retries)

            if processing_items:
                logger.info(f"Recovered {len(processing_items)} orders from Redis processing set")

        # 2. Восстановление из БД (заказы в статусе PROCESSING)
        async with async_session_factory() as session:
            order_service = OrderService(session)
            stuck_orders = await order_service.get_stuck_orders(timeout_minutes=10)

            for order in stuck_orders:
                logger.warning(f"Recovering stuck order {order.id} from DB (was PROCESSING)")
                await order_service.return_to_pending(order.id)
                await self._queue.enqueue(order.id)

            await session.commit()

            if stuck_orders:
                logger.info(f"Recovered {len(stuck_orders)} stuck orders from DB")

        # 3. Восстановление pending заказов, которые не попали в очередь
        # (например, если воркер был недоступен при создании заказа)
        if not recover_pending:
            return

        async with async_session_factory() as session:
            order_service = OrderService(session)
            pending_orders = await order_service.get_pending_orders(limit=100)

            recovered_pending = 0
            for order in pending_orders:
                logger.warning(f"Recovering pending order {order.id} (was never queued)")
                await self._queue.enqueue(order.id)
                recovered_pending += 1

            if recovered_pending > 0:
                logger.info(f"Recovered {recovered_pending} pending orders that were never queued")
            elif not stuck_orders:
                logger.info("No stuck orders found in DB")

    async def _recover_retryable_failed_orders(self) -> None:
        """Requeue old failed non-balance orders caused by Fragment wallet funds."""
        recovered_order_ids: list[int] = []
        async with async_session_factory() as session:
            order_service = OrderService(session)
            orders = await order_service.get_retryable_failed_orders(
                older_than_seconds=int(self._maintenance_interval)
            )

            for order in orders:
                if await order_service.retry_order(order.id, debit_balance=False):
                    recovered_order_ids.append(order.id)

            await session.commit()

        for order_id in recovered_order_ids:
            await self._queue.enqueue(order_id)

        if recovered_order_ids:
            logger.info(f"Recovered {len(recovered_order_ids)} retryable failed orders")

    async def enqueue_order(self, order_id: int, max_retries: int = 3) -> None:
        """
        Добавить заказ в очередь.

        Этот метод вызывается из handlers после создания заказа.
        """
        await self._queue.enqueue(order_id, max_retries=max_retries)
        logger.info(f"Order {order_id} enqueued for processing")

    @property
    def is_running(self) -> bool:
        """Проверить, запущен ли воркер."""
        return self._running

    async def get_queue_size(self) -> int:
        """Получить размер очереди."""
        return await self._queue.size()

    async def check_accounts_available(self) -> bool:
        """Проверить, есть ли доступные аккаунты."""
        account_data, _ = await self._get_fragment_account()
        return account_data is not None or self._fallback_config is not None

    def get_pool_stats(self) -> dict:
        """
        Получить статистику пула клиентов и warmth для мониторинга.

        Returns:
            Словарь со статистикой:
            - pool_size: количество клиентов в пуле
            - accounts: детали по каждому аккаунту (warmth, circuit breaker)
        """
        now = time.time()
        stats = {
            "pool_size": len(self._client_pool),
            "accounts": {},
        }

        for account_id, (client, last_used) in self._client_pool.items():
            warmth = self._account_warmth.get(account_id)
            stats["accounts"][account_id] = {
                "pool_idle_seconds": round(now - last_used, 1),
                "circuit_breaker_state": client._circuit_breaker.state,
                "circuit_breaker_failures": client._circuit_breaker.failure_count,
                "warmth": {
                    "score": round(warmth.warmth_score, 2) if warmth else None,
                    "success_rate": round(warmth.success_rate, 2) if warmth else None,
                    "total_orders": warmth.total_orders if warmth else 0,
                    "active_orders": warmth.active_orders if warmth else 0,
                } if warmth else None,
            }

        return stats


# ===== ГЛОБАЛЬНОЕ УПРАВЛЕНИЕ ВОРКЕРАМИ =====

# Список всех воркеров (для поддержки нескольких)
_order_workers: list[OrderWorker] = []

# Основной воркер (для обратной совместимости)
_order_worker: OrderWorker | None = None


def get_order_worker() -> OrderWorker | None:
    """Получить основной экземпляр воркера (для обратной совместимости)."""
    return _order_worker


def get_all_order_workers() -> list[OrderWorker]:
    """Получить все запущенные воркеры."""
    return _order_workers.copy()


def set_order_worker(worker: OrderWorker | None) -> None:
    """Установить основной экземпляр воркера (для обратной совместимости)."""
    global _order_worker
    _order_worker = worker


async def create_and_start_worker(
    fallback_config: FragmentConfig | None = None,
    max_concurrent_orders: int = 1,
    worker_id: str | None = None,
) -> OrderWorker:
    """
    Создать и запустить один воркер.

    Args:
        fallback_config: Fallback конфиг из .env
        max_concurrent_orders: Сколько заказов обрабатывать параллельно
        worker_id: ID воркера для логирования
    """
    worker = OrderWorker(
        fallback_config=fallback_config,
        max_concurrent_orders=max_concurrent_orders,
        worker_id=worker_id,
    )
    _order_workers.append(worker)

    # Первый воркер становится основным
    if _order_worker is None:
        set_order_worker(worker)

    await worker.start()
    return worker


async def create_and_start_workers(
    num_workers: int = 1,
    max_concurrent_per_worker: int = 3,
    fallback_config: FragmentConfig | None = None,
) -> list[OrderWorker]:
    """
    Создать и запустить несколько воркеров.

    Оптимальная конфигурация:
    - num_workers: количество воркеров (обычно 1-3)
    - max_concurrent_per_worker: заказов на воркер (обычно 3-5)

    Пример:
    - 2 воркера x 3 concurrent = 6 параллельных заказов
    - 3 воркера x 5 concurrent = 15 параллельных заказов

    Args:
        num_workers: Количество воркеров
        max_concurrent_per_worker: Макс. параллельных заказов на воркер
        fallback_config: Fallback конфиг из .env
    """
    workers = []
    for i in range(num_workers):
        worker = await create_and_start_worker(
            fallback_config=fallback_config,
            max_concurrent_orders=max_concurrent_per_worker,
            worker_id=f"worker-{i + 1}",
        )
        workers.append(worker)

    logger.info(
        f"Started {num_workers} workers with {max_concurrent_per_worker} concurrent orders each. "
        f"Total capacity: {num_workers * max_concurrent_per_worker} parallel orders"
    )

    return workers


async def stop_all_workers(wait_for_active: bool = True) -> None:
    """
    Остановить все воркеры.

    Args:
        wait_for_active: Ждать завершения активных заказов
    """
    global _order_worker, _order_workers

    logger.info(f"Stopping {len(_order_workers)} workers...")

    # Останавливаем все воркеры параллельно
    await asyncio.gather(
        *[w.stop(wait_for_active=wait_for_active) for w in _order_workers],
        return_exceptions=True,
    )

    _order_workers.clear()
    _order_worker = None

    logger.info("All workers stopped")


def get_workers_stats() -> dict:
    """
    Получить сводную статистику по всем воркерам.

    Returns:
        Словарь со статистикой:
        - total_workers: количество воркеров
        - total_capacity: общая пропускная способность
        - workers: детали по каждому воркеру
    """
    stats = {
        "total_workers": len(_order_workers),
        "total_capacity": sum(w._max_concurrent_orders for w in _order_workers),
        "total_active_orders": sum(len(w._active_orders) for w in _order_workers),
        "workers": {},
    }

    for worker in _order_workers:
        stats["workers"][worker._worker_id] = {
            "is_running": worker.is_running,
            "max_concurrent": worker._max_concurrent_orders,
            "active_orders": len(worker._active_orders),
            "stats": worker._stats.copy(),
            "pool": worker.get_pool_stats(),
        }

    return stats


# ===== ДИНАМИЧЕСКОЕ УПРАВЛЕНИЕ ВОРКЕРАМИ =====

async def scale_workers(
    target_workers: int,
    max_concurrent_per_worker: int = 3,
    fallback_config: FragmentConfig | None = None,
) -> dict:
    """
    Масштабировать количество воркеров до нужного числа.

    Можно вызывать без перезапуска бота!

    Args:
        target_workers: Желаемое количество воркеров
        max_concurrent_per_worker: Concurrent orders для новых воркеров
        fallback_config: Fallback конфиг

    Returns:
        Статистика изменений
    """
    global _order_workers, _order_worker

    current_count = len(_order_workers)

    result = {
        "previous_workers": current_count,
        "target_workers": target_workers,
        "added": 0,
        "removed": 0,
    }

    if target_workers > current_count:
        # Добавляем воркеры
        for i in range(current_count, target_workers):
            worker = await create_and_start_worker(
                fallback_config=fallback_config,
                max_concurrent_orders=max_concurrent_per_worker,
                worker_id=f"worker-{i + 1}",
            )
            result["added"] += 1
            logger.info(f"Added worker {worker._worker_id}")

    elif target_workers < current_count:
        # Удаляем лишние воркеры (с конца)
        workers_to_remove = current_count - target_workers
        for _ in range(workers_to_remove):
            if _order_workers:
                worker = _order_workers.pop()
                await worker.stop(wait_for_active=True)
                result["removed"] += 1
                logger.info(f"Removed worker {worker._worker_id}")

        # Обновляем основной воркер если удалили его
        if _order_worker not in _order_workers:
            _order_worker = _order_workers[0] if _order_workers else None

    result["current_workers"] = len(_order_workers)
    result["total_capacity"] = sum(w._max_concurrent_orders for w in _order_workers)

    logger.info(
        f"Scaled workers: {current_count} -> {len(_order_workers)}, "
        f"capacity: {result['total_capacity']}"
    )

    return result


async def add_worker(
    max_concurrent_orders: int = 3,
    fallback_config: FragmentConfig | None = None,
) -> OrderWorker:
    """
    Добавить одного воркера на лету.

    Пример использования из админки:
        await add_worker(max_concurrent_orders=5)
    """
    worker_id = f"worker-{len(_order_workers) + 1}"
    worker = await create_and_start_worker(
        fallback_config=fallback_config,
        max_concurrent_orders=max_concurrent_orders,
        worker_id=worker_id,
    )
    logger.info(f"Dynamically added {worker_id} with {max_concurrent_orders} concurrent")
    return worker


async def remove_worker(worker_id: str | None = None) -> bool:
    """
    Удалить воркера на лету.

    Args:
        worker_id: ID воркера для удаления (или последний если не указан)

    Returns:
        True если воркер удалён
    """
    global _order_workers, _order_worker

    if not _order_workers:
        return False

    worker_to_remove = None

    if worker_id:
        # Ищем конкретный воркер
        for w in _order_workers:
            if w._worker_id == worker_id:
                worker_to_remove = w
                break
    else:
        # Удаляем последний
        worker_to_remove = _order_workers[-1]

    if not worker_to_remove:
        return False

    # Останавливаем и удаляем
    await worker_to_remove.stop(wait_for_active=True)
    _order_workers.remove(worker_to_remove)

    # Обновляем основной воркер
    if _order_worker == worker_to_remove:
        _order_worker = _order_workers[0] if _order_workers else None

    logger.info(f"Dynamically removed {worker_to_remove._worker_id}")
    return True


async def update_worker_concurrency(worker_id: str, new_concurrent: int) -> bool:
    """
    Изменить concurrent orders для воркера.

    Внимание: пересоздаёт воркер с новыми настройками!
    Активные заказы будут дообработаны.

    Args:
        worker_id: ID воркера
        new_concurrent: Новое значение max_concurrent_orders

    Returns:
        True если успешно
    """
    global _order_workers, _order_worker

    # Находим воркер
    old_worker = None
    worker_index = -1
    for i, w in enumerate(_order_workers):
        if w._worker_id == worker_id:
            old_worker = w
            worker_index = i
            break

    if not old_worker:
        return False

    # Запоминаем настройки
    fallback_config = old_worker._fallback_config

    # Останавливаем старый (ждём активные заказы)
    logger.info(f"Updating {worker_id}: {old_worker._max_concurrent_orders} -> {new_concurrent}")
    await old_worker.stop(wait_for_active=True)

    # Создаём новый с теми же настройками но новым concurrent
    new_worker = OrderWorker(
        fallback_config=fallback_config,
        max_concurrent_orders=new_concurrent,
        worker_id=worker_id,  # Сохраняем тот же ID
    )
    await new_worker.start()

    # Заменяем в списке
    _order_workers[worker_index] = new_worker

    # Обновляем основной если нужно
    if _order_worker == old_worker:
        _order_worker = new_worker

    logger.info(f"Worker {worker_id} updated to {new_concurrent} concurrent")
    return True
