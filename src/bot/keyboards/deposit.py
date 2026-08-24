"""
Клавиатуры для раздела пополнения баланса.
"""
from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.menu import MenuCallback
from src.locales import t
from src.utils import format_decimal_compact


class DepositCallback:
    """Callback data для раздела пополнения."""

    # Способы оплаты
    PAY_CRYPTOBOT = "deposit:pay:cryptobot"
    PAY_TON = "deposit:pay:ton"
    PAY_PLATEGA_SBP = "deposit:pay:platega_sbp"
    PAY_LAVA = "deposit:pay:lava"

    # Проверка оплаты
    CHECK_PAYMENT = "deposit:check"  # CryptoBot
    CHECK_TON_PAYMENT = "deposit:check:ton"  # TON прямой
    CHECK_PLATEGA_PAYMENT = "deposit:check:platega"
    CHECK_LAVA_PAYMENT = "deposit:check:lava"
    CANCEL_PAYMENT = "deposit:cancel"

    # Навигация
    BACK_TO_DEPOSIT = "deposit:back"
    BACK_TO_AMOUNT = "deposit:back:amount"
    BACK_TO_METHOD = "deposit:back:method"
    BACK_TO_PAYMENT = "deposit:back:payment"


def get_deposit_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура меню пополнения."""
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


def get_amount_input_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура для ввода суммы (только кнопка назад)."""
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
                    callback_data=DepositCallback.PAY_LAVA,
                )
            ]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=t("deposit.methods.cryptobot", lang),
                    callback_data=DepositCallback.PAY_CRYPTOBOT,
                ),
                InlineKeyboardButton(
                    text=t("deposit.methods.ton", lang),
                    callback_data=DepositCallback.PAY_TON,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=DepositCallback.BACK_TO_AMOUNT,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_payment_pending_keyboard(pay_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты CryptoBot."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.pay", lang),
                    url=pay_url,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.check", lang),
                    callback_data=DepositCallback.CHECK_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.cancel", lang),
                    callback_data=DepositCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_ton_payment_keyboard(ton_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты TON."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.pay_ton", lang),
                    url=ton_url,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.check", lang),
                    callback_data=DepositCallback.CHECK_TON_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.cancel", lang),
                    callback_data=DepositCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_platega_payment_keyboard(pay_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты СБП."""
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
                    text=t("deposit.buttons.check", lang),
                    callback_data=DepositCallback.CHECK_PLATEGA_PAYMENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.cancel", lang),
                    callback_data=DepositCallback.CANCEL_PAYMENT,
                ),
            ],
        ]
    )


def get_lava_payment_keyboard(
    pay_url: str | None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    """Клавиатура ожидания оплаты Lava."""
    keyboard = []
    if pay_url:
        keyboard.append(
            [InlineKeyboardButton(text=t("common.buttons.pay_lava_full", lang), url=pay_url)]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.check", lang),
                    callback_data=DepositCallback.CHECK_LAVA_PAYMENT,
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("deposit.buttons.cancel", lang),
                    callback_data=DepositCallback.CANCEL_PAYMENT,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=keyboard
    )


def get_back_to_deposit_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Кнопка назад к меню пополнения."""
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


def get_payment_error_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура при ошибке оплаты — возврат к выбору способа оплаты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=DepositCallback.BACK_TO_METHOD,
                ),
            ],
        ]
    )
