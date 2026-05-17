"""
Абстракция очереди заказов.

Поддерживает две реализации:
- InMemoryQueue: для разработки и тестирования
- RedisQueue: для продакшена

Очередь работает с order_id, а не с полными объектами заказов.
"""
import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    """Элемент очереди."""

    order_id: int
    retry_count: int = 0
    max_retries: int = 3
    enqueued_at: datetime = None

    def __post_init__(self):
        if self.enqueued_at is None:
            self.enqueued_at = datetime.utcnow()

    @property
    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries


class OrderQueue(ABC):
    """Абстрактный интерфейс очереди заказов."""

    @abstractmethod
    async def enqueue(self, order_id: int, max_retries: int = 3) -> None:
        """Добавить заказ в очередь."""
        pass

    @abstractmethod
    async def dequeue(self, timeout: float = None) -> QueueItem | None:
        """
        Извлечь заказ из очереди.

        Args:
            timeout: Максимальное время ожидания (None = не ждать)

        Returns:
            QueueItem или None если очередь пуста
        """
        pass

    @abstractmethod
    async def requeue(self, item: QueueItem, delay: float = 0) -> None:
        """
        Вернуть заказ в очередь для повторной обработки.

        Args:
            item: Элемент очереди
            delay: Задержка перед повторной обработкой (секунды)
        """
        pass

    @abstractmethod
    async def size(self) -> int:
        """Получить размер очереди."""
        pass

    @abstractmethod
    async def clear(self) -> None:
        """Очистить очередь."""
        pass


class InMemoryQueue(OrderQueue):
    """
    In-memory реализация очереди для разработки и тестирования.

    Особенности:
    - Не персистентная (теряется при перезапуске)
    - FIFO порядок
    - Поддержка ожидания через asyncio.Event
    """

    def __init__(self):
        self._queue: deque[QueueItem] = deque()
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._delayed_tasks: set[asyncio.Task] = set()  # Для отложенных requeue

    async def enqueue(self, order_id: int, max_retries: int = 3) -> None:
        async with self._lock:
            item = QueueItem(order_id=order_id, max_retries=max_retries)
            self._queue.append(item)
            self._event.set()
            logger.debug(f"Order {order_id} enqueued (size={len(self._queue)})")

    async def dequeue(self, timeout: float = None) -> QueueItem | None:
        # Быстрая проверка без ожидания
        async with self._lock:
            if self._queue:
                item = self._queue.popleft()
                if not self._queue:
                    self._event.clear()
                logger.debug(f"Order {item.order_id} dequeued (size={len(self._queue)})")
                return item

        if timeout is None:
            return None

        # Ожидание с таймаутом
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            async with self._lock:
                if self._queue:
                    item = self._queue.popleft()
                    if not self._queue:
                        self._event.clear()
                    logger.debug(f"Order {item.order_id} dequeued after wait")
                    return item
        except asyncio.TimeoutError:
            pass

        return None

    async def requeue(self, item: QueueItem, delay: float = 0) -> None:
        if delay > 0:
            # Не блокируем вызывающего — планируем отложенное добавление
            task = asyncio.create_task(self._delayed_requeue(item, delay))
            self._delayed_tasks.add(task)
            task.add_done_callback(self._delayed_tasks.discard)
            logger.debug(f"Order {item.order_id} scheduled for requeue in {delay}s")
            return

        async with self._lock:
            item.retry_count += 1
            item.enqueued_at = datetime.utcnow()
            self._queue.append(item)
            self._event.set()
            logger.debug(
                f"Order {item.order_id} requeued "
                f"(retry={item.retry_count}, size={len(self._queue)})"
            )

    async def _delayed_requeue(self, item: QueueItem, delay: float) -> None:
        """Отложенное добавление в очередь (не блокирует caller)."""
        await asyncio.sleep(delay)
        async with self._lock:
            item.retry_count += 1
            item.enqueued_at = datetime.utcnow()
            self._queue.append(item)
            self._event.set()
            logger.debug(
                f"Order {item.order_id} requeued after {delay}s delay "
                f"(retry={item.retry_count}, size={len(self._queue)})"
            )

    async def size(self) -> int:
        async with self._lock:
            return len(self._queue)

    async def clear(self) -> None:
        async with self._lock:
            self._queue.clear()
            self._event.clear()
            logger.debug("Queue cleared")


# Глобальный экземпляр очереди (можно заменить на Redis в продакшене)
_order_queue: OrderQueue | None = None


def get_order_queue() -> OrderQueue:
    """Получить глобальный экземпляр очереди."""
    global _order_queue
    if _order_queue is None:
        _order_queue = InMemoryQueue()
    return _order_queue


def set_order_queue(queue: OrderQueue) -> None:
    """Установить глобальный экземпляр очереди."""
    global _order_queue
    _order_queue = queue


class RedisQueue(OrderQueue):
    """
    Redis-based персистентная очередь для продакшена.

    Ключи Redis:
    - orders:queue — LIST для основной FIFO очереди
    - orders:delayed — SORTED SET для отложенных элементов (score = timestamp готовности)
    - orders:processing — HASH для отслеживания обрабатываемых элементов (для recovery)

    Особенности:
    - Персистентная (переживает перезапуск)
    - FIFO порядок
    - Поддержка отложенного requeue (экспоненциальный backoff)
    - Background task для переноса готовых delayed элементов в основную очередь
    """

    MAIN_QUEUE_KEY = "orders:queue"
    DELAYED_SET_KEY = "orders:delayed"
    PROCESSING_KEY = "orders:processing"

    def __init__(
        self,
        redis_url: str,
        delayed_poll_interval: float = 1.0,
        reconnect_interval: float = 5.0,
        max_reconnect_attempts: int = 10,
    ):
        """
        Args:
            redis_url: URL подключения к Redis (например redis://localhost:6379/0)
            delayed_poll_interval: Интервал проверки delayed очереди (секунды)
            reconnect_interval: Интервал между попытками переподключения (секунды)
            max_reconnect_attempts: Максимальное число попыток переподключения
        """
        self._redis_url = redis_url
        self._delayed_poll_interval = delayed_poll_interval
        self._reconnect_interval = reconnect_interval
        self._max_reconnect_attempts = max_reconnect_attempts

        self._redis: Optional[aioredis.Redis] = None
        self._delayed_task: Optional[asyncio.Task] = None
        self._running = False
        self._connection_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Установить соединение с Redis и запустить background tasks."""
        self._redis = aioredis.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
        )

        # Проверить соединение
        await self._redis.ping()

        # Запустить обработчик delayed очереди
        self._running = True
        self._delayed_task = asyncio.create_task(self._process_delayed_queue())

        logger.info(f"RedisQueue connected to {self._redis_url}")

    async def _ensure_connected(self) -> None:
        """Проверить соединение и переподключиться при необходимости."""
        async with self._connection_lock:
            for attempt in range(self._max_reconnect_attempts):
                try:
                    if self._redis is None:
                        self._redis = aioredis.from_url(
                            self._redis_url,
                            encoding="utf-8",
                            decode_responses=True,
                        )
                    await self._redis.ping()
                    return  # Соединение работает
                except Exception as e:
                    logger.warning(
                        f"Redis connection check failed (attempt {attempt + 1}/{self._max_reconnect_attempts}): {e}"
                    )
                    # Закрываем старое соединение
                    if self._redis:
                        try:
                            await self._redis.close()
                        except Exception:
                            pass
                        self._redis = None

                    if attempt < self._max_reconnect_attempts - 1:
                        await asyncio.sleep(self._reconnect_interval)

            raise ConnectionError(f"Failed to connect to Redis after {self._max_reconnect_attempts} attempts")

    async def close(self) -> None:
        """Закрыть соединение с Redis и остановить background tasks."""
        self._running = False

        if self._delayed_task:
            self._delayed_task.cancel()
            try:
                await self._delayed_task
            except asyncio.CancelledError:
                pass

        if self._redis:
            await self._redis.close()

        logger.info("RedisQueue closed")

    async def enqueue(self, order_id: int, max_retries: int = 3) -> None:
        """Добавить заказ в очередь."""
        await self._ensure_connected()

        item = QueueItem(
            order_id=order_id,
            retry_count=0,
            max_retries=max_retries,
            enqueued_at=datetime.utcnow(),
        )

        item_json = self._serialize_item(item)
        await self._redis.rpush(self.MAIN_QUEUE_KEY, item_json)

        logger.debug(f"Order {order_id} enqueued to Redis")

    async def dequeue(self, timeout: float = None) -> Optional[QueueItem]:
        """
        Извлечь заказ из очереди.

        Использует BLPOP для блокирующего ожидания с таймаутом.
        """
        await self._ensure_connected()

        if timeout is None:
            # Неблокирующее извлечение
            item_json = await self._redis.lpop(self.MAIN_QUEUE_KEY)
        else:
            # Блокирующее ожидание с таймаутом
            # Используем max(1, int(timeout)) чтобы избежать 0 или отрицательных значений
            blpop_timeout = max(1, int(timeout)) if timeout >= 1 else 1
            result = await self._redis.blpop(
                self.MAIN_QUEUE_KEY,
                timeout=blpop_timeout,
            )
            item_json = result[1] if result else None

        if item_json is None:
            return None

        item = self._deserialize_item(item_json)

        # Отслеживать в processing set (для crash recovery)
        await self._redis.hset(
            self.PROCESSING_KEY,
            str(item.order_id),
            item_json,
        )

        logger.debug(f"Order {item.order_id} dequeued from Redis")
        return item

    async def requeue(self, item: QueueItem, delay: float = 0) -> None:
        """
        Вернуть элемент в очередь для повторной обработки.

        Если delay > 0, добавляет в delayed sorted set.
        """
        await self._ensure_connected()

        # Удалить из processing
        await self._redis.hdel(self.PROCESSING_KEY, str(item.order_id))

        # Увеличить счётчик попыток
        item.retry_count += 1
        item.enqueued_at = datetime.utcnow()

        item_json = self._serialize_item(item)

        if delay > 0:
            # Добавить в delayed set с score = timestamp готовности
            ready_at = time.time() + delay
            await self._redis.zadd(self.DELAYED_SET_KEY, {item_json: ready_at})
            logger.debug(f"Order {item.order_id} delayed for {delay}s")
        else:
            # Сразу добавить в основную очередь
            await self._redis.rpush(self.MAIN_QUEUE_KEY, item_json)
            logger.debug(f"Order {item.order_id} requeued immediately")

    async def ack(self, order_id: int) -> None:
        """
        Подтвердить успешную обработку (удалить из processing set).

        Вызывается worker'ом после завершения или окончательного отказа.
        """
        try:
            await self._ensure_connected()
            await self._redis.hdel(self.PROCESSING_KEY, str(order_id))
        except Exception as e:
            logger.error(f"Failed to ack order {order_id}: {e}")

    async def size(self) -> int:
        """Получить общий размер очереди (main + delayed)."""
        await self._ensure_connected()
        main_size = await self._redis.llen(self.MAIN_QUEUE_KEY)
        delayed_size = await self._redis.zcard(self.DELAYED_SET_KEY)
        return main_size + delayed_size

    async def clear(self) -> None:
        """Очистить все очереди."""
        await self._ensure_connected()
        await self._redis.delete(
            self.MAIN_QUEUE_KEY,
            self.DELAYED_SET_KEY,
            self.PROCESSING_KEY,
        )
        logger.debug("Redis queue cleared")

    async def get_processing_items(self) -> list[QueueItem]:
        """
        Получить все элементы в состоянии processing (для recovery).

        Используется при запуске для восстановления заказов,
        которые были прерваны при crash.
        """
        await self._ensure_connected()
        items_dict = await self._redis.hgetall(self.PROCESSING_KEY)
        return [self._deserialize_item(json_str) for json_str in items_dict.values()]

    async def _process_delayed_queue(self) -> None:
        """
        Background task для переноса готовых delayed элементов в основную очередь.

        Работает непрерывно пока очередь активна.
        """
        while self._running:
            try:
                await self._ensure_connected()
                now = time.time()

                # Получить элементы, которые готовы (score <= now)
                ready_items = await self._redis.zrangebyscore(
                    self.DELAYED_SET_KEY,
                    min="-inf",
                    max=now,
                    start=0,
                    num=100,
                )

                if ready_items:
                    # Атомарно перенести в основную очередь через pipeline
                    async with self._redis.pipeline(transaction=True) as pipe:
                        for item_json in ready_items:
                            pipe.zrem(self.DELAYED_SET_KEY, item_json)
                            pipe.rpush(self.MAIN_QUEUE_KEY, item_json)
                        await pipe.execute()

                    logger.debug(f"Moved {len(ready_items)} delayed items to main queue")

                await asyncio.sleep(self._delayed_poll_interval)

            except asyncio.CancelledError:
                break
            except ConnectionError as e:
                logger.error(f"Redis connection lost in delayed queue processor: {e}")
                await asyncio.sleep(self._reconnect_interval)
            except Exception as e:
                logger.exception(f"Error in delayed queue processor: {e}")
                await asyncio.sleep(self._delayed_poll_interval)

    def _serialize_item(self, item: QueueItem) -> str:
        """Сериализовать QueueItem в JSON строку."""
        return json.dumps({
            "order_id": item.order_id,
            "retry_count": item.retry_count,
            "max_retries": item.max_retries,
            "enqueued_at": item.enqueued_at.isoformat() if item.enqueued_at else None,
        })

    def _deserialize_item(self, json_str: str) -> QueueItem:
        """Десериализовать JSON строку в QueueItem."""
        data = json.loads(json_str)
        return QueueItem(
            order_id=data["order_id"],
            retry_count=data["retry_count"],
            max_retries=data["max_retries"],
            enqueued_at=datetime.fromisoformat(data["enqueued_at"]) if data.get("enqueued_at") else None,
        )


async def create_redis_queue(redis_url: str) -> RedisQueue:
    """Создать и подключить RedisQueue."""
    queue = RedisQueue(redis_url)
    await queue.connect()
    return queue
