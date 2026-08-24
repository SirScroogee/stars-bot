"""
Клавиатуры для раздела покупки Premium.
"""
from decimal import Decimal, ROUND_HALF_UP

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.menu import MenuCallback
from src.locales import t
from src.utils import format_decimal_compact


class PremiumCallback:
    """Callback data для раздела Premium."""

    # Главное меню Premium
    BUY = "premium:buy"
    WITHDRAW = "premium:withdraw"
    CALCULATOR = "premium:calculator"

    # Выбор получателя
    RECIPIENT_SELF = "premium:recipient:self"

    # Выбор срока
    DURATION_3 = "premium:duration:3"
    DURATION_6 = "premium:duration:6"
    DURATION_12 = "premium:duration:12"

    # Способы оплаты
    PAY_BALANCE = "premium:pay:balance"
    PAY_CRYPTOBOT = "premium:pay:cryptobot"
    PAY_TON = "premium:pay:ton"
    PAY_PLATEGA_SBP = "premium:pay:platega_sbp"
    PAY_LAVA = "premium:pay:lava"

    # Проверка оплаты
    CHECK_PAYMENT = "premium:check"
    CHECK_TON_PAYMENT = "premium:check:ton"
    CHECK_PLATEGA_PAYMENT = "premium:check:platega"
    CHECK_LAVA_PAYMENT = "premium:check:lava"
    CANCEL_PAYMENT = "premium:cancel"

    # Подтверждение
    CONFIRM = "premium:confirm"

    # Навигация
    BACK_TO_PREMIUM = "premium:back"
    BACK_TO_RECIPIENT = "premium:back:recipient"
    BACK_TO_DURATION = "premium:back:duration"
    BACK_TO_PAYMENT = "premium:back:payment"


def get_premium_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Главное меню раздела Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("premium_section.menu.buy_btn", lang),
                    callback_data=PremiumCallback.BUY,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("premium_section.menu.withdraw_btn", lang),
                    callback_data=PremiumCallback.WITHDRAW,
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


def get_premium_recipient_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора получателя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.recipient.self", lang),
                    callback_data=PremiumCallback.RECIPIENT_SELF,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=PremiumCallback.BACK_TO_PREMIUM,
                ),
            ],
        ]
    )


def _duration_button_text(
    lang: str,
    months: int,
    prices: dict[int, Decimal] | None,
    usdt_rub_rate: Decimal | None,
) -> str:
    base_text = t(f"premium_section.duration.months_{months}", lang)
    if prices and usdt_rub_rate and months in prices:
        rub_amount = (prices[months] * usdt_rub_rate).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        if rub_amount > 0:
            return f"{base_text} ({rub_amount:,.2f} RUB)"
    return base_text


def get_duration_keyboard(
    lang: str = "ru",
    prices: dict[int, Decimal] | None = None,
    usdt_rub_rate: Decimal | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура выбора срока Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_duration_button_text(lang, 3, prices, usdt_rub_rate),
                    callback_data=PremiumCallback.DURATION_3,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_duration_button_text(lang, 6, prices, usdt_rub_rate),
                    callback_data=PremiumCallback.DURATION_6,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_duration_button_text(lang, 12, prices, usdt_rub_rate),
                    callback_data=PremiumCallback.DURATION_12,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=PremiumCallback.BACK_TO_RECIPIENT,
                ),
            ],
        ]
    )


def get_premium_payment_method_keyboard(
    lang: str = "ru",
    *,
    lava_enabled: bool = False,
    lava_fee_percent: Decimal = Decimal("3.4"),
) -> InlineKeyboardMarkup:
    """Клавиатура выбора способа оплаты."""
    keyboard = []
    if lava_enabled:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=t(
                        "common.buttons.pay_lava",
                        lang,
                        fee=format_decimal_compact(lava_fee_percent),
                    ),
                    callback_data=PremiumCallback.PAY_LAVA,
                )
            ]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_cryptobot", lang),
                    callback_data=PremiumCallback.PAY_CRYPTOBOT,
                ),
                InlineKeyboardButton(
                    text=t("common.buttons.pay_ton", lang),
                    callback_data=PremiumCallback.PAY_TON,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_balance", lang),
                    callback_data=PremiumCallback.PAY_BALANCE,
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=PremiumCallback.BACK_TO_DURATION,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_premium_payment_pending_keyboard(pay_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты CryptoBot для Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay", lang),
                    url=pay_url,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.check_payment", lang),
                    callback_data=PremiumCallback.CHECK_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=PremiumCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_premium_ton_payment_keyboard(ton_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты TON для Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_ton_full", lang),
                    url=ton_url,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.check_payment", lang),
                    callback_data=PremiumCallback.CHECK_TON_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=PremiumCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_premium_platega_payment_keyboard(pay_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты СБП для Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏦 Оплатить СБП",
                    url=pay_url,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.check_payment", lang),
                    callback_data=PremiumCallback.CHECK_PLATEGA_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=PremiumCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_premium_lava_payment_keyboard(
    pay_url: str | None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты Lava для Premium."""
    keyboard = []
    if pay_url:
        keyboard.append(
            [InlineKeyboardButton(text=t("common.buttons.pay_lava_full", lang), url=pay_url)]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=t("common.buttons.check_payment", lang),
                    callback_data=PremiumCallback.CHECK_LAVA_PAYMENT,
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=PremiumCallback.CANCEL_PAYMENT,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=keyboard
    )


def get_confirm_premium_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура подтверждения получения Premium с баланса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.confirmation.confirm_btn", lang),
                    callback_data=PremiumCallback.CONFIRM,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=PremiumCallback.BACK_TO_DURATION,
                ),
            ],
        ]
    )


def get_back_to_premium_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Кнопка назад к меню Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=PremiumCallback.BACK_TO_PREMIUM,
                ),
            ],
        ]
    )


def get_payment_error_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура при ошибке оплаты — возврат к выбору способа оплаты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=PremiumCallback.BACK_TO_PAYMENT,
                ),
            ],
        ]
    )
