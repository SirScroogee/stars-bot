"""Helpers for optional photos attached to user-facing menus."""
import logging

from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from src.services.bot_settings_service import get_bot_settings

logger = logging.getLogger(__name__)

MENU_MEDIA_ITEMS = {
    "main": "Главное меню",
    "stars": "Покупка звезд",
    "premium": "Premium",
    "deposit": "Пополнение",
    "checks": "Чеки",
    "profile": "Профиль",
    "referral": "Реферальная система",
    "support": "Поддержка",
    "giveaways": "Розыгрыши",
}


def get_menu_media(settings: dict) -> dict:
    media = settings.get("menu_media") or {}
    return media if isinstance(media, dict) else {}


async def get_menu_photo(menu_key: str) -> str | None:
    settings = await get_bot_settings()
    photo = get_menu_media(settings).get(menu_key)
    return photo or None


async def answer_menu_message(
    message: Message,
    menu_key: str,
    text: str,
    reply_markup=None,
    disable_web_page_preview: bool = False,
) -> Message:
    photo = await get_menu_photo(menu_key)
    if photo:
        return await message.answer_photo(
            photo=photo,
            caption=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    return await message.answer(
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=disable_web_page_preview,
    )


async def edit_menu_message(
    callback: CallbackQuery,
    menu_key: str,
    text: str,
    reply_markup=None,
    disable_web_page_preview: bool = False,
) -> Message | None:
    photo = await get_menu_photo(menu_key)
    message = callback.message

    if photo:
        try:
            await message.edit_media(
                media=InputMediaPhoto(media=photo, caption=text, parse_mode="HTML"),
                reply_markup=reply_markup,
            )
            return message
        except Exception as e:
            logger.debug(f"Failed to edit menu media for {menu_key}: {e}")
            try:
                await message.delete()
            except Exception:
                pass
            return await callback.message.answer_photo(
                photo=photo,
                caption=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )

    try:
        await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=disable_web_page_preview,
        )
        return message
    except Exception as e:
        logger.debug(f"Failed to edit menu text for {menu_key}: {e}")
        try:
            await message.delete()
        except Exception:
            pass
        return await callback.message.answer(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=disable_web_page_preview,
        )
