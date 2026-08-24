"""Independent SLA monitor for paid orders."""
import asyncio
import logging
from datetime import datetime

from src.db.session import async_session_factory
from src.services.order_attention_service import notify_order_attention
from src.services.order_service import OrderService


logger = logging.getLogger(__name__)

URGENT_ERROR_CODES = {
    "insufficient_funds",
    "no_fragment_account",
    "session_expired",
    "access_denied",
    "processing_timeout",
    "worker_exception",
}


class OrderMonitor:
    def __init__(
        self,
        interval_seconds: float = 60.0,
        warning_minutes: int = 5,
        critical_minutes: int = 60,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._warning_minutes = warning_minutes
        self._critical_minutes = critical_minutes
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="order-sla-monitor")
        logger.info("OrderMonitor started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("OrderMonitor stopped")

    async def run_once(self) -> None:
        async with async_session_factory() as session:
            orders = await OrderService(session).get_active_orders_for_monitoring()

        now = datetime.utcnow()
        for order in orders:
            age_minutes = max(0, (now - order.created_at).total_seconds()) / 60
            urgent = order.last_error_code in URGENT_ERROR_CODES

            if urgent or age_minutes >= self._warning_minutes:
                await notify_order_attention(order.id)
            if age_minutes >= self._critical_minutes:
                await notify_order_attention(order.id, critical=True)

    async def _run(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("OrderMonitor iteration failed")
            await asyncio.sleep(self._interval_seconds)
