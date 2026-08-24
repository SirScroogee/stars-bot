"""Tests for successful payment logs."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services import order_runtime_service
from src.services.telegram_logger import TelegramLogger


@pytest.mark.asyncio
async def test_order_payment_log_contains_required_details():
    telegram_logger = TelegramLogger()
    with (
        patch.object(telegram_logger, "_send", AsyncMock(return_value=True)) as send,
        patch(
            "src.services.rub_rate_service.format_usdt_with_rub",
            AsyncMock(return_value="1.25 USDT (112.50 RUB)"),
        ),
    ):
        await telegram_logger.log_payment_completed(
            order_id=17,
            user_id=42,
            username="buyer",
            amount_usdt=Decimal("1.25"),
            provider="cryptobot",
            product_type="stars",
            quantity=100,
            recipient="recipient",
        )

    send.assert_awaited_once()
    topic, event, text = send.await_args.args
    assert (topic, event) == ("payments", "payment_completed")
    assert "Время (МСК)" in text
    assert "Покупка 100 Telegram Stars" in text
    assert "Заказ:</b> #17" in text
    assert "@buyer (<code>42</code>)" in text
    assert "@recipient" in text
    assert "1.25 USDT (112.50 RUB)" in text
    assert "CryptoBot" in text


@pytest.mark.asyncio
async def test_deposit_log_contains_rub_amount_and_provider_details():
    telegram_logger = TelegramLogger()
    with patch.object(telegram_logger, "_send", AsyncMock(return_value=True)) as send:
        await telegram_logger.log_deposit(
            user_id=43,
            username=None,
            amount=Decimal("5"),
            currency="USDT",
            provider="СБП (Platega)",
            amount_rub=Decimal("450.75"),
        )

    text = send.await_args.args[2]
    assert "Пополнение внутреннего баланса" in text
    assert "5.00 USDT (450.75 RUB)" in text
    assert "СБП (Platega)" in text


@pytest.mark.asyncio
async def test_deposit_log_distinguishes_paid_and_credited_amounts():
    telegram_logger = TelegramLogger()
    with (
        patch.object(telegram_logger, "_send", AsyncMock(return_value=True)) as send,
        patch(
            "src.services.rub_rate_service.format_usdt_with_rub",
            AsyncMock(return_value="5.25 USDT (472.50 RUB)"),
        ),
    ):
        await telegram_logger.log_deposit(
            user_id=46,
            username="depositor",
            amount=Decimal("5.00"),
            currency="USDT",
            provider="CryptoBot",
            paid_amount_usdt=Decimal("5.25"),
        )

    text = send.await_args.args[2]
    assert "Сумма:</b> 5.25 USDT (472.50 RUB)" in text
    assert "Зачислено на баланс:</b> +5.00 USDT" in text


@pytest.mark.asyncio
async def test_created_paid_order_emits_order_and_payment_logs(monkeypatch):
    order_log = AsyncMock()
    payment_log = AsyncMock()
    monkeypatch.setattr(order_runtime_service.tg_logger, "log_order_created", order_log)
    monkeypatch.setattr(order_runtime_service.tg_logger, "log_payment_completed", payment_log)
    order = SimpleNamespace(
        id=19,
        user_id=44,
        product_type="premium",
        quantity=6,
        price_usdt=Decimal("15.00"),
        payment_provider="ton",
        recipient_username="premium_user",
    )

    await order_runtime_service.log_created_order(
        order,
        "buyer",
        provider_amount="0.1234 TON",
    )

    order_log.assert_awaited_once()
    payment_log.assert_awaited_once_with(
        order_id=19,
        user_id=44,
        username="buyer",
        amount_usdt=Decimal("15.00"),
        amount_rub=None,
        provider="ton",
        product_type="premium",
        quantity=6,
        recipient="premium_user",
        provider_amount="0.1234 TON",
    )


@pytest.mark.asyncio
async def test_zero_price_inventory_order_is_not_logged_as_payment(monkeypatch):
    monkeypatch.setattr(
        order_runtime_service.tg_logger,
        "log_order_created",
        AsyncMock(),
    )
    payment_log = AsyncMock()
    monkeypatch.setattr(order_runtime_service.tg_logger, "log_payment_completed", payment_log)
    order = SimpleNamespace(
        id=20,
        user_id=45,
        product_type="stars",
        quantity=50,
        price_usdt=Decimal("0"),
        payment_provider="balance",
        recipient_username="inventory_user",
    )

    await order_runtime_service.log_created_order(order, "buyer")

    payment_log.assert_not_awaited()
