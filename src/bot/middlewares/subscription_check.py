"""
Middleware for a global required channel subscription.
"""
import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    Message,
    TelegramObject,
)

from src.config import get_config
from src.bot.handlers.start import get_welcome_message
from src.bot.keyboards.menu import get_main_menu_keyboard
from src.bot.legal import get_legal_links_text
from src.bot.menu_media import edit_menu_message
from src.db.session import async_session_factory
from src.locales import get_user_locale
from src.services.bot_settings_service import get_bot_settings
from src.services.user_service import UserService
from src.services.user_registration_service import finalize_new_user_registration
from src.services.giveaway_service import process_activity_for_giveaways

logger = logging.getLogger(__name__)

CHECK_CALLBACK = "subscription:check"
CHECK_TIMEOUT_SECONDS = 8.0


def _extract_username(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None

    if value.startswith("@"):
        return value[1:]

    match = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{5,})/?$", value)
    if match:
        return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9_]{5,}", value):
        return value

    return None


def _resolve_chat_id(value: str) -> str | int | None:
    value = value.strip()
    if not value:
        return None

    if re.fullmatch(r"-?\d+", value):
        return int(value)

    username = _extract_username(value)
    if username:
        return f"@{username}"

    return None


def _resolve_subscribe_url(channel: str, configured_url: str | None) -> str | None:
    if configured_url:
        configured_url = configured_url.strip()
        if configured_url:
            if configured_url.startswith(("http://", "https://")):
                return configured_url
            username = _extract_username(configured_url)
            if username:
                return f"https://t.me/{username}"

    username = _extract_username(channel)
    if username:
        return f"https://t.me/{username}"

    return None


def _subscription_keyboard(channel: str, url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text="Подписаться", url=url, style="primary")])
    rows.append([InlineKeyboardButton(text="Проверить подписку", callback_data=CHECK_CALLBACK, style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _subscription_text(url: str | None) -> str:
    if url:
        return (
            "Для работы с ботом нужно подписаться на обязательный канал.\n\n"
            "Подпишитесь и нажмите «Проверить подписку»."
        )
    return (
        "Для работы с ботом нужно подписаться на обязательный канал.\n\n"
        "Подпишитесь на канал и нажмите «Проверить подписку»."
    )


class SubscriptionCheckMiddleware(BaseMiddleware):
    """
    Blocks bot actions until the user is subscribed to the configured channel.

    Settings:
    - required_subscription_channel: @username, username or numeric channel id.
    - required_subscription_url: optional public/invite URL for the subscribe button.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # A successful payment has already charged the user and must always reach
        # its financial handler, even if subscription state changed meanwhile.
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)

        user_id = self._get_user_id(event)
        if not user_id:
            return await handler(event, data)

        is_check_callback = isinstance(event, CallbackQuery) and event.data == CHECK_CALLBACK
        if is_check_callback:
            logger.info("Required subscription check requested by user %s", user_id)
            try:
                await event.answer("Проверяю подписку...")
            except Exception:
                logger.debug("Failed to answer subscription check callback", exc_info=True)

        if self._is_admin(user_id):
            if is_check_callback:
                logger.info("Required subscription check bypassed for admin user %s", user_id)
                await self._show_main_menu(event)
                return None
            return await handler(event, data)

        settings = await get_bot_settings()
        channel = str(settings.get("required_subscription_channel") or "").strip()
        if not channel:
            if is_check_callback:
                await self._show_main_menu(event)
                return None
            return await handler(event, data)

        chat_id = _resolve_chat_id(channel)
        subscribe_url = _resolve_subscribe_url(
            channel,
            str(settings.get("required_subscription_url") or "").strip(),
        )

        if not chat_id:
            logger.error("required_subscription_channel is not a valid @username or chat id: %s", channel)
            return await self._block(event, channel, subscribe_url)

        bot = data["bot"]
        try:
            member = await asyncio.wait_for(
                bot.get_chat_member(chat_id=chat_id, user_id=user_id),
                timeout=CHECK_TIMEOUT_SECONDS,
            )
            is_subscribed = member.status not in ("left", "kicked")
        except TimeoutError:
            logger.error(
                "Timed out checking required subscription for user %s in %s",
                user_id,
                chat_id,
            )
            is_subscribed = False
        except Exception as e:
            logger.error("Failed to check required subscription for user %s in %s: %s", user_id, chat_id, e)
            is_subscribed = False

        if is_subscribed:
            if is_check_callback:
                logger.info("Required subscription confirmed for user %s in %s", user_id, chat_id)
                await self._show_main_menu(event)
                return None
            return await handler(event, data)

        if is_check_callback:
            logger.info("Required subscription not confirmed for user %s in %s", user_id, chat_id)
        return await self._block(event, channel, subscribe_url, answer_callback=not is_check_callback)

    @staticmethod
    def _get_user_id(event: TelegramObject) -> int | None:
        if isinstance(event, Message):
            return event.from_user.id if event.from_user else None
        if isinstance(event, CallbackQuery):
            return event.from_user.id if event.from_user else None
        if isinstance(event, InlineQuery):
            return event.from_user.id if event.from_user else None
        return None

    @staticmethod
    def _is_admin(user_id: int) -> bool:
        admin_ids = get_config().admin_ids or []
        return user_id in admin_ids

    async def _block(
        self,
        event: TelegramObject,
        channel: str,
        url: str | None,
        answer_callback: bool = True,
    ) -> None:
        text = _subscription_text(url)
        keyboard = _subscription_keyboard(channel, url)

        if isinstance(event, Message):
            await event.answer(text, reply_markup=keyboard)
            return

        if isinstance(event, CallbackQuery):
            if answer_callback:
                await event.answer("Сначала подпишитесь на канал", show_alert=True)
            if event.message and hasattr(event.message, "answer"):
                try:
                    await event.message.edit_text(text, reply_markup=keyboard)
                except Exception as e:
                    if "message is not modified" in str(e).lower():
                        return
                    await event.message.answer(text, reply_markup=keyboard)
            return

        if isinstance(event, InlineQuery):
            await event.answer(
                results=[],
                cache_time=1,
                is_personal=True,
                switch_pm_text="Подпишитесь на канал",
                switch_pm_parameter="subscription",
            )

    async def _show_main_menu(self, callback: CallbackQuery) -> None:
        tg_user = callback.from_user
        lang = get_user_locale(tg_user.language_code)

        async with async_session_factory() as session:
            user_service = UserService(session)
            db_user, created = await user_service.get_or_create_user(
                user_id=tg_user.id,
                username=tg_user.username,
                language_code=lang,
            )
            await session.commit()
            if created:
                await finalize_new_user_registration(
                    user_id=tg_user.id,
                    username=tg_user.username,
                    language=db_user.language_code or lang,
                )
            else:
                # This callback is handled inside the subscription middleware,
                # so the regular activity middleware does not see it.
                try:
                    await process_activity_for_giveaways(tg_user.id)
                except Exception:
                    logger.exception(
                        "Failed to process giveaway activity after subscription check for user %s",
                        tg_user.id,
                    )
            from src.services.giveaway_service import GiveawayService
            active_giveaways = await GiveawayService(session).has_active_giveaways()

        lang = db_user.language_code or lang
        welcome_text = get_welcome_message(
            balance_usdt=db_user.balance_usdt,
            balance_stars=db_user.balance_stars,
            balance_premium_months=db_user.balance_premium_months,
            lang=lang,
        )
        bot_settings = await get_bot_settings()

        await edit_menu_message(
            callback,
            "main",
            text=f"{welcome_text}\n\n{get_legal_links_text(lang)}",
            reply_markup=get_main_menu_keyboard(
                lang,
                bot_settings.get("news_channel_url"),
                is_admin=db_user.is_admin,
                has_active_giveaways=active_giveaways,
            ),
            disable_web_page_preview=True,
        )
