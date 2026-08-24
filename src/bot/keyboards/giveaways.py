"""User keyboards for giveaways."""
from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.menu import MenuCallback
from src.locales import t

MOSCOW_TZ = timezone(timedelta(hours=3))


def _deadline(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ).strftime("%d.%m %H:%M")


def get_giveaway_list_keyboard(giveaways: list, lang: str = "ru") -> InlineKeyboardMarkup:
    rows = []
    for giveaway in giveaways:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(
                        "giveaways.list.item",
                        lang,
                        deadline=_deadline(giveaway.ends_at),
                    ),
                    callback_data=f"giveaway:view:{giveaway.id}",
                    style="danger",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=t("giveaways.buttons.back", lang),
                callback_data=MenuCallback.BACK_TO_MENU,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_giveaway_detail_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("giveaways.buttons.buy_stars", lang),
                    callback_data=MenuCallback.BUY_STARS,
                    style="success",
                ),
                InlineKeyboardButton(
                    text=t("giveaways.buttons.buy_premium", lang),
                    callback_data=MenuCallback.BUY_PREMIUM,
                    style="success",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("giveaways.buttons.back_to_list", lang),
                    callback_data=MenuCallback.GIVEAWAYS,
                )
            ],
        ]
    )
