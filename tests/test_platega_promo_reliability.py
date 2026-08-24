"""Focused reliability tests for Platega polling and promo result delivery."""
import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.bot.handlers.profile import deliver_promo_result
from src.services.platega_service import (
    PLATEGA_CONFIRMED,
    PlategaError,
    PlategaProcessResult,
    _edit_payment_message,
    _normalize_status,
    _request_platega,
    process_pending_platega_payments,
)


class FakeMessage:
    def __init__(self, bot):
        self.bot = bot
        self.chat = SimpleNamespace(id=101)
        self.from_user = SimpleNamespace(id=202)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, model, user_id):
        return SimpleNamespace(language_code="ru")


class PendingPaymentsSession(FakeSession):
    async def execute(self, statement):
        return SimpleNamespace(fetchall=lambda: [(1,), (2,), (3,)])


class TimeoutRequest:
    async def __aenter__(self):
        raise asyncio.TimeoutError

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TimeoutClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def request(self, *args, **kwargs):
        return TimeoutRequest()


class PromoDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_uses_caption_when_prompt_is_a_media_message(self):
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(side_effect=RuntimeError("no text")),
            edit_message_caption=AsyncMock(return_value=True),
            send_message=AsyncMock(return_value=True),
        )

        delivered = await deliver_promo_result(FakeMessage(bot), 303, "success", "ru")

        self.assertTrue(delivered)
        bot.edit_message_caption.assert_awaited_once()
        bot.send_message.assert_not_awaited()

    async def test_result_sends_new_message_when_prompt_cannot_be_edited(self):
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(side_effect=RuntimeError("no text")),
            edit_message_caption=AsyncMock(side_effect=RuntimeError("no caption")),
            send_message=AsyncMock(return_value=True),
        )

        delivered = await deliver_promo_result(FakeMessage(bot), 303, "success", "ru")

        self.assertTrue(delivered)
        bot.send_message.assert_awaited_once()


class PlategaReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_timeout_becomes_platega_error(self):
        settings = {
            "enabled": True,
            "merchant_id": "merchant",
            "secret": "secret",
            "base_url": "https://app.platega.io",
        }
        with (
            patch(
                "src.services.platega_service.get_platega_settings",
                new=AsyncMock(return_value=settings),
            ),
            patch(
                "src.services.platega_service.aiohttp.ClientSession",
                TimeoutClientSession,
            ),
        ):
            with self.assertRaises(PlategaError):
                await _request_platega("GET", "/transaction/test", timeout_seconds=0.1)

    async def test_payment_result_uses_caption_for_media_message(self):
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(side_effect=RuntimeError("no text")),
            edit_message_caption=AsyncMock(return_value=True),
            send_message=AsyncMock(return_value=True),
        )
        payment = SimpleNamespace(
            id=1,
            user_id=101,
            message_id=303,
            operation_type="deposit",
            amount_usdt=Decimal("10"),
            metadata_json="{}",
        )

        with patch(
            "src.services.platega_service.async_session_factory",
            side_effect=lambda: FakeSession(),
        ):
            await _edit_payment_message(bot, payment, PLATEGA_CONFIRMED)

        bot.edit_message_caption.assert_awaited_once()
        bot.send_message.assert_not_awaited()

    async def test_payment_result_sends_new_message_when_editing_fails(self):
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(side_effect=RuntimeError("no text")),
            edit_message_caption=AsyncMock(side_effect=RuntimeError("no caption")),
            send_message=AsyncMock(return_value=True),
        )
        payment = SimpleNamespace(
            id=1,
            user_id=101,
            message_id=303,
            operation_type="deposit",
            amount_usdt=Decimal("10"),
            metadata_json="{}",
        )

        with patch(
            "src.services.platega_service.async_session_factory",
            side_effect=lambda: FakeSession(),
        ):
            await _edit_payment_message(bot, payment, PLATEGA_CONFIRMED)

        bot.send_message.assert_awaited_once()

    async def test_pending_payments_are_checked_concurrently_and_isolated(self):
        active = 0
        max_active = 0

        async def process_payment(payment_id, *, bot=None):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            if payment_id == 2:
                raise RuntimeError("provider failure")
            return PlategaProcessResult(status=PLATEGA_CONFIRMED, final=True)

        with (
            patch(
                "src.services.platega_service.async_session_factory",
                side_effect=lambda: PendingPaymentsSession(),
            ),
            patch(
                "src.services.platega_service.process_platega_payment",
                side_effect=process_payment,
            ),
        ):
            processed = await process_pending_platega_payments()

        self.assertEqual(processed, 2)
        self.assertGreater(max_active, 1)

    def test_official_confirmed_status_is_normalized(self):
        self.assertEqual(_normalize_status("CONFIRMED"), PLATEGA_CONFIRMED)


if __name__ == "__main__":
    unittest.main()
