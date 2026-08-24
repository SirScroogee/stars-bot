"""Focused tests for the Lava Business API integration."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.bot.keyboards.admin import get_settings_lava_keyboard
from src.bot.keyboards.deposit import (
    DepositCallback,
    get_lava_payment_keyboard,
    get_payment_method_keyboard as deposit_methods,
)
from src.bot.keyboards.premium import (
    PremiumCallback,
    get_premium_lava_payment_keyboard,
    get_premium_payment_method_keyboard,
)
from src.bot.keyboards.stars import (
    StarsCallback,
    get_payment_method_keyboard as stars_methods,
    get_stars_lava_payment_keyboard,
)
from src.services.bot_settings_service import get_lava_settings
from src.services.lava_service import (
    LAVA_CANCELED,
    LAVA_CONFIRMED,
    LAVA_FAILED,
    LAVA_PENDING,
    LavaNetworkError,
    LavaProcessResult,
    LavaValidationError,
    _request_lava,
    _apply_confirmed_payment,
    _validate_invoice,
    build_lava_payment_text,
    build_lava_invoice_request,
    calculate_lava_amounts,
    normalize_lava_status,
    process_lava_payment,
    process_pending_lava_payments,
    serialize_lava_payload,
    sign_lava_payload,
)
from src.db.models import BalanceLedger, LavaPayment, Order, Transaction, User


def _callbacks(keyboard) -> set[str]:
    return {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    }


def _callback_rows(keyboard) -> list[list[str]]:
    return [
        [button.callback_data for button in row if button.callback_data]
        for row in keyboard.inline_keyboard
    ]


@pytest_asyncio.fixture
async def lava_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(Order.__table__.create)
        await connection.run_sync(Transaction.__table__.create)
        await connection.run_sync(BalanceLedger.__table__.create)
        await connection.run_sync(LavaPayment.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _create_lava_user_and_payment(factory, operation_type: str) -> int:
    async with factory() as session:
        session.add(
            User(
                id=777,
                username="lava_buyer",
                referral_code="lava-777",
                balance_stars=Decimal("0"),
                balance_usdt=Decimal("0"),
                balance_premium_months=0,
            )
        )
        metadata = (
            {"recipient_username": "recipient", "quantity": 50}
            if operation_type == "stars"
            else {"amount_usdt": "1.00"}
        )
        payment = LavaPayment(
            user_id=777,
            operation_type=operation_type,
            provider_order_id=f"DS-{operation_type}",
            provider_invoice_id=f"invoice-{operation_type}",
            status=LAVA_PENDING,
            amount_usdt=Decimal("1.00"),
            base_amount_rub=Decimal("100.00"),
            amount_rub=Decimal("103.40"),
            fee_percent=Decimal("3.4"),
            usdt_rub_rate=Decimal("100"),
            rate_source="platega",
            metadata_json=json.dumps(metadata),
        )
        session.add(payment)
        await session.commit()
        return payment.id


def test_signature_is_computed_from_exact_transmitted_json_bytes():
    payload = {
        "shopId": "shop-id",
        "sum": 103.4,
        "orderId": "DS-STA-1",
        "comment": "Покупка звёзд",
        "includeService": ["sbp"],
    }
    body = serialize_lava_payload(payload)

    assert body == (
        '{"shopId":"shop-id","sum":103.4,"orderId":"DS-STA-1",'
        '"comment":"Покупка звёзд","includeService":["sbp"]}'
    ).encode("utf-8")
    assert sign_lava_payload(body, "secret") == hmac.new(
        b"secret", body, hashlib.sha256
    ).hexdigest()


def test_amount_calculation_applies_configured_3_4_percent_fee():
    billable_usdt, base_rub, total_rub = calculate_lava_amounts(
        Decimal("1"), Decimal("100"), Decimal("3.4")
    )

    assert billable_usdt == Decimal("1.00")
    assert base_rub == Decimal("100.00")
    assert total_rub == Decimal("103.40")


@pytest.mark.parametrize(
    ("amount", "rate", "fee"),
    [
        (Decimal("NaN"), Decimal("100"), Decimal("3.4")),
        (Decimal("1"), Decimal("Infinity"), Decimal("3.4")),
        (Decimal("1"), Decimal("100"), Decimal("NaN")),
    ],
)
def test_amount_calculation_rejects_non_finite_values(amount, rate, fee):
    with pytest.raises(LavaValidationError):
        calculate_lava_amounts(amount, rate, fee)


@pytest.mark.asyncio
async def test_confirmed_lava_deposit_is_credited_exactly_once(lava_session_factory):
    payment_id = await _create_lava_user_and_payment(lava_session_factory, "deposit")

    async with lava_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)
        first_processed, first_order_id = await _apply_confirmed_payment(session, payment)
        await session.commit()
    async with lava_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)
        second_processed, second_order_id = await _apply_confirmed_payment(session, payment)
        await session.commit()
        user = await session.get(User, 777)
        transaction_count = await session.scalar(select(func.count(Transaction.id)))
        ledger_count = await session.scalar(select(func.count(BalanceLedger.id)))

    assert (first_processed, first_order_id) == (True, None)
    assert (second_processed, second_order_id) == (False, None)
    assert user.balance_usdt == Decimal("1.000000")
    assert transaction_count == 1
    assert ledger_count == 1


@pytest.mark.asyncio
async def test_confirmed_lava_purchase_creates_one_order_and_transaction(lava_session_factory):
    payment_id = await _create_lava_user_and_payment(lava_session_factory, "stars")

    async with lava_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)
        first_processed, first_order_id = await _apply_confirmed_payment(session, payment)
        await session.commit()
    async with lava_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)
        second_processed, second_order_id = await _apply_confirmed_payment(session, payment)
        await session.commit()
        order_count = await session.scalar(select(func.count(Order.id)))
        transaction_count = await session.scalar(select(func.count(Transaction.id)))

    assert first_processed is True
    assert second_processed is False
    assert second_order_id == first_order_id
    assert order_count == 1
    assert transaction_count == 1


@pytest.mark.asyncio
async def test_expired_local_ttl_does_not_finalize_payment_when_lava_is_unavailable(
    lava_session_factory,
):
    payment_id = await _create_lava_user_and_payment(lava_session_factory, "deposit")
    async with lava_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)
        payment.expires_at = datetime.utcnow() - timedelta(minutes=1)
        await session.commit()

    with (
        patch(
            "src.services.lava_service.async_session_factory",
            lava_session_factory,
        ),
        patch(
            "src.services.lava_service.check_lava_status",
            AsyncMock(side_effect=LavaNetworkError("temporary outage")),
        ),
    ):
        result = await process_lava_payment(payment_id)

    async with lava_session_factory() as session:
        payment = await session.get(LavaPayment, payment_id)

    assert result.status == LAVA_PENDING
    assert result.final is False
    assert payment.status == LAVA_PENDING


@pytest.mark.asyncio
async def test_poller_prioritizes_current_invoices_over_expired_ambiguous_records(
    lava_session_factory,
):
    expired_id = await _create_lava_user_and_payment(lava_session_factory, "deposit")
    async with lava_session_factory() as session:
        expired = await session.get(LavaPayment, expired_id)
        expired.expires_at = datetime.utcnow() - timedelta(minutes=1)
        current = LavaPayment(
            user_id=777,
            operation_type="deposit",
            provider_order_id="DS-current",
            provider_invoice_id="invoice-current",
            status=LAVA_PENDING,
            amount_usdt=Decimal("1.00"),
            base_amount_rub=Decimal("100.00"),
            amount_rub=Decimal("103.40"),
            fee_percent=Decimal("3.4"),
            usdt_rub_rate=Decimal("100"),
            rate_source="platega",
            metadata_json='{"amount_usdt":"1.00"}',
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        )
        session.add(current)
        await session.commit()
        current_id = current.id

    processor = AsyncMock(
        return_value=LavaProcessResult(status=LAVA_PENDING, final=False)
    )
    with (
        patch(
            "src.services.lava_service.async_session_factory",
            lava_session_factory,
        ),
        patch("src.services.lava_service.process_lava_payment", processor),
    ):
        await process_pending_lava_payments(limit=1)

    processor.assert_awaited_once_with(current_id, bot=None)


def test_invoice_request_is_sbp_only_and_expires_in_30_minutes():
    request = build_lava_invoice_request(
        shop_id="shop-id",
        amount_rub=Decimal("103.40"),
        provider_order_id="DS-STA-1",
        return_url="https://t.me/test_bot?start=lava",
        ttl_minutes=30,
        payment_id=17,
        operation_type="stars",
        description="Stars purchase",
    )

    assert request["includeService"] == ["sbp"]
    assert request["expire"] == 30
    assert "hookUrl" not in request
    assert json.loads(request["customFields"]) == {
        "payment_id": 17,
        "operation": "stars",
    }


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("created", LAVA_PENDING),
        ("pending", LAVA_PENDING),
        ("success", LAVA_CONFIRMED),
        ("paid", LAVA_CONFIRMED),
        ("cancel", LAVA_CANCELED),
        ("error", LAVA_FAILED),
    ],
)
def test_lava_status_normalization(provider_status, expected):
    assert normalize_lava_status(provider_status) == expected


@pytest.mark.asyncio
async def test_lava_settings_include_all_encrypted_credentials_and_fee():
    with patch(
        "src.services.bot_settings_service.get_bot_settings",
        AsyncMock(
            return_value={
                "lava_enabled": "true",
                "lava_shop_id": "shop-id",
                "lava_secret_key": "secret-key",
                "lava_additional_key": "additional-key",
                "lava_poll_interval_seconds": "7",
                "payment_fee_lava": "3.4",
            }
        ),
    ):
        settings = await get_lava_settings()

    assert settings["enabled"] is True
    assert settings["configured"] is True
    assert settings["shop_id"] == "shop-id"
    assert settings["secret_key"] == "secret-key"
    assert settings["additional_key"] == "additional-key"
    assert settings["poll_interval_seconds"] == 7
    assert settings["fee_percent"] == Decimal("3.4")


class _FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return '{"data":{"status":"created"},"status":200,"status_check":true}'


class _FakeClientSession:
    def __init__(self, capture, **_kwargs):
        self.capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, *, data, headers):
        self.capture.update({"url": url, "data": data, "headers": headers})
        return _FakeResponse()


@pytest.mark.asyncio
async def test_api_request_sends_the_same_bytes_that_were_signed(monkeypatch):
    capture = {}
    settings = {
        "enabled": True,
        "configured": True,
        "shop_id": "shop-id",
        "secret_key": "secret",
        "base_url": "https://api.lava.ru",
    }
    monkeypatch.setattr(
        "src.services.lava_service.aiohttp.ClientSession",
        lambda **kwargs: _FakeClientSession(capture, **kwargs),
    )
    monkeypatch.setattr(
        "src.services.lava_service.get_lava_settings",
        AsyncMock(return_value=settings),
    )
    payload = {"shopId": "shop-id", "sum": 103.4, "orderId": "DS-1"}

    await _request_lava("/business/invoice/status", payload, require_enabled=False)

    assert capture["data"] == serialize_lava_payload(payload)
    assert capture["headers"]["Signature"] == sign_lava_payload(
        capture["data"], "secret"
    )


def test_confirmed_invoice_identity_and_amount_are_validated():
    payment = SimpleNamespace(
        provider_order_id="DS-1",
        amount_rub=Decimal("103.40"),
    )
    _validate_invoice(
        payment,
        {
            "order_id": "DS-1",
            "shop_id": "shop-id",
            "amount": 103.4,
        },
        require_identity=True,
        expected_shop_id="shop-id",
    )

    with pytest.raises(LavaValidationError):
        _validate_invoice(
            payment,
            {
                "order_id": "DS-1",
                "shop_id": "shop-id",
                "amount": 103.41,
            },
            require_identity=True,
            expected_shop_id="shop-id",
        )


def test_lava_button_is_hidden_until_enabled_and_configured():
    assert StarsCallback.PAY_LAVA not in _callbacks(stars_methods("ru"))
    assert DepositCallback.PAY_LAVA not in _callbacks(deposit_methods("ru"))
    assert PremiumCallback.PAY_LAVA not in _callbacks(
        get_premium_payment_method_keyboard("ru")
    )


def test_payment_method_layout_prioritizes_sbp_and_hides_platega():
    fee = Decimal("3.4000")
    stars_keyboard = stars_methods("ru", lava_enabled=True, lava_fee_percent=fee)
    premium_keyboard = get_premium_payment_method_keyboard(
        "ru", lava_enabled=True, lava_fee_percent=fee
    )
    deposit_keyboard = deposit_methods("ru", lava_enabled=True, lava_fee_percent=fee)

    assert _callback_rows(stars_keyboard)[:3] == [
        [StarsCallback.PAY_LAVA],
        [StarsCallback.PAY_CRYPTOBOT, StarsCallback.PAY_TON],
        [StarsCallback.PAY_BALANCE],
    ]
    assert _callback_rows(premium_keyboard)[:3] == [
        [PremiumCallback.PAY_LAVA],
        [PremiumCallback.PAY_CRYPTOBOT, PremiumCallback.PAY_TON],
        [PremiumCallback.PAY_BALANCE],
    ]
    assert _callback_rows(deposit_keyboard)[:2] == [
        [DepositCallback.PAY_LAVA],
        [DepositCallback.PAY_CRYPTOBOT, DepositCallback.PAY_TON],
    ]
    assert StarsCallback.PAY_PLATEGA_SBP not in _callbacks(stars_keyboard)
    assert PremiumCallback.PAY_PLATEGA_SBP not in _callbacks(premium_keyboard)
    assert DepositCallback.PAY_PLATEGA_SBP not in _callbacks(deposit_keyboard)

    sbp_button = stars_keyboard.inline_keyboard[0][0]
    assert sbp_button.text == "🏦 СБП (+3.4%)"
    assert "3.4000" not in sbp_button.text


def test_lava_payment_text_uses_sbp_title_and_compact_fee():
    text = build_lava_payment_text(
        lang="ru",
        item_line="Покупка",
        amount_usdt=Decimal("1.00"),
        base_amount_rub=Decimal("100.00"),
        amount_rub=Decimal("103.40"),
        fee_percent=Decimal("3.4000"),
        ttl_minutes=30,
    )

    assert "Оплата через СБП" in text
    assert "Комиссия: <b>3.4%</b>" in text
    assert "3.4000%" not in text

    assert StarsCallback.PAY_LAVA in _callbacks(
        stars_methods("ru", lava_enabled=True, lava_fee_percent=Decimal("3.4"))
    )
    assert DepositCallback.PAY_LAVA in _callbacks(
        deposit_methods("ru", lava_enabled=True, lava_fee_percent=Decimal("3.4"))
    )
    assert PremiumCallback.PAY_LAVA in _callbacks(
        get_premium_payment_method_keyboard(
            "ru", lava_enabled=True, lava_fee_percent=Decimal("3.4")
        )
    )


@pytest.mark.parametrize(
    ("keyboard", "check_callback"),
    [
        (get_stars_lava_payment_keyboard(None, "ru"), StarsCallback.CHECK_LAVA_PAYMENT),
        (
            get_premium_lava_payment_keyboard(None, "ru"),
            PremiumCallback.CHECK_LAVA_PAYMENT,
        ),
        (get_lava_payment_keyboard(None, "ru"), DepositCallback.CHECK_LAVA_PAYMENT),
    ],
)
def test_ambiguous_invoice_keyboard_keeps_manual_check_without_payment_url(
    keyboard,
    check_callback,
):
    assert check_callback in _callbacks(keyboard)
    assert all(button.url is None for row in keyboard.inline_keyboard for button in row)


def test_lava_admin_keyboard_exposes_settings_without_connection_test():
    callbacks = _callbacks(get_settings_lava_keyboard(enabled=False))
    assert {
        "admin:settings:lava:toggle",
        "admin:settings:edit:lava_shop_id",
        "admin:settings:edit:lava_secret_key",
        "admin:settings:edit:lava_additional_key",
        "admin:settings:edit:lava_poll_interval_seconds",
        "admin:settings:edit:payment_fee_lava",
    }.issubset(callbacks)
    assert "admin:settings:lava:test" not in callbacks
