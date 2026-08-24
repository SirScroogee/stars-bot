"""Reliability tests for new-user side effects and Telegram audit logs."""
import unittest
from unittest.mock import AsyncMock, patch

from src.services.telegram_logger import TelegramLogger, tg_logger
from src.services.user_registration_service import finalize_new_user_registration


class RegistrationFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_runs_giveaway_accounting_and_log(self):
        with (
            patch(
                "src.services.giveaway_service.process_registration_for_giveaways",
                AsyncMock(return_value=[]),
            ) as giveaway_hook,
            patch.object(tg_logger, "log_user_registered", AsyncMock(return_value=True)) as log_hook,
        ):
            await finalize_new_user_registration(
                user_id=42,
                username="new_user",
                language="ru",
                referrer_code="ABCDEFG",
            )

        giveaway_hook.assert_awaited_once_with(42)
        log_hook.assert_awaited_once_with(
            user_id=42,
            username="new_user",
            language="ru",
            referrer_code="ABCDEFG",
        )

    async def test_log_still_runs_if_giveaway_accounting_fails(self):
        with (
            patch(
                "src.services.giveaway_service.process_registration_for_giveaways",
                AsyncMock(side_effect=RuntimeError("database error")),
            ),
            patch.object(tg_logger, "log_user_registered", AsyncMock(return_value=True)) as log_hook,
        ):
            await finalize_new_user_registration(
                user_id=43,
                username=None,
                language="en",
            )

        log_hook.assert_awaited_once()


class TelegramLoggerRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_fields_are_html_escaped(self):
        logger = TelegramLogger()
        with patch.object(logger, "_send", AsyncMock(return_value=True)) as send:
            delivered = await logger.log_user_registered(
                user_id=42,
                username="safe_name",
                language="<ru>",
                referrer_code="</code>",
            )

        self.assertTrue(delivered)
        text = send.await_args.args[2]
        self.assertIn("&lt;ru&gt;", text)
        self.assertIn("&lt;/code&gt;", text)

    async def test_transient_send_failure_is_retried(self):
        bot = type("FakeBot", (), {})()
        bot.send_message = AsyncMock(side_effect=[RuntimeError("temporary"), object()])
        TelegramLogger.set_bot(bot)
        logger = TelegramLogger()

        with (
            patch.object(logger, "_is_enabled", AsyncMock(return_value=True)),
            patch.object(logger, "_get_group_id", AsyncMock(return_value=-1001)),
            patch.object(logger, "_get_topic_id", AsyncMock(return_value=19)),
            patch("src.services.telegram_logger.asyncio.sleep", AsyncMock()) as sleep,
        ):
            delivered = await logger._send("users", "user_registered", "test")

        self.assertTrue(delivered)
        self.assertEqual(bot.send_message.await_count, 2)
        sleep.assert_awaited_once()
        TelegramLogger._bot = None


if __name__ == "__main__":
    unittest.main()
