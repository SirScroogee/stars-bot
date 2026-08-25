"""Reliable, auditable delivery of Telegram Gifts initiated by administrators."""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramServerError,
)
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    AdminGift,
    AdminGiftPayment,
    AdminGiftPaymentStatus,
    AdminGiftStatus,
)


logger = logging.getLogger(__name__)

PRE_CHECKOUT_RESERVATION_MINUTES = 10


@dataclass(slots=True)
class GiftSendOutcome:
    attempt: AdminGift
    status: str
    performed: bool
    error: str | None = None


class GiftInvoiceAlreadyPaidError(ValueError):
    """A second Telegram charge arrived for an invoice that was already paid."""


class AdminGiftService:
    """Persist and send one gift exactly once for a generated operation key."""

    def __init__(self, session: AsyncSession, bot: Bot):
        self._session = session
        self._bot = bot

    async def create_or_get_attempt(
        self,
        *,
        operation_key: str,
        admin_id: int,
        admin_username: str | None,
        recipient_id: int,
        recipient_username: str | None,
        recipient_was_banned: bool,
        gift_id: str,
        gift_emoji: str | None,
        gift_star_count: int,
        gift_text: str,
        bot_balance_before: int | None,
        controller_chat_id: int | None = None,
        controller_message_id: int | None = None,
    ) -> tuple[AdminGift, bool]:
        """Create an audit row, returning the existing row on a duplicate callback."""
        attempt = AdminGift(
            operation_key=operation_key,
            admin_id=admin_id,
            admin_username_snapshot=admin_username,
            recipient_id=recipient_id,
            recipient_username_snapshot=recipient_username,
            recipient_was_banned=recipient_was_banned,
            gift_id=gift_id,
            gift_emoji=gift_emoji,
            gift_star_count=gift_star_count,
            pay_for_upgrade=False,
            gift_text=gift_text,
            bot_balance_before=bot_balance_before,
            controller_chat_id=controller_chat_id,
            controller_message_id=controller_message_id,
            status=AdminGiftStatus.PENDING.value,
        )
        self._session.add(attempt)
        try:
            await self._session.commit()
            return attempt, True
        except IntegrityError:
            await self._session.rollback()

        result = await self._session.execute(
            select(AdminGift).where(AdminGift.operation_key == operation_key)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            raise RuntimeError("Gift operation key conflict could not be resolved")
        return existing, False

    async def get_attempt(self, attempt_id: int) -> AdminGift | None:
        result = await self._session.execute(
            select(AdminGift).where(AdminGift.id == attempt_id)
        )
        return result.scalar_one_or_none()

    async def create_payment_request(
        self,
        *,
        attempt_id: int,
        admin_id: int,
        requested_stars: int,
    ) -> tuple[AdminGiftPayment, bool]:
        """Atomically move a pending Gift to payment and create one active invoice."""
        if requested_stars <= 0:
            raise ValueError("requested_stars must be positive")

        now = datetime.utcnow()
        claim = await self._session.execute(
            update(AdminGift)
            .where(
                AdminGift.id == attempt_id,
                AdminGift.admin_id == admin_id,
                AdminGift.status == AdminGiftStatus.PENDING.value,
            )
            .values(
                status=AdminGiftStatus.AWAITING_PAYMENT.value,
                updated_at=now,
            )
            .returning(AdminGift.id)
        )
        if claim.scalar_one_or_none() is not None:
            payment = AdminGiftPayment(
                gift_attempt_id=attempt_id,
                invoice_payload=f"agift:{secrets.token_hex(24)}",
                requested_stars=requested_stars,
                status=AdminGiftPaymentStatus.INVOICE_PENDING.value,
            )
            self._session.add(payment)
            await self._session.commit()
            return payment, True

        await self._session.commit()
        result = await self._session.execute(
            select(AdminGiftPayment)
            .where(
                AdminGiftPayment.gift_attempt_id == attempt_id,
                AdminGiftPayment.status.in_(
                    [
                        AdminGiftPaymentStatus.INVOICE_PENDING.value,
                        AdminGiftPaymentStatus.INVOICE_SENT.value,
                        AdminGiftPaymentStatus.PRECHECKOUT.value,
                    ]
                ),
            )
            .order_by(AdminGiftPayment.id.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            raise RuntimeError("Gift is awaiting payment but has no active invoice")
        return existing, False

    async def mark_invoice_sent(
        self, payment_id: int, invoice_message_id: int
    ) -> AdminGiftPayment:
        now = datetime.utcnow()
        await self._session.execute(
            update(AdminGiftPayment)
            .where(
                AdminGiftPayment.id == payment_id,
                AdminGiftPayment.status
                == AdminGiftPaymentStatus.INVOICE_PENDING.value,
            )
            .values(
                status=AdminGiftPaymentStatus.INVOICE_SENT.value,
                invoice_message_id=invoice_message_id,
                error_message=None,
                updated_at=now,
            )
        )
        await self._session.commit()
        result = await self._session.execute(
            select(AdminGiftPayment).where(AdminGiftPayment.id == payment_id)
        )
        return result.scalar_one()

    async def mark_invoice_failed(
        self, payment_id: int, error: BaseException
    ) -> None:
        now = datetime.utcnow()
        payment_result = await self._session.execute(
            update(AdminGiftPayment)
            .where(
                AdminGiftPayment.id == payment_id,
                AdminGiftPayment.status.in_(
                    [
                        AdminGiftPaymentStatus.INVOICE_PENDING.value,
                        AdminGiftPaymentStatus.INVOICE_SENT.value,
                    ]
                ),
            )
            .values(
                status=AdminGiftPaymentStatus.FAILED.value,
                error_message=str(error),
                updated_at=now,
            )
            .returning(AdminGiftPayment.gift_attempt_id)
        )
        attempt_id = payment_result.scalar_one_or_none()
        if attempt_id is not None:
            await self._session.execute(
                update(AdminGift)
                .where(
                    AdminGift.id == attempt_id,
                    AdminGift.status == AdminGiftStatus.AWAITING_PAYMENT.value,
                )
                .values(
                    status=AdminGiftStatus.PENDING.value,
                    error_type=None,
                    error_message=None,
                    updated_at=now,
                )
            )
        await self._session.commit()

    async def refresh_attempt_gift(
        self,
        attempt_id: int,
        *,
        gift_star_count: int,
        gift_emoji: str | None,
        bot_balance_before: int,
    ) -> AdminGift:
        await self._session.execute(
            update(AdminGift)
            .where(
                AdminGift.id == attempt_id,
                AdminGift.status == AdminGiftStatus.PENDING.value,
            )
            .values(
                gift_star_count=gift_star_count,
                gift_emoji=gift_emoji,
                bot_balance_before=bot_balance_before,
                updated_at=datetime.utcnow(),
            )
        )
        await self._session.commit()
        attempt = await self.get_attempt(attempt_id)
        if attempt is None:
            raise LookupError("Gift attempt not found")
        return attempt

    async def fail_pending_attempt(
        self, attempt_id: int, error: BaseException
    ) -> AdminGift:
        await self._session.execute(
            update(AdminGift)
            .where(
                AdminGift.id == attempt_id,
                AdminGift.status.in_(
                    [
                        AdminGiftStatus.PENDING.value,
                        AdminGiftStatus.AWAITING_PAYMENT.value,
                    ]
                ),
            )
            .values(
                status=AdminGiftStatus.FAILED.value,
                error_type=type(error).__name__,
                error_message=str(error),
                updated_at=datetime.utcnow(),
            )
        )
        await self._session.commit()
        attempt = await self.get_attempt(attempt_id)
        if attempt is None:
            raise LookupError("Gift attempt not found")
        return attempt

    async def get_payment_context(
        self, invoice_payload: str
    ) -> tuple[AdminGiftPayment | None, AdminGift | None]:
        result = await self._session.execute(
            select(AdminGiftPayment).where(
                AdminGiftPayment.invoice_payload == invoice_payload
            )
        )
        payment = result.scalar_one_or_none()
        if payment is None:
            return None, None
        return payment, await self.get_attempt(payment.gift_attempt_id)

    async def get_active_payment(self, attempt_id: int) -> AdminGiftPayment | None:
        result = await self._session.execute(
            select(AdminGiftPayment)
            .where(
                AdminGiftPayment.gift_attempt_id == attempt_id,
                AdminGiftPayment.status.in_(
                    [
                        AdminGiftPaymentStatus.INVOICE_PENDING.value,
                        AdminGiftPaymentStatus.INVOICE_SENT.value,
                        AdminGiftPaymentStatus.PRECHECKOUT.value,
                    ]
                ),
            )
            .order_by(AdminGiftPayment.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def claim_pre_checkout(
        self,
        payment_id: int,
        payer_id: int,
        query_id: str,
    ) -> bool:
        """Reserve a shareable invoice for the first payer at final checkout.

        Telegram may expose the same forwarded invoice to many people. The atomic
        update ensures that only one of their pre-checkout queries is accepted.
        A stale reservation may be reclaimed because Telegram cancels unanswered
        pre-checkout queries after ten seconds; the wider window also covers short
        delivery delays and bot restarts.
        """
        now = datetime.utcnow()
        stale_before = now - timedelta(minutes=PRE_CHECKOUT_RESERVATION_MINUTES)
        result = await self._session.execute(
            update(AdminGiftPayment)
            .where(
                AdminGiftPayment.id == payment_id,
                or_(
                    AdminGiftPayment.status.in_(
                        [
                            AdminGiftPaymentStatus.INVOICE_PENDING.value,
                            AdminGiftPaymentStatus.INVOICE_SENT.value,
                        ]
                    ),
                    and_(
                        AdminGiftPayment.status
                        == AdminGiftPaymentStatus.PRECHECKOUT.value,
                        or_(
                            AdminGiftPayment.pre_checkout_query_id == query_id,
                            AdminGiftPayment.pre_checkout_at.is_(None),
                            AdminGiftPayment.pre_checkout_at < stale_before,
                        ),
                    ),
                ),
            )
            .values(
                status=AdminGiftPaymentStatus.PRECHECKOUT.value,
                pre_checkout_payer_id=payer_id,
                pre_checkout_query_id=query_id,
                pre_checkout_at=now,
                updated_at=now,
            )
            .returning(AdminGiftPayment.id)
        )
        claimed = result.scalar_one_or_none() is not None
        await self._session.commit()
        return claimed

    async def record_successful_payment(
        self,
        *,
        invoice_payload: str,
        payer_id: int,
        currency: str,
        total_amount: int,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str,
    ) -> tuple[AdminGiftPayment, AdminGift, bool]:
        """Idempotently record XTR payment and return the Gift to pending delivery."""
        payment, attempt = await self.get_payment_context(invoice_payload)
        if payment is None or attempt is None:
            raise ValueError("Unknown admin Gift invoice")
        if currency != "XTR" or total_amount != payment.requested_stars:
            raise ValueError("Invoice currency or amount does not match")
        if payment.status == AdminGiftPaymentStatus.PAID.value:
            if payment.telegram_payment_charge_id != telegram_payment_charge_id:
                raise GiftInvoiceAlreadyPaidError(
                    "Invoice already has a different payment charge"
                )
            if payment.payer_id != payer_id:
                raise ValueError("Invoice already belongs to a different payer")
            return payment, attempt, False
        if payment.status != AdminGiftPaymentStatus.PRECHECKOUT.value:
            raise ValueError("Invoice is no longer payable")

        now = datetime.utcnow()
        result = await self._session.execute(
            update(AdminGiftPayment)
            .where(
                AdminGiftPayment.id == payment.id,
                AdminGiftPayment.status == AdminGiftPaymentStatus.PRECHECKOUT.value,
            )
            .values(
                status=AdminGiftPaymentStatus.PAID.value,
                payer_id=payer_id,
                telegram_payment_charge_id=telegram_payment_charge_id,
                provider_payment_charge_id=provider_payment_charge_id,
                paid_stars=total_amount,
                paid_at=now,
                updated_at=now,
                error_message=None,
            )
            .returning(AdminGiftPayment.id)
        )
        claimed = result.scalar_one_or_none() is not None
        if claimed:
            await self._session.execute(
                update(AdminGift)
                .where(
                    AdminGift.id == attempt.id,
                    AdminGift.status == AdminGiftStatus.AWAITING_PAYMENT.value,
                )
                .values(
                    status=AdminGiftStatus.PENDING.value,
                    updated_at=now,
                )
            )
        await self._session.commit()

        payment, attempt = await self.get_payment_context(invoice_payload)
        if payment is None or attempt is None:
            raise RuntimeError("Paid Gift invoice disappeared")
        if not claimed:
            if (
                payment.status == AdminGiftPaymentStatus.PAID.value
                and payment.telegram_payment_charge_id == telegram_payment_charge_id
                and payment.payer_id == payer_id
            ):
                return payment, attempt, False
            if payment.status == AdminGiftPaymentStatus.PAID.value:
                raise GiftInvoiceAlreadyPaidError(
                    "A concurrent charge already paid this invoice"
                )
            raise ValueError("Invoice changed state while recording its payment")
        return payment, attempt, claimed

    async def mark_payment_refunded(
        self, payment_id: int, error: BaseException | None = None
    ) -> None:
        await self._session.execute(
            update(AdminGiftPayment)
            .where(
                AdminGiftPayment.id == payment_id,
                AdminGiftPayment.status.in_(
                    [
                        AdminGiftPaymentStatus.PAID.value,
                        AdminGiftPaymentStatus.REFUND_FAILED.value,
                    ]
                ),
            )
            .values(
                status=(
                    AdminGiftPaymentStatus.REFUNDED.value
                    if error is None
                    else AdminGiftPaymentStatus.REFUND_FAILED.value
                ),
                refunded_at=datetime.utcnow() if error is None else None,
                error_message=str(error) if error else None,
                updated_at=datetime.utcnow(),
            )
        )
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def get_paid_payments(self, attempt_id: int) -> list[AdminGiftPayment]:
        result = await self._session.execute(
            select(AdminGiftPayment)
            .where(
                AdminGiftPayment.gift_attempt_id == attempt_id,
                AdminGiftPayment.status == AdminGiftPaymentStatus.PAID.value,
            )
            .order_by(AdminGiftPayment.id)
        )
        return list(result.scalars().all())

    async def get_refundable_payments(
        self, attempt_id: int
    ) -> list[AdminGiftPayment]:
        """Return payments that still need a confirmed successful refund."""
        result = await self._session.execute(
            select(AdminGiftPayment)
            .where(
                AdminGiftPayment.gift_attempt_id == attempt_id,
                AdminGiftPayment.status.in_(
                    [
                        AdminGiftPaymentStatus.PAID.value,
                        AdminGiftPaymentStatus.REFUND_FAILED.value,
                    ]
                ),
            )
            .order_by(AdminGiftPayment.id)
        )
        return list(result.scalars().all())

    async def _get_attempt(self, attempt_id: int, admin_id: int) -> AdminGift | None:
        result = await self._session.execute(
            select(AdminGift).where(
                AdminGift.id == attempt_id,
                AdminGift.admin_id == admin_id,
            )
        )
        return result.scalar_one_or_none()

    async def _claim_attempt(
        self, attempt_id: int, admin_id: int
    ) -> tuple[AdminGift | None, bool]:
        now = datetime.utcnow()
        result = await self._session.execute(
            update(AdminGift)
            .where(
                AdminGift.id == attempt_id,
                AdminGift.admin_id == admin_id,
                AdminGift.status == AdminGiftStatus.PENDING.value,
            )
            .values(
                status=AdminGiftStatus.SENDING.value,
                updated_at=now,
            )
            .returning(AdminGift.id)
        )
        claimed = result.scalar_one_or_none() is not None
        await self._session.commit()
        return await self._get_attempt(attempt_id, admin_id), claimed

    async def _finalize(
        self,
        attempt_id: int,
        *,
        status: AdminGiftStatus,
        error: BaseException | None = None,
    ) -> AdminGift:
        now = datetime.utcnow()
        values = {
            "status": status.value,
            "updated_at": now,
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
        }
        if status == AdminGiftStatus.SUCCEEDED:
            values["sent_at"] = now

        await self._session.execute(
            update(AdminGift)
            .where(
                AdminGift.id == attempt_id,
                AdminGift.status == AdminGiftStatus.SENDING.value,
            )
            .values(**values)
        )
        await self._session.commit()
        result = await self._session.execute(
            select(AdminGift).where(AdminGift.id == attempt_id)
        )
        attempt = result.scalar_one()
        return attempt

    async def send_attempt(self, attempt_id: int, admin_id: int) -> GiftSendOutcome:
        """
        Claim and send an attempt once.

        Network/server/unknown exceptions are recorded as ``unknown`` and are never
        retried automatically because Telegram's sendGift method has no idempotency key.
        """
        attempt, claimed = await self._claim_attempt(attempt_id, admin_id)
        if attempt is None:
            raise LookupError("Gift attempt not found")
        if not claimed:
            return GiftSendOutcome(
                attempt=attempt,
                status=attempt.status,
                performed=False,
                error=attempt.error_message,
            )

        try:
            sent = await self._bot.send_gift(
                user_id=attempt.recipient_id,
                gift_id=attempt.gift_id,
                pay_for_upgrade=False,
                text=attempt.gift_text or None,
                request_timeout=30,
            )
            if not sent:
                error = RuntimeError("Telegram returned False from sendGift")
                finalized = await self._finalize(
                    attempt.id,
                    status=AdminGiftStatus.FAILED,
                    error=error,
                )
                return GiftSendOutcome(
                    attempt=finalized,
                    status=finalized.status,
                    performed=True,
                    error=str(error),
                )
        except (TelegramNetworkError, TelegramServerError) as exc:
            logger.warning("Ambiguous Telegram Gift result for attempt %s: %s", attempt.id, exc)
            finalized = await self._finalize(
                attempt.id,
                status=AdminGiftStatus.UNKNOWN,
                error=exc,
            )
            return GiftSendOutcome(
                attempt=finalized,
                status=finalized.status,
                performed=True,
                error=str(exc),
            )
        except TelegramAPIError as exc:
            logger.info("Telegram rejected Gift attempt %s: %s", attempt.id, exc)
            finalized = await self._finalize(
                attempt.id,
                status=AdminGiftStatus.FAILED,
                error=exc,
            )
            return GiftSendOutcome(
                attempt=finalized,
                status=finalized.status,
                performed=True,
                error=str(exc),
            )
        except Exception as exc:
            logger.exception("Unknown Gift delivery error for attempt %s", attempt.id)
            finalized = await self._finalize(
                attempt.id,
                status=AdminGiftStatus.UNKNOWN,
                error=exc,
            )
            return GiftSendOutcome(
                attempt=finalized,
                status=finalized.status,
                performed=True,
                error=str(exc),
            )

        finalized = await self._finalize(
            attempt.id,
            status=AdminGiftStatus.SUCCEEDED,
        )
        return GiftSendOutcome(
            attempt=finalized,
            status=finalized.status,
            performed=True,
        )
