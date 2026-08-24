"""Lava Business API integration for SBP payments."""
import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    BalanceLedger,
    LavaPayment,
    Order,
    PaymentProvider,
    ProductType,
    Transaction,
    User,
)
from src.db.session import async_session_factory
from src.locales import t
from src.services.bot_settings_service import get_lava_settings
from src.services.order_runtime_service import enqueue_order_reliably, log_created_order
from src.services.order_service import OrderService
from src.services.rub_rate_service import get_display_usdt_rub_rate
from src.services.telegram_logger import tg_logger
from src.utils import format_decimal_compact

logger = logging.getLogger(__name__)

LAVA_PENDING = "pending"
LAVA_CONFIRMED = "confirmed"
LAVA_CANCELED = "canceled"
LAVA_FAILED = "failed"
LAVA_EXPIRED = "expired"

FINAL_STATUSES = {
    LAVA_CONFIRMED,
    LAVA_CANCELED,
    LAVA_FAILED,
    LAVA_EXPIRED,
}


class LavaError(Exception):
    """Base Lava integration error."""


class LavaConfigError(LavaError):
    """Lava is disabled or credentials are incomplete."""


class LavaAPIError(LavaError):
    """Lava returned an unsuccessful API response."""


class LavaNetworkError(LavaError):
    """The API request result is unknown because of a network failure."""


class LavaCreatePendingError(LavaError):
    """Invoice creation is ambiguous and will be recovered by the poller."""

    def __init__(self, payment_id: int, message: str):
        super().__init__(message)
        self.payment_id = payment_id


class LavaValidationError(LavaError):
    """Provider data does not match the payment recorded by the bot."""


@dataclass
class CreatedLavaPayment:
    payment: LavaPayment
    pay_url: str
    amount_to_pay_usdt: Decimal
    base_amount_rub: Decimal
    amount_with_fee_rub: Decimal
    fee_percent: Decimal
    ttl_minutes: int


@dataclass
class LavaProcessResult:
    status: str
    final: bool
    processed: bool = False
    message: str | None = None


def serialize_lava_payload(payload: dict[str, Any]) -> bytes:
    """Serialize once so Lava receives exactly the bytes that were signed."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_lava_payload(payload_bytes: bytes, secret_key: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()


def normalize_lava_status(status: Any) -> str:
    value = str(status or "").strip().lower()
    mapping = {
        "0": LAVA_PENDING,
        "new": LAVA_PENDING,
        "created": LAVA_PENDING,
        "pending": LAVA_PENDING,
        "processing": LAVA_PENDING,
        "success": LAVA_CONFIRMED,
        "paid": LAVA_CONFIRMED,
        "completed": LAVA_CONFIRMED,
        "confirmed": LAVA_CONFIRMED,
        "cancel": LAVA_CANCELED,
        "canceled": LAVA_CANCELED,
        "cancelled": LAVA_CANCELED,
        "error": LAVA_FAILED,
        "failed": LAVA_FAILED,
        "expired": LAVA_EXPIRED,
    }
    return mapping.get(value, LAVA_PENDING)


def calculate_lava_amounts(
    amount_usdt: Decimal,
    usdt_rub_rate: Decimal,
    fee_percent: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return billable USDT, base RUB and final RUB amounts."""
    if not amount_usdt.is_finite() or amount_usdt <= 0:
        raise LavaValidationError("Payment amount must be a positive finite number")
    if not usdt_rub_rate.is_finite() or usdt_rub_rate <= 0:
        raise LavaValidationError("USDT/RUB rate must be a positive finite number")
    if not fee_percent.is_finite() or fee_percent < 0 or fee_percent > 100:
        raise LavaValidationError("Lava fee must be between 0 and 100 percent")
    amount_to_pay_usdt = amount_usdt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    base_amount_rub = (amount_to_pay_usdt * usdt_rub_rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    amount_rub = (
        base_amount_rub * (Decimal("1") + fee_percent / Decimal("100"))
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return amount_to_pay_usdt, base_amount_rub, amount_rub


def build_lava_invoice_request(
    *,
    shop_id: str,
    amount_rub: Decimal,
    provider_order_id: str,
    return_url: str,
    ttl_minutes: int,
    payment_id: int,
    operation_type: str,
    description: str,
) -> dict[str, Any]:
    """Build the signed request body; Lava is intentionally restricted to SBP."""
    return {
        "shopId": shop_id,
        "sum": float(amount_rub),
        "orderId": provider_order_id,
        "successUrl": return_url,
        "failUrl": return_url,
        "expire": ttl_minutes,
        "customFields": json.dumps(
            {"payment_id": payment_id, "operation": operation_type},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "comment": description[:255],
        "includeService": ["sbp"],
    }


def _api_error_text(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)
    error = data.get("error") or data.get("message") or data.get("error_message")
    if isinstance(error, dict):
        parts: list[str] = []
        for key, value in error.items():
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            parts.append(f"{key}: {value}")
        return "; ".join(parts)
    return str(error or data)


async def _request_lava(
    path: str,
    payload: dict[str, Any],
    *,
    require_enabled: bool,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    settings = await get_lava_settings()
    if require_enabled and not settings["enabled"]:
        raise LavaConfigError("Lava выключена в настройках")
    if not settings["configured"]:
        raise LavaConfigError("Shop ID или Secret Key Lava не настроены")

    body = serialize_lava_payload(payload)
    signature = sign_lava_payload(body, settings["secret_key"])
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Signature": signature,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    url = f"{settings['base_url']}/{path.lstrip('/')}"

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body, headers=headers) as response:
                response_text = await response.text()
                try:
                    data = json.loads(response_text) if response_text else {}
                except json.JSONDecodeError:
                    data = {"raw": response_text}

                if response.status >= 500:
                    raise LavaAPIError(f"Lava server error ({response.status})")
                if response.status >= 300:
                    raise LavaAPIError(
                        f"Lava API error ({response.status}): {_api_error_text(data)}"
                    )
                if not isinstance(data, dict):
                    raise LavaAPIError("Lava returned a non-object response")
                if data.get("status_check") is False:
                    raise LavaAPIError(_api_error_text(data))
                body_status = data.get("status")
                if isinstance(body_status, int) and body_status >= 300:
                    raise LavaAPIError(_api_error_text(data))
                if isinstance(body_status, str) and body_status.lower() == "error":
                    raise LavaAPIError(_api_error_text(data))
                return data
    except LavaError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise LavaNetworkError(
            f"Lava request failed for {path}: {type(error).__name__ or 'network error'}"
        ) from error


def _response_data(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise LavaAPIError(f"Lava returned an incomplete response: {_api_error_text(response)}")
    return data


def _invoice_id(data: dict[str, Any]) -> str | None:
    value = data.get("id") or data.get("invoiceId") or data.get("invoice_id")
    return str(value) if value else None


def _invoice_url(data: dict[str, Any], invoice_id: str | None) -> str | None:
    value = data.get("url") or data.get("paymentUrl") or data.get("payment_url")
    if value:
        return str(value)
    if invoice_id:
        return f"https://pay.lava.ru/invoice/{invoice_id}?lang=ru"
    return None


def _invoice_order_id(data: dict[str, Any]) -> str | None:
    value = data.get("order_id") or data.get("orderId")
    return str(value) if value is not None else None


def _invoice_shop_id(data: dict[str, Any]) -> str | None:
    value = data.get("shop_id") or data.get("shopId")
    return str(value) if value else None


def _invoice_amount(data: dict[str, Any]) -> Decimal | None:
    value = data.get("amount")
    if value is None:
        value = data.get("sum")
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (ValueError, TypeError, ArithmeticError) as error:
        raise LavaValidationError("Lava returned an invalid invoice amount") from error


def _validate_invoice(
    payment: LavaPayment,
    data: dict[str, Any],
    *,
    require_identity: bool,
    expected_shop_id: str,
) -> None:
    order_id = _invoice_order_id(data)
    shop_id = _invoice_shop_id(data)
    amount = _invoice_amount(data)

    if require_identity and (not order_id or not shop_id or amount is None):
        raise LavaValidationError("Lava status response is missing invoice identity fields")
    if order_id and order_id != payment.provider_order_id:
        raise LavaValidationError("Lava order ID does not match the local payment")
    if shop_id and shop_id.lower() != expected_shop_id.lower():
        raise LavaValidationError("Lava Shop ID does not match the configured project")
    if amount is not None and amount != payment.amount_rub.quantize(Decimal("0.01")):
        raise LavaValidationError("Lava invoice amount does not match the expected amount")


async def check_lava_status(
    *,
    provider_invoice_id: str | None = None,
    provider_order_id: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if not provider_invoice_id and not provider_order_id:
        raise LavaError("Invoice ID or order ID is required")
    settings = await get_lava_settings()
    payload: dict[str, Any] = {"shopId": settings["shop_id"]}
    if provider_invoice_id:
        payload["invoiceId"] = provider_invoice_id
    else:
        payload["orderId"] = provider_order_id
    response = await _request_lava(
        "/business/invoice/status",
        payload,
        require_enabled=False,
        timeout_seconds=5,
    )
    data = _response_data(response)
    return normalize_lava_status(data.get("status")), response, data


async def _set_creation_error(payment_id: int, error: str, *, final: bool) -> None:
    async with async_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)
        if not payment:
            return
        payment.error_message = error[:2000]
        if final:
            payment.status = LAVA_FAILED
        await session.commit()


async def _finish_invoice_creation(
    payment_id: int,
    response: dict[str, Any],
) -> CreatedLavaPayment:
    data = _response_data(response)
    invoice_id = _invoice_id(data)
    pay_url = _invoice_url(data, invoice_id)
    if not invoice_id or not pay_url:
        raise LavaAPIError("Lava returned an invoice without ID or payment URL")

    settings = await get_lava_settings()
    async with async_session_factory() as session:
        result = await session.execute(
            select(LavaPayment).where(LavaPayment.id == payment_id).with_for_update()
        )
        payment = result.scalar_one()
        _validate_invoice(
            payment,
            data,
            require_identity=False,
            expected_shop_id=settings["shop_id"],
        )
        payment.provider_invoice_id = invoice_id
        payment.payment_url = pay_url
        payment.response_json = json.dumps(response, ensure_ascii=False)
        payment.error_message = None
        await session.commit()

    return CreatedLavaPayment(
        payment=payment,
        pay_url=pay_url,
        amount_to_pay_usdt=payment.amount_usdt,
        base_amount_rub=payment.base_amount_rub,
        amount_with_fee_rub=payment.amount_rub,
        fee_percent=payment.fee_percent,
        ttl_minutes=settings["payment_ttl_minutes"],
    )


async def create_lava_payment(
    *,
    user_id: int,
    operation_type: str,
    amount_usdt: Decimal,
    description: str,
    metadata: dict[str, Any],
    return_url: str,
    message_id: int | None = None,
) -> CreatedLavaPayment:
    settings = await get_lava_settings()
    if not settings["enabled"] or not settings["configured"]:
        raise LavaConfigError("Lava временно недоступна: способ оплаты не настроен")
    if operation_type not in ("deposit", "stars", "premium"):
        raise LavaError(f"Unsupported Lava operation: {operation_type}")
    if not amount_usdt.is_finite() or amount_usdt <= 0:
        raise LavaError("Payment amount must be positive")

    usdt_rub_rate, rate_source = await get_display_usdt_rub_rate()
    if not usdt_rub_rate or not rate_source:
        raise LavaError("Не удалось получить курс USDT/RUB")

    amount_to_pay_usdt, base_amount_rub, amount_rub = calculate_lava_amounts(
        amount_usdt,
        usdt_rub_rate,
        settings["fee_percent"],
    )
    provider_order_id = f"DS-{operation_type[:3].upper()}-{uuid4().hex}"
    expires_at = datetime.utcnow() + timedelta(minutes=settings["payment_ttl_minutes"])

    payment_metadata = dict(metadata)
    payment_metadata.update(
        {
            "lava_usdt_rub_rate": str(usdt_rub_rate),
            "lava_rate_source": rate_source,
            "lava_base_amount_rub": str(base_amount_rub),
            "lava_amount_rub_with_fee": str(amount_rub),
        }
    )

    async with async_session_factory() as session:
        payment = LavaPayment(
            user_id=user_id,
            operation_type=operation_type,
            provider_order_id=provider_order_id,
            status=LAVA_PENDING,
            amount_usdt=amount_to_pay_usdt,
            base_amount_rub=base_amount_rub,
            amount_rub=amount_rub,
            fee_percent=settings["fee_percent"],
            usdt_rub_rate=usdt_rub_rate,
            rate_source=rate_source,
            metadata_json=json.dumps(payment_metadata, ensure_ascii=False),
            message_id=message_id,
            expires_at=expires_at,
        )
        session.add(payment)
        await session.commit()
        payment_id = payment.id

    request_body = build_lava_invoice_request(
        shop_id=settings["shop_id"],
        amount_rub=amount_rub,
        provider_order_id=provider_order_id,
        return_url=return_url,
        ttl_minutes=settings["payment_ttl_minutes"],
        payment_id=payment_id,
        operation_type=operation_type,
        description=description,
    )

    async with async_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)
        if payment:
            payment.request_json = serialize_lava_payload(request_body).decode("utf-8")
            await session.commit()

    try:
        response = await _request_lava(
            "/business/invoice/create",
            request_body,
            require_enabled=True,
        )
        return await _finish_invoice_creation(payment_id, response)
    except LavaError as creation_error:
        try:
            _status, recovered_response, _data = await check_lava_status(
                provider_order_id=provider_order_id
            )
            logger.warning("Recovered ambiguous Lava invoice %s", provider_order_id)
            return await _finish_invoice_creation(payment_id, recovered_response)
        except LavaError as recovery_error:
            error_text = f"{creation_error}; recovery: {recovery_error}"
            if isinstance(creation_error, LavaNetworkError):
                await _set_creation_error(payment_id, error_text, final=False)
                raise LavaCreatePendingError(payment_id, error_text) from creation_error
            await _set_creation_error(payment_id, error_text, final=True)
            raise creation_error


def build_lava_payment_text(
    *,
    lang: str,
    item_line: str,
    amount_usdt: Decimal,
    base_amount_rub: Decimal,
    amount_rub: Decimal,
    fee_percent: Decimal,
    ttl_minutes: int,
) -> str:
    return t(
        "common.lava_payment.details",
        lang,
        item_line=item_line,
        amount_usdt=f"{amount_usdt:,.2f}",
        base_amount_rub=f"{base_amount_rub:,.2f}",
        amount_rub=f"{amount_rub:,.2f}",
        fee_percent=format_decimal_compact(fee_percent),
        ttl_minutes=ttl_minutes,
    )


async def process_lava_payment(
    payment_id: int,
    *,
    bot: Bot | None = None,
    force_check: bool = False,
) -> LavaProcessResult:
    async with async_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)
        if not payment:
            return LavaProcessResult(status=LAVA_FAILED, final=True, message="Платеж не найден")
        if payment.status in FINAL_STATUSES:
            return LavaProcessResult(status=payment.status, final=True)
        invoice_id = payment.provider_invoice_id
        provider_order_id = payment.provider_order_id
        locally_expired = bool(payment.expires_at and datetime.utcnow() > payment.expires_at)

    try:
        status, raw, data = await check_lava_status(
            provider_invoice_id=invoice_id,
            provider_order_id=provider_order_id,
        )
    except LavaError as error:
        async with async_session_factory() as session:
            result = await session.execute(
                select(LavaPayment).where(LavaPayment.id == payment_id).with_for_update()
            )
            payment = result.scalar_one_or_none()
            if not payment or payment.status in FINAL_STATUSES:
                return LavaProcessResult(
                    status=payment.status if payment else LAVA_FAILED,
                    final=True,
                )
            payment.error_message = str(error)[:2000]
            await session.commit()
        logger.warning("Could not check Lava payment %s: %s", provider_order_id, error)
        return LavaProcessResult(status=LAVA_PENDING, final=False, message=str(error))

    settings = await get_lava_settings()
    order_id: int | None = None
    processed = False
    recovered_invoice = False

    async with async_session_factory() as session:
        result = await session.execute(
            select(LavaPayment).where(LavaPayment.id == payment_id).with_for_update()
        )
        payment = result.scalar_one_or_none()
        if not payment:
            return LavaProcessResult(status=LAVA_FAILED, final=True, message="Платеж не найден")
        if payment.status in FINAL_STATUSES:
            return LavaProcessResult(status=payment.status, final=True)

        payment.response_json = json.dumps(raw, ensure_ascii=False)
        try:
            _validate_invoice(
                payment,
                data,
                require_identity=status == LAVA_CONFIRMED,
                expected_shop_id=settings["shop_id"],
            )
        except LavaValidationError as error:
            payment.status = LAVA_FAILED
            payment.error_message = str(error)
            await session.commit()
            await _notify_validation_error(payment, str(error))
            await _edit_payment_message(bot, payment, LAVA_FAILED)
            return LavaProcessResult(status=LAVA_FAILED, final=True, message=str(error))

        response_invoice_id = _invoice_id(data)
        if response_invoice_id and not payment.provider_invoice_id:
            payment.provider_invoice_id = response_invoice_id
            payment.payment_url = _invoice_url(data, response_invoice_id)
            recovered_invoice = bool(payment.payment_url)

        if status == LAVA_PENDING:
            payment.error_message = None
            if locally_expired:
                payment.status = LAVA_EXPIRED
                payment.error_message = "Local Lava payment TTL expired after provider check"
            await session.commit()
        elif status == LAVA_CONFIRMED:
            payment.status = LAVA_CONFIRMED
            processed, order_id = await _apply_confirmed_payment(session, payment)
            if payment.status == LAVA_CONFIRMED:
                payment.confirmed_at = datetime.utcnow()
                payment.error_message = None
            await session.commit()
        else:
            payment.status = status
            payment.error_message = f"Lava payment finished with status {status}"
            await session.commit()

    if status == LAVA_PENDING:
        if locally_expired:
            await _edit_payment_message(bot, payment, LAVA_EXPIRED)
            return LavaProcessResult(status=LAVA_EXPIRED, final=True)
        if recovered_invoice:
            await _edit_payment_message(bot, payment, LAVA_PENDING)
        return LavaProcessResult(status=LAVA_PENDING, final=False)

    if status == LAVA_CONFIRMED and payment.status == LAVA_CONFIRMED:
        if processed and order_id:
            await _enqueue_order(order_id, payment.amount_rub)
        elif processed and payment.operation_type == "deposit":
            await _log_confirmed_deposit(payment)
        await _edit_payment_message(bot, payment, LAVA_CONFIRMED)
        return LavaProcessResult(status=LAVA_CONFIRMED, final=True, processed=processed)

    effective_status = payment.status
    await _edit_payment_message(bot, payment, effective_status)
    return LavaProcessResult(
        status=effective_status,
        final=True,
        processed=False,
        message=payment.error_message,
    )


async def process_pending_lava_payments(bot: Bot | None = None, *, limit: int = 25) -> int:
    now = datetime.utcnow()
    async with async_session_factory() as session:
        result = await session.execute(
            select(LavaPayment.id)
            .where(LavaPayment.status == LAVA_PENDING)
            # Current invoices must not be starved by old ambiguous records that
            # cannot be reconciled while the provider returns an error.
            .order_by(
                case(
                    (
                        LavaPayment.expires_at.isnot(None)
                        & (LavaPayment.expires_at < now),
                        1,
                    ),
                    else_=0,
                ),
                LavaPayment.created_at.asc(),
            )
            .limit(limit)
        )
        payment_ids = [row[0] for row in result.fetchall()]

    results = await asyncio.gather(
        *(process_lava_payment(payment_id, bot=bot) for payment_id in payment_ids),
        return_exceptions=True,
    )
    processed = 0
    for payment_id, result in zip(payment_ids, results):
        if isinstance(result, BaseException):
            logger.error(
                "Unexpected error while processing Lava payment %s: %s",
                payment_id,
                result,
                exc_info=(type(result), result, result.__traceback__),
            )
        elif result.final:
            processed += 1
    return processed


async def _apply_confirmed_payment(
    session: AsyncSession,
    payment: LavaPayment,
) -> tuple[bool, int | None]:
    invoice_id = payment.provider_invoice_id or payment.provider_order_id
    external_id = f"lava:{invoice_id}"
    existing_tx = await session.execute(
        select(Transaction).where(Transaction.external_id == external_id)
    )
    if existing_tx.scalar_one_or_none():
        logger.info("Lava payment already processed: %s", external_id)
        return False, payment.order_id

    user_result = await session.execute(
        select(User).where(User.id == payment.user_id).with_for_update()
    )
    db_user = user_result.scalar_one_or_none()
    if not db_user:
        payment.status = LAVA_FAILED
        payment.error_message = "User not found"
        return False, None

    metadata = json.loads(payment.metadata_json or "{}")
    if payment.operation_type == "deposit":
        balance_before = db_user.balance_usdt
        db_user.balance_usdt += payment.amount_usdt
        transaction = Transaction(
            user_id=payment.user_id,
            type="deposit",
            amount_usdt=payment.amount_usdt,
            description="SBP deposit via Lava",
            external_id=external_id,
        )
        session.add(transaction)
        await session.flush()
        session.add(
            BalanceLedger(
                user_id=payment.user_id,
                transaction_id=transaction.id,
                operation="credit",
                amount_stars=Decimal("0"),
                amount_usdt=payment.amount_usdt,
                amount_premium=0,
                balance_stars_before=db_user.balance_stars,
                balance_stars_after=db_user.balance_stars,
                balance_usdt_before=balance_before,
                balance_usdt_after=db_user.balance_usdt,
                balance_premium_before=db_user.balance_premium_months,
                balance_premium_after=db_user.balance_premium_months,
                description=f"Lava deposit: +{payment.amount_usdt} USDT",
            )
        )
        return True, None

    if payment.operation_type not in ("stars", "premium"):
        payment.status = LAVA_FAILED
        payment.error_message = f"Unknown operation type: {payment.operation_type}"
        return False, None

    recipient_username = metadata.get("recipient_username")
    quantity = int(metadata.get("quantity", 0))
    if not recipient_username or quantity <= 0:
        payment.status = LAVA_FAILED
        payment.error_message = "Invalid payment metadata"
        return False, None

    product_type = ProductType.STARS if payment.operation_type == "stars" else ProductType.PREMIUM
    order_service = OrderService(session)
    order, created = await order_service.create_order(
        user_id=payment.user_id,
        recipient_username=recipient_username,
        product_type=product_type,
        quantity=quantity,
        price_usdt=payment.amount_usdt,
        payment_provider=PaymentProvider.LAVA,
        order_key=f"LAVA{payment.id:08d}",
    )
    payment.order_id = order.id
    if created:
        session.add(
            Transaction(
                user_id=payment.user_id,
                order_id=order.id,
                type=f"{payment.operation_type}_purchase",
                amount_usdt=payment.amount_usdt,
                description=f"{payment.operation_type.title()} purchase via SBP/Lava",
                external_id=external_id,
            )
        )
        await session.flush()
        order.message_id = payment.message_id
    return created, order.id


async def _enqueue_order(order_id: int, amount_rub: Decimal) -> None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Order, User.username)
            .join(User, User.id == Order.user_id)
            .where(Order.id == order_id)
        )
        row = result.one_or_none()
    if not row:
        logger.error("Could not load confirmed Lava order %s", order_id)
        return
    order, username = row
    await enqueue_order_reliably(order_id)
    await log_created_order(
        order,
        username,
        paid_amount_rub=amount_rub,
        provider_amount=f"{amount_rub:,.2f} RUB (Lava / SBP)",
    )


async def _log_confirmed_deposit(payment: LavaPayment) -> None:
    try:
        async with async_session_factory() as session:
            user = await session.get(User, payment.user_id)
        await tg_logger.log_deposit(
            user_id=payment.user_id,
            username=user.username if user else None,
            amount=payment.amount_usdt,
            currency="USDT",
            provider="Lava / СБП",
            amount_rub=payment.amount_rub,
        )
    except Exception:
        logger.exception("Could not log confirmed Lava deposit %s", payment.id)


def _payment_keyboard(payment: LavaPayment, lang: str) -> InlineKeyboardMarkup:
    check_callbacks = {
        "stars": "stars:check:lava",
        "premium": "premium:check:lava",
        "deposit": "deposit:check:lava",
    }
    cancel_callbacks = {
        "stars": "stars:cancel",
        "premium": "premium:cancel",
        "deposit": "deposit:cancel",
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_lava_full", lang),
                    url=payment.payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.check_payment", lang),
                    callback_data=check_callbacks[payment.operation_type],
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=cancel_callbacks[payment.operation_type],
                )
            ],
        ]
    )


def _item_line(payment: LavaPayment, metadata: dict[str, Any], lang: str) -> str:
    if payment.operation_type == "deposit":
        return t(
            "common.lava_payment.deposit_item",
            lang,
            amount=f"{payment.amount_usdt:,.2f}",
        )
    recipient = metadata.get("recipient_username", "")
    quantity = int(metadata.get("quantity", 0))
    if payment.operation_type == "stars":
        return t(
            "common.lava_payment.stars_item",
            lang,
            recipient=recipient,
            quantity=f"{quantity:,}",
        )
    return t(
        "common.lava_payment.premium_item",
        lang,
        recipient=recipient,
        quantity=quantity,
    )


async def _edit_payment_message(bot: Bot | None, payment: LavaPayment, status: str) -> None:
    if not bot:
        return
    lang = "ru"
    try:
        async with async_session_factory() as session:
            user = await session.get(User, payment.user_id)
        if user:
            lang = user.language_code
    except Exception as error:
        logger.warning("Could not load language for Lava payment %s: %s", payment.id, error)

    try:
        metadata = json.loads(payment.metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}

    reply_markup = None
    if status == LAVA_PENDING and payment.payment_url:
        text = build_lava_payment_text(
            lang=lang,
            item_line=_item_line(payment, metadata, lang),
            amount_usdt=payment.amount_usdt,
            base_amount_rub=payment.base_amount_rub,
            amount_rub=payment.amount_rub,
            fee_percent=payment.fee_percent,
            ttl_minutes=30,
        )
        reply_markup = _payment_keyboard(payment, lang)
    elif status == LAVA_CONFIRMED:
        if payment.operation_type == "deposit":
            text = t(
                "common.lava_payment.deposit_received",
                lang,
                amount=f"{payment.amount_usdt:,.2f}",
            )
        else:
            order = await _get_order(payment.order_id)
            order_key = order.order_key if order else f"LAVA{payment.id:08d}"
            recipient = metadata.get("recipient_username", "")
            quantity = int(metadata.get("quantity", 0))
            quantity_line = (
                t("common.order.quantity_stars", lang, amount=f"{quantity:,}")
                if payment.operation_type == "stars"
                else t("common.order.quantity_premium", lang, months=quantity)
            )
            price = (
                f"{payment.amount_usdt:,.2f} USDT "
                f"({payment.amount_rub:,.2f} RUB)"
            )
            text = (
                f"{t('common.payment_status.received_title', lang)}\n\n"
                f"{t('common.order.created_title', lang, order_key=order_key)}\n\n"
                f"<blockquote>{t('common.order.recipient', lang, username=recipient)}\n"
                f"{quantity_line}\n"
                f"{t('common.order.price', lang, price=price)}</blockquote>\n\n"
                f"{t('common.order.processing', lang)}"
            )
    elif status == LAVA_EXPIRED:
        text = t("common.lava_payment.expired", lang)
    elif status == LAVA_CANCELED:
        text = t("common.lava_payment.cancelled", lang)
    else:
        text = t("common.lava_payment.failed", lang)

    if payment.message_id:
        try:
            await bot.edit_message_text(
                chat_id=payment.user_id,
                message_id=payment.message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        except Exception as text_error:
            if "message is not modified" in str(text_error).lower():
                return
        try:
            await bot.edit_message_caption(
                chat_id=payment.user_id,
                message_id=payment.message_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        except Exception as caption_error:
            if "message is not modified" in str(caption_error).lower():
                return
            logger.warning("Could not edit Lava payment message %s", payment.id)

    try:
        await bot.send_message(
            chat_id=payment.user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("Could not notify user about Lava payment %s", payment.id)


async def _get_order(order_id: int | None) -> Order | None:
    if not order_id:
        return None
    async with async_session_factory() as session:
        return await session.get(Order, order_id)


async def _notify_validation_error(payment: LavaPayment, error: str) -> None:
    try:
        await tg_logger.log_error(
            error_type="LavaPaymentValidation",
            error_message=error,
            user_id=payment.user_id,
            details=(
                f"Payment ID: {payment.id}\n"
                f"Order ID: {payment.provider_order_id}\n"
                f"Invoice ID: {payment.provider_invoice_id or '-'}"
            ),
        )
    except Exception:
        logger.exception("Could not report Lava validation error for payment %s", payment.id)
