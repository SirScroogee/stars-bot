"""
Клавиатуры главного меню.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.locales import t


class MenuCallback:
    """Callback data для меню."""

    BUY_STARS = "menu:buy_stars"
    BUY_PREMIUM = "menu:buy_premium"
    DEPOSIT = "menu:deposit"
    CHECKS = "menu:checks"
    PROFILE = "menu:profile"
    REFERRAL = "menu:referral"
    SUPPORT = "menu:support"
    BACK_TO_MENU = "menu:main"


def get_main_menu_keyboard(
    lang: str = "ru",
    news_channel_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Получить клавиатуру главного меню."""
    service_buttons = []
    if news_channel_url:
        service_buttons.append(
            InlineKeyboardButton(
                text=t("menu.buttons.news", lang),
                url=news_channel_url,
            )
        )
    service_buttons.append(
        InlineKeyboardButton(
            text=t("menu.buttons.support", lang),
            callback_data=MenuCallback.SUPPORT,
        )
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            # Строка 1: Купить звёзды | Купить Premium
            [
                InlineKeyboardButton(
                    text=t("menu.buttons.stars", lang),
                    callback_data=MenuCallback.BUY_STARS,
                    style="success",
                ),
                InlineKeyboardButton(
                    text=t("menu.buttons.premium", lang),
                    callback_data=MenuCallback.BUY_PREMIUM,
                    style="success",
                ),
            ],
            # Строка 2: Пополнить баланс
            [
                InlineKeyboardButton(
                    text=t("menu.buttons.deposit", lang),
                    callback_data=MenuCallback.DEPOSIT,
                    style="primary",
                ),
            ],
            # Строка 3: Чеки | Профиль
            [
                InlineKeyboardButton(
                    text=t("menu.buttons.checks", lang),
                    callback_data=MenuCallback.CHECKS,
                ),
                InlineKeyboardButton(
                    text=t("menu.buttons.profile", lang),
                    callback_data=MenuCallback.PROFILE,
                ),
            ],
            # Строка 4: Новости | Поддержка
            service_buttons,
        ]
    )


def get_back_button(lang: str = "ru") -> InlineKeyboardMarkup:
    """Кнопка 'Назад' в главное меню."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=MenuCallback.BACK_TO_MENU,
                ),
            ],
        ]
    )


def get_back_button_row(lang: str = "ru") -> list[InlineKeyboardButton]:
    """Кнопка 'Назад' как ряд для добавления в другие клавиатуры."""
    return [
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=MenuCallback.BACK_TO_MENU,
        ),
    ]


def get_support_keyboard(support_username: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура поддержки с кнопкой связи."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("support.contact_btn", lang),
                    url=f"https://t.me/{support_username}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=MenuCallback.BACK_TO_MENU,
                ),
            ],
        ]
    )
