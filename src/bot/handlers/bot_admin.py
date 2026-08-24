"""
Handler для отслеживания добавления/удаления бота из каналов.
"""
import logging

from aiogram import Router
from aiogram.types import ChatMemberUpdated
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, ADMINISTRATOR, KICKED, LEFT, MEMBER

from sqlalchemy import select

from src.db.models import BotChannel
from src.db.session import async_session_factory
from src.services.bot_settings_service import BotSettingsService, invalidate_bot_settings_cache

logger = logging.getLogger(__name__)

router = Router(name="bot_admin")


@router.my_chat_member(
    ChatMemberUpdatedFilter(member_status_changed=(IS_NOT_MEMBER | KICKED | LEFT | MEMBER) >> ADMINISTRATOR)
)
async def on_bot_promoted_to_admin(event: ChatMemberUpdated) -> None:
    """Бот был назначен администратором канала."""
    chat = event.chat

    # Только для каналов и супергрупп
    if chat.type not in ("channel", "supergroup"):
        return

    logger.info(f"Bot promoted to admin in {chat.type} '{chat.title}' (ID: {chat.id})")

    async with async_session_factory() as session:
        # Проверяем, есть ли уже запись
        result = await session.execute(
            select(BotChannel).where(BotChannel.channel_id == chat.id)
        )
        bot_channel = result.scalar_one_or_none()

        user_id = event.from_user.id if event.from_user else None

        if bot_channel:
            # Обновляем существующую запись (НЕ меняем added_by_user_id!)
            bot_channel.channel_username = chat.username
            bot_channel.channel_title = chat.title or str(chat.id)
            bot_channel.is_active = True
            # Если added_by_user_id был None, устанавливаем его
            if bot_channel.added_by_user_id is None and user_id:
                bot_channel.added_by_user_id = user_id
                logger.info(f"Assigned channel '{chat.title}' to user {user_id}")
        else:
            # Создаём новую запись
            bot_channel = BotChannel(
                channel_id=chat.id,
                channel_username=chat.username,
                channel_title=chat.title or str(chat.id),
                added_by_user_id=user_id,
                is_active=True,
            )
            session.add(bot_channel)

        await session.commit()
        logger.info(f"Saved channel '{chat.title}' to bot_channels")


@router.my_chat_member(
    ChatMemberUpdatedFilter(member_status_changed=ADMINISTRATOR >> (IS_NOT_MEMBER | KICKED | LEFT | MEMBER))
)
async def on_bot_demoted_from_admin(event: ChatMemberUpdated) -> None:
    """Бот был лишён прав администратора или удалён из канала."""
    chat = event.chat

    # Только для каналов и супергрупп
    if chat.type not in ("channel", "supergroup"):
        return

    logger.info(f"Bot demoted/removed from {chat.type} '{chat.title}' (ID: {chat.id})")

    async with async_session_factory() as session:
        result = await session.execute(
            select(BotChannel).where(BotChannel.channel_id == chat.id)
        )
        bot_channel = result.scalar_one_or_none()

        if bot_channel:
            bot_channel.is_active = False
            cleared_required_subscription = False
            settings_service = BotSettingsService(session)
            settings = await settings_service.get_settings()
            required_channel = str(settings.get("required_subscription_channel") or "")
            channel_values = {str(chat.id)}
            if chat.username:
                channel_values.add(f"@{chat.username}")
                channel_values.add(chat.username)

            if required_channel in channel_values:
                await settings_service.set_setting(
                    "required_subscription_channel",
                    "",
                    old_value=required_channel,
                )
                await settings_service.set_setting(
                    "required_subscription_url",
                    "",
                    old_value=str(settings.get("required_subscription_url") or ""),
                )
                cleared_required_subscription = True
                logger.warning(
                    "Cleared required subscription because bot was removed from active channel '%s'",
                    chat.title,
                )

            await session.commit()
            if cleared_required_subscription:
                invalidate_bot_settings_cache()
            logger.info(f"Deactivated channel '{chat.title}' in bot_channels")
