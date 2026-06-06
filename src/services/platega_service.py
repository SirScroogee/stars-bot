"""Platega SBP payment integration."""
import html
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import aiohttp
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.queue import get_order_queue
from src.db.models import (
    BalanceLedger,
    Order,
    PaymentProvider,
    PaymentStatus,
    PlategaPayment,
    ProductType,
    Transaction,
    User,
)
from src.db.session import async_session_factory
from src.locales import t
from src.services.bot_settings_service import get_platega_settings
from src.services.order_service import OrderService
from src.services.rates_service import convert_usdt_to_currency, get_rates
from src.services.telegram_logger import tg_logger
from src.workers.order_worker import get_order_worker

logger = logging.getLogger(__name__)

PLATEGA_PENDING = "pending"
PLATEGA_CONFIRMED = "confirmed"
PLATEGA_CANCELED = "canceled"
PLATEGA_FAILED = "failed"
PLATEGA_EXPIRED = "expired"
PLATEGA_CHARGEBACKED = "chargebacked"

FINAL_STATUSES = {
    PLATEGA_CONFIRMED,
    PLATEGA_CANCELED,
    PLATEGA_FAILED,
    PLATEGA_EXPIRED,
    PLATEGA_CHARGEBACKED,
}

PLATEGA_SBP_FEE_PERCENT = Decimal("8")


class PlategaError(Exception):
    """Base Platega integration error."""


class PlategaConfigError(PlategaError):
    """Platega is disabled or credentials are incomplete."""


@dataclass
class CreatedPlategaPayment:
    payment: PlategaPayment
    pay_url: str
    amount_to_pay_usdt: Decimal
    amount_rub: Decimal
    amount_with_fee_rub: Decimal
    fee_percent: Decimal
    ttl_minutes: int


@dataclass
class PlategaProcessResult:
    status: str
    final: bool
    processed: bool = False
    message: str | None = None


def _normalize_status(status: str | None) -> str:
    value = (status or "").strip().lower()
    mapping = {
        "new": PLATEGA_PENDING,
        "created": PLATEGA_PENDING,
        "pending": PLATEGA_PENDING,
        "processing": PLATEGA_PENDING,
        "confirmed": PLATEGA_CONFIRMED,
        "success": PLATEGA_CONFIRMED,
        "paid": PLATEGA_CONFIRMED,
        "completed": PLATEGA_CONFIRMED,
        "canceled": PLATEGA_CANCELED,
        "cancelled": PLATEGA_CANCELED,
        "failed": PLATEGA_FAILED,
        "declined": PLATEGA_FAILED,
        "expired": PLATEGA_EXPIRED,
        "chargebacked": PLATEGA_CHARGEBACKED,
        "chargeback": PLATEGA_CHARGEBACKED,
    }
    return mapping.get(value, PLATEGA_PENDING)


def _extract_transaction_id(response: dict[str, Any]) -> str | None:
    for key in ("transactionId", "transaction_id", "id", "uuid"):
        value = response.get(key)
        if value:
            return str(value)
    transaction = response.get("transaction")
    if isinstance(transaction, dict):
        for key in ("transactionId", "transaction_id", "id", "uuid"):
            value = transaction.get(key)
            if value:
                return str(value)
    return None


def _extract_payment_url(response: dict[str, Any]) -> str | None:
    for key in ("redirect", "paymentLink", "payment_link", "payUrl", "pay_url", "url"):
        value = response.get(key)
        if value:
            return str(value)
    transaction = response.get("transaction")
    if isinstance(transaction, dict):
        for key in ("redirect", "paymentLink", "payment_link", "payUrl", "pay_url", "url"):
            value = transaction.get(key)
            if value:
                return str(value)
    return None


async def _request_platega(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = await get_platega_settings()
    if not settings["enabled"]:
        raise PlategaConfigError("СБП выключен в настройках")
    if not settings["merchant_id"] or not settings["secret"]:
        raise PlategaConfigError("MerchantId или Secret Platega не настроены")

    url = f"{settings['base_url']}/{path.lstrip('/')}"
    headers = {
        "X-MerchantId": settings["merchant_id"],
        "X-Secret": settings["secret"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, json=json_body, params=params) as response:
            text = await response.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {"raw": text}

            if response.status in (400, 401, 403):
                raise PlategaError(f"Platega rejected request ({response.status}): {data}")
            if response.status == 404:
                raise PlategaError(f"Platega transaction not found: {data}")
            if response.status >= 500:
                raise PlategaError(f"Platega server error ({response.status}): {data}")
            if response.status >= 300:
                raise PlategaError(f"Platega API error ({response.status}): {data}")
            return data


def _extract_platega_rate(response: dict[str, Any]) -> Decimal:
    candidates: list[Any] = []
    for key in ("rate", "usdtRate", "exchangeRate", "exchange_rate", "raw"):
        candidates.append(response.get(key))

    data = response.get("data")
    if isinstance(data, dict):
        for key in ("rate", "usdtRate", "exchangeRate", "exchange_rate"):
            candidates.append(data.get(key))

    transaction = response.get("transaction")
    if isinstance(transaction, dict):
        for key in ("rate", "usdtRate", "exchangeRate", "exchange_rate"):
            candidates.append(transaction.get(key))

    for value in candidates:
        if value is None or value == "":
            continue
        try:
            rate = Decimal(str(value))
        except Exception:
            continue
        if rate > 0:
            return rate

    raise PlategaError(f"Platega returned rate response without rate: {response}")


async def get_platega_usdt_rub_rate() -> Decimal:
    """Return RUB per 1 USDT for SBP according to Platega."""
    settings = await get_platega_settings()
    request_variants = (
        {"currencyFrom": "RUB", "currencyTo": "USDT"},
        {"currencyFrom": "USDT", "currencyTo": "RUB"},
    )
    last_error: Exception | None = None

    for variant in request_variants:
        try:
            response = await _request_platega(
                "GET",
                "/rates/payment_method_rate",
                params={
                    **variant,
                    "merchantId": settings["merchant_id"],
                    "paymentMethod": settings["sbp_method_id"],
                },
            )
            rate = _extract_platega_rate(response)
        except PlategaError as e:
            last_error = e
            logger.warning("Could not get Platega rate for %s: %s", variant, e)
            continue

        if variant["currencyFrom"] == "RUB" and variant["currencyTo"] == "USDT":
            return Decimal("1") / rate
        return rate

    raise PlategaError(f"Could not get Platega SBP rate: {last_error}")


async def _get_sbp_usdt_rub_rate() -> tuple[Decimal, str]:
    try:
        return await get_platega_usdt_rub_rate(), "platega"
    except PlategaError as e:
        logger.warning("Falling back to common RUB rate because Platega rate is unavailable: %s", e)
        rates = await get_rates()
        fallback_rate = convert_usdt_to_currency(Decimal("1"), "rub", rates)
        return fallback_rate, "fallback"


async def create_platega_payment(
    session: AsyncSession,
    *,
    user_id: int,
    operation_type: str,
    amount_usdt: Decimal,
    description: str,
    metadata: dict[str, Any],
    message_id: int | None = None,
) -> CreatedPlategaPayment:
    settings = await get_platega_settings()
    if not settings["enabled"] or not settings["merchant_id"] or not settings["secret"]:
        raise PlategaConfigError("СБП временно недоступен: не заполнены настройки Platega")

    fee_percent = PLATEGA_SBP_FEE_PERCENT
    amount_to_pay_usdt = amount_usdt.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    usdt_rub_rate, rate_source = await _get_sbp_usdt_rub_rate()
    amount_rub = (amount_to_pay_usdt * usdt_rub_rate).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    amount_with_fee_rub = (amount_rub * (Decimal("1") + fee_percent / Decimal("100"))).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    payment_metadata = dict(metadata)
    payment_metadata["sbp_usdt_rub_rate"] = str(usdt_rub_rate.quantize(Decimal("0.0001")))
    payment_metadata["sbp_rate_source"] = rate_source
    payment_metadata["sbp_display_amount_rub_with_fee"] = str(amount_with_fee_rub)
    expires_at = datetime.utcnow() + timedelta(minutes=settings["payment_ttl_minutes"])
    payload = f"{operation_type}:{user_id}:{int(datetime.utcnow().timestamp())}"

    request_body = {
        "paymentMethod": settings["sbp_method_id"],
        "paymentDetails": {
            "amount": float(amount_rub),
            "currency": "RUB",
        },
        "description": description,
        "return": "https://t.me",
        "failedUrl": "https://t.me",
        "payload": payload,
    }

    response = await _request_platega("POST", "/transaction/process", json_body=request_body)
    provider_tx_id = _extract_transaction_id(response)
    pay_url = _extract_payment_url(response)
    if response.get("usdtRate"):
        payment_metadata["platega_response_usdt_rate"] = str(response["usdtRate"])

    if not provider_tx_id or not pay_url:
        raise PlategaError(f"Platega returned incomplete payment response: {response}")

    payment = PlategaPayment(
        user_id=user_id,
        operation_type=operation_type,
        provider_tx_id=provider_tx_id,
        status=PLATEGA_PENDING,
        amount_usdt=amount_usdt,
        amount_rub=amount_rub,
        fee_percent=fee_percent,
        payment_url=pay_url,
        payload=payload,
        metadata_json=json.dumps(payment_metadata, ensure_ascii=False),
        response_json=json.dumps(response, ensure_ascii=False),
        message_id=message_id,
        expires_at=expires_at,
    )
    session.add(payment)
    await session.flush()

    return CreatedPlategaPayment(
        payment=payment,
        pay_url=pay_url,
        amount_to_pay_usdt=amount_to_pay_usdt,
        amount_rub=amount_rub,
        amount_with_fee_rub=amount_with_fee_rub,
        fee_percent=fee_percent,
        ttl_minutes=settings["payment_ttl_minutes"],
    )


async def check_platega_status(provider_tx_id: str) -> tuple[str, dict[str, Any]]:
    response = await _request_platega("GET", f"/transaction/{provider_tx_id}")
    status = response.get("status")
    if not status and isinstance(response.get("transaction"), dict):
        status = response["transaction"].get("status")
    return _normalize_status(status), response


def build_platega_payment_text(
    *,
    title: str,
    item_line: str,
    amount_usdt: Decimal,
    amount_rub: Decimal,
    fee_percent: Decimal,
    ttl_minutes: int,
) -> str:
    return (
        f"{title}\n\n"
        f"<blockquote>{item_line}\n"
        f"Сумма покупки: <b>{amount_usdt:,.2f} USDT ({amount_rub:,.2f} RUB)</b>"
        "</blockquote>\n\n"
        "Нажмите кнопку оплаты, затем бот сам проверит платеж. "
        "Можно также нажать «Проверить оплату» вручную.\n\n"
        f"Срок действия платежа: <b>{ttl_minutes} мин.</b>"
    )


async def process_platega_payment(
    payment_id: int,
    *,
    bot: Bot | None = None,
    force_check: bool = False,
) -> PlategaProcessResult:
    async with async_session_factory() as session:
        result = await session.execute(
            select(PlategaPayment).where(PlategaPayment.id == payment_id).with_for_update()
        )
        payment = result.scalar_one_or_none()
        if not payment:
            return PlategaProcessResult(status=PLATEGA_FAILED, final=True, message="Платеж не найден")

        if payment.status in FINAL_STATUSES and not force_check:
            return PlategaProcessResult(status=payment.status, final=True)

        if payment.expires_at and datetime.utcnow() > payment.expires_at and payment.status == PLATEGA_PENDING:
            payment.status = PLATEGA_EXPIRED
            payment.error_message = "Local payment TTL expired"
            await session.commit()
            await _edit_payment_message(bot, payment, PLATEGA_EXPIRED)
            return PlategaProcessResult(status=PLATEGA_EXPIRED, final=True)

        try:
            status, raw = await check_platega_status(payment.provider_tx_id)
            payment.response_json = json.dumps(raw, ensure_ascii=False)
        except PlategaError as e:
            payment.error_message = str(e)
            await session.commit()
            logger.warning("Could not check Platega payment %s: %s", payment.provider_tx_id, e)
            return PlategaProcessResult(status=payment.status, final=False, message=str(e))

        if status == PLATEGA_PENDING:
            await session.commit()
            return PlategaProcessResult(status=status, final=False)

        payment.status = status

        if status == PLATEGA_CONFIRMED:
            processed, order_id = await _apply_confirmed_payment(session, payment)
            payment.confirmed_at = datetime.utcnow()
            await session.commit()
            if processed and order_id:
                await _enqueue_order(order_id)
            await _edit_payment_message(bot, payment, PLATEGA_CONFIRMED)
            return PlategaProcessResult(status=status, final=True, processed=processed)

        if status == PLATEGA_CHARGEBACKED:
            payment.error_message = "Platega payment chargebacked"
            await session.commit()
            await _notify_chargeback(payment)
            await _edit_payment_message(bot, payment, PLATEGA_CHARGEBACKED)
            return PlategaProcessResult(status=status, final=True)

        payment.error_message = f"Platega payment finished with status {status}"
        await session.commit()
        await _edit_payment_message(bot, payment, status)
        return PlategaProcessResult(status=status, final=True)


async def process_pending_platega_payments(bot: Bot | None = None, *, limit: int = 25) -> int:
    async with async_session_factory() as session:
        result = await session.execute(
            select(PlategaPayment.id)
            .where(PlategaPayment.status == PLATEGA_PENDING)
            .order_by(PlategaPayment.created_at.asc())
            .limit(limit)
        )
        payment_ids = [row[0] for row in result.fetchall()]

    processed = 0
    for payment_id in payment_ids:
        result = await process_platega_payment(payment_id, bot=bot)
        if result.final:
            processed += 1
    return processed


async def _apply_confirmed_payment(
    session: AsyncSession,
    payment: PlategaPayment,
) -> tuple[bool, int | None]:
    external_id = f"platega:{payment.provider_tx_id}"
    existing_tx = await session.execute(select(Transaction).where(Transaction.external_id == external_id))
    if existing_tx.scalar_one_or_none():
        logger.info("Platega payment already processed: %s", external_id)
        return False, payment.order_id

    user_result = await session.execute(select(User).where(User.id == payment.user_id).with_for_update())
    db_user = user_result.scalar_one_or_none()
    if not db_user:
        payment.status = PLATEGA_FAILED
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
            description="SBP deposit via Platega",
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
                description=f"SBP deposit: +{payment.amount_usdt} USDT",
            )
        )
        await tg_logger.log_deposit(
            user_id=payment.user_id,
            username=db_user.username,
            amount=payment.amount_usdt,
            currency="USDT (SBP)",
        )
        return True, None

    if payment.operation_type not in ("stars", "premium"):
        payment.status = PLATEGA_FAILED
        payment.error_message = f"Unknown operation type: {payment.operation_type}"
        return False, None

    recipient_username = metadata.get("recipient_username")
    quantity = int(metadata.get("quantity", 0))
    if not recipient_username or quantity <= 0:
        payment.status = PLATEGA_FAILED
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
        payment_provider=PaymentProvider.PLATEGA,
        order_key=f"PLG{payment.id:08d}",
    )
    payment.order_id = order.id

    if created:
        session.add(
            Transaction(
                user_id=payment.user_id,
                order_id=order.id,
                type=f"{payment.operation_type}_purchase",
                amount_usdt=payment.amount_usdt,
                description=f"{payment.operation_type.title()} purchase via SBP/Platega",
                external_id=external_id,
            )
        )
        await session.flush()
        order.message_id = payment.message_id
    return created, order.id


async def _enqueue_order(order_id: int) -> None:
    worker = get_order_worker()
    if worker:
        await worker.enqueue_order(order_id)
    else:
        await get_order_queue().enqueue(order_id)


async def _edit_payment_message(bot: Bot | None, payment: PlategaPayment, status: str) -> None:
    if not bot or not payment.message_id:
        return

    async with async_session_factory() as session:
        user = await session.get(User, payment.user_id)

    lang = user.language_code if user else "ru"
    metadata = json.loads(payment.metadata_json or "{}")

    if status == PLATEGA_CONFIRMED:
        if payment.operation_type == "deposit":
            text = (
                "✅ <b>Пополнение выполнено</b>\n\n"
                f"<blockquote>Зачислено: <b>{payment.amount_usdt:,.2f} USDT</b></blockquote>"
            )
        else:
            recipient = metadata.get("recipient_username", "")
            quantity = int(metadata.get("quantity", 0))
            if payment.operation_type == "stars":
                quantity_line = t("common.order.quantity_stars", lang, amount=f"{quantity:,}")
            else:
                quantity_line = t("common.order.quantity_premium", lang, months=quantity)
            order = await _get_order(payment.order_id)
            order_key = order.order_key if order else f"PLG{payment.id:08d}"
            text = (
                f"{t('common.order.created_title', lang, order_key=order_key)}\n\n"
                f"<blockquote>{t('common.order.recipient', lang, username=recipient)}\n"
                f"{quantity_line}\n"
                f"{t('common.order.price', lang, price=f'{payment.amount_usdt:,.2f}')}</blockquote>\n\n"
                f"{t('common.order.processing', lang)}"
            )
    elif status == PLATEGA_CHARGEBACKED:
        text = (
            "⚠️ <b>Платеж оспорен</b>\n\n"
            "Платеж получил статус chargeback. Администратор уже уведомлен."
        )
    elif status == PLATEGA_EXPIRED:
        text = "⌛ <b>Платеж истек</b>\n\nСоздайте новый платеж, если оплата еще актуальна."
    else:
        text = "❌ <b>Платеж не прошел</b>\n\nСоздайте новый платеж или выберите другой способ оплаты."

    try:
        await bot.edit_message_text(
            chat_id=payment.user_id,
            message_id=payment.message_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.debug("Could not edit Platega payment message %s: %s", payment.id, e)


async def _get_order(order_id: int | None) -> Order | None:
    if not order_id:
        return None
    async with async_session_factory() as session:
        return await session.get(Order, order_id)


async def _notify_chargeback(payment: PlategaPayment) -> None:
    text = (
        "<b>SBP CHARGEBACK</b>\n\n"
        f"Платеж: <code>{html.escape(payment.provider_tx_id)}</code>\n"
        f"Пользователь: <code>{payment.user_id}</code>\n"
        f"Тип: <b>{html.escape(payment.operation_type)}</b>\n"
        f"Сумма: <b>{payment.amount_usdt:,.2f} USDT</b> / <b>{payment.amount_rub:,.2f} RUB</b>"
    )
    await tg_logger.log_error(
        error_type="PlategaChargeback",
        error_message=f"Chargeback for payment {payment.provider_tx_id}",
        user_id=payment.user_id,
        details=text,
    )
    try:
        await tg_logger._notify_admins(text)
    except Exception as e:
        logger.warning("Could not notify admins about chargeback: %s", e)
