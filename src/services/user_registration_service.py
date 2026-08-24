"""Shared side effects that must run after a new user transaction is committed."""
from __future__ import annotations

import logging

from src.services.telegram_logger import tg_logger

logger = logging.getLogger(__name__)


async def finalize_new_user_registration(
    *,
    user_id: int,
    username: str | None,
    language: str,
    referrer_code: str | None = None,
) -> None:
    """Account for registration giveaways and emit the registration audit log."""
    try:
        from src.services.giveaway_service import process_registration_for_giveaways

        await process_registration_for_giveaways(user_id)
    except Exception:
        logger.exception("Failed to register user %s in active giveaways", user_id)

    try:
        delivered = await tg_logger.log_user_registered(
            user_id=user_id,
            username=username,
            language=language,
            referrer_code=referrer_code,
        )
        if not delivered:
            logger.warning("Registration log was not delivered for user %s", user_id)
    except Exception:
        logger.exception("Failed to log registration for user %s", user_id)
