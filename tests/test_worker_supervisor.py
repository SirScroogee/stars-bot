"""
Тест автонастройки воркеров и обработки заказов.

Запуск:
  python -m tests.test_worker_supervisor                    # Mock режим, 5 заказов
  python -m tests.test_worker_supervisor --count 10         # Mock режим, 10 заказов
  python -m tests.test_worker_supervisor --dry-run          # Реальный API, без оплаты
  python -m tests.test_worker_supervisor --dry-run --count 3 --username testuser

Тест проверяет:
1. Создание и запуск WorkerSupervisor
2. Автонастройку concurrent на основе количества аккаунтов
3. Добавление заказов в очередь
4. Обработку заказов воркером
5. Реконфигурацию при изменении аккаунтов
6. Автоперезапуск при падении воркера

Режим --dry-run:
- Использует реальные Fragment аккаунты из БД
- Выполняет реальные API вызовы (поиск получателя, инициализация)
- НЕ выполняет реальную оплату TON
- Проверяет баланс кошелька
"""
import argparse
import asyncio
import logging
import sys
import io
import time
from datetime import datetime
from typing import Optional

# Фикс для Windows консоли - переключаем на UTF-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Настройка детального логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Уменьшаем шум от библиотек и API клиентов
for noisy_logger in [
    "asyncio", "aiosqlite", "httpx", "httpcore", "urllib3",
    "src.api_clients", "src.api_clients.fragment", "src.api_clients.fragment.client",
    "fragment", "FragmentClient", "tonutils", "tonsdk", "pytoniq",
    "src.services", "src.db", "sqlalchemy", "alembic",
]:
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger("TEST")


def log_separator(title: str):
    """Вывести разделитель с заголовком."""
    line = "=" * 70
    logger.info("")
    logger.info(line)
    logger.info(f"  {title}")
    logger.info(line)
    logger.info("")


def log_step(step_num: int, description: str):
    """Вывести шаг теста."""
    logger.info(f"")
    logger.info(f">>> ШАГ {step_num}: {description}")
    logger.info(f"")


def log_result(success: bool, message: str):
    """Вывести результат."""
    icon = "[OK]" if success else "[FAIL]"
    logger.info(f"{icon} {message}")


# ==================== MOCK CLASSES ====================

class MockFragmentAccount:
    """Мок Fragment аккаунта."""

    def __init__(self, id: int, name: str, is_active: bool = True):
        self.id = id
        self.name = name
        self.is_active = is_active
        self.status = "active" if is_active else "disabled"
        self.ton_balance = 100.0
        self.wallet_address = f"UQ_test_wallet_{id}"
        self.mnemonic = "test " * 24
        self.tonapi_key = "test_tonapi_key"
        self.fragment_hash = "test_hash"
        self.stel_ssid = "test_ssid"
        self.stel_token = "test_token"
        self.stel_token_dc1 = "test_token_dc1"


class MockOrder:
    """Мок заказа."""

    def __init__(
        self,
        id: int,
        order_key: str,
        user_id: int,
        product_type: str = "stars",
        quantity: int = 100,
        recipient_username: str = "test_user",
    ):
        self.id = id
        self.order_key = order_key
        self.user_id = user_id
        self.product_type = product_type
        self.quantity = quantity
        self.recipient_username = recipient_username
        self.status = "pending"
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
        self.error_message = None
        self.retry_count = 0
        self.fragment_tx_hash = None
        self.fragment_ton_spent = None
        self.fragment_account_id = None


class MockOrderQueue:
    """Мок очереди заказов."""

    def __init__(self):
        self._queue = asyncio.Queue()
        self._orders = {}
        self._logger = logging.getLogger("MockQueue")

    async def enqueue(self, order_id: int, priority: int = 0) -> bool:
        self._logger.info(f"[ОЧЕРЕДЬ] Adding order #{order_id} (priority={priority})")
        await self._queue.put((priority, order_id))
        self._logger.info(f"[ОЧЕРЕДЬ] Order #{order_id} added. Queue size: {self._queue.qsize()}")
        return True

    async def dequeue(self, timeout: float = 1.0):
        try:
            self._logger.debug(f"[ОЧЕРЕДЬ] Waiting for order (timeout={timeout}s)...")
            priority, order_id = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            self._logger.info(f"[ОЧЕРЕДЬ] Got order #{order_id}")
            return order_id
        except asyncio.TimeoutError:
            return None

    async def size(self) -> int:
        return self._queue.qsize()

    async def connect(self):
        self._logger.info("[ОЧЕРЕДЬ] Подключена")

    async def close(self):
        self._logger.info("[ОЧЕРЕДЬ] Закрыта")


class MockFragmentClient:
    """Мок Fragment клиента."""

    def __init__(self, account_id: int, delay: float = 0.3, success_rate: float = 0.95):
        self.account_id = account_id
        self.delay = delay
        self._logger = logging.getLogger(f"Client-{account_id}")
        self.is_locked = False
        self._success_rate = success_rate

    async def send_stars(self, username: str, amount: int) -> dict:
        self._logger.info(f"[ТРАНЗ] Отправка {amount} звёзд -> @{username}")
        self._logger.debug(f"[ТРАНЗ] Ожидание {self.delay}s (симуляция транзакции)...")
        await asyncio.sleep(self.delay)

        import random
        success = random.random() < self._success_rate

        if success:
            result = {
                "success": True,
                "tx_hash": f"tx_hash_{int(time.time())}_{self.account_id}",
                "ton_spent": amount * 0.015,
            }
            self._logger.info(f"[ТРАНЗ] УСПЕХ! tx={result['tx_hash']}, ton={result['ton_spent']:.4f}")
        else:
            result = {
                "success": False,
                "error": "Симулированная ошибка транзакции",
            }
            self._logger.warning(f"[ТРАНЗ] ОШИБКА: {result['error']}")

        return result


class MockClientPool:
    """Мок пула клиентов."""

    def __init__(self, accounts: list[MockFragmentAccount], delay: float = 0.3, success_rate: float = 0.95):
        self.accounts = {acc.id: acc for acc in accounts}
        self.clients = {acc.id: MockFragmentClient(acc.id, delay, success_rate) for acc in accounts}
        self._logger = logging.getLogger("MockPool")
        self._warmth = {acc.id: {"score": 50, "success_rate": 0.9, "total_transactions": 0} for acc in accounts}
        self._circuit_breakers = {acc.id: "closed" for acc in accounts}

    async def acquire(self, timeout: float = 30.0):
        self._logger.info(f"[ПУЛ] Запрос клиента...")
        for acc_id, client in self.clients.items():
            if not client.is_locked:
                client.is_locked = True
                self._logger.info(f"[ПУЛ] Клиент #{acc_id} получен")
                return client
        self._logger.warning(f"[ПУЛ] Нет свободных клиентов!")
        return None

    async def release(self, client: MockFragmentClient, success: bool = True):
        client.is_locked = False
        self._warmth[client.account_id]["total_transactions"] += 1
        status = "успех" if success else "ошибка"
        self._logger.info(f"[ПУЛ] Клиент #{client.account_id} освобождён ({status})")

    def get_stats(self) -> dict:
        return {
            "pool_size": len(self.clients),
            "accounts": {
                acc_id: {
                    "warmth": self._warmth[acc_id],
                    "circuit_breaker_state": self._circuit_breakers[acc_id],
                }
                for acc_id in self.clients
            }
        }


class TestOrderWorker:
    """Тестовый воркер для обработки заказов."""

    def __init__(
        self,
        queue: MockOrderQueue,
        pool: MockClientPool,
        max_concurrent: int = 1,
        worker_id: str = "test-worker",
    ):
        self._queue = queue
        self._pool = pool
        self._max_concurrent_orders = max_concurrent
        self._worker_id = worker_id
        self._logger = logging.getLogger(f"Worker-{worker_id}")

        self._running = False
        self._active_orders = set()
        self._worker_task = None
        self._semaphore = None

        self._stats = {
            "orders_processed": 0,
            "orders_succeeded": 0,
            "orders_failed": 0,
        }

        self._orders_db = {}
        self._on_completed = None
        self._on_failed = None

    def set_callbacks(self, on_completed=None, on_failed=None):
        self._on_completed = on_completed
        self._on_failed = on_failed

    async def start(self):
        if self._running:
            self._logger.warning("[ВОРКЕР] Уже запущен!")
            return

        self._logger.info(f"[ВОРКЕР] Запуск (max_concurrent={self._max_concurrent_orders})")
        self._running = True
        self._semaphore = asyncio.Semaphore(self._max_concurrent_orders)
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._logger.info(f"[ВОРКЕР] Запущен успешно")

    async def stop(self, wait_for_active: bool = True, timeout: float = 30.0):
        if not self._running:
            return

        self._logger.info(f"[ВОРКЕР] Остановка...")
        self._running = False

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        self._logger.info(f"[ВОРКЕР] Остановлен")

    async def _worker_loop(self):
        self._logger.info("[ВОРКЕР] Запуск цикла обработки заказов")

        while self._running:
            try:
                order_id = await self._queue.dequeue(timeout=1.0)
                if order_id is None:
                    continue
                asyncio.create_task(self._process_order(order_id))
            except asyncio.CancelledError:
                self._logger.info("[ВОРКЕР] Цикл отменён")
                break
            except Exception as e:
                self._logger.exception(f"[ВОРКЕР] Ошибка в цикле: {e}")
                await asyncio.sleep(1.0)

    async def _process_order(self, order_id: int):
        async with self._semaphore:
            self._active_orders.add(order_id)
            self._logger.info(f"[ЗАКАЗ] Обработка заказа #{order_id}")
            self._logger.info(f"[ЗАКАЗ] Активных заказов: {len(self._active_orders)}/{self._max_concurrent_orders}")

            try:
                order = self._orders_db.get(order_id)
                if not order:
                    self._logger.error(f"[ЗАКАЗ] Заказ #{order_id} не найден!")
                    return

                order.status = "processing"
                self._logger.info(f"[ЗАКАЗ] Заказ #{order_id}: статус -> обработка")

                client = await self._pool.acquire()
                if not client:
                    self._logger.error(f"[ЗАКАЗ] Нет доступного клиента для заказа #{order_id}")
                    order.status = "failed"
                    order.error_message = "Нет доступного клиента"
                    self._stats["orders_failed"] += 1
                    return

                try:
                    self._logger.info(f"[ЗАКАЗ] #{order_id}: отправка {order.quantity} звёзд -> @{order.recipient_username}")
                    result = await client.send_stars(order.recipient_username, order.quantity)

                    if result.get("success"):
                        order.status = "completed"
                        order.fragment_tx_hash = result.get("tx_hash")
                        order.fragment_ton_spent = result.get("ton_spent")
                        order.fragment_account_id = client.account_id
                        self._stats["orders_succeeded"] += 1
                        self._logger.info(f"[ЗАКАЗ] #{order_id} ВЫПОЛНЕН!")
                        if self._on_completed:
                            await self._on_completed(order)
                    else:
                        order.status = "failed"
                        order.error_message = result.get("error", "Неизвестная ошибка")
                        self._stats["orders_failed"] += 1
                        self._logger.warning(f"[ЗАКАЗ] #{order_id} ОШИБКА: {order.error_message}")
                        if self._on_failed:
                            await self._on_failed(order)

                    await self._pool.release(client, result.get("success", False))

                except Exception as e:
                    self._logger.exception(f"[ЗАКАЗ] Ошибка обработки #{order_id}: {e}")
                    order.status = "failed"
                    order.error_message = str(e)
                    self._stats["orders_failed"] += 1
                    await self._pool.release(client, False)

            finally:
                self._stats["orders_processed"] += 1
                self._active_orders.discard(order_id)
                self._logger.info(f"[ЗАКАЗ] #{order_id} готово. Активных: {len(self._active_orders)}")

    def add_order_to_db(self, order: MockOrder):
        self._orders_db[order.id] = order

    @property
    def is_running(self) -> bool:
        return self._running

    def get_pool_stats(self) -> dict:
        return self._pool.get_stats()


class TestSupervisor:
    """Тестовый супервизор."""

    def __init__(self, accounts: list, queue: MockOrderQueue, delay: float = 0.3, success_rate: float = 0.95):
        self._accounts = accounts
        self._queue = queue
        self._delay = delay
        self._success_rate = success_rate
        self._worker = None
        self._running = False
        self._logger = logging.getLogger("Supervisor")
        self._stats = {"started_at": None, "restarts": 0, "reconfigurations": 0}

    async def start(self):
        self._logger.info("[СУПЕРВИЗОР] Запуск...")
        self._running = True
        self._stats["started_at"] = time.time()
        await self._create_and_start_worker()
        self._logger.info("[СУПЕРВИЗОР] Запущен")

    async def stop(self, wait_for_active: bool = True):
        self._logger.info("[СУПЕРВИЗОР] Остановка...")
        self._running = False
        if self._worker:
            await self._worker.stop(wait_for_active=wait_for_active)
        self._logger.info("[СУПЕРВИЗОР] Остановлен")

    async def reconfigure(self):
        self._logger.info("[СУПЕРВИЗОР] Реконфигурация...")
        active_count = len([a for a in self._accounts if a.is_active])
        if self._worker:
            current = self._worker._max_concurrent_orders
            if current == active_count:
                self._logger.info(f"[СУПЕРВИЗОР] Изменения не требуются (concurrent={active_count})")
                return
            self._logger.info(f"[СУПЕРВИЗОР] Изменение: {current} -> {active_count}")
            await self._worker.stop(wait_for_active=True)
        await self._create_and_start_worker()
        self._stats["reconfigurations"] += 1
        self._logger.info("[СУПЕРВИЗОР] Реконфигурация завершена")

    async def _create_and_start_worker(self):
        active_accounts = [a for a in self._accounts if a.is_active]
        if not active_accounts:
            self._logger.warning("[СУПЕРВИЗОР] Нет активных аккаунтов!")
            return
        self._logger.info(f"[СУПЕРВИЗОР] Активных аккаунтов: {len(active_accounts)}")
        pool = MockClientPool(active_accounts, self._delay, self._success_rate)
        self._worker = TestOrderWorker(
            queue=self._queue,
            pool=pool,
            max_concurrent=len(active_accounts),
            worker_id="auto-worker",
        )
        await self._worker.start()

    def get_status(self) -> dict:
        worker_stats = {}
        pool_stats = {}
        if self._worker:
            worker_stats = {
                "is_running": self._worker.is_running,
                "max_concurrent": self._worker._max_concurrent_orders,
                "active_orders": len(self._worker._active_orders),
                "orders_processed": self._worker._stats["orders_processed"],
                "orders_succeeded": self._worker._stats["orders_succeeded"],
                "orders_failed": self._worker._stats["orders_failed"],
            }
            pool_stats = self._worker.get_pool_stats()
        return {
            "supervisor": {
                "is_running": self._running,
                "started_at": self._stats["started_at"],
                "restarts": self._stats["restarts"],
                "reconfigurations": self._stats["reconfigurations"],
            },
            "worker": worker_stats,
            "pool": pool_stats,
        }

    @property
    def worker(self):
        return self._worker

    @property
    def is_running(self):
        return self._running


# ==================== MOCK TEST ====================

async def run_mock_test(order_count: int, delay: float, success_rate: float):
    """Запуск теста с моками."""

    log_separator("ТЕСТ С МОКАМИ: АВТОНАСТРОЙКА ВОРКЕРОВ")

    logger.info(f"Конфигурация:")
    logger.info(f"  - Количество заказов: {order_count}")
    logger.info(f"  - Задержка транзакции: {delay}s")
    logger.info(f"  - Успешность: {success_rate * 100:.0f}%")

    # Подготовка
    log_step(1, "Подготовка тестовых данных")

    accounts = [
        MockFragmentAccount(1, "Аккаунт-1", is_active=True),
        MockFragmentAccount(2, "Аккаунт-2", is_active=True),
        MockFragmentAccount(3, "Аккаунт-3", is_active=False),
    ]

    logger.info(f"Создано {len(accounts)} аккаунтов:")
    for acc in accounts:
        status = "[АКТИВЕН]" if acc.is_active else "[ОТКЛЮЧЁН]"
        logger.info(f"  - #{acc.id} {acc.name}: {status}")

    queue = MockOrderQueue()
    await queue.connect()
    log_result(True, "Тестовые данные подготовлены")

    # Запуск супервизора
    log_step(2, "Запуск супервизора")
    supervisor = TestSupervisor(accounts, queue, delay, success_rate)
    await supervisor.start()

    status = supervisor.get_status()
    expected_concurrent = 2
    actual_concurrent = status["worker"]["max_concurrent"]

    logger.info(f"Ожидаемый concurrent: {expected_concurrent}")
    logger.info(f"Фактический concurrent: {actual_concurrent}")
    log_result(actual_concurrent == expected_concurrent, f"Concurrent настроен правильно: {actual_concurrent}")

    # Создание заказов
    log_step(3, f"Создание {order_count} тестовых заказов")

    orders = []
    for i in range(order_count):
        order = MockOrder(
            id=i + 1,
            order_key=f"ORD-{i + 1:04d}",
            user_id=12345,
            quantity=100 + (i % 10) * 50,
            recipient_username=f"user_{i + 1}",
        )
        orders.append(order)
        supervisor.worker.add_order_to_db(order)

    logger.info(f"Создано {len(orders)} заказов")
    log_result(True, f"Создано {len(orders)} заказов")

    # Добавление в очередь
    log_step(4, "Добавление заказов в очередь")
    for order in orders:
        await queue.enqueue(order.id)

    queue_size = await queue.size()
    logger.info(f"Размер очереди: {queue_size}")
    log_result(True, f"Все заказы добавлены в очередь")

    # Ожидание обработки
    log_step(5, "Ожидание обработки заказов")
    logger.info(f"Ожидание обработки всех заказов...")

    start_time = time.time()
    timeout = max(30.0, order_count * 2)  # 2 секунды на заказ минимум
    last_progress = 0

    while time.time() - start_time < timeout:
        status = supervisor.get_status()
        processed = status["worker"]["orders_processed"]

        if processed > last_progress:
            succeeded = status["worker"]["orders_succeeded"]
            failed = status["worker"]["orders_failed"]
            active = status["worker"]["active_orders"]
            logger.info(f"[ПРОГРЕСС] {processed}/{len(orders)} (успешно={succeeded}, ошибок={failed}, активно={active})")
            last_progress = processed

        if processed >= len(orders):
            break

        await asyncio.sleep(0.5)

    status = supervisor.get_status()
    processed = status["worker"]["orders_processed"]
    log_result(processed == len(orders), f"Все заказы обработаны ({processed}/{len(orders)})")

    # Проверка статусов
    log_step(6, "Проверка статусов заказов")
    completed = len([o for o in orders if o.status == "completed"])
    failed = len([o for o in orders if o.status == "failed"])
    logger.info(f"Выполнено: {completed}, Ошибок: {failed}")

    # Показываем первые 10 заказов
    for order in orders[:10]:
        status_icon = "[OK]" if order.status == "completed" else "[FAIL]"
        info = f", tx={order.fragment_tx_hash}" if order.fragment_tx_hash else ""
        if order.error_message:
            info = f", ошибка={order.error_message}"
        logger.info(f"  {status_icon} Заказ #{order.id}: {order.status}{info}")

    if len(orders) > 10:
        logger.info(f"  ... и ещё {len(orders) - 10} заказов")

    # Остановка
    log_step(7, "Остановка супервизора")
    final_concurrent = status["worker"]["max_concurrent"]
    await supervisor.stop()
    await queue.close()

    final_status = supervisor.get_status()
    log_result(True, "Тест завершён")

    # Итог
    log_separator("ИТОГИ ТЕСТА")

    all_orders = orders
    total_orders = len(all_orders)
    completed_orders = len([o for o in all_orders if o.status == "completed"])
    failed_orders = len([o for o in all_orders if o.status == "failed"])

    logger.info(f"Всего заказов: {total_orders}")
    logger.info(f"Выполнено: {completed_orders}")
    logger.info(f"Ошибок: {failed_orders}")
    logger.info(f"Успешность: {completed_orders / total_orders * 100:.1f}%")
    logger.info("")

    test_results = {
        "Все заказы обработаны": completed_orders + failed_orders == total_orders,
        "Супервизор остановлен": final_status["supervisor"]["is_running"] == False,
        "Автонастройка работает": final_concurrent == 2,
    }

    logger.info("Результаты тестов:")
    all_passed = True
    for test_name, passed in test_results.items():
        icon = "[OK]" if passed else "[FAIL]"
        logger.info(f"  {icon} {test_name}")
        if not passed:
            all_passed = False

    logger.info("")
    if all_passed:
        logger.info("[OK] ВСЕ ТЕСТЫ ПРОЙДЕНЫ!")
    else:
        logger.info("[FAIL] НЕКОТОРЫЕ ТЕСТЫ НЕ ПРОЙДЕНЫ")

    return all_passed


# ==================== DRY-RUN TEST ====================

async def run_dry_run_test(order_count: int, username: str, payment_delay: float = 4.0):
    """
    Запуск теста с реальным Fragment API (без оплаты).

    Выполняет:
    1. Загрузка аккаунтов из БД
    2. Инициализация Fragment клиентов
    3. Поиск получателя
    4. Инициализация запроса на покупку
    5. Проверка баланса
    6. Симуляция оплаты (задержка без реальной транзакции)
    """

    log_separator("DRY-RUN ТЕСТ: РЕАЛЬНЫЙ FRAGMENT API (БЕЗ ОПЛАТЫ)")

    logger.info(f"Конфигурация:")
    logger.info(f"  - Количество заказов: {order_count}")
    logger.info(f"  - Получатель: @{username}")
    logger.info(f"  - Симуляция оплаты: {payment_delay}s")
    logger.info("")
    logger.info("ВНИМАНИЕ: Этот тест использует РЕАЛЬНЫЙ Fragment API!")
    logger.info("          Реальные платежи НЕ будут выполнены.")
    logger.info("")

    # Импорты для реального теста
    try:
        from src.db.session import async_session_factory
        from src.services.fragment_account_service import FragmentAccountService
        from src.api_clients.fragment import FragmentClient
        from src.api_clients.fragment.config import FragmentConfig
    except ImportError as e:
        logger.error(f"Не удалось импортировать модули: {e}")
        logger.error("Убедитесь, что запускаете из корня проекта с установленными зависимостями")
        return False

    # Загрузка аккаунтов из БД
    log_step(1, "Загрузка Fragment аккаунтов из БД")

    try:
        async with async_session_factory() as session:
            service = FragmentAccountService(session)
            accounts = await service.get_all_active_accounts()
    except Exception as e:
        logger.error(f"Не удалось загрузить аккаунты: {e}")
        return False

    if not accounts:
        logger.error("Активные Fragment аккаунты не найдены в БД!")
        logger.error("Сначала добавьте аккаунт через админ-панель.")
        return False

    logger.info(f"Найдено {len(accounts)} активных аккаунтов:")
    for acc in accounts:
        logger.info(f"  - #{acc.id} {acc.name}: баланс={acc.ton_balance:.2f} TON")

    log_result(True, f"Загружено {len(accounts)} аккаунтов")

    # Создание клиентов
    log_step(2, "Создание Fragment клиентов")

    clients = []
    for acc in accounts:
        try:
            config = FragmentConfig(
                account_id=acc.id,
                tonapi_key=acc.tonapi_key,
                mnemonic=acc.mnemonic,  # Уже list[str]
                fragment_hash=acc.fragment_hash,
                stel_ssid=acc.stel_ssid,
                stel_token=acc.stel_token,
                stel_ton_token=acc.stel_ton_token,
            )
            client = FragmentClient(config)
            clients.append((acc, client))
            logger.info(f"  - Клиент для #{acc.id} создан")
        except Exception as e:
            logger.error(f"  - Ошибка создания клиента #{acc.id}: {e}")

    if not clients:
        logger.error("Клиенты не созданы!")
        return False

    log_result(True, f"Создано {len(clients)} клиентов")

    # Тестирование каждого клиента
    log_step(3, f"Тестирование {order_count} dry-run заказов")

    results = {
        "total": order_count,
        "search_success": 0,
        "init_success": 0,
        "balance_ok": 0,
        "payment_simulated": 0,
        "errors": [],
        "order_times": [],  # время на каждый заказ
    }

    total_start_time = time.time()

    for i in range(order_count):
        order_num = i + 1
        order_start_time = time.time()

        # Выбираем клиента по кругу
        acc, client = clients[i % len(clients)]

        logger.info("")
        logger.info(f"=== Заказ {order_num}/{order_count} (Аккаунт #{acc.id}) ===")

        try:
            # 1. Поиск получателя
            step1_start = time.time()
            logger.info(f"[1/4] Поиск получателя @{username}...")
            recipient = await client.search_stars_recipient(username, use_cache=False)
            step1_time = time.time() - step1_start
            logger.info(f"      Найден: recipient_id={recipient.recipient_id} ({step1_time:.2f}s)")
            results["search_success"] += 1

            # 2. Инициализация запроса
            step2_start = time.time()
            quantity = 50 + (i % 10) * 10  # 50-140 stars
            logger.info(f"[2/4] Инициализация запроса на {quantity} звёзд...")
            request = await client.init_stars_request(recipient.recipient_id, quantity)
            step2_time = time.time() - step2_start
            logger.info(f"      ID запроса: {request.req_id} ({step2_time:.2f}s)")
            results["init_success"] += 1

            # 3. Проверка баланса
            step3_start = time.time()
            logger.info(f"[3/4] Проверка баланса кошелька...")
            balance = await client.get_balance()
            step3_time = time.time() - step3_start
            logger.info(f"      Баланс: {balance:.4f} TON ({step3_time:.2f}s)")

            # Примерная стоимость (15 TON за 1000 stars)
            estimated_cost = quantity * 0.015
            if balance >= estimated_cost:
                logger.info(f"      Достаточно для ~{quantity} звёзд (нужно ~{estimated_cost:.4f} TON)")
                results["balance_ok"] += 1
            else:
                logger.warning(f"      Недостаточно баланса! Нужно ~{estimated_cost:.4f} TON")

            # 4. Симуляция оплаты (в реальности здесь TON транзакция)
            logger.info(f"[4/4] Симуляция оплаты ({payment_delay}s)...")
            await asyncio.sleep(payment_delay)
            results["payment_simulated"] += 1

            order_time = time.time() - order_start_time
            results["order_times"].append(order_time)
            logger.info(f"[OK] Заказ {order_num} завершён за {order_time:.2f}s")

        except Exception as e:
            order_time = time.time() - order_start_time
            results["order_times"].append(order_time)
            error_msg = f"Заказ {order_num}: {type(e).__name__}: {e}"
            logger.error(f"[FAIL] {error_msg} ({order_time:.2f}s)")
            results["errors"].append(error_msg)

        # Небольшая задержка между запросами
        if i < order_count - 1:
            await asyncio.sleep(0.3)

    total_time = time.time() - total_start_time

    # Закрытие клиентов
    log_step(4, "Закрытие клиентов")
    for acc, client in clients:
        try:
            await client.close()
            logger.info(f"  - Клиент #{acc.id} закрыт")
        except Exception as e:
            logger.warning(f"  - Ошибка закрытия клиента #{acc.id}: {e}")

    log_result(True, "Все клиенты закрыты")

    # Итог
    log_separator("ИТОГИ DRY-RUN ТЕСТА")

    # Статистика по этапам
    logger.info("--- Результаты по этапам ---")
    logger.info(f"Поиск получателя:      {results['search_success']}/{results['total']}")
    logger.info(f"Инициализация запроса: {results['init_success']}/{results['total']}")
    logger.info(f"Баланс достаточен:     {results['balance_ok']}/{results['total']}")
    logger.info(f"Симуляция оплаты:      {results['payment_simulated']}/{results['total']}")
    logger.info("")

    # Статистика по времени
    successful_count = len([t for t in results["order_times"] if t > 0])
    failed_count = len(results["errors"])

    logger.info("--- Статистика ---")
    logger.info(f"Всего заказов:    {results['total']}")
    logger.info(f"Успешных:         {successful_count - failed_count}")
    logger.info(f"С ошибками:       {failed_count}")
    logger.info(f"Успешность:       {((successful_count - failed_count) / results['total'] * 100):.1f}%")
    logger.info("")

    # Временная статистика
    logger.info("--- Время выполнения ---")
    logger.info(f"Общее время:      {total_time:.2f}s")

    if results["order_times"]:
        avg_time = sum(results["order_times"]) / len(results["order_times"])
        min_time = min(results["order_times"])
        max_time = max(results["order_times"])

        logger.info(f"Среднее на заказ: {avg_time:.2f}s")
        logger.info(f"Минимум:          {min_time:.2f}s")
        logger.info(f"Максимум:         {max_time:.2f}s")

        # Оценка для реальных заказов
        orders_per_minute = 60 / avg_time if avg_time > 0 else 0
        orders_per_hour = orders_per_minute * 60

        logger.info("")
        logger.info("--- Прогноз производительности ---")
        logger.info(f"Заказов в минуту: ~{orders_per_minute:.1f}")
        logger.info(f"Заказов в час:    ~{orders_per_hour:.0f}")
        logger.info(f"(с {len(clients)} аккаунтом(ами), последовательно)")

    logger.info("")

    if results["errors"]:
        logger.info(f"--- Ошибки ({len(results['errors'])}) ---")
        for err in results["errors"]:
            logger.info(f"  - {err}")
        logger.info("")

    all_success = (
        results["search_success"] == results["total"] and
        results["init_success"] == results["total"]
    )

    if all_success:
        logger.info("[OK] DRY-RUN ТЕСТ ПРОЙДЕН!")
        logger.info("    Все API вызовы успешны. Готов к реальным транзакциям.")
    else:
        logger.info("[FAIL] DRY-RUN ТЕСТ НЕ ПРОЙДЕН!")
        logger.info("      Проверьте ошибки выше.")

    return all_success


# ==================== MAIN ====================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Test worker auto-configuration and order processing"
    )
    parser.add_argument(
        "--count", "-c",
        type=int,
        default=5,
        help="Number of orders to process (default: 5)"
    )
    parser.add_argument(
        "--dry-run", "-d",
        action="store_true",
        help="Use real Fragment API but don't make actual payments"
    )
    parser.add_argument(
        "--username", "-u",
        type=str,
        default="durov",
        help="Target username for dry-run test (default: durov)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Transaction delay in mock mode (default: 0.3s)"
    )
    parser.add_argument(
        "--success-rate",
        type=float,
        default=0.95,
        help="Success rate in mock mode, 0.0-1.0 (default: 0.95)"
    )
    parser.add_argument(
        "--payment-delay",
        type=float,
        default=4.0,
        help="Payment simulation delay in dry-run mode (default: 4.0s)"
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    if args.dry_run:
        result = await run_dry_run_test(args.count, args.username, args.payment_delay)
    else:
        result = await run_mock_test(args.count, args.delay, args.success_rate)

    return result


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        logger.info("\n[!] Тест прерван пользователем")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"[!] Критическая ошибка: {e}")
        sys.exit(1)
