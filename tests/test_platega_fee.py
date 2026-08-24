"""Tests for the configurable Platega customer fee."""
import json
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.bot.keyboards.admin import get_settings_platega_keyboard
from src.services.bot_settings_service import get_platega_settings
from src.services.platega_service import build_platega_payment_text, create_platega_payment


class CreatePaymentSession:
    def __init__(self):
        self.add = Mock()
        self.flush = AsyncMock()


@pytest.mark.asyncio
async def test_platega_settings_return_configured_fee():
    with patch(
        "src.services.bot_settings_service.get_bot_settings",
        AsyncMock(
            return_value={
                "platega_enabled": "true",
                "payment_fee_platega": "2.5",
            }
        ),
    ):
        settings = await get_platega_settings()

    assert settings["fee_percent"] == Decimal("2.5")


@pytest.mark.asyncio
async def test_created_invoice_uses_total_rub_amount_with_configured_fee():
    session = CreatePaymentSession()
    provider_request = AsyncMock(
        return_value={
            "transactionId": "platega-test-id",
            "redirect": "https://pay.platega.io/test",
        }
    )
    settings = {
        "enabled": True,
        "merchant_id": "merchant",
        "secret": "secret",
        "base_url": "https://app.platega.io",
        "sbp_method_id": 2,
        "poll_interval_seconds": 5,
        "payment_ttl_minutes": 30,
        "fee_percent": Decimal("2.5"),
    }

    with (
        patch(
            "src.services.platega_service.get_platega_settings",
            AsyncMock(return_value=settings),
        ),
        patch(
            "src.services.platega_service._get_sbp_usdt_rub_rate",
            AsyncMock(return_value=(Decimal("90"), "platega")),
        ),
        patch("src.services.platega_service._request_platega", provider_request),
    ):
        created = await create_platega_payment(
            session,
            user_id=42,
            operation_type="stars",
            amount_usdt=Decimal("10"),
            description="Test payment",
            metadata={"quantity": 500},
        )

    request_body = provider_request.await_args.kwargs["json_body"]
    assert request_body["paymentDetails"] == {
        "amount": 922.5,
        "currency": "RUB",
    }
    assert created.amount_rub == Decimal("900.00")
    assert created.amount_with_fee_rub == Decimal("922.50")
    assert created.payment.amount_rub == Decimal("922.50")
    assert created.payment.fee_percent == Decimal("2.5")
    metadata = json.loads(created.payment.metadata_json)
    assert metadata["sbp_base_amount_rub"] == "900.00"
    assert metadata["sbp_display_amount_rub_with_fee"] == "922.50"


def test_platega_settings_keyboard_has_fee_button():
    keyboard = get_settings_platega_keyboard(enabled=True)
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }
    assert "admin:settings:edit:payment_fee_platega" in callbacks


def test_payment_text_shows_fee_and_total_separately():
    text = build_platega_payment_text(
        title="СБП",
        item_line="500 звезд",
        amount_usdt=Decimal("10"),
        amount_rub=Decimal("922.50"),
        fee_percent=Decimal("2.5"),
        ttl_minutes=30,
    )

    assert "Сумма покупки: <b>10.00 USDT</b>" in text
    assert "Комиссия СБП: <b>2.5%</b>" in text
    assert "К оплате: <b>922.50 RUB</b>" in text
