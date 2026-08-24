"""Send and immediately delete a probe in the configured registration-log topic."""
import asyncio
import os

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from src.config import get_config
from src.services.log_settings_service import get_log_settings


async def main() -> None:
    settings = await get_log_settings()
    users_topic = settings.get("topics", {}).get("users", {})
    if not settings.get("enabled", True):
        raise RuntimeError("Telegram logging is disabled")
    if not users_topic.get("enabled", True):
        raise RuntimeError("Users log topic is disabled")
    if not settings.get("events", {}).get("user_registered", True):
        raise RuntimeError("Registration logging event is disabled")

    group_id = settings.get("group_id")
    topic_id = users_topic.get("id")
    if not group_id or not topic_id:
        raise RuntimeError("Registration log destination is incomplete")

    proxy = os.getenv("TELEGRAM_PROXY")
    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    bot = Bot(token=get_config().bot_token, session=session)
    try:
        message = await bot.send_message(
            chat_id=group_id,
            message_thread_id=topic_id,
            text="Registration log delivery audit probe",
        )
        await bot.delete_message(chat_id=group_id, message_id=message.message_id)
    finally:
        await bot.session.close()
    print("REGISTRATION_LOG_DELIVERY_OK")


if __name__ == "__main__":
    asyncio.run(main())
