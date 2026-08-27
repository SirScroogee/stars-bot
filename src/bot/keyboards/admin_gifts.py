"""Inline keyboards for the administrator Telegram Gifts wizard."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
)

from src.bot.keyboards.admin import AdminCallback


CATALOG_PAGE_SIZE = 6
ARCHIVED_GIFT_BUTTON_TITLE_LIMIT = 32


def _archived_gift_button_title(value: str) -> str:
    if len(value) <= ARCHIVED_GIFT_BUTTON_TITLE_LIMIT:
        return value
    return value[: ARCHIVED_GIFT_BUTTON_TITLE_LIMIT - 1].rstrip() + "…"


def admin_gift_search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="admin:gifts:cancel",
                )
            ]
        ]
    )


def admin_gift_recipient_picker_keyboard(request_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="👤 Выбрать пользователя",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=request_id,
                        user_is_bot=False,
                        max_quantity=1,
                        request_name=True,
                        request_username=True,
                    ),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        selective=True,
        input_field_placeholder="Выберите пользователя или отправьте ID",
    )


def admin_gift_catalog_keyboard(
    gifts: list[dict], page: int = 0
) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(gifts) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * CATALOG_PAGE_SIZE
    buttons: list[list[InlineKeyboardButton]] = []

    for index in range(start, min(start + CATALOG_PAGE_SIZE, len(gifts))):
        gift = gifts[index]
        emoji = gift.get("emoji") or "🎁"
        remaining = gift.get("remaining_count")
        limited = f" · осталось {remaining}" if remaining is not None else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} {gift['star_count']} ⭐{limited}",
                    callback_data=f"admin:gifts:select:{index}",
                )
            ]
        )

    if total_pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="◀",
                    callback_data=f"admin:gifts:page:{page - 1}",
                )
            )
        navigation.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="admin:gifts:nop",
            )
        )
        if page + 1 < total_pages:
            navigation.append(
                InlineKeyboardButton(
                    text="▶",
                    callback_data=f"admin:gifts:page:{page + 1}",
                )
            )
        buttons.append(navigation)

    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    text="🗃 Архивные подарки",
                    callback_data="admin:gifts:archive:choose",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить каталог",
                    callback_data="admin:gifts:catalog:refresh",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Другой получатель",
                    callback_data="admin:gifts:recipient",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="admin:gifts:cancel",
                ),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_gift_selected_keyboard(
    other_gift_callback: str = "admin:gifts:catalog",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Продолжить",
                    callback_data="admin:gifts:comment",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Другой подарок",
                    callback_data=other_gift_callback,
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="admin:gifts:cancel",
                ),
            ],
        ]
    )


def admin_gift_comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Без комментария",
                    callback_data="admin:gifts:comment:skip",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="admin:gifts:selected",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="admin:gifts:cancel",
                ),
            ],
        ]
    )


def admin_gift_confirm_keyboard(
    *,
    archived: bool = False,
    other_gift_callback: str = "admin:gifts:catalog",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "🎁 Отправить по архивному ID"
                        if archived
                        else "🎁 Отправить подарок"
                    ),
                    callback_data="admin:gifts:confirm",
                    style="danger" if archived else "success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить комментарий",
                    callback_data="admin:gifts:comment",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Другой подарок",
                    callback_data=other_gift_callback,
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="admin:gifts:cancel",
                ),
            ],
        ]
    )


def archived_gift_choose_keyboard(
    gifts: list,
    page: int = 0,
) -> InlineKeyboardMarkup:
    active = [gift for gift in gifts if gift.is_active]
    total_pages = max(1, (len(active) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * CATALOG_PAGE_SIZE
    buttons: list[list[InlineKeyboardButton]] = []

    for gift in active[start : start + CATALOG_PAGE_SIZE]:
        title = _archived_gift_button_title(gift.title)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{gift.emoji or '🎁'} {title} · {gift.star_count} ⭐",
                    callback_data=f"admin:gifts:archive:select:{gift.id}",
                )
            ]
        )

    if total_pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="◀",
                    callback_data=f"admin:gifts:archive:choose:page:{page - 1}",
                )
            )
        navigation.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="admin:gifts:nop",
            )
        )
        if page + 1 < total_pages:
            navigation.append(
                InlineKeyboardButton(
                    text="▶",
                    callback_data=f"admin:gifts:archive:choose:page:{page + 1}",
                )
            )
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Обычные подарки",
                callback_data="admin:gifts:catalog",
            ),
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="admin:gifts:cancel",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_gift_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 Подарить ещё",
                    callback_data=AdminCallback.GIFTS,
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ В админ-панель",
                    callback_data=AdminCallback.BACK,
                )
            ],
        ]
    )


def admin_gift_payment_wait_keyboard(attempt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату и отправить",
                    callback_data=f"admin:gifts:resume:{attempt_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ В админ-панель",
                    callback_data=AdminCallback.BACK,
                )
            ],
        ]
    )
