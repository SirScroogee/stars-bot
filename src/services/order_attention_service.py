"""Deduplicated administrator and user notifications for delayed orders."""
import logging
from datetime import datetime

from sqlalchemy import select

from src.db.models import Order, User
from src.db.session import async_session_factory
from src.services.order_notification_service import notify_order_delayed
from src.services.order_service import OrderService
from src.services.telegram_logger import tg_logger


logger = logging.getLogger(__name__)


async def notify_order_attention(order_id: int, *, critical: bool = False) -> bool:
    """Send each warning level at most once for an active order."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Order, User)
            .join(User, User.id == Order.user_id)
            .where(Order.id == order_id)
        )
        row = result.one_or_none()
        if not row:
            return False

        order, user = row
        if order.status not in {"pending", "processing"}:
            return False

        service = OrderService(session)
        admin_claimed = await service.mark_admin_alerted(order.id, critical=critical)
        user_claimed = False
        if not critical:
            user_claimed = await service.mark_user_delay_notified(order.id)
        await session.commit()

        if not admin_claimed and not user_claimed:
            return False

        age_minutes = int(max(0, (datetime.utcnow() - order.created_at).total_seconds()) // 60)
        reason_code = order.last_error_code or "delayed"
        reason = order.error_message or "Заказ выполняется дольше ожидаемого"

        admin_delivered = True
        if admin_claimed:
            admin_delivered = await tg_logger.log_order_attention(
                order_id=order.id,
                user_id=order.user_id,
                username=user.username,
                reason_code=reason_code,
                reason=reason,
                age_minutes=age_minutes,
                critical=critical,
            )
        user_delivered = True
        if user_claimed:
            user_delivered = await notify_order_delayed(order)

        if (admin_claimed and not admin_delivered) or (user_claimed and not user_delivered):
            async with async_session_factory() as retry_session:
                retry_service = OrderService(retry_session)
                if admin_claimed and not admin_delivered:
                    await retry_service.release_admin_alert(order.id, critical=critical)
                if user_claimed and not user_delivered:
                    await retry_service.release_user_delay_notification(order.id)
                await retry_session.commit()

        return (admin_claimed and admin_delivered) or (user_claimed and user_delivered)
