"""
Handlers для пунктов главного меню.
"""
import logging
from typing import Optional

from aiogram import F, Router
from aiogram.types import CallbackQuery

from src.bot.legal import get_legal_links_text
from src.bot.keyboards.menu import MenuCallback, get_back_button, get_support_keyboard
from src.db.session import async_session_factory
from src.locales import t, get_user_locale
from src.services.user_service import UserService
from src.bot.menu_media import edit_menu_message

logger = logging.getLogger(__name__)

# Кэш username бота (заполняется при первом запросе)
_bot_username_cache: Optional[str] = None

router = Router(name="menu")


@router.callback_query(F.data == MenuCallback.REFERRAL)
async def callback_referral(callback: CallbackQuery) -> None:
    """Реферальная система."""
    global _bot_username_cache

    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            lang = get_user_locale(user.language_code)
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        lang = db_user.language_code or get_user_locale(user.language_code)
        if await user_service.normalize_referral_code(db_user):
            await session.commit()

        # Получаем полную статистику рефералов
        ref_stats = await user_service.get_referral_stats(user.id)

        # Кэшируем username бота для избежания лишних API вызовов
        if _bot_username_cache is None:
            _bot_username_cache = (await callback.bot.get_me()).username

        referral_link = f"https://t.me/{_bot_username_cache}?start=ref_{db_user.referral_code}"

        # Безопасное получение значений с дефолтами
        total_earnings = ref_stats.get('total_earnings', 0) or 0
        earnings_formatted = f"{total_earnings:,.2f}"

        text = (
            f"{t('referrals.title', lang)}\n"
            f"{t('referrals.description', lang)}\n\n"
            f"{t('referrals.your_link', lang)}\n"
            f"<code>{referral_link}</code>\n\n"
            f"{t('referrals.stats', lang, level1=ref_stats.get('level_1', 0), level2=ref_stats.get('level_2', 0), level3=ref_stats.get('level_3', 0), earnings=earnings_formatted)}"
        )

        try:
            await edit_menu_message(
                callback,
                "referral",
                text=text,
                reply_markup=get_back_button(lang),
            )
        except Exception as e:
            logger.debug(f"Failed to edit referral message: {e}")

    await callback.answer()


@router.callback_query(F.data == MenuCallback.SUPPORT)
async def callback_support(callback: CallbackQuery) -> None:
    """Поддержка."""
    from src.services.bot_settings_service import get_bot_settings

    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        lang = (
            db_user.language_code
            if db_user
            else get_user_locale(user.language_code)
        )

    # Получаем username поддержки из настроек
    bot_settings = await get_bot_settings()
    support_username = bot_settings.get("support_username") or "support"

    text = f"{t('support.title', lang)}\n\n{t('support.text', lang)}\n\n{get_legal_links_text(lang)}"

    try:
        await edit_menu_message(
            callback,
            "support",
            text=text,
            reply_markup=get_support_keyboard(support_username, lang),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.debug(f"Failed to edit support message: {e}")

    await callback.answer()
