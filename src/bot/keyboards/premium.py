"""
Клавиатуры для раздела покупки Premium.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.menu import MenuCallback
from src.locales import t


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

    # Проверка оплаты
    CHECK_PAYMENT = "premium:check"
    CHECK_TON_PAYMENT = "premium:check:ton"
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


def get_duration_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора срока Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("premium_section.duration.months_3", lang),
                    callback_data=PremiumCallback.DURATION_3,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("premium_section.duration.months_6", lang),
                    callback_data=PremiumCallback.DURATION_6,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("premium_section.duration.months_12", lang),
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


def get_premium_payment_method_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора способа оплаты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_balance", lang),
                    callback_data=PremiumCallback.PAY_BALANCE,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_cryptobot", lang),
                    callback_data=PremiumCallback.PAY_CRYPTOBOT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_ton", lang),
                    callback_data=PremiumCallback.PAY_TON,
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
