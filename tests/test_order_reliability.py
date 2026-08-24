from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.queue import InMemoryQueue, QueueItem
from src.db.models import BalanceLedger, Order, OrderAttempt, Transaction, User
from src.services.order_service import OrderService
from src.services import order_attention_service as order_attention_module
from src.workers import order_worker as order_worker_module
from src.workers.order_worker import OrderWorker


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(Order.__table__.create)
        await connection.run_sync(OrderAttempt.__table__.create)
        await connection.run_sync(Transaction.__table__.create)
        await connection.run_sync(BalanceLedger.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _create_user_and_order(
    factory,
    *,
    status: str = "pending",
    payment_provider: str = "cryptobot",
    product_type: str = "stars",
    quantity: int = 100,
    price_usdt: Decimal = Decimal("1.00"),
    created_at: datetime | None = None,
) -> int:
    async with factory() as session:
        user = await session.get(User, 1001)
        if not user:
            user = User(
                id=1001,
                username="buyer",
                referral_code="buyer-1001",
                balance_stars=Decimal("0"),
                balance_usdt=Decimal("0"),
                balance_premium_months=0,
            )
            session.add(user)
        order = Order(
            order_key=f"TEST-{datetime.utcnow().timestamp()}",
            user_id=user.id,
            recipient_username="recipient",
            product_type=product_type,
            quantity=quantity,
            price_usdt=price_usdt,
            status=status,
            payment_provider=payment_provider,
            created_at=created_at or datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(order)
        await session.commit()
        return order.id


@pytest.mark.asyncio
async def test_attempt_state_survives_retries(session_factory):
    order_id = await _create_user_and_order(session_factory)
    retry_at = datetime.utcnow() + timedelta(minutes=5)

    async with session_factory() as session:
        service = OrderService(session)
        assert await service.set_processing(order_id, fragment_account_id=7)
        await session.commit()
        assert await service.return_to_pending(
            order_id,
            error_code="insufficient_funds",
            error_message="need 1 TON, have 0.1 TON",
            next_retry_at=retry_at,
        )
        await session.commit()

        order = await service.get_order(order_id)
        attempts = list((await session.execute(select(OrderAttempt))).scalars())
        assert order.status == "pending"
        assert order.attempt_count == 1
        assert order.retry_count == 0
        assert order.last_error_code == "insufficient_funds"
        assert order.next_retry_at == retry_at
        assert len(attempts) == 1
        assert attempts[0].status == "retrying"

        assert await service.set_processing(order_id, fragment_account_id=8)
        assert await service.set_completed(order_id, "tx-hash", Decimal("0.5"))
        await session.commit()
        order = await service.get_order(order_id)
        attempts = list(
            (await session.execute(select(OrderAttempt).order_by(OrderAttempt.attempt_number))).scalars()
        )
        assert order.status == "completed"
        assert order.attempt_count == 2
        assert order.error_message is None
        assert [attempt.status for attempt in attempts] == ["retrying", "completed"]


@pytest.mark.asyncio
async def test_old_pending_order_is_problematic_and_alert_claims_are_idempotent(session_factory):
    order_id = await _create_user_and_order(
        session_factory,
        created_at=datetime.utcnow() - timedelta(hours=2),
    )
    async with session_factory() as session:
        service = OrderService(session)
        problems = await service.get_problem_orders(stuck_timeout_minutes=10)
        assert [order.id for order in problems] == [order_id]
        assert await service.mark_admin_alerted(order_id)
        assert not await service.mark_admin_alerted(order_id)
        assert await service.mark_admin_alerted(order_id, critical=True)
        assert not await service.mark_admin_alerted(order_id, critical=True)
        assert await service.mark_user_delay_notified(order_id)
        assert not await service.mark_user_delay_notified(order_id)


@pytest.mark.asyncio
async def test_resource_deferral_does_not_consume_technical_retry():
    queue = InMemoryQueue()
    item = QueueItem(order_id=42, retry_count=2, max_retries=3)
    await queue.requeue(item, count_retry=False)
    restored = await queue.dequeue()
    assert restored is item
    assert restored.retry_count == 2


@pytest.mark.asyncio
async def test_delayed_in_memory_order_is_visible_to_recovery_and_clearable():
    queue = InMemoryQueue()
    item = QueueItem(order_id=43)
    await queue.requeue(item, delay=60, count_retry=False)
    assert await queue.get_queued_order_ids() == {43}
    await queue.clear()
    assert await queue.get_queued_order_ids() == set()


class RecordingQueue:
    def __init__(self):
        self.requeued = []
        self.acked = []

    async def requeue(self, item, delay=0, count_retry=True):
        if count_retry:
            item.retry_count += 1
        self.requeued.append((item, delay, count_retry))

    async def ack(self, order_id):
        self.acked.append(order_id)


@pytest.mark.asyncio
async def test_timeout_retry_returns_processing_order_to_pending(session_factory, monkeypatch):
    order_id = await _create_user_and_order(session_factory)
    async with session_factory() as session:
        service = OrderService(session)
        assert await service.set_processing(order_id, fragment_account_id=1)
        await session.commit()

    monkeypatch.setattr(order_worker_module, "async_session_factory", session_factory)
    queue = RecordingQueue()
    worker = OrderWorker(queue=queue)
    item = QueueItem(order_id=order_id)
    await worker._retry_or_fail(
        item,
        error_code="processing_timeout",
        error_message="timed out",
        delay=5,
    )

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status == "pending"
        assert order.last_error_code == "processing_timeout"
        assert order.retry_count == 1
        assert order.next_retry_at is not None
    assert len(queue.requeued) == 1
    assert queue.requeued[0][0].retry_count == 1
    assert queue.requeued[0][2] is False


@pytest.mark.asyncio
async def test_resource_shortage_keeps_technical_retry_budget(session_factory, monkeypatch):
    order_id = await _create_user_and_order(session_factory)
    async with session_factory() as session:
        service = OrderService(session)
        assert await service.set_processing(order_id, fragment_account_id=1)
        await session.commit()

    monkeypatch.setattr(order_worker_module, "async_session_factory", session_factory)
    monkeypatch.setattr(order_worker_module, "notify_order_attention", AsyncMock())
    queue = RecordingQueue()
    worker = OrderWorker(queue=queue)
    item = QueueItem(order_id=order_id)
    await worker._defer_resource_order(
        item,
        error_code="insufficient_funds",
        error_message="wallet is low",
        delay=300,
    )

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status == "pending"
        assert order.attempt_count == 1
        assert order.retry_count == 0
    assert queue.requeued[0][2] is False


@pytest.mark.asyncio
async def test_failed_balance_order_refunds_once(session_factory, monkeypatch):
    order_id = await _create_user_and_order(
        session_factory,
        status="processing",
        payment_provider="balance",
        product_type="stars",
        quantity=250,
        price_usdt=Decimal("0"),
    )
    monkeypatch.setattr(order_worker_module, "async_session_factory", session_factory)
    monkeypatch.setattr(order_worker_module.tg_logger, "log_order_failed", AsyncMock())
    monkeypatch.setattr(order_worker_module.tg_logger, "log_order_error", AsyncMock())
    queue = RecordingQueue()
    worker = OrderWorker(queue=queue)

    assert await worker._fail_order(order_id, "access_denied", "denied")
    assert not await worker._fail_order(order_id, "access_denied", "denied")

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        user = await session.get(User, 1001)
        assert order.status == "failed"
        assert user.balance_stars == Decimal("250")
    assert queue.acked == []  # Only RedisQueue requires an explicit ack.


@pytest.mark.asyncio
async def test_exhausted_retry_budget_fails_instead_of_requeueing(session_factory, monkeypatch):
    order_id = await _create_user_and_order(
        session_factory,
        status="processing",
    )
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        order.retry_count = 3
        await session.commit()

    monkeypatch.setattr(order_worker_module, "async_session_factory", session_factory)
    monkeypatch.setattr(order_worker_module.tg_logger, "log_order_failed", AsyncMock())
    monkeypatch.setattr(order_worker_module.tg_logger, "log_order_error", AsyncMock())
    queue = RecordingQueue()
    worker = OrderWorker(queue=queue)
    await worker._retry_or_fail(
        QueueItem(order_id=order_id, retry_count=3, max_retries=3),
        error_code="temporary_error",
        error_message="retry budget exhausted",
        delay=5,
    )

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status == "failed"
        assert order.retry_count == 3
    assert queue.requeued == []


@pytest.mark.asyncio
async def test_bulk_retry_returns_only_orders_it_reactivated(session_factory):
    safe_id = await _create_user_and_order(session_factory, status="failed")
    unsafe_id = await _create_user_and_order(session_factory, status="failed")
    async with session_factory() as session:
        safe = await session.get(Order, safe_id)
        unsafe = await session.get(Order, unsafe_id)
        safe.last_error_code = "transaction_failed"
        safe.retry_count = 3
        unsafe.last_error_code = "access_denied"
        await session.commit()

        retried_ids = await OrderService(session).retry_all_failed()
        await session.commit()

        assert retried_ids == [safe_id]
        await session.refresh(safe)
        await session.refresh(unsafe)
        assert safe.status == "pending"
        assert safe.retry_count == 0
        assert unsafe.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product_type", "quantity", "price_usdt", "balance_field", "expected"),
    [
        ("stars", 100, Decimal("1.25"), "balance_usdt", Decimal("1.25")),
        ("stars", 100, Decimal("0"), "balance_stars", Decimal("100")),
        ("premium", 3, Decimal("0"), "balance_premium_months", 3),
    ],
)
async def test_completed_balance_refund_credits_original_asset_once(
    session_factory,
    product_type,
    quantity,
    price_usdt,
    balance_field,
    expected,
):
    order_id = await _create_user_and_order(
        session_factory,
        status="completed",
        payment_provider="balance",
        product_type=product_type,
        quantity=quantity,
        price_usdt=price_usdt,
    )

    async with session_factory() as session:
        service = OrderService(session)
        assert await service.refund_balance_order(order_id) == "balance_credited"
        await session.commit()
        assert await service.refund_balance_order(order_id) is None
        await session.commit()

        order = await session.get(Order, order_id)
        user = await session.get(User, 1001)
        transactions = list(
            (
                await session.execute(
                    select(Transaction).where(Transaction.order_id == order_id)
                )
            ).scalars()
        )
        ledger_entries = list((await session.execute(select(BalanceLedger))).scalars())

        assert order.status == "refunded"
        assert getattr(user, balance_field) == expected
        assert len(transactions) == 1
        assert transactions[0].type == "refund"
        assert transactions[0].external_id == f"order_refund:{order_id}"
        assert len(ledger_entries) == 1
        assert ledger_entries[0].operation == "credit"


@pytest.mark.asyncio
async def test_failed_balance_refund_does_not_credit_twice(session_factory):
    order_id = await _create_user_and_order(
        session_factory,
        status="failed",
        payment_provider="balance",
        price_usdt=Decimal("1.00"),
    )
    async with session_factory() as session:
        user = await session.get(User, 1001)
        user.balance_usdt = Decimal("1.00")  # Simulate the worker's automatic refund.
        await session.commit()

        service = OrderService(session)
        assert await service.refund_balance_order(order_id) == "already_credited"
        await session.commit()

        order = await session.get(Order, order_id)
        await session.refresh(user)
        assert order.status == "refunded"
        assert user.balance_usdt == Decimal("1.00")
        assert not list((await session.execute(select(Transaction))).scalars())


@pytest.mark.asyncio
async def test_admin_retry_debits_failed_balance_order_once(session_factory):
    order_id = await _create_user_and_order(
        session_factory,
        status="failed",
        payment_provider="balance",
        price_usdt=Decimal("1.00"),
    )
    async with session_factory() as session:
        user = await session.get(User, 1001)
        user.balance_usdt = Decimal("1.00")
        await session.commit()

        service = OrderService(session)
        assert await service.retry_order(order_id)
        await session.commit()
        assert not await service.retry_order(order_id)
        await session.commit()

        order = await session.get(Order, order_id)
        await session.refresh(user)
        assert order.status == "pending"
        assert user.balance_usdt == Decimal("0.00")


@pytest.mark.asyncio
async def test_failed_attention_delivery_is_released_for_retry(session_factory, monkeypatch):
    order_id = await _create_user_and_order(session_factory)
    monkeypatch.setattr(order_attention_module, "async_session_factory", session_factory)
    admin_sender = AsyncMock(return_value=False)
    user_sender = AsyncMock(return_value=False)
    monkeypatch.setattr(order_attention_module.tg_logger, "log_order_attention", admin_sender)
    monkeypatch.setattr(order_attention_module, "notify_order_delayed", user_sender)

    assert not await order_attention_module.notify_order_attention(order_id)
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.admin_alerted_at is None
        assert order.user_delay_notified_at is None

    admin_sender.return_value = True
    user_sender.return_value = True
    assert await order_attention_module.notify_order_attention(order_id)
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.admin_alerted_at is not None
        assert order.user_delay_notified_at is not None
