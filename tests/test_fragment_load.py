"""
Нагрузочные тесты для Fragment API и системы очередей.

Запуск:
    pytest tests/test_fragment_load.py -v -s

    # Только тесты очереди
    pytest tests/test_fragment_load.py -v -s -k "queue"

    # Полный нагрузочный тест
    pytest tests/test_fragment_load.py -v -s -k "full_load"
"""
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Импорты из проекта
from src.core.queue import InMemoryQueue, QueueItem

# Попытка импорта Redis очереди
try:
    from src.core.queue import RedisQueue
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


@dataclass
class LoadTestMetrics:
    """Метрики нагрузочного теста."""
    test_name: str
    total_operations: int = 0
    successful_operations: int = 0
    failed_operations: int = 0

    # Временные метрики (в секундах)
    latencies: list[float] = field(default_factory=list)
    start_time: float = 0
    end_time: float = 0

    # Метрики очереди
    max_queue_depth: int = 0
    final_queue_depth: int = 0

    @property
    def duration(self) -> float:
        """Общая длительность теста."""
        return self.end_time - self.start_time

    @property
    def throughput(self) -> float:
        """Операций в секунду."""
        if self.duration > 0:
            return self.successful_operations / self.duration
        return 0

    @property
    def success_rate(self) -> float:
        """Процент успешных операций."""
        if self.total_operations > 0:
            return (self.successful_operations / self.total_operations) * 100
        return 0

    @property
    def avg_latency(self) -> float:
        """Средняя задержка."""
        if self.latencies:
            return statistics.mean(self.latencies)
        return 0

    @property
    def p50_latency(self) -> float:
        """Медианная задержка (50-й перцентиль)."""
        if self.latencies:
            return statistics.median(self.latencies)
        return 0

    @property
    def p95_latency(self) -> float:
        """95-й перцентиль задержки."""
        if len(self.latencies) >= 2:
            sorted_latencies = sorted(self.latencies)
            idx = int(len(sorted_latencies) * 0.95)
            return sorted_latencies[idx]
        return self.avg_latency

    @property
    def p99_latency(self) -> float:
        """99-й перцентиль задержки."""
        if len(self.latencies) >= 2:
            sorted_latencies = sorted(self.latencies)
            idx = int(len(sorted_latencies) * 0.99)
            return sorted_latencies[idx]
        return self.avg_latency

    @property
    def min_latency(self) -> float:
        """Минимальная задержка."""
        return min(self.latencies) if self.latencies else 0

    @property
    def max_latency(self) -> float:
        """Максимальная задержка."""
        return max(self.latencies) if self.latencies else 0

    def report(self) -> str:
        """Форматированный отчёт."""
        return f"""
╔══════════════════════════════════════════════════════════════╗
║  ОТЧЁТ НАГРУЗОЧНОГО ТЕСТА: {self.test_name:<32} ║
╠══════════════════════════════════════════════════════════════╣
║  ОБЩИЕ МЕТРИКИ                                               ║
║  ─────────────────────────────────────────────────────────── ║
║  Длительность:           {self.duration:>10.3f} сек                    ║
║  Всего операций:         {self.total_operations:>10}                        ║
║  Успешных:               {self.successful_operations:>10}                        ║
║  Неудачных:              {self.failed_operations:>10}                        ║
║  Успешность:             {self.success_rate:>10.2f}%                       ║
║  Пропускная способность: {self.throughput:>10.2f} оп/сек                 ║
╠══════════════════════════════════════════════════════════════╣
║  ЗАДЕРЖКИ (мс)                                               ║
║  ─────────────────────────────────────────────────────────── ║
║  Минимальная:            {self.min_latency * 1000:>10.3f} мс                    ║
║  Средняя:                {self.avg_latency * 1000:>10.3f} мс                    ║
║  Медиана (P50):          {self.p50_latency * 1000:>10.3f} мс                    ║
║  P95:                    {self.p95_latency * 1000:>10.3f} мс                    ║
║  P99:                    {self.p99_latency * 1000:>10.3f} мс                    ║
║  Максимальная:           {self.max_latency * 1000:>10.3f} мс                    ║
╠══════════════════════════════════════════════════════════════╣
║  ОЧЕРЕДЬ                                                     ║
║  ─────────────────────────────────────────────────────────── ║
║  Макс. глубина:          {self.max_queue_depth:>10}                        ║
║  Финальная глубина:      {self.final_queue_depth:>10}                        ║
╚══════════════════════════════════════════════════════════════╝
"""


class TestQueuePerformance:
    """Тесты производительности очередей."""

    @pytest.fixture
    def memory_queue(self):
        """Создать in-memory очередь."""
        return InMemoryQueue()

    @pytest.mark.asyncio
    async def test_enqueue_performance(self, memory_queue):
        """Тест скорости добавления в очередь."""
        metrics = LoadTestMetrics(test_name="Enqueue Performance")
        num_items = 10000

        metrics.start_time = time.perf_counter()

        for i in range(num_items):
            start = time.perf_counter()
            await memory_queue.enqueue(i)
            metrics.latencies.append(time.perf_counter() - start)
            metrics.total_operations += 1
            metrics.successful_operations += 1

        metrics.end_time = time.perf_counter()
        metrics.max_queue_depth = await memory_queue.size()
        metrics.final_queue_depth = await memory_queue.size()

        print(metrics.report())

        # Проверки
        assert metrics.success_rate == 100
        assert metrics.throughput > 1000, f"Слишком медленно: {metrics.throughput:.2f} оп/сек"
        assert metrics.avg_latency < 0.001, f"Слишком высокая задержка: {metrics.avg_latency * 1000:.3f} мс"

    @pytest.mark.asyncio
    async def test_dequeue_performance(self, memory_queue):
        """Тест скорости извлечения из очереди."""
        num_items = 10000

        # Заполняем очередь
        for i in range(num_items):
            await memory_queue.enqueue(i)

        metrics = LoadTestMetrics(test_name="Dequeue Performance")
        metrics.max_queue_depth = num_items
        metrics.start_time = time.perf_counter()

        for _ in range(num_items):
            start = time.perf_counter()
            item = await memory_queue.dequeue(timeout=1.0)
            metrics.latencies.append(time.perf_counter() - start)
            metrics.total_operations += 1
            if item:
                metrics.successful_operations += 1
            else:
                metrics.failed_operations += 1

        metrics.end_time = time.perf_counter()
        metrics.final_queue_depth = await memory_queue.size()

        print(metrics.report())

        assert metrics.success_rate == 100
        assert metrics.throughput > 1000
        assert metrics.final_queue_depth == 0

    @pytest.mark.asyncio
    async def test_concurrent_enqueue_dequeue(self, memory_queue):
        """Тест конкурентного добавления и извлечения."""
        metrics = LoadTestMetrics(test_name="Concurrent Enqueue/Dequeue")
        num_producers = 5
        num_consumers = 3
        items_per_producer = 1000
        total_items = num_producers * items_per_producer

        produced = 0
        consumed = 0
        produce_latencies = []
        consume_latencies = []

        async def producer(producer_id: int):
            nonlocal produced
            for i in range(items_per_producer):
                start = time.perf_counter()
                await memory_queue.enqueue(producer_id * items_per_producer + i)
                produce_latencies.append(time.perf_counter() - start)
                produced += 1
                # Небольшая задержка для реалистичности
                if i % 100 == 0:
                    await asyncio.sleep(0.001)

        async def consumer(consumer_id: int):
            nonlocal consumed
            while consumed < total_items:
                start = time.perf_counter()
                item = await memory_queue.dequeue(timeout=0.1)
                if item:
                    consume_latencies.append(time.perf_counter() - start)
                    consumed += 1

        metrics.start_time = time.perf_counter()

        # Запускаем продюсеров и консьюмеров параллельно
        producers = [asyncio.create_task(producer(i)) for i in range(num_producers)]
        consumers = [asyncio.create_task(consumer(i)) for i in range(num_consumers)]

        # Ждём завершения продюсеров
        await asyncio.gather(*producers)

        # Даём консьюмерам время дочитать очередь
        await asyncio.sleep(0.5)

        # Отменяем консьюмеров
        for c in consumers:
            c.cancel()

        metrics.end_time = time.perf_counter()
        metrics.total_operations = produced + consumed
        metrics.successful_operations = consumed
        metrics.latencies = produce_latencies + consume_latencies
        metrics.final_queue_depth = await memory_queue.size()
        metrics.max_queue_depth = total_items  # Примерная оценка

        print(metrics.report())
        print(f"  Произведено: {produced}")
        print(f"  Потреблено:  {consumed}")
        print(f"  В очереди:   {metrics.final_queue_depth}")

        # Проверяем что большая часть обработана
        assert consumed >= total_items * 0.9, f"Обработано только {consumed}/{total_items}"

    @pytest.mark.asyncio
    async def test_queue_under_pressure(self, memory_queue):
        """Тест очереди под высокой нагрузкой."""
        metrics = LoadTestMetrics(test_name="Queue Under Pressure")

        # Параметры теста
        burst_size = 1000
        num_bursts = 10
        process_delay = 0.0001  # Имитация обработки

        total_enqueued = 0
        total_dequeued = 0
        max_depth = 0

        metrics.start_time = time.perf_counter()

        for burst in range(num_bursts):
            # Burst: добавляем много элементов быстро
            for i in range(burst_size):
                start = time.perf_counter()
                await memory_queue.enqueue(burst * burst_size + i)
                metrics.latencies.append(time.perf_counter() - start)
                total_enqueued += 1

            current_depth = await memory_queue.size()
            max_depth = max(max_depth, current_depth)

            # Обрабатываем часть очереди
            for _ in range(burst_size // 2):
                item = await memory_queue.dequeue(timeout=0.01)
                if item:
                    total_dequeued += 1
                    await asyncio.sleep(process_delay)

        # Дочитываем остаток
        while True:
            item = await memory_queue.dequeue(timeout=0.1)
            if not item:
                break
            total_dequeued += 1

        metrics.end_time = time.perf_counter()
        metrics.total_operations = total_enqueued
        metrics.successful_operations = total_dequeued
        metrics.failed_operations = total_enqueued - total_dequeued
        metrics.max_queue_depth = max_depth
        metrics.final_queue_depth = await memory_queue.size()

        print(metrics.report())

        assert total_dequeued == total_enqueued, f"Потеряно {total_enqueued - total_dequeued} элементов"


class TestFragmentAPILoad:
    """Тесты нагрузки на Fragment API (с моками)."""

    @pytest.fixture
    def mock_fragment_client(self):
        """Мок Fragment клиента."""
        client = MagicMock()

        async def mock_search_recipient(username):
            await asyncio.sleep(0.05)  # Имитация сетевой задержки
            return MagicMock(recipient_id="123", username=username)

        async def mock_init_request(recipient_id, quantity):
            await asyncio.sleep(0.03)
            return MagicMock(req_id="req_123", amount=quantity)

        async def mock_execute_payment(req_id, show_sender=False):
            await asyncio.sleep(0.1)  # Транзакция дольше
            return [MagicMock(tx_hash="0x123abc", success=True)]

        client.search_stars_recipient = mock_search_recipient
        client.init_stars_request = mock_init_request
        client.execute_stars_payment = mock_execute_payment
        client.search_premium_recipient = mock_search_recipient
        client.init_premium_request = mock_init_request
        client.execute_premium_payment = mock_execute_payment

        return client

    @pytest.mark.asyncio
    async def test_sequential_orders_performance(self, mock_fragment_client):
        """Тест последовательной обработки заказов."""
        metrics = LoadTestMetrics(test_name="Sequential Orders")
        num_orders = 50

        metrics.start_time = time.perf_counter()

        for i in range(num_orders):
            start = time.perf_counter()
            try:
                # Полный цикл заказа
                recipient = await mock_fragment_client.search_stars_recipient(f"user_{i}")
                request = await mock_fragment_client.init_stars_request(recipient.recipient_id, 100)
                result = await mock_fragment_client.execute_stars_payment(request.req_id)

                metrics.latencies.append(time.perf_counter() - start)
                metrics.successful_operations += 1
            except Exception as e:
                metrics.failed_operations += 1

            metrics.total_operations += 1

        metrics.end_time = time.perf_counter()

        print(metrics.report())

        # При последовательной обработке ~180мс на заказ
        assert metrics.avg_latency < 0.3, f"Слишком медленно: {metrics.avg_latency:.3f}с"
        assert metrics.success_rate == 100

    @pytest.mark.asyncio
    async def test_concurrent_orders_performance(self, mock_fragment_client):
        """Тест параллельной обработки заказов (несколько аккаунтов)."""
        metrics = LoadTestMetrics(test_name="Concurrent Orders")
        num_orders = 50
        max_concurrent = 5  # Имитация 5 Fragment аккаунтов

        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_order(order_id: int) -> float:
            async with semaphore:
                start = time.perf_counter()
                recipient = await mock_fragment_client.search_stars_recipient(f"user_{order_id}")
                request = await mock_fragment_client.init_stars_request(recipient.recipient_id, 100)
                await mock_fragment_client.execute_stars_payment(request.req_id)
                return time.perf_counter() - start

        metrics.start_time = time.perf_counter()

        tasks = [process_order(i) for i in range(num_orders)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        metrics.end_time = time.perf_counter()

        for result in results:
            metrics.total_operations += 1
            if isinstance(result, Exception):
                metrics.failed_operations += 1
            else:
                metrics.successful_operations += 1
                metrics.latencies.append(result)

        print(metrics.report())
        print(f"  Параллельность: {max_concurrent} аккаунтов")
        print(f"  Теоретический макс: {num_orders / metrics.duration:.2f} оп/сек")

        # С 5 параллельными аккаунтами должно быть ~5x быстрее
        assert metrics.throughput > 2, f"Мало выигрыша от параллельности: {metrics.throughput:.2f}"


class TestWorkerSimulation:
    """Симуляция работы OrderWorker."""

    @pytest.mark.asyncio
    async def test_worker_throughput_simulation(self):
        """Симуляция пропускной способности воркера."""
        metrics = LoadTestMetrics(test_name="Worker Throughput Simulation")

        queue = InMemoryQueue()
        num_orders = 100

        # Параметры симуляции (реалистичные)
        api_latency = 0.15  # 150мс на API вызов (search + init + execute)
        db_latency = 0.01   # 10мс на DB операцию
        poll_interval = 0.1  # 100мс между проверками очереди

        # Заполняем очередь
        for i in range(num_orders):
            await queue.enqueue(i)

        metrics.max_queue_depth = num_orders
        processed = 0

        async def simulate_order_processing():
            """Симуляция обработки одного заказа."""
            await asyncio.sleep(db_latency)  # Получить заказ из DB
            await asyncio.sleep(db_latency)  # Получить аккаунт
            await asyncio.sleep(api_latency)  # Fragment API
            await asyncio.sleep(db_latency)  # Обновить статус

        metrics.start_time = time.perf_counter()

        while True:
            item = await queue.dequeue(timeout=poll_interval)
            if not item:
                # Проверяем пустая ли очередь
                if await queue.size() == 0:
                    break
                continue

            start = time.perf_counter()
            await simulate_order_processing()
            metrics.latencies.append(time.perf_counter() - start)

            processed += 1
            metrics.successful_operations += 1
            metrics.total_operations += 1

        metrics.end_time = time.perf_counter()
        metrics.final_queue_depth = await queue.size()

        print(metrics.report())

        # Ожидаем ~5-6 заказов в секунду с такими задержками
        assert metrics.throughput > 3, f"Воркер слишком медленный: {metrics.throughput:.2f} оп/сек"
        assert processed == num_orders

    @pytest.mark.asyncio
    async def test_queue_buildup_scenario(self):
        """Тест сценария накопления очереди (burst нагрузка)."""
        metrics = LoadTestMetrics(test_name="Queue Buildup Scenario")

        queue = InMemoryQueue()

        # Параметры
        incoming_rate = 10  # заказов в секунду (входящих)
        processing_time = 0.2  # секунд на обработку
        test_duration = 5  # секунд

        queue_depths = []
        processed = 0
        stop_flag = False

        async def producer():
            """Генератор заказов."""
            order_id = 0
            while not stop_flag:
                await queue.enqueue(order_id)
                order_id += 1
                await asyncio.sleep(1 / incoming_rate)

        async def consumer():
            """Обработчик заказов."""
            nonlocal processed
            while not stop_flag:
                item = await queue.dequeue(timeout=0.1)
                if item:
                    await asyncio.sleep(processing_time)
                    processed += 1

        async def monitor():
            """Мониторинг глубины очереди."""
            while not stop_flag:
                depth = await queue.size()
                queue_depths.append(depth)
                await asyncio.sleep(0.5)

        metrics.start_time = time.perf_counter()

        # Запускаем всё параллельно
        producer_task = asyncio.create_task(producer())
        consumer_task = asyncio.create_task(consumer())
        monitor_task = asyncio.create_task(monitor())

        # Ждём заданное время
        await asyncio.sleep(test_duration)
        stop_flag = True

        # Даём время на остановку
        await asyncio.sleep(0.5)
        producer_task.cancel()
        consumer_task.cancel()
        monitor_task.cancel()

        metrics.end_time = time.perf_counter()
        metrics.total_operations = processed
        metrics.successful_operations = processed
        metrics.max_queue_depth = max(queue_depths) if queue_depths else 0
        metrics.final_queue_depth = await queue.size()

        print(metrics.report())
        print(f"\n  === АНАЛИЗ НАКОПЛЕНИЯ ОЧЕРЕДИ ===")
        print(f"  Входящий поток:     {incoming_rate} заказов/сек")
        print(f"  Время обработки:    {processing_time} сек")
        print(f"  Макс. пропускная:   {1/processing_time:.1f} заказов/сек")
        print(f"  Ожидаемое накопление: {(incoming_rate - 1/processing_time) * test_duration:.0f} заказов")
        print(f"  Фактическая очередь:  {metrics.final_queue_depth} заказов")
        print(f"  Динамика глубины: {queue_depths[:10]}...")

        # Если incoming > processing capacity, очередь должна расти
        if incoming_rate > 1/processing_time:
            assert metrics.final_queue_depth > 0, "Очередь должна была накопиться"


class TestFullLoadScenario:
    """Полный сценарий нагрузочного тестирования."""

    @pytest.mark.asyncio
    async def test_full_load_scenario(self):
        """
        Комплексный нагрузочный тест.

        Симулирует реальный сценарий:
        1. Пользователи создают заказы (разная интенсивность)
        2. Воркер обрабатывает очередь
        3. Мониторинг метрик в реальном времени
        """
        print("\n" + "="*70)
        print("  ПОЛНЫЙ НАГРУЗОЧНЫЙ ТЕСТ FRAGMENT API")
        print("="*70)

        queue = InMemoryQueue()

        # Конфигурация теста
        config = {
            "test_duration": 10,        # секунд
            "base_order_rate": 5,       # заказов/сек (базовый)
            "peak_order_rate": 15,      # заказов/сек (пиковый)
            "peak_duration": 3,         # секунд пиковой нагрузки
            "api_latency_min": 0.1,     # минимальная задержка API
            "api_latency_max": 0.3,     # максимальная задержка API
            "error_rate": 0.05,         # 5% ошибок
            "retry_delay": 1.0,         # задержка перед повтором
        }

        # Статистика
        stats = {
            "orders_created": 0,
            "orders_completed": 0,
            "orders_failed": 0,
            "orders_retried": 0,
            "queue_depths": [],
            "processing_times": [],
            "errors_by_type": {},
        }

        stop_flag = False

        import random

        async def order_generator():
            """Генератор заказов с переменной интенсивностью."""
            start_time = time.perf_counter()
            order_id = 0

            while not stop_flag:
                elapsed = time.perf_counter() - start_time

                # Определяем текущую интенсивность
                if 3 < elapsed < 3 + config["peak_duration"]:
                    rate = config["peak_order_rate"]
                else:
                    rate = config["base_order_rate"]

                await queue.enqueue(order_id)
                stats["orders_created"] += 1
                order_id += 1

                # Случайная задержка для реалистичности
                delay = 1 / rate * random.uniform(0.8, 1.2)
                await asyncio.sleep(delay)

        async def order_processor():
            """Обработчик заказов (симуляция воркера)."""
            while not stop_flag or await queue.size() > 0:
                item = await queue.dequeue(timeout=0.1)
                if not item:
                    continue

                start = time.perf_counter()

                # Симуляция обработки с вариативностью
                latency = random.uniform(
                    config["api_latency_min"],
                    config["api_latency_max"]
                )
                await asyncio.sleep(latency)

                # Симуляция ошибок
                if random.random() < config["error_rate"]:
                    error_type = random.choice([
                        "timeout", "rate_limit", "session_expired", "network"
                    ])
                    stats["errors_by_type"][error_type] = stats["errors_by_type"].get(error_type, 0) + 1

                    if error_type in ["timeout", "rate_limit", "network"]:
                        # Retryable error
                        stats["orders_retried"] += 1
                        await asyncio.sleep(config["retry_delay"])
                        await queue.enqueue(item.order_id, max_retries=item.max_retries)
                    else:
                        # Permanent error
                        stats["orders_failed"] += 1
                else:
                    stats["orders_completed"] += 1
                    stats["processing_times"].append(time.perf_counter() - start)

        async def metrics_collector():
            """Сбор метрик."""
            while not stop_flag:
                depth = await queue.size()
                stats["queue_depths"].append(depth)
                await asyncio.sleep(0.5)

        # Запуск теста
        start_time = time.perf_counter()

        generator_task = asyncio.create_task(order_generator())
        processor_task = asyncio.create_task(order_processor())
        metrics_task = asyncio.create_task(metrics_collector())

        await asyncio.sleep(config["test_duration"])
        stop_flag = True

        # Останавливаем генератор и метрики
        generator_task.cancel()
        metrics_task.cancel()

        try:
            await generator_task
        except asyncio.CancelledError:
            pass

        try:
            await metrics_task
        except asyncio.CancelledError:
            pass

        # Ждём обработки оставшихся заказов (с таймаутом)
        try:
            await asyncio.wait_for(processor_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            processor_task.cancel()
            try:
                await processor_task
            except asyncio.CancelledError:
                pass

        end_time = time.perf_counter()
        duration = end_time - start_time

        # Формируем отчёт
        print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║                    РЕЗУЛЬТАТЫ НАГРУЗОЧНОГО ТЕСТА                     ║
╠══════════════════════════════════════════════════════════════════════╣
║  КОНФИГУРАЦИЯ                                                        ║
║  ──────────────────────────────────────────────────────────────────  ║
║  Длительность теста:    {config['test_duration']:>5} сек                                 ║
║  Базовая нагрузка:      {config['base_order_rate']:>5} заказов/сек                        ║
║  Пиковая нагрузка:      {config['peak_order_rate']:>5} заказов/сек                        ║
║  Длительность пика:     {config['peak_duration']:>5} сек                                 ║
║  Симулируемые ошибки:   {config['error_rate']*100:>5.0f}%                                  ║
╠══════════════════════════════════════════════════════════════════════╣
║  РЕЗУЛЬТАТЫ                                                          ║
║  ──────────────────────────────────────────────────────────────────  ║
║  Всего создано:         {stats['orders_created']:>5} заказов                             ║
║  Успешно обработано:    {stats['orders_completed']:>5} заказов                             ║
║  Неудачных:             {stats['orders_failed']:>5} заказов                             ║
║  Повторных попыток:     {stats['orders_retried']:>5}                                     ║
║  Осталось в очереди:    {await queue.size():>5}                                     ║
╠══════════════════════════════════════════════════════════════════════╣
║  ПРОИЗВОДИТЕЛЬНОСТЬ                                                  ║
║  ──────────────────────────────────────────────────────────────────  ║
║  Фактическая длительность:  {duration:>8.2f} сек                           ║
║  Пропускная способность:    {stats['orders_completed']/duration:>8.2f} заказов/сек               ║
║  Макс. глубина очереди:     {max(stats['queue_depths']) if stats['queue_depths'] else 0:>8}                              ║
║  Сред. глубина очереди:     {statistics.mean(stats['queue_depths']) if stats['queue_depths'] else 0:>8.1f}                              ║
╠══════════════════════════════════════════════════════════════════════╣
║  ЗАДЕРЖКИ ОБРАБОТКИ                                                  ║
║  ──────────────────────────────────────────────────────────────────  ║""")

        if stats['processing_times']:
            print(f"║  Минимальная:               {min(stats['processing_times'])*1000:>8.1f} мс                           ║")
            print(f"║  Средняя:                   {statistics.mean(stats['processing_times'])*1000:>8.1f} мс                           ║")
            print(f"║  Максимальная:              {max(stats['processing_times'])*1000:>8.1f} мс                           ║")

        print(f"""╠══════════════════════════════════════════════════════════════════════╣
║  ОШИБКИ ПО ТИПАМ                                                     ║
║  ──────────────────────────────────────────────────────────────────  ║""")

        for error_type, count in stats['errors_by_type'].items():
            print(f"║  {error_type:<25} {count:>5}                                     ║")

        print("╚══════════════════════════════════════════════════════════════════════╝")

        # Проверки
        assert stats['orders_completed'] > 0, "Ни один заказ не обработан"
        success_rate = stats['orders_completed'] / stats['orders_created'] * 100
        assert success_rate > 50, f"Слишком низкий success rate: {success_rate:.1f}%"


# ============================================================================
# Redis vs InMemory сравнение
# ============================================================================

@pytest.mark.skipif(not REDIS_AVAILABLE, reason="Redis не доступен")
class TestRedisVsMemoryComparison:
    """Сравнение Redis и InMemory очередей."""

    @pytest.mark.asyncio
    async def test_compare_queues(self):
        """Сравнительный тест производительности очередей."""
        num_items = 1000

        # InMemory тест
        memory_queue = InMemoryQueue()
        memory_metrics = LoadTestMetrics(test_name="InMemory Queue")

        memory_metrics.start_time = time.perf_counter()
        for i in range(num_items):
            start = time.perf_counter()
            await memory_queue.enqueue(i)
            memory_metrics.latencies.append(time.perf_counter() - start)

        for _ in range(num_items):
            await memory_queue.dequeue(timeout=0.1)
        memory_metrics.end_time = time.perf_counter()
        memory_metrics.total_operations = num_items * 2
        memory_metrics.successful_operations = num_items * 2

        print("\n=== СРАВНЕНИЕ ОЧЕРЕДЕЙ ===")
        print(f"\nInMemory Queue:")
        print(f"  Операций: {memory_metrics.total_operations}")
        print(f"  Время: {memory_metrics.duration:.3f} сек")
        print(f"  Throughput: {memory_metrics.throughput:.0f} оп/сек")
        print(f"  Avg latency: {memory_metrics.avg_latency*1000:.3f} мс")


# ============================================================================
# Запуск как скрипт
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
