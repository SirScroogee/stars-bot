"""
Клавиатуры для раздела чеков.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, SwitchInlineQueryChosenChat

from src.bot.keyboards.menu import MenuCallback
from src.locales import t


class ChecksCallback:
    """Callback data для раздела чеков."""

    # Главное меню чеков
    CREATE = "checks:create"
    MY_CHECKS = "checks:my"
    HISTORY = "checks:history"

    # Содержимое чека
    CONTENT_STARS = "checks:content:stars"
    CONTENT_PREMIUM = "checks:content:premium"

    # Пропуск опциональных шагов
    SKIP_RECIPIENT = "checks:skip:recipient"
    SKIP_PASSWORD = "checks:skip:password"
    SKIP_CHANNEL = "checks:skip:channel"

    # Оплата
    PAY_USDT = "checks:pay:usdt"
    PAY_BALANCE = "checks:pay:balance"  # Оплата с баланса звёзд/премиум
    TOP_UP = "checks:topup"

    # Просмотр чеков
    VIEW_CHECK = "checks:view:"  # + check_id
    DELETE_CHECK = "checks:delete:"  # + check_id
    SHARE_CHECK = "checks:share:"  # + check_id
    ADD_DESCRIPTION = "checks:description:"  # + check_id
    RESTRICTIONS = "checks:restrictions:"  # + check_id

    # Пагинация
    MY_CHECKS_PAGE = "checks:my:page:"  # + page_num
    HISTORY_PAGE = "checks:history:page:"  # + page_num

    # Навигация
    BACK_TO_CHECKS = "checks:back"
    BACK_TO_CONTENT = "checks:back:content"
    BACK_TO_AMOUNT = "checks:back:amount"
    BACK_TO_ACTIVATIONS = "checks:back:activations"
    BACK_TO_RECIPIENT = "checks:back:recipient"
    BACK_TO_PASSWORD = "checks:back:password"
    BACK_TO_CHANNEL = "checks:back:channel"
    BACK_TO_PAYMENT = "checks:back:payment"
    BACK_TO_MY_CHECKS = "checks:back:my"
    BACK_TO_HISTORY = "checks:back:history"


def get_checks_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Главное меню раздела чеков."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("checks.menu.create_btn", lang),
                    callback_data=ChecksCallback.CREATE,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("checks.menu.my_checks_btn", lang),
                    callback_data=ChecksCallback.MY_CHECKS,
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


def get_check_content_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора содержимого чека (звёзды / Premium)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("checks.content.stars_btn", lang),
                    callback_data=ChecksCallback.CONTENT_STARS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("checks.content.premium_btn", lang),
                    callback_data=ChecksCallback.CONTENT_PREMIUM,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_CHECKS,
                ),
            ],
        ]
    )


def get_check_amount_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ввода суммы чека (кнопки-пресеты)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="50⭐", callback_data="checks:amount:50"),
                InlineKeyboardButton(text="100⭐", callback_data="checks:amount:100"),
                InlineKeyboardButton(text="500⭐", callback_data="checks:amount:500"),
                InlineKeyboardButton(text="1000⭐", callback_data="checks:amount:1000"),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_CONTENT,
                ),
            ],
        ]
    )


def get_check_premium_duration_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора срока Premium для чека."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("checks.premium_duration.months_3", lang), callback_data="checks:premium:3"),
                InlineKeyboardButton(text=t("checks.premium_duration.months_6", lang), callback_data="checks:premium:6"),
                InlineKeyboardButton(text=t("checks.premium_duration.months_12", lang), callback_data="checks:premium:12"),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_CONTENT,
                ),
            ],
        ]
    )


def get_check_activations_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора количества активаций."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("checks.activations.max_btn", lang),
                    callback_data="checks:activations:max",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_AMOUNT,
                ),
                InlineKeyboardButton(
                    text=t("checks.activations.skip_btn", lang),
                    callback_data="checks:activations:1",
                ),
            ],
        ]
    )


def get_check_activations_premium_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора количества активаций для Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("checks.activations.max_btn", lang),
                    callback_data="checks:activations:max",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data="checks:back:premium_duration",
                ),
                InlineKeyboardButton(
                    text=t("checks.activations.skip_btn", lang),
                    callback_data="checks:activations:1",
                ),
            ],
        ]
    )


def get_check_recipient_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ввода получателя (с возможностью пропуска)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_ACTIVATIONS,
                ),
                InlineKeyboardButton(
                    text=t("checks.recipient.skip_btn", lang),
                    callback_data=ChecksCallback.SKIP_RECIPIENT,
                ),
            ],
        ]
    )


def get_check_password_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ввода пароля (с возможностью пропуска) - для одноразового чека."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_RECIPIENT,
                ),
                InlineKeyboardButton(
                    text=t("checks.password.skip_btn", lang),
                    callback_data=ChecksCallback.SKIP_PASSWORD,
                ),
            ],
        ]
    )


def get_check_password_keyboard_multi(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура ввода пароля для мульти-чека."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_RECIPIENT,
                ),
                InlineKeyboardButton(
                    text=t("checks.password.skip_btn", lang),
                    callback_data=ChecksCallback.SKIP_PASSWORD,
                ),
            ],
        ]
    )


def get_check_channel_keyboard(
    lang: str = "ru",
    available_channels: list[dict] | None = None,
    selected_channel_ids: set[int] | None = None,
    bot_username: str = "",
    limit_reached: bool = False,
    max_channels: int = 3,
) -> InlineKeyboardMarkup:
    """Клавиатура выбора канала для подписки (с возможностью пропуска).

    Args:
        lang: Язык интерфейса
        available_channels: Список каналов где бот админ [{"id": ..., "title": ..., "username": ...}]
        selected_channel_ids: Множество ID выбранных каналов
        bot_username: Username бота для ссылки добавления
        limit_reached: Достигнут ли лимит каналов
        max_channels: Максимальное количество каналов
    """
    buttons = []
    selected_channel_ids = selected_channel_ids or set()

    # Показываем доступные каналы как toggle-кнопки
    if available_channels:
        for channel in available_channels:
            is_selected = channel["id"] in selected_channel_ids
            icon = "✅ " if is_selected else ""
            buttons.append([
                InlineKeyboardButton(
                    text=f"{icon}{channel['title']}",
                    callback_data=f"checks:channel:toggle:{channel['id']}",
                ),
            ])

    # Кнопка обновить каналы
    buttons.append([
        InlineKeyboardButton(
            text=t("checks.channel.refresh_btn", lang),
            callback_data="checks:channel:refresh",
        ),
    ])

    # Кнопки добавить бота в канал/группу (ссылки)
    # Права: invite_users - для создания пригласительных ссылок
    # Показываем только если лимит не достигнут
    if bot_username and not limit_reached:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.channel.add_bot_btn", lang),
                url=f"https://t.me/{bot_username}?startchannel&admin=invite_users",
            ),
            InlineKeyboardButton(
                text=t("checks.channel.add_bot_group_btn", lang),
                url=f"https://t.me/{bot_username}?startgroup&admin=invite_users",
            ),
        ])
    elif limit_reached:
        # Показываем сообщение о лимите
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.channel.limit_reached", lang, limit=max_channels),
                callback_data="checks:channel:limit_info",
            ),
        ])

    # Подтвердить выбор (если есть выбранные каналы)
    if selected_channel_ids:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.channel.confirm_btn", lang),
                callback_data="checks:channel:confirm",
            ),
        ])

    # Назад и Без подписки в одной строке
    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=ChecksCallback.BACK_TO_PASSWORD,
        ),
        InlineKeyboardButton(
            text=t("checks.channel.skip_btn", lang),
            callback_data=ChecksCallback.SKIP_CHANNEL,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_check_payment_keyboard(
    lang: str = "ru",
    content_type: str = "stars",
    total_stars: int = 0,
    total_premium: int = 0,
    user_balance_stars: float = 0,
    user_balance_premium: int = 0,
    user_balance_usdt: float = 0,
    price_usdt: float = 0,
) -> InlineKeyboardMarkup:
    """Клавиатура выбора способа оплаты чека.

    Показывает только доступные способы оплаты (где хватает баланса).
    """
    buttons = []

    # 1. Кнопка оплаты USDT (если хватает баланса)
    has_enough_usdt = user_balance_usdt >= price_usdt
    if has_enough_usdt:
        usdt_text = t("checks.payment.usdt_balance_btn", lang, balance=f"{user_balance_usdt:,.2f}")
        buttons.append([
            InlineKeyboardButton(
                text=usdt_text,
                callback_data=ChecksCallback.PAY_USDT,
            ),
        ])

    # 2. Кнопка оплаты с баланса звёзд/premium (если хватает баланса)
    if content_type == "stars":
        has_enough_balance = user_balance_stars >= total_stars
        if has_enough_balance:
            balance_text = t("checks.payment.stars_balance_btn", lang, balance=f"{user_balance_stars:,.0f}")
            buttons.append([
                InlineKeyboardButton(
                    text=balance_text,
                    callback_data=ChecksCallback.PAY_BALANCE,
                ),
            ])
    else:
        has_enough_balance = user_balance_premium >= total_premium
        if has_enough_balance:
            balance_text = t("checks.payment.premium_balance_btn", lang, balance=user_balance_premium)
            buttons.append([
                InlineKeyboardButton(
                    text=balance_text,
                    callback_data=ChecksCallback.PAY_BALANCE,
                ),
            ])

    # Кнопка назад
    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=ChecksCallback.BACK_TO_CHANNEL,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_topup_keyboard(lang: str = "ru", amount_needed: float = 0) -> InlineKeyboardMarkup:
    """Клавиатура пополнения баланса при нехватке средств."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("checks.payment.topup_btn", lang, amount=f"{amount_needed:.2f}"),
                    callback_data=ChecksCallback.TOP_UP,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_PAYMENT,
                ),
            ],
        ]
    )


def get_back_to_checks_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Кнопка назад к меню чеков."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=ChecksCallback.BACK_TO_CHECKS,
                ),
            ],
        ]
    )


def get_my_checks_keyboard(
    checks: list[dict],
    page: int = 0,
    total_pages: int = 1,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    """Клавиатура списка активных чеков.

    Args:
        checks: Список чеков [{"id": ..., "code": ..., "content_type": ..., "amount": ..., "remaining": ..., "total": ..., "activations": ...}]
        page: Текущая страница (0-indexed)
        total_pages: Всего страниц
        lang: Язык интерфейса
    """
    buttons = []

    # Кнопки чеков
    for check in checks:
        activations = check["activations"]
        total = check["total"]

        if check["content_type"] == "premium":
            button_text = t("checks.my_checks.premium_item", lang, amount=check['amount'], activations=activations, total=total)
        else:
            button_text = t("checks.my_checks.stars_item", lang, amount=check['amount'], activations=activations, total=total)

        buttons.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"{ChecksCallback.VIEW_CHECK}{check['id']}",
            ),
        ])

    # Пагинация
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=f"{ChecksCallback.MY_CHECKS_PAGE}{page - 1}",
                )
            )
        pagination_row.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="noop",
            )
        )
        if page < total_pages - 1:
            pagination_row.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=f"{ChecksCallback.MY_CHECKS_PAGE}{page + 1}",
                )
            )
        buttons.append(pagination_row)

    # Кнопка назад
    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=ChecksCallback.BACK_TO_CHECKS,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_check_detail_keyboard(
    check_id: int,
    is_active: bool,
    check_code: str,
    back_to: str = "my",
    lang: str = "ru",
    has_description: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура детального просмотра чека.

    Args:
        check_id: ID чека
        is_active: Активен ли чек
        check_code: Код чека (для inline share)
        back_to: Куда возвращаться (my / history)
        lang: Язык интерфейса
        has_description: Есть ли описание у чека
    """
    buttons = []

    if is_active:
        # Кнопка поделиться (выбор чата для отправки)
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.detail_buttons.share", lang),
                switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
                    query=check_code,
                    allow_user_chats=True,
                    allow_bot_chats=True,
                    allow_group_chats=True,
                    allow_channel_chats=True,
                ),
            ),
        ])

        # Кнопка описания: если есть - удалить, если нет - добавить
        if has_description:
            buttons.append([
                InlineKeyboardButton(
                    text=t("checks.edit_description.remove_btn", lang),
                    callback_data=f"checks:description:remove:{check_id}",
                ),
            ])
        else:
            buttons.append([
                InlineKeyboardButton(
                    text=t("checks.detail_buttons.add_description", lang),
                    callback_data=f"{ChecksCallback.ADD_DESCRIPTION}{check_id}",
                ),
            ])

        # Кнопка ограничения
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.detail_buttons.restrictions", lang),
                callback_data=f"{ChecksCallback.RESTRICTIONS}{check_id}",
            ),
        ])

        # Кнопка удалить чек
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.detail_buttons.delete", lang),
                callback_data=f"{ChecksCallback.DELETE_CHECK}{check_id}",
            ),
        ])

    # Кнопка назад
    back_callback = ChecksCallback.BACK_TO_MY_CHECKS if back_to == "my" else ChecksCallback.BACK_TO_HISTORY
    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=back_callback,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_delete_confirm_keyboard(check_id: int, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура подтверждения удаления чека."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("checks.delete.confirm_btn", lang),
                    callback_data=f"checks:delete:confirm:{check_id}",
                ),
                InlineKeyboardButton(
                    text=t("checks.delete.cancel_btn", lang),
                    callback_data=f"{ChecksCallback.VIEW_CHECK}{check_id}",
                ),
            ],
        ]
    )


def get_description_edit_keyboard(check_id: int, has_description: bool, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура редактирования описания чека."""
    buttons = []

    # Кнопка удалить описание (если есть)
    if has_description:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.edit_description.remove_btn", lang),
                callback_data=f"checks:description:remove:{check_id}",
            ),
        ])

    # Кнопка назад
    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=f"{ChecksCallback.VIEW_CHECK}{check_id}",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_restrictions_keyboard(
    check_id: int,
    lang: str = "ru",
    has_password: bool = False,
    has_recipient: bool = False,
    require_premium: bool = False,
    require_new_user: bool = False,
    has_subscription: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура ограничений чека."""
    buttons = []

    # Пароль: если установлен - кнопка удаления, иначе - настройки
    if has_password:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.restrictions.password.remove_btn", lang),
                callback_data=f"checks:restrict:password:remove:{check_id}",
            ),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.restrictions.password_btn", lang),
                callback_data=f"checks:restrict:password:{check_id}",
            ),
        ])

    # Конкретный пользователь: если установлен - кнопка удаления
    if has_recipient:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.restrictions.user.remove_btn", lang),
                callback_data=f"checks:restrict:user:remove:{check_id}",
            ),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.restrictions.specific_user_btn", lang),
                callback_data=f"checks:restrict:user:{check_id}",
            ),
        ])

    # Premium: переключение сразу при нажатии
    premium_key = "checks.restrictions.premium_only_btn_on" if require_premium else "checks.restrictions.premium_only_btn"
    buttons.append([
        InlineKeyboardButton(
            text=t(premium_key, lang),
            callback_data=f"checks:restrict:premium:toggle:{check_id}",
        ),
    ])

    # Новые пользователи: переключение сразу при нажатии
    new_users_key = "checks.restrictions.new_users_btn_on" if require_new_user else "checks.restrictions.new_users_btn"
    buttons.append([
        InlineKeyboardButton(
            text=t(new_users_key, lang),
            callback_data=f"checks:restrict:new_users:toggle:{check_id}",
        ),
    ])

    # Подписка
    subscription_key = "checks.restrictions.subscription_btn_set" if has_subscription else "checks.restrictions.subscription_btn"
    buttons.append([
        InlineKeyboardButton(
            text=t(subscription_key, lang),
            callback_data=f"checks:restrict:subscription:{check_id}",
        ),
    ])

    # Кнопка назад
    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=f"{ChecksCallback.VIEW_CHECK}{check_id}",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_restriction_password_keyboard(check_id: int, has_password: bool, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура настройки пароля."""
    buttons = []

    if has_password:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.restrictions.password.remove_btn", lang),
                callback_data=f"checks:restrict:password:remove:{check_id}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=f"{ChecksCallback.RESTRICTIONS}{check_id}",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_restriction_user_keyboard(check_id: int, has_recipient: bool, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура настройки конкретного пользователя."""
    buttons = []

    if has_recipient:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.restrictions.user.remove_btn", lang),
                callback_data=f"checks:restrict:user:remove:{check_id}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=f"{ChecksCallback.RESTRICTIONS}{check_id}",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_restriction_toggle_keyboard(
    check_id: int,
    restriction_type: str,
    is_enabled: bool,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    """Клавиатура для toggle-ограничений (premium, new_users)."""
    if is_enabled:
        toggle_btn = InlineKeyboardButton(
            text=t(f"checks.restrictions.{restriction_type}.disable_btn", lang),
            callback_data=f"checks:restrict:{restriction_type}:disable:{check_id}",
        )
    else:
        toggle_btn = InlineKeyboardButton(
            text=t(f"checks.restrictions.{restriction_type}.enable_btn", lang),
            callback_data=f"checks:restrict:{restriction_type}:enable:{check_id}",
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle_btn],
            [
                InlineKeyboardButton(
                    text=t("common.back", lang),
                    callback_data=f"{ChecksCallback.RESTRICTIONS}{check_id}",
                ),
            ],
        ]
    )


def get_restriction_subscription_keyboard(
    check_id: int,
    available_channels: list[dict] | None = None,
    selected_channel_ids: set[int] | None = None,
    has_subscription: bool = False,
    lang: str = "ru",
    bot_username: str = "",
    limit_reached: bool = False,
    max_channels: int = 3,
) -> InlineKeyboardMarkup:
    """Клавиатура настройки подписки на канал."""
    buttons = []
    selected_channel_ids = selected_channel_ids or set()

    # Показываем доступные каналы
    if available_channels:
        for channel in available_channels:
            is_selected = channel["id"] in selected_channel_ids
            icon = "✅ " if is_selected else ""
            buttons.append([
                InlineKeyboardButton(
                    text=f"{icon}{channel['title']}",
                    callback_data=f"checks:restrict:subscription:toggle:{check_id}:{channel['id']}",
                ),
            ])

    # Кнопка обновить
    buttons.append([
        InlineKeyboardButton(
            text=t("checks.restrictions.subscription.refresh_btn", lang),
            callback_data=f"checks:restrict:subscription:refresh:{check_id}",
        ),
    ])

    # Кнопки добавить бота в канал/группу (показываем только если лимит не достигнут)
    if bot_username and not limit_reached:
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.channel.add_bot_btn", lang),
                url=f"https://t.me/{bot_username}?startchannel&admin=invite_users",
            ),
            InlineKeyboardButton(
                text=t("checks.channel.add_bot_group_btn", lang),
                url=f"https://t.me/{bot_username}?startgroup&admin=invite_users",
            ),
        ])
    elif limit_reached:
        # Показываем сообщение о лимите
        buttons.append([
            InlineKeyboardButton(
                text=t("checks.channel.limit_reached", lang, limit=max_channels),
                callback_data="checks:restrict:limit_info",
            ),
        ])

    # Назад и Подтвердить (всегда показываем)
    bottom_row = [
        InlineKeyboardButton(
            text=t("common.back", lang),
            callback_data=f"{ChecksCallback.RESTRICTIONS}{check_id}",
        ),
        InlineKeyboardButton(
            text=t("checks.channel.confirm_btn", lang),
            callback_data=f"checks:restrict:subscription:confirm:{check_id}",
        ),
    ]
    buttons.append(bottom_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)
