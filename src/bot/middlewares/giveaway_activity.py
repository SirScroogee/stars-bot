"""Record real user activity for activity-based giveaways."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject

from src.services.giveaway_service import process_activity_for_giveaways

logger = logging.getLogger(__name__)


class GiveawayActivityMiddleware(BaseMiddleware):
    """Add a user once when they perform an allowed action during a giveaway."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = self._get_user_id(event)
        if user_id:
            try:
                awards = await process_activity_for_giveaways(user_id)
                if awards:
                    logger.info(
                        "Recorded activity giveaway entries for user %s: %s",
                        user_id,
                        [award.giveaway_id for award in awards],
                    )
            except Exception:
                # Giveaway accounting must not make the rest of the bot unusable.
                logger.exception("Failed to process giveaway activity for user %s", user_id)
        return await handler(event, data)

    @staticmethod
    def _get_user_id(event: TelegramObject) -> int | None:
        if isinstance(event, (Message, CallbackQuery, InlineQuery)) and event.from_user:
            return event.from_user.id
        return None
