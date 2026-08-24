"""Background poller for Lava payments."""
import asyncio
import logging
from time import monotonic

from aiogram import Bot

from src.services.bot_settings_service import get_lava_settings
from src.services.lava_service import process_pending_lava_payments

logger = logging.getLogger(__name__)


class LavaPaymentPoller:
    def __init__(self, bot: Bot):
        self._bot = bot
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="lava-payment-poller")
        logger.info("Lava payment poller started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Lava payment poller stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            cycle_started_at = monotonic()
            interval = 5
            try:
                settings = await get_lava_settings()
                interval = settings["poll_interval_seconds"]
                # Disabling Lava hides it from new purchases. Existing invoices
                # must still be confirmed while credentials remain configured.
                if settings["configured"]:
                    processed = await process_pending_lava_payments(self._bot)
                    if processed:
                        logger.info("Lava poller processed %s final payments", processed)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Lava poller error: %s", error, exc_info=True)
                interval = 5

            remaining_delay = max(0.1, interval - (monotonic() - cycle_started_at))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=remaining_delay)
            except asyncio.TimeoutError:
                pass
