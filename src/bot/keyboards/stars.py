"""
Клавиатуры для раздела покупки звёзд.
"""
from decimal import Decimal, ROUND_HALF_UP

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.menu import MenuCallback
from src.locales import t
from src.utils import format_decimal_compact


class StarsCallback:
    """Callback data для раздела звёзд."""

    # Главное меню звёзд
    BUY = "stars:buy"
    WITHDRAW = "stars:withdraw"
    CALCULATOR = "stars:calculator"

    # Выбор получателя
    RECIPIENT_SELF = "stars:recipient:self"

    # Выбор количества
    AMOUNT_50 = "stars:amount:50"
    AMOUNT_100 = "stars:amount:100"
    AMOUNT_500 = "stars:amount:500"
    AMOUNT_1000 = "stars:amount:1000"
    AMOUNT_ALL = "stars:amount:all"  # На весь баланс

    # Способы оплаты
    PAY_BALANCE = "stars:pay:balance"
    PAY_CRYPTOBOT = "stars:pay:cryptobot"
    PAY_TON = "stars:pay:ton"
    PAY_PLATEGA_SBP = "stars:pay:platega_sbp"
    PAY_LAVA = "stars:pay:lava"
    CONFIRM_BALANCE = "stars:pay:balance:confirm"
    CANCEL_BALANCE = "stars:pay:balance:cancel"

    # Проверка оплаты
    CHECK_PAYMENT = "stars:check"
    CHECK_TON_PAYMENT = "stars:check:ton"
    CHECK_PLATEGA_PAYMENT = "stars:check:platega"
    CHECK_LAVA_PAYMENT = "stars:check:lava"
    CANCEL_PAYMENT = "stars:cancel"

    # Подтверждение
    CONFIRM = "stars:confirm"

    # Навигация
    BACK_TO_STARS = "stars:back"
    BACK_TO_RECIPIENT = "stars:back:recipient"
    BACK_TO_AMOUNT = "stars:back:amount"
    BACK_TO_PAYMENT = "stars:back:payment"


def get_stars_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Главное меню раздела звёзд."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("stars_section.menu.buy_btn", lang),
                    callback_data=StarsCallback.BUY,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("stars_section.menu.withdraw_btn", lang),
                    callback_data=StarsCallback.WITHDRAW,
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


def get_recipient_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора получателя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.recipient.self", lang),
                    callback_data=StarsCallback.RECIPIENT_SELF,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=StarsCallback.BACK_TO_STARS,
                ),
            ],
        ]
    )


def _amount_button_text(
    amount: int,
    star_price: Decimal | None,
    usdt_rub_rate: Decimal | None,
) -> str:
    if star_price and usdt_rub_rate:
        rub_amount = (Decimal(amount) * star_price * usdt_rub_rate).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        if rub_amount > 0:
            return f"{amount} ({rub_amount:,.2f} RUB)"
    return f"{amount}⭐"


def get_amount_keyboard(
    lang: str = "ru",
    max_stars: int = 0,
    star_price: Decimal | None = None,
    usdt_rub_rate: Decimal | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура выбора количества звёзд."""
    keyboard = [
        [
            InlineKeyboardButton(
                text=_amount_button_text(50, star_price, usdt_rub_rate),
                callback_data=StarsCallback.AMOUNT_50,
            ),
            InlineKeyboardButton(
                text=_amount_button_text(100, star_price, usdt_rub_rate),
                callback_data=StarsCallback.AMOUNT_100,
            ),
        ],
        [
            InlineKeyboardButton(
                text=_amount_button_text(500, star_price, usdt_rub_rate),
                callback_data=StarsCallback.AMOUNT_500,
            ),
            InlineKeyboardButton(
                text=_amount_button_text(1000, star_price, usdt_rub_rate),
                callback_data=StarsCallback.AMOUNT_1000,
            ),
        ],
    ]

    # Добавляем кнопку "На весь баланс" если есть доступные звёзды
    if max_stars > 0:
        keyboard.append([
            InlineKeyboardButton(
                text=t("stars_section.amount.use_all_balance", lang, amount=max_stars),
                callback_data=StarsCallback.AMOUNT_ALL,
            ),
        ])

    keyboard.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=StarsCallback.BACK_TO_RECIPIENT,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_payment_method_keyboard(
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
                    callback_data=StarsCallback.PAY_LAVA,
                )
            ]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_cryptobot", lang),
                    callback_data=StarsCallback.PAY_CRYPTOBOT,
                ),
                InlineKeyboardButton(
                    text=t("common.buttons.pay_ton", lang),
                    callback_data=StarsCallback.PAY_TON,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_balance", lang),
                    callback_data=StarsCallback.PAY_BALANCE,
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=StarsCallback.BACK_TO_AMOUNT,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_balance_confirm_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура подтверждения оплаты с баланса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.balance_payment.confirm_btn", lang),
                    callback_data=StarsCallback.CONFIRM_BALANCE,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=StarsCallback.CANCEL_BALANCE,
                ),
            ],
        ]
    )


def get_stars_payment_pending_keyboard(pay_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты CryptoBot для Stars."""
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
                    callback_data=StarsCallback.CHECK_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=StarsCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_stars_ton_payment_keyboard(ton_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты TON для Stars."""
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
                    callback_data=StarsCallback.CHECK_TON_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=StarsCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_stars_platega_payment_keyboard(pay_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты СБП для Stars."""
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
                    callback_data=StarsCallback.CHECK_PLATEGA_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=StarsCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_stars_lava_payment_keyboard(
    pay_url: str | None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты Lava для Stars."""
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
                    callback_data=StarsCallback.CHECK_LAVA_PAYMENT,
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("common.cancel", lang),
                    callback_data=StarsCallback.CANCEL_PAYMENT,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=keyboard
    )


def get_confirm_withdraw_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура подтверждения получения звёзд с баланса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.confirmation.confirm_btn", lang),
                    callback_data=StarsCallback.CONFIRM,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=StarsCallback.BACK_TO_AMOUNT,
                ),
            ],
        ]
    )


def get_back_to_stars_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Кнопка назад к меню звёзд."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=StarsCallback.BACK_TO_STARS,
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
                    callback_data=StarsCallback.BACK_TO_PAYMENT,
                ),
            ],
        ]
    )


def get_back_to_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Кнопка назад в главное меню."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.to_menu", lang),
                    callback_data=MenuCallback.BACK_TO_MENU,
                ),
            ],
        ]
    )
