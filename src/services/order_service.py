"""
OrderService — управление заказами с идемпотентностью.

Этот сервис:
- Создаёт заказы (idempotent по order_key)
- Проверяет дубликаты
- Управляет статусами
- Записывает результаты в БД

Используется из:
- Handlers (создание заказа)
- OrderWorker (обновление статусов)
"""
import logging
import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    BalanceLedger,
    LedgerOperation,
    Order,
    OrderStatus,
    PaymentProvider,
    ProductType,
    Transaction,
    TransactionType,
    User,
)

logger = logging.getLogger(__name__)


class OrderService:
    """
    Сервис управления заказами.

    Гарантии:
    - Идемпотентность: повторный вызов create_order с тем же order_key вернёт существующий заказ
    - Атомарность: обновление баланса + запись в ledger в одной транзакции
    - Консистентность: статус заказа всегда отражает реальное состояние
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    @staticmethod
    def generate_order_key(
        user_id: int,
        product_type: ProductType,
        quantity: int,
        recipient_username: str,
    ) -> str:
        """
        Генерировать уникальный ключ заказа.

        Формат: короткий читаемый код (8 символов).

        Этот ключ используется для идемпотентности — повторный запрос
        с теми же параметрами в короткий промежуток времени (1 минута)
        вернёт существующий заказ.

        Детерминистичен в пределах одной минуты.
        """
        import hashlib
        import time

        # Округляем время до минуты для идемпотентности
        time_bucket = int(time.time() // 60)

        # Создаём хэш от параметров + времени
        product_type_str = product_type.value if hasattr(product_type, 'value') else str(product_type)
        data = f"{user_id}:{product_type_str}:{quantity}:{recipient_username}:{time_bucket}"
        hash_bytes = hashlib.sha256(data.encode()).digest()

        # Конвертируем в читаемый формат (8 символов)
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        result = []
        for i in range(8):
            idx = hash_bytes[i] % len(alphabet)
            result.append(alphabet[idx])

        return "".join(result)

    async def create_order(
        self,
        user_id: int,
        recipient_username: str,
        product_type: ProductType,
        quantity: int,
        price_usdt: Decimal,
        payment_provider: PaymentProvider,
        order_key: str | None = None,
    ) -> tuple[Order, bool]:
        """
        Создать заказ (idempotent).

        Args:
            user_id: ID пользователя
            recipient_username: Username получателя в Telegram
            product_type: Тип продукта (stars или premium)
            quantity: Количество (звёзд или месяцев Premium)
            price_usdt: Цена в USDT
            payment_provider: Способ оплаты
            order_key: Ключ идемпотентности (генерируется если не указан)

        Returns:
            (Order, created): Заказ и флаг, был ли он создан
        """
        if order_key is None:
            order_key = self.generate_order_key(
                user_id, product_type, quantity, recipient_username
            )

        # Проверяем существующий заказ
        existing = await self._session.execute(
            select(Order).where(Order.order_key == order_key)
        )
        existing_order = existing.scalar_one_or_none()

        if existing_order:
            logger.info(f"Order already exists: {order_key} (id={existing_order.id})")
            return existing_order, False

        # Создаём новый заказ
        order = Order(
            order_key=order_key,
            user_id=user_id,
            recipient_username=recipient_username,
            product_type=product_type.value if isinstance(product_type, ProductType) else product_type,
            quantity=quantity,
            price_usdt=price_usdt,
            status=OrderStatus.PENDING.value,
            payment_provider=payment_provider.value if isinstance(payment_provider, PaymentProvider) else payment_provider,
        )

        self._session.add(order)
        await self._session.flush()

        logger.info(f"Order created: {order_key} (id={order.id})")
        return order, True

    async def get_order(self, order_id: int) -> Order | None:
        """Получить заказ по ID."""
        result = await self._session.execute(
            select(Order).where(Order.id == order_id)
        )
        return result.scalar_one_or_none()

    async def get_order_by_key(self, order_key: str) -> Order | None:
        """Получить заказ по ключу идемпотентности."""
        result = await self._session.execute(
            select(Order).where(Order.order_key == order_key)
        )
        return result.scalar_one_or_none()

    async def set_processing(self, order_id: int) -> bool:
        """
        Установить статус PROCESSING.

        Returns:
            True если статус обновлён, False если заказ уже не PENDING
        """
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PENDING.value)
            .values(status=OrderStatus.PROCESSING.value, updated_at=datetime.utcnow())
            .returning(Order.id)
        )
        updated = result.scalar_one_or_none()

        if updated:
            logger.info(f"Order {order_id} -> PROCESSING")
            return True

        logger.warning(f"Order {order_id} is not PENDING, cannot set PROCESSING")
        return False

    async def set_completed(
        self,
        order_id: int,
        fragment_tx_id: str,
        fragment_ton_spent: Decimal | None = None,
    ) -> bool:
        """
        Установить статус COMPLETED.

        Args:
            order_id: ID заказа
            fragment_tx_id: Hash транзакции Fragment
            fragment_ton_spent: Сумма TON потраченная на Fragment

        Returns:
            True если статус обновлён
        """
        values = {
            "status": OrderStatus.COMPLETED.value,
            "fragment_tx_id": fragment_tx_id,
            "completed_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        if fragment_ton_spent is not None:
            values["fragment_ton_spent"] = fragment_ton_spent

        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PROCESSING.value)
            .values(**values)
            .returning(Order.id)
        )
        updated = result.scalar_one_or_none()

        if updated:
            logger.info(f"Order {order_id} -> COMPLETED (tx={fragment_tx_id}, ton={fragment_ton_spent})")
            return True

        logger.warning(f"Order {order_id} is not PROCESSING, cannot set COMPLETED")
        return False

    async def set_failed(
        self,
        order_id: int,
        error_message: str,
    ) -> bool:
        """
        Установить статус FAILED.

        Args:
            order_id: ID заказа
            error_message: Сообщение об ошибке

        Returns:
            True если статус обновлён
        """
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PROCESSING.value)
            .values(
                status=OrderStatus.FAILED.value,
                error_message=error_message,
                updated_at=datetime.utcnow(),
            )
            .returning(Order.id)
        )
        updated = result.scalar_one_or_none()

        if updated:
            logger.info(f"Order {order_id} -> FAILED: {error_message}")
            return True

        logger.warning(f"Order {order_id} is not PROCESSING, cannot set FAILED")
        return False

    async def return_to_pending(self, order_id: int) -> bool:
        """
        Вернуть заказ в очередь (PROCESSING -> PENDING).

        Используется при retry после временных ошибок.
        """
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PROCESSING.value)
            .values(status=OrderStatus.PENDING.value, updated_at=datetime.utcnow())
            .returning(Order.id)
        )
        updated = result.scalar_one_or_none()

        if updated:
            logger.info(f"Order {order_id} -> PENDING (retry)")
            return True

        return False

    async def credit_user_balance(
        self,
        user_id: int,
        order_id: int,
        amount_stars: Decimal,
        description: str,
    ) -> None:
        """
        Зачислить Stars на баланс пользователя (атомарно).

        Создаёт Transaction + BalanceLedger + обновляет User.balance_stars.
        """
        # Получаем текущий баланс
        user_result = await self._session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = user_result.scalar_one()

        balance_before = user.balance_stars
        balance_after = balance_before + amount_stars

        # Создаём транзакцию
        transaction = Transaction(
            user_id=user_id,
            order_id=order_id,
            type=TransactionType.PURCHASE,
            amount_stars=amount_stars,
            description=description,
        )
        self._session.add(transaction)
        await self._session.flush()

        # Создаём запись в ledger
        ledger_entry = BalanceLedger(
            user_id=user_id,
            transaction_id=transaction.id,
            operation=LedgerOperation.CREDIT,
            amount_stars=amount_stars,
            amount_usdt=Decimal("0"),
            amount_premium=0,
            balance_stars_before=balance_before,
            balance_stars_after=balance_after,
            balance_usdt_before=user.balance_usdt,
            balance_usdt_after=user.balance_usdt,
            balance_premium_before=user.balance_premium_months,
            balance_premium_after=user.balance_premium_months,
            description=description,
        )
        self._session.add(ledger_entry)

        # Обновляем баланс пользователя
        user.balance_stars = balance_after

        logger.info(
            f"User {user_id} balance credited: {amount_stars} stars "
            f"({balance_before} -> {balance_after})"
        )

    async def debit_user_balance(
        self,
        user_id: int,
        order_id: int | None,
        amount_stars: Decimal,
        description: str,
    ) -> bool:
        """
        Списать Stars с баланса пользователя (атомарно).

        Returns:
            True если списание успешно, False если недостаточно средств
        """
        # Получаем текущий баланс с блокировкой
        user_result = await self._session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = user_result.scalar_one()

        if user.balance_stars < amount_stars:
            logger.warning(
                f"User {user_id} insufficient balance: "
                f"need {amount_stars}, have {user.balance_stars}"
            )
            return False

        balance_before = user.balance_stars
        balance_after = balance_before - amount_stars

        # Создаём транзакцию
        transaction = Transaction(
            user_id=user_id,
            order_id=order_id,
            type=TransactionType.WITHDRAWAL,
            amount_stars=-amount_stars,
            description=description,
        )
        self._session.add(transaction)
        await self._session.flush()

        # Создаём запись в ledger
        ledger_entry = BalanceLedger(
            user_id=user_id,
            transaction_id=transaction.id,
            operation=LedgerOperation.DEBIT,
            amount_stars=amount_stars,
            amount_usdt=Decimal("0"),
            amount_premium=0,
            balance_stars_before=balance_before,
            balance_stars_after=balance_after,
            balance_usdt_before=user.balance_usdt,
            balance_usdt_after=user.balance_usdt,
            balance_premium_before=user.balance_premium_months,
            balance_premium_after=user.balance_premium_months,
            description=description,
        )
        self._session.add(ledger_entry)

        # Обновляем баланс пользователя
        user.balance_stars = balance_after

        logger.info(
            f"User {user_id} balance debited: {amount_stars} stars "
            f"({balance_before} -> {balance_after})"
        )
        return True

    async def get_pending_orders(self, limit: int = 100) -> list[Order]:
        """Получить список заказов в статусе PENDING."""
        result = await self._session.execute(
            select(Order)
            .where(Order.status == OrderStatus.PENDING.value)
            .order_by(Order.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_stuck_orders(self, timeout_minutes: int = 10) -> list[Order]:
        """
        Получить заказы, застрявшие в статусе PROCESSING.

        Используется для восстановления после падений.
        """
        from datetime import timedelta

        threshold = datetime.utcnow() - timedelta(minutes=timeout_minutes)

        result = await self._session.execute(
            select(Order)
            .where(
                Order.status == OrderStatus.PROCESSING.value,
                Order.updated_at < threshold,
            )
            .order_by(Order.updated_at)
        )
        return list(result.scalars().all())

    # ==================== МЕТОДЫ ДЛЯ АДМИН-ПАНЕЛИ ====================

    async def get_all_orders(
        self,
        status_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Order]:
        """
        Получить все заказы с фильтрацией по статусу.

        Args:
            status_filter: Фильтр по статусу (pending, processing, completed, failed, cancelled)
            limit: Максимальное количество
            offset: Смещение для пагинации

        Returns:
            Список заказов
        """
        query = select(Order)

        if status_filter and status_filter != "all":
            query = query.where(Order.status == status_filter)

        query = query.order_by(Order.created_at.desc()).offset(offset).limit(limit)

        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def get_orders_count(self, status_filter: str | None = None) -> int:
        """Получить количество заказов с фильтрацией."""
        from sqlalchemy import func

        query = select(func.count(Order.id))

        if status_filter and status_filter != "all":
            query = query.where(Order.status == status_filter)

        result = await self._session.execute(query)
        return result.scalar() or 0

    async def search_orders(
        self,
        query: str,
        limit: int = 20,
    ) -> list[Order]:
        """
        Поиск заказов по order_key, recipient_username или user_id.

        Args:
            query: Поисковый запрос
            limit: Максимальное количество результатов

        Returns:
            Список найденных заказов
        """
        from sqlalchemy import or_

        search_query = select(Order)

        # Если query - число, ищем по user_id или order_id
        if query.isdigit():
            user_id = int(query)
            search_query = search_query.where(
                or_(
                    Order.user_id == user_id,
                    Order.id == user_id,
                )
            )
        else:
            # Ищем по order_key или recipient_username
            search_query = search_query.where(
                or_(
                    Order.order_key.ilike(f"%{query}%"),
                    Order.recipient_username.ilike(f"%{query}%"),
                )
            )

        search_query = search_query.order_by(Order.created_at.desc()).limit(limit)

        result = await self._session.execute(search_query)
        return list(result.scalars().all())

    async def get_failed_orders(self, limit: int = 100) -> list[Order]:
        """Получить список неудачных заказов."""
        result = await self._session.execute(
            select(Order)
            .where(Order.status == OrderStatus.FAILED.value)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_problem_orders(self, stuck_timeout_minutes: int = 10) -> list[Order]:
        """
        Получить проблемные заказы.

        Включает:
        - Неудачные (failed)
        - Застрявшие в processing больше timeout_minutes
        """
        from datetime import timedelta
        from sqlalchemy import or_

        threshold = datetime.utcnow() - timedelta(minutes=stuck_timeout_minutes)

        result = await self._session.execute(
            select(Order)
            .where(
                or_(
                    Order.status == OrderStatus.FAILED.value,
                    (Order.status == OrderStatus.PROCESSING.value) & (Order.updated_at < threshold),
                )
            )
            .order_by(Order.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_orders_stats(
        self,
        period: str = "all",
    ) -> dict:
        """
        Получить статистику заказов за период.

        Args:
            period: Период - "24h", "7d", "30d", "all"

        Returns:
            dict со статистикой
        """
        from datetime import timedelta
        from sqlalchemy import func

        # Определяем временной фильтр
        time_filter = None
        if period == "24h":
            time_filter = datetime.utcnow() - timedelta(hours=24)
        elif period == "7d":
            time_filter = datetime.utcnow() - timedelta(days=7)
        elif period == "30d":
            time_filter = datetime.utcnow() - timedelta(days=30)

        # Базовый запрос
        base_query = select(Order)
        if time_filter:
            base_query = base_query.where(Order.created_at >= time_filter)

        # Получаем все заказы за период
        result = await self._session.execute(base_query)
        orders = list(result.scalars().all())

        # Считаем статистику
        total = len(orders)
        pending = sum(1 for o in orders if o.status == OrderStatus.PENDING.value)
        processing = sum(1 for o in orders if o.status == OrderStatus.PROCESSING.value)
        completed = sum(1 for o in orders if o.status == OrderStatus.COMPLETED.value)
        failed = sum(1 for o in orders if o.status == OrderStatus.FAILED.value)
        cancelled = sum(1 for o in orders if o.status == OrderStatus.CANCELLED.value)
        refunded = sum(1 for o in orders if o.status == OrderStatus.REFUNDED.value)

        # Сумма по завершённым заказам
        completed_orders = [o for o in orders if o.status == OrderStatus.COMPLETED.value]
        total_revenue_usdt = sum(o.price_usdt for o in completed_orders)
        total_stars_sold = sum(o.quantity for o in completed_orders if o.product_type == "stars")
        total_ton_spent = sum(o.fragment_ton_spent or Decimal("0") for o in completed_orders)

        # Средний чек
        avg_order_usdt = total_revenue_usdt / len(completed_orders) if completed_orders else Decimal("0")

        # Успешность
        success_rate = (completed / total * 100) if total > 0 else 0

        return {
            "total": total,
            "pending": pending,
            "processing": processing,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "refunded": refunded,
            "total_revenue_usdt": total_revenue_usdt,
            "total_stars_sold": total_stars_sold,
            "total_ton_spent": total_ton_spent,
            "avg_order_usdt": avg_order_usdt,
            "success_rate": success_rate,
        }

    async def set_cancelled(self, order_id: int) -> bool:
        """
        Отменить заказ (PENDING -> CANCELLED).

        Returns:
            True если статус обновлён
        """
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PENDING.value)
            .values(status=OrderStatus.CANCELLED.value, updated_at=datetime.utcnow())
            .returning(Order.id)
        )
        updated = result.scalar_one_or_none()

        if updated:
            logger.info(f"Order {order_id} -> CANCELLED")
            return True

        logger.warning(f"Order {order_id} is not PENDING, cannot cancel")
        return False

    async def set_refunded(self, order_id: int) -> bool:
        """
        Пометить заказ как возвращённый.

        Returns:
            True если статус обновлён
        """
        result = await self._session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status.in_([OrderStatus.COMPLETED.value, OrderStatus.FAILED.value])
            )
            .values(status=OrderStatus.REFUNDED.value, updated_at=datetime.utcnow())
            .returning(Order.id)
        )
        updated = result.scalar_one_or_none()

        if updated:
            logger.info(f"Order {order_id} -> REFUNDED")
            return True

        logger.warning(f"Order {order_id} cannot be refunded")
        return False

    async def retry_order(self, order_id: int) -> bool:
        """
        Повторить неудачный заказ (FAILED -> PENDING).

        Returns:
            True если статус обновлён
        """
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.FAILED.value)
            .values(
                status=OrderStatus.PENDING.value,
                error_message=None,
                updated_at=datetime.utcnow(),
            )
            .returning(Order.id)
        )
        updated = result.scalar_one_or_none()

        if updated:
            logger.info(f"Order {order_id} -> PENDING (retry)")
            return True

        logger.warning(f"Order {order_id} is not FAILED, cannot retry")
        return False

    async def retry_all_failed(self) -> int:
        """
        Повторить все неудачные заказы.

        Returns:
            Количество обновлённых заказов
        """
        result = await self._session.execute(
            update(Order)
            .where(Order.status == OrderStatus.FAILED.value)
            .values(
                status=OrderStatus.PENDING.value,
                error_message=None,
                updated_at=datetime.utcnow(),
            )
        )
        count = result.rowcount
        logger.info(f"Retried {count} failed orders")
        return count

    async def get_user_orders(self, user_id: int, limit: int = 50) -> list[Order]:
        """Получить заказы пользователя."""
        result = await self._session.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
