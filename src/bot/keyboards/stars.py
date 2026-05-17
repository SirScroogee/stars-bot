"""
Клавиатуры для раздела покупки звёзд.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.menu import MenuCallback
from src.locales import t


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
    CONFIRM_BALANCE = "stars:pay:balance:confirm"
    CANCEL_BALANCE = "stars:pay:balance:cancel"

    # Проверка оплаты
    CHECK_PAYMENT = "stars:check"
    CHECK_TON_PAYMENT = "stars:check:ton"
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


def get_amount_keyboard(lang: str = "ru", max_stars: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура выбора количества звёзд."""
    keyboard = [
        [
            InlineKeyboardButton(text="50⭐", callback_data=StarsCallback.AMOUNT_50),
            InlineKeyboardButton(text="100⭐", callback_data=StarsCallback.AMOUNT_100),
            InlineKeyboardButton(text="500⭐", callback_data=StarsCallback.AMOUNT_500),
            InlineKeyboardButton(text="1000⭐", callback_data=StarsCallback.AMOUNT_1000),
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


def get_payment_method_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора способа оплаты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_balance", lang),
                    callback_data=StarsCallback.PAY_BALANCE,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_cryptobot", lang),
                    callback_data=StarsCallback.PAY_CRYPTOBOT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.buttons.pay_ton", lang),
                    callback_data=StarsCallback.PAY_TON,
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