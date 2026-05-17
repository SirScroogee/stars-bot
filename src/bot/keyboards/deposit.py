"""
Клавиатуры для раздела пополнения баланса.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.menu import MenuCallback
from src.locales import t


class DepositCallback:
    """Callback data для раздела пополнения."""

    # Способы оплаты
    PAY_CRYPTOBOT = "deposit:pay:cryptobot"
    PAY_TON = "deposit:pay:ton"

    # Проверка оплаты
    CHECK_PAYMENT = "deposit:check"  # CryptoBot
    CHECK_TON_PAYMENT = "deposit:check:ton"  # TON прямой
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


def get_payment_method_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора способа оплаты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("deposit.methods.cryptobot", lang),
                    callback_data=DepositCallback.PAY_CRYPTOBOT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("deposit.methods.ton", lang),
                    callback_data=DepositCallback.PAY_TON,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=DepositCallback.BACK_TO_AMOUNT,
                ),
            ],
        ]
    )


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
