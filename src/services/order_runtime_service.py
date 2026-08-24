"""Runtime helpers shared by order-creation handlers."""
import logging

from src.core.queue import get_order_queue
from src.services.telegram_logger import tg_logger
from src.workers.order_worker import get_order_worker


logger = logging.getLogger(__name__)


async def enqueue_order_reliably(order_id: int, max_retries: int = 3) -> None:
    """Queue an order even while the auto-configured worker is unavailable."""
    worker = get_order_worker()
    if worker and worker.is_running:
        try:
            await worker.enqueue_order(order_id, max_retries=max_retries)
            return
        except Exception:
            logger.exception("Worker enqueue failed for order %s; using queue directly", order_id)

    try:
        await get_order_queue().enqueue(order_id, max_retries=max_retries)
        logger.warning("Order %s queued directly because OrderWorker is unavailable", order_id)
    except Exception:
        # The DB remains authoritative and startup recovery will enqueue it.
        logger.exception("Could not enqueue order %s; it remains pending in the database", order_id)


async def log_created_order(
    order,
    username: str | None,
    *,
    paid_amount_usdt=None,
    paid_amount_rub=None,
    provider_amount: str | None = None,
) -> None:
    try:
        await tg_logger.log_order_created(
            order_id=order.id,
            user_id=order.user_id,
            username=username,
            product_type=order.product_type,
            quantity=order.quantity,
            price_usdt=order.price_usdt,
            recipient=order.recipient_username,
        )
    except Exception:
        logger.exception("Could not send creation log for order %s", order.id)

    if order.price_usdt > 0:
        try:
            await tg_logger.log_payment_completed(
                order_id=order.id,
                user_id=order.user_id,
                username=username,
                amount_usdt=(
                    paid_amount_usdt
                    if paid_amount_usdt is not None
                    else order.price_usdt
                ),
                amount_rub=paid_amount_rub,
                provider=order.payment_provider,
                product_type=order.product_type,
                quantity=order.quantity,
                recipient=order.recipient_username,
                provider_amount=provider_amount,
            )
        except Exception:
            logger.exception("Could not send payment log for order %s", order.id)
