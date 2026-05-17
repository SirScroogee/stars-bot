"""
Клавиатуры профиля.
"""
from datetime import datetime, timezone, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.menu import get_back_button_row
from src.locales import t

# Московский часовой пояс (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))


def format_date_msk(dt: datetime, fmt: str = "%d.%m.%Y") -> str:
    """Форматировать дату в московское время."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK_TZ).strftime(fmt)


def format_datetime_short_msk(dt: datetime) -> str:
    """Форматировать дату и время коротко: 24.01 12:28."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK_TZ).strftime("%d.%m %H:%M")


class ProfileCallback:
    """Callback data для профиля."""

    REFERRALS = "profile:referrals"
    REFERRALS_LIST = "profile:ref_list"
    REFERRAL_WITHDRAW = "profile:ref_withdraw"
    HISTORY = "profile:history"
    HISTORY_PAGE = "profile:history:"  # profile:history:{page}
    ORDER_VIEW = "profile:order:"  # profile:order:{order_id}
    TX_VIEW = "profile:tx:"  # profile:tx:{tx_id}
    PROMO = "profile:promo"
    LANGUAGE = "profile:language"
    LANG_RU = "profile:lang:ru"
    LANG_EN = "profile:lang:en"
    BACK = "profile:back"


def get_profile_keyboard(lang: str = "ru", has_referral_balance: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура профиля."""
    buttons = [
        [
            InlineKeyboardButton(
                text=t("profile.buttons.history", lang),
                callback_data=ProfileCallback.HISTORY,
            ),
            InlineKeyboardButton(
                text=t("profile.buttons.promo", lang),
                callback_data=ProfileCallback.PROMO,
            ),
        ],
        [
            InlineKeyboardButton(
                text=t("profile.buttons.referrals", lang),
                callback_data=ProfileCallback.REFERRALS,
            ),
        ],
    ]

    # Кнопка вывода реферального баланса (если есть баланс)
    if has_referral_balance:
        buttons.append([
            InlineKeyboardButton(
                text=t("profile.buttons.withdraw_referral", lang),
                callback_data=ProfileCallback.REFERRAL_WITHDRAW,
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text=t("profile.buttons.language", lang),
            callback_data=ProfileCallback.LANGUAGE,
        ),
    ])
    buttons.append(get_back_button_row(lang))

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_referrals_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура рефералов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("referrals.buttons.my_referrals", lang),
                    callback_data=ProfileCallback.REFERRALS_LIST,
                ),
                InlineKeyboardButton(
                    text=t("referrals.buttons.withdraw", lang),
                    callback_data=ProfileCallback.REFERRAL_WITHDRAW,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ProfileCallback.BACK,
                ),
            ],
        ]
    )


def get_back_to_referrals_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой назад к рефералам."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ProfileCallback.REFERRALS,
                ),
            ],
        ]
    )


def get_language_keyboard(current_lang: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора языка."""
    ru_mark = " ✓" if current_lang == "ru" else ""
    en_mark = " ✓" if current_lang == "en" else ""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{t('language.buttons.ru', current_lang)}{ru_mark}",
                    callback_data=ProfileCallback.LANG_RU,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{t('language.buttons.en', current_lang)}{en_mark}",
                    callback_data=ProfileCallback.LANG_EN,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", current_lang),
                    callback_data=ProfileCallback.BACK,
                ),
            ],
        ]
    )


def get_promo_back_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой назад для промокодов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ProfileCallback.BACK,
                ),
            ],
        ]
    )


# Маппинг статусов на эмодзи
STATUS_EMOJIS = {
    "pending": "🕐",
    "processing": "⏳",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "🚫",
    "refunded": "↩️",
}

# Маппинг типов транзакций на эмодзи
TRANSACTION_EMOJIS = {
    "deposit": "💳",
    "purchase": "🛒",
    "referral": "👥",
    "promo": "🎁",
    "check_created": "🎟",
    "check_activated": "✅",
    "withdrawal": "📤",
}


def get_history_keyboard(
    orders: list,
    page: int = 0,
    total_count: int = 0,
    page_size: int = 5,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    """
    Клавиатура списка заказов.

    Args:
        orders: Список заказов Order
        page: Текущая страница (0-based)
        total_count: Общее количество записей
        page_size: Размер страницы
        lang: Язык интерфейса
    """
    buttons = []

    # Единица измерения месяцев
    months_unit = "мес." if lang == "ru" else "mo."

    for order in orders:
        datetime_str = format_datetime_short_msk(order.created_at)
        status_emoji = STATUS_EMOJIS.get(order.status, "❓")

        # Формат: ✅ #ID | 118 ⭐ | 24.01 12:28
        # или:   ✅ #ID | 3 мес. 👑 | 24.01 12:28
        if order.product_type == "stars":
            button_text = f"{status_emoji} #{order.order_key} | {order.quantity} ⭐ | {datetime_str}"
        else:
            button_text = f"{status_emoji} #{order.order_key} | {order.quantity} {months_unit} 👑 | {datetime_str}"

        callback = f"{ProfileCallback.ORDER_VIEW}{order.id}"
        buttons.append([
            InlineKeyboardButton(text=button_text, callback_data=callback)
        ])

    # Пагинация в одну строку: < | 1/2 | >
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

    if total_pages > 1:
        nav_buttons = []

        # Кнопка "назад"
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=f"{ProfileCallback.HISTORY_PAGE}{page - 1}",
                )
            )

        # Номер страницы
        nav_buttons.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="noop",  # Ничего не делает
            )
        )

        # Кнопка "вперёд"
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=f"{ProfileCallback.HISTORY_PAGE}{page + 1}",
                )
            )

        buttons.append(nav_buttons)

    # Кнопка назад к профилю
    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=ProfileCallback.BACK,
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_order_detail_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура просмотра заказа."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ProfileCallback.HISTORY,
                ),
            ],
        ]
    )


def get_transaction_detail_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура просмотра транзакции."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ProfileCallback.HISTORY,
                ),
            ],
        ]
    )