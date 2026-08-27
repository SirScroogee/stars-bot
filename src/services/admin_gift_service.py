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
TELEGRAM_GIFT_TEXT_LIMIT = 128
TELEGRAM_USER_ID_MAX = 0xFFFFFFFFFF


@dataclass(slots=True)
class GiftSendOutcome:
    attempt: AdminGift
    status: str
    performed: bool
    error: str | None = None


@dataclass(slots=True)
class GiftCancellationOutcome:
    attempt: AdminGift
    payment: AdminGiftPayment | None
    cancelled: bool
    reason: str | None = None


class GiftPaymentRefundRequiredError(ValueError):
    """A completed Telegram charge cannot be applied to this Gift invoice."""


class GiftInvoiceAlreadyPaidError(GiftPaymentRefundRequiredError):
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
        gift_source: str = "live",
        gift_title: str | None = None,
        archived_gift_id: int | None = None,
        controller_chat_id: int | None = None,
        controller_message_id: int | None = None,
    ) -> tuple[AdminGift, bool]:
        """Create an audit row, returning the existing row on a duplicate callback."""
        if admin_id <= 0:
            raise ValueError("admin_id must be positive")
        if recipient_id <= 0 or recipient_id > TELEGRAM_USER_ID_MAX:
            raise ValueError("recipient_id is outside the Telegram user ID range")
        if not gift_id:
            raise ValueError("gift_id is required")
        if gift_star_count <= 0:
            raise ValueError("gift_star_count must be positive")
        if gift_source not in {"live", "archive"}:
            raise ValueError("gift_source must be live or archive")
        if len(gift_text.encode("utf-16-le")) // 2 > TELEGRAM_GIFT_TEXT_LIMIT:
            raise ValueError("gift_text exceeds the Telegram limit")

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
            gift_source=gift_source,
            gift_title_snapshot=gift_title,
            archived_gift_id=archived_gift_id,
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
        expected_snapshot = (
            admin_id,
            recipient_id,
            gift_id,
            gift_star_count,
            gift_text,
            gift_source,
            archived_gift_id,
        )
        existing_snapshot = (
            existing.admin_id,
            existing.recipient_id,
            existing.gift_id,
            existing.gift_star_count,
            existing.gift_text,
            existing.gift_source,
            existing.archived_gift_id,
        )
        if existing_snapshot != expected_snapshot:
            raise RuntimeError("Gift operation key was reused with different data")
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
                AdminGiftPayment.gift_attempt_id.in_(
                    select(AdminGift.id).where(
                        AdminGift.status
                        == AdminGiftStatus.AWAITING_PAYMENT.value
                    )
                ),
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
            raise GiftPaymentRefundRequiredError("Unknown admin Gift invoice")
        if currency != "XTR" or total_amount != payment.requested_stars:
            raise GiftPaymentRefundRequiredError(
                "Invoice currency or amount does not match"
            )
        recorded_statuses = {
            AdminGiftPaymentStatus.PAID.value,
            AdminGiftPaymentStatus.REFUNDED.value,
            AdminGiftPaymentStatus.REFUND_FAILED.value,
        }
        if payment.status in recorded_statuses:
            if payment.telegram_payment_charge_id != telegram_payment_charge_id:
                raise GiftInvoiceAlreadyPaidError(
                    "Invoice already has a different payment charge"
                )
            if payment.payer_id != payer_id:
                raise GiftInvoiceAlreadyPaidError(
                    "Invoice already belongs to a different payer"
                )
            return payment, attempt, False
        if attempt.status != AdminGiftStatus.AWAITING_PAYMENT.value:
            raise GiftPaymentRefundRequiredError("Gift invoice is no longer active")
        if payment.status == AdminGiftPaymentStatus.CANCELLED.value:
            raise GiftPaymentRefundRequiredError(
                "A cancelled Gift invoice produced a late successful payment"
            )
        if payment.status != AdminGiftPaymentStatus.PRECHECKOUT.value:
            raise GiftPaymentRefundRequiredError("Invoice is no longer payable")
        if payment.pre_checkout_payer_id != payer_id:
            raise GiftPaymentRefundRequiredError(
                "Successful payment payer does not match the pre-checkout reservation"
            )

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
            if payment.status in recorded_statuses:
                if (
                    payment.telegram_payment_charge_id == telegram_payment_charge_id
                    and payment.payer_id == payer_id
                ):
                    return payment, attempt, False
                raise GiftInvoiceAlreadyPaidError(
                    "A concurrent charge already paid this invoice"
                )
            if payment.status == AdminGiftPaymentStatus.CANCELLED.value:
                raise GiftPaymentRefundRequiredError(
                    "Gift invoice was cancelled while its payment arrived"
                )
            raise ValueError("Invoice changed state while recording its payment")
        return payment, attempt, claimed

    async def cancel_unpaid_attempt(
        self,
        attempt_id: int,
        admin_id: int,
    ) -> GiftCancellationOutcome:
        """Cancel an unpaid operation while serializing against pre-checkout."""
        attempt_result = await self._session.execute(
            select(AdminGift)
            .where(
                AdminGift.id == attempt_id,
                AdminGift.admin_id == admin_id,
            )
            .with_for_update()
        )
        attempt = attempt_result.scalar_one_or_none()
        if attempt is None:
            raise LookupError("Gift attempt not found")
        if attempt.status == AdminGiftStatus.CANCELLED.value:
            return GiftCancellationOutcome(attempt, None, True)
        if attempt.status not in {
            AdminGiftStatus.PENDING.value,
            AdminGiftStatus.AWAITING_PAYMENT.value,
        }:
            return GiftCancellationOutcome(
                attempt,
                None,
                False,
                "Операция уже оплачена или завершена.",
            )

        payment_result = await self._session.execute(
            select(AdminGiftPayment)
            .where(
                AdminGiftPayment.gift_attempt_id == attempt_id,
                AdminGiftPayment.status.in_(
                    [
                        AdminGiftPaymentStatus.INVOICE_PENDING.value,
                        AdminGiftPaymentStatus.INVOICE_SENT.value,
                        AdminGiftPaymentStatus.PRECHECKOUT.value,
                        AdminGiftPaymentStatus.PAID.value,
                        AdminGiftPaymentStatus.REFUND_FAILED.value,
                    ]
                ),
            )
            .order_by(AdminGiftPayment.id.desc())
            .with_for_update()
        )
        payments = list(payment_result.scalars().all())
        now = datetime.utcnow()
        stale_pre_checkout_before = now - timedelta(
            minutes=PRE_CHECKOUT_RESERVATION_MINUTES
        )
        if any(
            payment.status
            in {
                AdminGiftPaymentStatus.PAID.value,
                AdminGiftPaymentStatus.REFUND_FAILED.value,
            }
            or (
                payment.status == AdminGiftPaymentStatus.PRECHECKOUT.value
                and (
                    payment.pre_checkout_at is None
                    or payment.pre_checkout_at >= stale_pre_checkout_before
                )
            )
            for payment in payments
        ):
            await self._session.commit()
            return GiftCancellationOutcome(
                attempt,
                payments[0] if payments else None,
                False,
                "Оплата уже началась. Дождитесь результата и проверьте операцию.",
            )

        cancellable = [
            payment
            for payment in payments
            if payment.status
            in {
                AdminGiftPaymentStatus.INVOICE_PENDING.value,
                AdminGiftPaymentStatus.INVOICE_SENT.value,
                AdminGiftPaymentStatus.PRECHECKOUT.value,
            }
        ]
        for payment in cancellable:
            payment.status = AdminGiftPaymentStatus.CANCELLED.value
            payment.error_message = "Cancelled by administrator"
            payment.updated_at = now
        attempt.status = AdminGiftStatus.CANCELLED.value
        attempt.error_type = None
        attempt.error_message = None
        attempt.updated_at = now
        await self._session.commit()
        return GiftCancellationOutcome(
            attempt,
            cancellable[0] if cancellable else None,
            True,
        )

    async def mark_stale_sending_unknown(
        self,
        attempt_id: int,
        *,
        stale_before: datetime,
    ) -> tuple[AdminGift, bool]:
        """Seal an interrupted send without risking a duplicate Telegram Gift."""
        error = RuntimeError("Gift delivery was interrupted before its result was saved")
        result = await self._session.execute(
            update(AdminGift)
            .where(
                AdminGift.id == attempt_id,
                AdminGift.status == AdminGiftStatus.SENDING.value,
                AdminGift.updated_at < stale_before,
            )
            .values(
                status=AdminGiftStatus.UNKNOWN.value,
                error_type=type(error).__name__,
                error_message=str(error),
                updated_at=datetime.utcnow(),
            )
            .returning(AdminGift.id)
        )
        changed = result.scalar_one_or_none() is not None
        await self._session.commit()
        attempt = await self.get_attempt(attempt_id)
        if attempt is None:
            raise LookupError("Gift attempt not found")
        return attempt, changed

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
