"""
Клавиатуры админ-панели.
"""
from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Московское время (UTC+3)
MOSCOW_TZ = timezone(timedelta(hours=3))


def _to_moscow_time(dt: datetime) -> datetime:
    """Конвертировать UTC время в московское (UTC+3)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MOSCOW_TZ)


def _format_percent(value: str) -> str:
    """Форматировать процент - убрать .0 если целое число."""
    try:
        float_val = float(value)
        if float_val == int(float_val):
            return str(int(float_val))
        return str(float_val)
    except (ValueError, TypeError):
        return value


class AdminCallback:
    """Callback data для админ-панели."""

    # Главное меню
    STATS = "admin:stats"
    MANAGE_USERS = "admin:manage_users"
    MANAGE_ORDERS = "admin:manage_orders"
    PROMO = "admin:promo"
    BROADCAST = "admin:broadcast"
    SETTINGS = "admin:settings"
    LOGS = "admin:logs"
    CLOSE = "admin:close"
    BACK = "admin:back"

    # Промокоды
    PROMO_CREATE = "admin:promo:create"
    PROMO_LIST = "admin:promo:list"
    PROMO_TYPE_STARS = "admin:promo:type:stars"
    PROMO_TYPE_USDT = "admin:promo:type:usdt"
    PROMO_TYPE_PREMIUM = "admin:promo:type:premium"
    PROMO_SKIP_LIMIT = "admin:promo:skip:limit"
    PROMO_SKIP_EXPIRES = "admin:promo:skip:expires"
    PROMO_CONFIRM = "admin:promo:confirm"
    PROMO_CANCEL = "admin:promo:cancel"
    PROMO_BACK = "admin:promo:back"
    # Навигация назад по шагам создания
    PROMO_BACK_TO_CODE = "admin:promo:back:code"
    PROMO_BACK_TO_TYPE = "admin:promo:back:type"
    PROMO_BACK_TO_BONUS = "admin:promo:back:bonus"
    PROMO_BACK_TO_LIMIT = "admin:promo:back:limit"
    PROMO_BACK_TO_EXPIRES = "admin:promo:back:expires"

    # Разделы статистики
    STATS_USERS = "admin:stats:users"
    STATS_ORDERS = "admin:stats:orders"
    STATS_FINANCE = "admin:stats:finance"
    STATS_REFERRALS = "admin:stats:referrals"
    STATS_CHECKS = "admin:stats:checks"
    STATS_PROMO = "admin:stats:promo"
    STATS_TECH = "admin:stats:tech"
    STATS_BACK = "admin:stats:back"

    # Периоды (формат: admin:stats:{section}:{period})
    # Например: admin:stats:users:24h

    # Логирование
    LOGS_TOGGLE = "admin:logs:toggle"
    LOGS_GROUP = "admin:logs:group"
    LOGS_EVENTS = "admin:logs:events"
    LOGS_TOPICS = "admin:logs:topics"
    LOGS_BACK = "admin:logs:back"
    LOGS_CANCEL = "admin:logs:cancel"  # Отмена ввода

    # Настройки бота
    SETTINGS_STARS = "admin:settings:stars"
    SETTINGS_PREMIUM = "admin:settings:premium"
    SETTINGS_PREMIUM_COST = "admin:settings:premium:cost"  # Выбор себестоимости Premium
    SETTINGS_PREMIUM_PRICE = "admin:settings:premium:price"  # Выбор цены Premium
    SETTINGS_COST = "admin:settings:cost"
    SETTINGS_PAYMENTS = "admin:settings:payments"  # Способы оплаты
    SETTINGS_PAYMENT_CRYPTOBOT = "admin:settings:payment:cryptobot"  # Настройки CryptoBot
    SETTINGS_PAYMENT_TON = "admin:settings:payment:ton"  # Настройки TON
    SETTINGS_REFERRAL = "admin:settings:referral"
    SETTINGS_SUPPORT = "admin:settings:support"
    SETTINGS_MEDIA = "admin:settings:media"
    SETTINGS_BACK = "admin:settings:back"
    SETTINGS_CANCEL = "admin:settings:cancel"

    # Fragment аккаунты
    FRAGMENT = "admin:fragment"
    FRAGMENT_LIST = "admin:fragment:list"
    FRAGMENT_ADD = "admin:fragment:add"
    FRAGMENT_BACK = "admin:fragment:back"
    FRAGMENT_CANCEL = "admin:fragment:cancel"
    FRAGMENT_CONFIRM = "admin:fragment:confirm"
    FRAGMENT_REFRESH = "admin:fragment:refresh"
    # Динамические: admin:fragment:view:{id}, admin:fragment:toggle:{id},
    # admin:fragment:edit:{id}, admin:fragment:delete:{id}, admin:fragment:session:{id}

    # Воркеры (обработка заказов)
    WORKERS = "admin:workers"
    WORKERS_ADD = "admin:workers:add"
    WORKERS_REMOVE = "admin:workers:remove"
    WORKERS_REFRESH = "admin:workers:refresh"
    WORKERS_BACK = "admin:workers:back"
    # Динамические: admin:workers:remove:{id}, admin:workers:concurrent:{id}:{value}

    # Рассылка
    BROADCAST_TEXT = "admin:broadcast:text"
    BROADCAST_PHOTO = "admin:broadcast:photo"
    BROADCAST_STICKER = "admin:broadcast:sticker"
    BROADCAST_CONFIRM = "admin:broadcast:confirm"
    BROADCAST_CANCEL = "admin:broadcast:cancel"
    BROADCAST_BACK = "admin:broadcast:back"

    # Управление админами
    ADMINS = "admin:admins"
    ADMINS_ADD = "admin:admins:add"
    ADMINS_BACK = "admin:admins:back"
    ADMINS_CANCEL = "admin:admins:cancel"
    # Динамические: admin:admins:remove:{user_id}

    # Управление пользователями
    USERS_SEARCH = "admin:users:search"
    USERS_RECENT = "admin:users:recent"
    USERS_BACK = "admin:users:back"
    USERS_CANCEL = "admin:users:cancel"
    # Динамические: admin:users:view:{user_id}, admin:users:ban:{user_id}, admin:users:unban:{user_id}

    # Управление конкретным пользователем
    # Динамические: admin:users:manage:{user_id}
    # Балансы: admin:users:balances:{user_id}
    # Динамические: admin:users:balance:{type}:{user_id} (type: usdt, stars, premium, referral)
    # Динамические: admin:users:balance:confirm:{user_id}
    USERS_BALANCE_CANCEL = "admin:users:balance:cancel"
    # Рефералы: admin:users:referrals:{user_id}
    # Динамические: admin:users:referrals:change:{user_id}, admin:users:referrals:reset:{user_id}
    # Динамические: admin:users:referrals:list:{user_id}
    USERS_REFERRALS_CANCEL = "admin:users:referrals:cancel"
    # Сообщение: admin:users:message:{user_id}
    USERS_MESSAGE_CANCEL = "admin:users:message:cancel"

    # Управление заказами
    ORDERS = "admin:orders"
    ORDERS_LIST = "admin:orders:list"
    ORDERS_SEARCH = "admin:orders:search"
    ORDERS_STATS = "admin:orders:stats"
    ORDERS_PROBLEMS = "admin:orders:problems"
    ORDERS_BACK = "admin:orders:back"
    ORDERS_CANCEL = "admin:orders:cancel"
    # Фильтры: admin:orders:filter:{filter_type}
    # Динамические: admin:orders:view:{order_id}, admin:orders:retry:{order_id}, admin:orders:refund:{order_id}


def get_admin_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню админ-панели."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data=AdminCallback.STATS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Настройки",
                    callback_data=AdminCallback.SETTINGS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Пользователи",
                    callback_data=AdminCallback.MANAGE_USERS,
                ),
                InlineKeyboardButton(
                    text="👑 Админы",
                    callback_data=AdminCallback.ADMINS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📦 Заказы",
                    callback_data=AdminCallback.MANAGE_ORDERS,
                ),
                InlineKeyboardButton(
                    text="🎁 Промокоды",
                    callback_data=AdminCallback.PROMO,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📢 Рассылка",
                    callback_data=AdminCallback.BROADCAST,
                ),
                InlineKeyboardButton(
                    text="📋 Логи",
                    callback_data=AdminCallback.LOGS,
                ),
            ],
        ]
    )


def get_stats_menu_keyboard() -> InlineKeyboardMarkup:
    """Меню разделов статистики."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Пользователи",
                    callback_data=AdminCallback.STATS_USERS,
                ),
                InlineKeyboardButton(
                    text="📦 Заказы",
                    callback_data=AdminCallback.STATS_ORDERS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💰 Финансы",
                    callback_data=AdminCallback.STATS_FINANCE,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎫 Чеки",
                    callback_data=AdminCallback.STATS_CHECKS,
                ),
                InlineKeyboardButton(
                    text="🎁 Промокоды",
                    callback_data=AdminCallback.STATS_PROMO,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Рефералы",
                    callback_data=AdminCallback.STATS_REFERRALS,
                ),
                InlineKeyboardButton(
                    text="⚙️ Техническая",
                    callback_data=AdminCallback.STATS_TECH,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.BACK,
                ),
            ],
        ]
    )


def get_stats_section_keyboard(
    section: str,
    current_period: str = "all",
    date_from: str = None,
    date_to: str = None,
) -> InlineKeyboardMarkup:
    """Клавиатура раздела статистики с выбором периода."""
    periods = [
        ("24h", "24ч"),
        ("7d", "7д"),
        ("30d", "30д"),
        ("all", "Всё"),
    ]

    period_buttons = []
    for period_key, period_name in periods:
        mark = " ✓" if current_period == period_key else ""
        callback = f"admin:stats:{section}:{period_key}"
        period_buttons.append(
            InlineKeyboardButton(text=f"{period_name}{mark}", callback_data=callback)
        )

    # Кнопка выбора периода через календарь
    custom_mark = " ✓" if current_period == "custom" else ""
    if date_from and date_to:
        custom_text = f"📅 {date_from} — {date_to}{custom_mark}"
    else:
        custom_text = f"📅 Период{custom_mark}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            period_buttons,
            [
                InlineKeyboardButton(
                    text=custom_text,
                    callback_data=f"admin:stats:{section}:custom",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.STATS_BACK,
                ),
            ],
        ]
    )


def get_logs_menu_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Главное меню настроек логирования."""
    enabled = settings.get("enabled", True)
    status = "✅ Вкл" if enabled else "❌ Выкл"

    group_id = settings.get("group_id")
    group_text = f"Группа: {group_id}" if group_id else "Группа: не задана"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Логирование: {status}",
                    callback_data=AdminCallback.LOGS_TOGGLE,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🔗 {group_text}",
                    callback_data=AdminCallback.LOGS_GROUP,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📂 Топики",
                    callback_data=AdminCallback.LOGS_TOPICS,
                ),
                InlineKeyboardButton(
                    text="📋 События",
                    callback_data=AdminCallback.LOGS_EVENTS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.BACK,
                ),
            ],
        ]
    )


def get_logs_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой отмены."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.LOGS_CANCEL,
                ),
            ],
        ]
    )


def get_logs_topics_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура управления топиками."""
    topics = settings.get("topics", {})

    buttons = []
    for topic_key, topic_data in topics.items():
        enabled = topic_data.get("enabled", True)
        name = topic_data.get("name", topic_key)
        topic_id = topic_data.get("id", "?")
        status = "✅" if enabled else "❌"
        # Показываем статус и ID топика
        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {name}",
                callback_data=f"admin:logs:topic:toggle:{topic_key}",
            ),
            InlineKeyboardButton(
                text=f"#{topic_id}",
                callback_data=f"admin:logs:topic:edit:{topic_key}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.LOGS_BACK,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_logs_events_keyboard(settings: dict, page: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура управления событиями (с пагинацией)."""
    events = settings.get("events", {})

    # Группы событий для удобства
    event_groups = {
        "errors": ["error", "order_error"],
        "payments": ["deposit"],
        "orders": ["order_completed", "order_failed"],
        "checks": ["check_created"],
        "promo": ["promo_created"],
        "admin": ["admin_login", "admin_action", "user_banned"],
        "system": ["bot_started", "bot_stopped"],
        "users": ["user_registered"],
    }

    # Красивые названия событий
    event_names = {
        "error": "Ошибки",
        "order_error": "Ошибки заказов",
        "deposit": "Пополнение",
        "order_completed": "Заказ выполнен",
        "order_failed": "Ошибка заказа",
        "check_created": "Создание чека",
        "promo_created": "Создание промо",
        "admin_login": "Вход в админку",
        "admin_action": "Действие админа",
        "user_banned": "Бан пользователя",
        "bot_started": "Запуск бота",
        "bot_stopped": "Остановка бота",
        "user_registered": "Регистрация",
    }

    # Группы для страниц
    group_keys = list(event_groups.keys())
    items_per_page = 3
    total_pages = (len(group_keys) + items_per_page - 1) // items_per_page
    page = max(0, min(page, total_pages - 1))

    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    current_groups = group_keys[start_idx:end_idx]

    buttons = []

    for group_key in current_groups:
        group_events = event_groups.get(group_key, [])
        for event_key in group_events:
            enabled = events.get(event_key, True)
            name = event_names.get(event_key, event_key)
            status = "✅" if enabled else "❌"
            buttons.append([
                InlineKeyboardButton(
                    text=f"{status} {name}",
                    callback_data=f"admin:logs:event:{event_key}",
                ),
            ])

    # Пагинация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="◀", callback_data=f"admin:logs:events:page:{page - 1}")
        )
    nav_buttons.append(
        InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin:logs:events:nop")
    )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(text="▶", callback_data=f"admin:logs:events:page:{page + 1}")
        )

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.LOGS_BACK,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== ПРОМОКОДЫ ====================

def get_promo_menu_keyboard() -> InlineKeyboardMarkup:
    """Меню промокодов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Создать промокод",
                    callback_data=AdminCallback.PROMO_CREATE,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Список промокодов",
                    callback_data=AdminCallback.PROMO_LIST,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.BACK,
                ),
            ],
        ]
    )


def get_promo_type_keyboard() -> InlineKeyboardMarkup:
    """Выбор типа бонуса (Звёзды, Premium, USDT)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Звёзды",
                    callback_data=AdminCallback.PROMO_TYPE_STARS,
                ),
                InlineKeyboardButton(
                    text="👑 Premium",
                    callback_data=AdminCallback.PROMO_TYPE_PREMIUM,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💵 USDT",
                    callback_data=AdminCallback.PROMO_TYPE_USDT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_BACK_TO_CODE,
                ),
            ],
        ]
    )


def get_promo_bonus_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для ввода бонуса с кнопкой назад."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_BACK_TO_TYPE,
                ),
            ],
        ]
    )


def get_promo_premium_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора месяцев Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3 мес.", callback_data="admin:promo:premium:3"),
                InlineKeyboardButton(text="6 мес.", callback_data="admin:promo:premium:6"),
                InlineKeyboardButton(text="12 мес.", callback_data="admin:promo:premium:12"),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_BACK_TO_TYPE,
                ),
            ],
        ]
    )


def get_promo_limit_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для ввода лимита."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_BACK_TO_BONUS,
                ),
                InlineKeyboardButton(
                    text="♾️ Без лимита",
                    callback_data=AdminCallback.PROMO_SKIP_LIMIT,
                ),
            ],
        ]
    )


def get_promo_expires_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для ввода срока действия."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_BACK_TO_LIMIT,
                ),
                InlineKeyboardButton(
                    text="♾️ Бессрочно",
                    callback_data=AdminCallback.PROMO_SKIP_EXPIRES,
                ),
            ],
        ]
    )


def get_promo_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение создания промокода."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_BACK_TO_EXPIRES,
                ),
                InlineKeyboardButton(
                    text="✅ Создать",
                    callback_data=AdminCallback.PROMO_CONFIRM,
                ),
            ],
        ]
    )


def get_promo_code_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для ввода кода с кнопкой отмены."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.PROMO_CANCEL,
                ),
            ],
        ]
    )


def get_promo_back_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой назад в меню промокодов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_BACK,
                ),
            ],
        ]
    )


def get_promo_list_keyboard(
    promos: list,
    page: int = 0,
    page_size: int = 5,
) -> InlineKeyboardMarkup:
    """Список промокодов с пагинацией."""
    buttons = []

    total_count = len(promos)
    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_promos = promos[start_idx:end_idx]

    for promo in page_promos:
        status = "✅" if promo.is_active else "❌"
        uses_text = f"{promo.current_uses}/{promo.max_uses}" if promo.max_uses else f"{promo.current_uses}/∞"

        # Определяем тип бонуса
        if promo.bonus_stars > 0:
            bonus_text = f"+{promo.bonus_stars:,.0f}⭐"
        elif promo.bonus_usdt > 0:
            bonus_text = f"+{promo.bonus_usdt:,.0f}$"
        elif promo.bonus_premium > 0:
            bonus_text = f"+{promo.bonus_premium}👑"
        else:
            bonus_text = "—"

        button_text = f"{status} {promo.code} | {bonus_text} | {uses_text}"
        buttons.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin:promo:view:{promo.id}",
            )
        ])

    # Пагинация
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if total_pages > 1:
        nav_buttons = []
        can_go_back = page > 0
        can_go_forward = page < total_pages - 1

        if can_go_back:
            # Стрелка назад, потом номер страницы
            nav_buttons.append(
                InlineKeyboardButton(text="◀️", callback_data=f"admin:promo:page:{page - 1}")
            )

        nav_buttons.append(
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin:promo:nop")
        )

        if can_go_forward:
            # Номер страницы, потом стрелка вперёд
            nav_buttons.append(
                InlineKeyboardButton(text="▶️", callback_data=f"admin:promo:page:{page + 1}")
            )

        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.PROMO_BACK,
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_promo_detail_keyboard(promo_id: int, is_active: bool) -> InlineKeyboardMarkup:
    """Клавиатура для просмотра промокода."""
    toggle_text = "❌ Деактивировать" if is_active else "✅ Активировать"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"admin:promo:toggle:{promo_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Удалить",
                    callback_data=f"admin:promo:delete:confirm:{promo_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_LIST,
                ),
            ],
        ]
    )


def get_promo_delete_confirm_keyboard(promo_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления промокода."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"admin:promo:delete:{promo_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Отмена",
                    callback_data=f"admin:promo:view:{promo_id}",
                ),
            ],
        ]
    )


def get_promo_empty_list_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для пустого списка промокодов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.PROMO_BACK,
                ),
            ],
        ]
    )


# ==================== НАСТРОЙКИ БОТА ====================

def get_settings_menu_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Главное меню настроек бота."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Звёзды",
                    callback_data=AdminCallback.SETTINGS_STARS,
                ),
                InlineKeyboardButton(
                    text="👑 Premium",
                    callback_data=AdminCallback.SETTINGS_PREMIUM,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💳 Способы оплаты",
                    callback_data=AdminCallback.SETTINGS_PAYMENTS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💎 Fragment",
                    callback_data=AdminCallback.FRAGMENT,
                ),
                InlineKeyboardButton(
                    text="🔧 Воркеры",
                    callback_data=AdminCallback.WORKERS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Рефералы",
                    callback_data=AdminCallback.SETTINGS_REFERRAL,
                ),
                InlineKeyboardButton(
                    text="🔗 Ссылки",
                    callback_data=AdminCallback.SETTINGS_SUPPORT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Медиа",
                    callback_data=AdminCallback.SETTINGS_MEDIA,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.BACK,
                ),
            ],
        ]
    )


def get_settings_media_keyboard(media: dict, items: dict) -> InlineKeyboardMarkup:
    buttons = []
    for key, title in items.items():
        marker = "✅" if media.get(key) else "➕"
        buttons.append([
            InlineKeyboardButton(
                text=f"{marker} {title}",
                callback_data=f"admin:settings:media:set:{key}",
            ),
        ])
        if media.get(key):
            buttons.append([
                InlineKeyboardButton(
                    text=f"Удалить: {title}",
                    callback_data=f"admin:settings:media:remove:{key}",
                ),
            ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.SETTINGS_BACK,
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_settings_support_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура настроек внешних ссылок."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Изменить поддержку",
                    callback_data="admin:settings:edit:support_username",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📰 Изменить новостной канал",
                    callback_data="admin:settings:edit:news_channel_url",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_BACK,
                ),
            ],
        ]
    )


def get_settings_stars_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура настроек звёзд."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏭 Себестоимость",
                    callback_data="admin:settings:edit:star_cost_usdt",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💵 Цена в боте",
                    callback_data="admin:settings:edit:star_price_usdt",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📉 Минимум",
                    callback_data="admin:settings:edit:min_stars",
                ),
                InlineKeyboardButton(
                    text="📈 Максимум",
                    callback_data="admin:settings:edit:max_stars",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_BACK,
                ),
            ],
        ]
    )


def get_settings_premium_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура настроек Premium."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏭 Себестоимость",
                    callback_data=AdminCallback.SETTINGS_PREMIUM_COST,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💵 Цена в боте",
                    callback_data=AdminCallback.SETTINGS_PREMIUM_PRICE,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_BACK,
                ),
            ],
        ]
    )


def get_settings_premium_months_keyboard(setting_type: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора периода Premium (cost или price)."""
    # setting_type: "cost" или "price"
    prefix = f"premium_{setting_type}_"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 3 месяца",
                    callback_data=f"admin:settings:edit:{prefix}3m",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📅 6 месяцев",
                    callback_data=f"admin:settings:edit:{prefix}6m",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📅 12 месяцев",
                    callback_data=f"admin:settings:edit:{prefix}12m",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_PREMIUM,
                ),
            ],
        ]
    )


def get_settings_referral_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура настроек реферальной системы."""
    lvl1 = _format_percent(settings.get("referral_percent_level1", "5"))
    lvl2 = _format_percent(settings.get("referral_percent_level2", "3"))
    lvl3 = _format_percent(settings.get("referral_percent_level3", "1"))

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🥇 Уровень 1: {lvl1}%",
                    callback_data="admin:settings:edit:referral_percent_level1",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🥈 Уровень 2: {lvl2}%",
                    callback_data="admin:settings:edit:referral_percent_level2",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🥉 Уровень 3: {lvl3}%",
                    callback_data="admin:settings:edit:referral_percent_level3",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_BACK,
                ),
            ],
        ]
    )


def get_settings_payments_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек способов оплаты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 CryptoBot",
                    callback_data=AdminCallback.SETTINGS_PAYMENT_CRYPTOBOT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💎 TON",
                    callback_data=AdminCallback.SETTINGS_PAYMENT_TON,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_BACK,
                ),
            ],
        ]
    )


def get_settings_cryptobot_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек CryptoBot."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔑 Токен",
                    callback_data="admin:settings:edit:cryptobot_token",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Комиссия",
                    callback_data="admin:settings:edit:payment_fee_cryptobot",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_PAYMENTS,
                ),
            ],
        ]
    )


def get_settings_ton_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек TON."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📬 Адрес кошелька",
                    callback_data="admin:settings:edit:ton_wallet_address",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Комиссия",
                    callback_data="admin:settings:edit:payment_fee_ton",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_PAYMENTS,
                ),
            ],
        ]
    )


def get_settings_cost_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура настроек себестоимости."""
    star_cost = settings.get("star_cost_usdt", "0.015")
    premium_cost_3m = settings.get("premium_cost_3m", "6.00")
    premium_cost_6m = settings.get("premium_cost_6m", "10.00")
    premium_cost_12m = settings.get("premium_cost_12m", "18.00")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐ Звезда: {star_cost} USDT",
                    callback_data="admin:settings:edit:star_cost_usdt",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"👑 Premium 3м: {premium_cost_3m} USDT",
                    callback_data="admin:settings:edit:premium_cost_3m",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"👑 Premium 6м: {premium_cost_6m} USDT",
                    callback_data="admin:settings:edit:premium_cost_6m",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"👑 Premium 12м: {premium_cost_12m} USDT",
                    callback_data="admin:settings:edit:premium_cost_12m",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.SETTINGS_BACK,
                ),
            ],
        ]
    )


def get_settings_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой отмены."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.SETTINGS_CANCEL,
                ),
            ],
        ]
    )


# ==================== FRAGMENT АККАУНТЫ ====================


def get_fragment_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню управления Fragment аккаунтами."""
    buttons = [
        [InlineKeyboardButton(
            text="➕ Добавить аккаунт",
            callback_data=AdminCallback.FRAGMENT_ADD,
        )],
        [InlineKeyboardButton(
            text="📋 Список аккаунтов",
            callback_data=AdminCallback.FRAGMENT_LIST,
        )],
        [InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.SETTINGS_BACK,
        )],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_fragment_list_keyboard(
    accounts: list,
    page: int = 0,
    per_page: int = 5,
) -> InlineKeyboardMarkup:
    """Клавиатура со списком Fragment аккаунтов."""
    buttons = []

    # Пагинация
    total = len(accounts)
    start = page * per_page
    end = start + per_page
    page_accounts = accounts[start:end]

    # Статус иконки
    status_icons = {
        "active": "✅",
        "session_expired": "🔴",
        "low_balance": "💰",
        "disabled": "⏸️",
    }

    for acc in page_accounts:
        icon = status_icons.get(acc.status, "❓")
        active_mark = "" if acc.is_active else " [OFF]"
        balance = f" ({acc.ton_balance:.2f} TON)" if acc.ton_balance else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {acc.name}{active_mark}{balance}",
                callback_data=f"admin:fragment:view:{acc.id}",
            ),
        ])

    # Навигация по страницам
    nav_buttons = []
    total_pages = (total + per_page - 1) // per_page

    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text="« Пред",
                callback_data=f"admin:fragment:page:{page - 1}",
            )
        )

    if total_pages > 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="admin:fragment:nop",
            )
        )

    if end < total:
        nav_buttons.append(
            InlineKeyboardButton(
                text="След »",
                callback_data=f"admin:fragment:page:{page + 1}",
            )
        )

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.FRAGMENT,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_fragment_detail_keyboard(account_id: int, is_active: bool) -> InlineKeyboardMarkup:
    """Клавиатура детального просмотра аккаунта."""
    toggle_text = "⏸️ Отключить" if is_active else "▶️ Включить"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"admin:fragment:toggle:{account_id}",
                ),
                InlineKeyboardButton(
                    text="✏️ Редактировать",
                    callback_data=f"admin:fragment:edit:{account_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Удалить",
                    callback_data=f"admin:fragment:delete:{account_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад к списку",
                    callback_data=AdminCallback.FRAGMENT_LIST,
                ),
            ],
        ]
    )


def get_fragment_edit_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора поля для редактирования."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Имя",
                    callback_data=f"admin:fragment:edit:{account_id}:name",
                ),
                InlineKeyboardButton(
                    text="⚡ Приоритет",
                    callback_data=f"admin:fragment:edit:{account_id}:priority",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔑 TONAPI Key",
                    callback_data=f"admin:fragment:edit:{account_id}:tonapi",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔐 Мнемоника",
                    callback_data=f"admin:fragment:edit:{account_id}:mnemonic",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="#️⃣ Fragment Hash",
                    callback_data=f"admin:fragment:edit:{account_id}:hash",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🍪 Все токены сессии",
                    callback_data=f"admin:fragment:session:{account_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"admin:fragment:view:{account_id}",
                ),
            ],
        ]
    )


def get_fragment_priority_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора приоритета аккаунта."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Высокий",
                    callback_data=f"admin:fragment:priority:{account_id}:0",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Средний",
                    callback_data=f"admin:fragment:priority:{account_id}:1",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📉 Низкий",
                    callback_data=f"admin:fragment:priority:{account_id}:2",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"admin:fragment:edit:{account_id}",
                ),
            ],
        ]
    )


def get_fragment_confirm_delete_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения удаления."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"admin:fragment:delete:confirm:{account_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:fragment:view:{account_id}",
                ),
            ],
        ]
    )


def get_fragment_cancel_keyboard(account_id: int = None, return_to: str = "menu") -> InlineKeyboardMarkup:
    """
    Клавиатура отмены ввода.

    Args:
        account_id: ID аккаунта для возврата
        return_to: куда возвращаться - "menu", "edit", "view"
    """
    if account_id and return_to == "edit":
        callback_data = f"admin:fragment:edit:{account_id}"
    elif account_id and return_to == "view":
        callback_data = f"admin:fragment:view:{account_id}"
    else:
        callback_data = AdminCallback.FRAGMENT_CANCEL

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=callback_data,
                ),
            ],
        ]
    )


def get_fragment_add_step_keyboard(step: str) -> InlineKeyboardMarkup:
    """Клавиатура для шага добавления аккаунта."""
    buttons = []

    # Для некоторых шагов можно пропустить (опционально)
    if step == "wallet_address":
        buttons.append([
            InlineKeyboardButton(
                text="⏭️ Пропустить (определить автоматически)",
                callback_data="admin:fragment:add:skip:wallet",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=AdminCallback.FRAGMENT_CANCEL,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_fragment_confirm_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура подтверждения добавления аккаунта."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=AdminCallback.FRAGMENT_CONFIRM,
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.FRAGMENT_CANCEL,
                ),
            ],
        ]
    )


# ==================== ВОРКЕРЫ ====================

def get_workers_status_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура статуса воркеров (режим автонастройки).

    Только кнопки обновления и возврата — управление автоматическое.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data=AdminCallback.WORKERS_REFRESH,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.WORKERS_BACK,
                ),
            ],
        ]
    )


# ==================== РАССЫЛКА ====================

def get_broadcast_menu_keyboard(users_count: int = 0) -> InlineKeyboardMarkup:
    """Главное меню рассылки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Текстовое сообщение",
                    callback_data=AdminCallback.BROADCAST_TEXT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🖼️ Фото (с текстом или без)",
                    callback_data=AdminCallback.BROADCAST_PHOTO,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏷️ Стикер",
                    callback_data=AdminCallback.BROADCAST_STICKER,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"👥 Получателей: {users_count:,}",
                    callback_data="admin:broadcast:nop",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.BACK,
                ),
            ],
        ]
    )


def get_broadcast_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены при вводе рассылки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.BROADCAST_CANCEL,
                ),
            ],
        ]
    )


def get_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура подтверждения рассылки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить рассылку",
                    callback_data=AdminCallback.BROADCAST_CONFIRM,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.BROADCAST_CANCEL,
                ),
            ],
        ]
    )


# ==================== УПРАВЛЕНИЕ АДМИНАМИ ====================

def get_admins_keyboard(admins: list) -> InlineKeyboardMarkup:
    """Клавиатура списка админов."""
    buttons = []

    for admin in admins:
        name = admin.username or f"ID: {admin.id}"
        buttons.append([
            InlineKeyboardButton(
                text=f"👤 {name}",
                callback_data=f"admin:admins:view:{admin.id}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"admin:admins:remove:{admin.id}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text="➕ Добавить админа",
            callback_data=AdminCallback.ADMINS_ADD,
        ),
    ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.BACK,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admins_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены при добавлении админа."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.ADMINS_CANCEL,
                ),
            ],
        ]
    )


def get_admin_remove_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения удаления админа."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"admin:admins:remove:confirm:{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.ADMINS,
                ),
            ],
        ]
    )


# ==================== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ ====================

def get_users_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню управления пользователями."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Поиск пользователя",
                    callback_data=AdminCallback.USERS_SEARCH,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🕐 Последние регистрации",
                    callback_data=AdminCallback.USERS_RECENT,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.BACK,
                ),
            ],
        ]
    )


def get_users_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены при поиске."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.USERS_CANCEL,
                ),
            ],
        ]
    )


def get_user_detail_keyboard(user_id: int, is_banned: bool, is_admin: bool) -> InlineKeyboardMarkup:
    """Клавиатура детального просмотра пользователя."""
    buttons = []

    # 1 строка: Отправить сообщение
    buttons.append([
        InlineKeyboardButton(
            text="✉️ Отправить сообщение",
            callback_data=f"admin:users:message:{user_id}",
        ),
    ])

    # 2 строка: Балансы, Рефералы
    buttons.append([
        InlineKeyboardButton(
            text="💰 Балансы",
            callback_data=f"admin:users:balances:{user_id}",
        ),
        InlineKeyboardButton(
            text="👥 Рефералы",
            callback_data=f"admin:users:referrals:{user_id}",
        ),
    ])

    # 3 строка: Заказы
    buttons.append([
        InlineKeyboardButton(
            text="📦 Заказы",
            callback_data=f"admin:users:orders:{user_id}",
        ),
    ])

    # 4 строка: Бан/разбан + Админ права
    row4 = []
    if is_banned:
        row4.append(
            InlineKeyboardButton(
                text="✅ Разбанить",
                callback_data=f"admin:users:unban:{user_id}",
            )
        )
    else:
        row4.append(
            InlineKeyboardButton(
                text="🚫 Забанить",
                callback_data=f"admin:users:ban:{user_id}",
            )
        )

    if is_admin:
        row4.append(
            InlineKeyboardButton(
                text="👑 Снять админа",
                callback_data=f"admin:users:demote:{user_id}",
            )
        )
    else:
        row4.append(
            InlineKeyboardButton(
                text="👑 Дать админа",
                callback_data=f"admin:users:promote:{user_id}",
            )
        )
    buttons.append(row4)

    # 5 строка: Назад
    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.MANAGE_USERS,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_users_recent_keyboard(users: list, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    """Клавиатура последних регистраций."""
    buttons = []

    total = len(users)
    start = page * per_page
    end = start + per_page
    page_users = users[start:end]

    for user in page_users:
        name = user.username or f"ID: {user.id}"
        status = ""
        if user.is_banned:
            status = " 🚫"
        elif user.is_admin:
            status = " 👑"

        buttons.append([
            InlineKeyboardButton(
                text=f"👤 {name}{status}",
                callback_data=f"admin:users:view:{user.id}",
            ),
        ])

    # Пагинация
    total_pages = (total + per_page - 1) // per_page
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="◀", callback_data=f"admin:users:recent:page:{page - 1}")
            )
        nav_buttons.append(
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin:users:nop")
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="▶", callback_data=f"admin:users:recent:page:{page + 1}")
            )
        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.MANAGE_USERS,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_user_orders_keyboard(user_id: int, orders: list, page: int = 0, per_page: int = 5) -> InlineKeyboardMarkup:
    """Клавиатура заказов пользователя."""
    buttons = []

    total = len(orders)
    start = page * per_page
    end = start + per_page
    page_orders = orders[start:end]

    status_icons = {
        "pending": "⏳",
        "processing": "🔄",
        "completed": "✅",
        "failed": "❌",
        "cancelled": "🚫",
    }

    for order in page_orders:
        icon = status_icons.get(order.status, "❓")
        # Конвертируем в московское время
        created_msk = _to_moscow_time(order.created_at)
        date = created_msk.strftime("%d.%m %H:%M")
        # quantity - это звёзды для stars или месяцы для premium
        qty_text = f"{order.quantity}⭐" if order.product_type == "stars" else f"{order.quantity}мес"
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} #{order.id} ({order.order_key}) | {qty_text} | {date}",
                callback_data=f"admin:users:order:{order.id}",
            ),
        ])

    # Пагинация
    total_pages = max(1, (total + per_page - 1) // per_page)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="◀", callback_data=f"admin:users:orders:{user_id}:page:{page - 1}")
            )
        nav_buttons.append(
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin:users:nop")
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="▶", callback_data=f"admin:users:orders:{user_id}:page:{page + 1}")
            )
        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton(
            text="◀️ К пользователю",
            callback_data=f"admin:users:view:{user_id}",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЕМ ====================



def get_user_balances_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора баланса для изменения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💵 USDT",
                    callback_data=f"admin:users:balance:usdt:{user_id}",
                ),
                InlineKeyboardButton(
                    text="⭐ Звёзды",
                    callback_data=f"admin:users:balance:stars:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👑 Premium",
                    callback_data=f"admin:users:balance:premium:{user_id}",
                ),
                InlineKeyboardButton(
                    text="🎁 Реферальный",
                    callback_data=f"admin:users:balance:referral:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ К пользователю",
                    callback_data=f"admin:users:view:{user_id}",
                ),
            ],
        ]
    )


def get_user_balance_cancel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура отмены при изменении баланса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:users:balances:{user_id}",
                ),
            ],
        ]
    )


def get_user_balance_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения изменения баланса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"admin:users:balance:confirm:{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:users:balances:{user_id}",
                ),
            ],
        ]
    )


def get_user_referrals_keyboard(user_id: int, has_referrer: bool) -> InlineKeyboardMarkup:
    """Клавиатура управления рефералами пользователя."""
    buttons = [
        [
            InlineKeyboardButton(
                text="✏️ Изменить реферера",
                callback_data=f"admin:users:referrals:change:{user_id}",
            ),
        ],
    ]

    if has_referrer:
        buttons.append([
            InlineKeyboardButton(
                text="🗑️ Сбросить реферера",
                callback_data=f"admin:users:referrals:reset:{user_id}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text="📋 Список рефералов",
            callback_data=f"admin:users:referrals:list:{user_id}",
        ),
    ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ К пользователю",
            callback_data=f"admin:users:view:{user_id}",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_user_referrals_cancel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура отмены при изменении реферера."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:users:referrals:{user_id}",
                ),
            ],
        ]
    )


def get_user_referrals_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения сброса реферера."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, сбросить",
                    callback_data=f"admin:users:referrals:reset:confirm:{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:users:referrals:{user_id}",
                ),
            ],
        ]
    )


def get_user_referrals_list_keyboard(
    user_id: int,
    referrals: list,
    page: int = 0,
    per_page: int = 10
) -> InlineKeyboardMarkup:
    """Клавиатура списка рефералов пользователя."""
    buttons = []

    total = len(referrals)
    start = page * per_page
    end = start + per_page
    page_referrals = referrals[start:end]

    for ref in page_referrals:
        username = f"@{ref['username']}" if ref.get('username') else f"ID: {ref['id']}"
        spent = ref.get('spent', 0)
        buttons.append([
            InlineKeyboardButton(
                text=f"{username} • {spent:.2f} USDT",
                callback_data=f"admin:users:view:{ref['id']}",
            ),
        ])

    # Пагинация
    total_pages = max(1, (total + per_page - 1) // per_page)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀",
                    callback_data=f"admin:users:referrals:list:{user_id}:page:{page - 1}"
                )
            )
        nav_buttons.append(
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin:users:nop")
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="▶",
                    callback_data=f"admin:users:referrals:list:{user_id}:page:{page + 1}"
                )
            )
        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=f"admin:users:referrals:{user_id}",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_user_message_cancel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура отмены при отправке сообщения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:users:view:{user_id}",
                ),
            ],
        ]
    )


def get_user_message_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения отправки сообщения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить",
                    callback_data=f"admin:users:message:confirm:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить текст",
                    callback_data=f"admin:users:message:edit:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:users:view:{user_id}",
                ),
            ],
        ]
    )


def get_user_message_back_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой назад после отправки сообщения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ К пользователю",
                    callback_data=f"admin:users:view:{user_id}",
                ),
            ],
        ]
    )


# ==================== УПРАВЛЕНИЕ ЗАКАЗАМИ ====================


def get_orders_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню управления заказами."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Все заказы",
                    callback_data=AdminCallback.ORDERS_LIST,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Поиск заказа",
                    callback_data=AdminCallback.ORDERS_SEARCH,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Проблемные заказы",
                    callback_data=AdminCallback.ORDERS_PROBLEMS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data=AdminCallback.ORDERS_STATS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.BACK,
                ),
            ],
        ]
    )


def get_orders_list_keyboard(
    orders: list,
    page: int = 0,
    per_page: int = 8,
    current_filter: str = "all",
) -> InlineKeyboardMarkup:
    """Клавиатура списка заказов с фильтрами."""
    buttons = []

    # Фильтры
    filters = [
        ("all", "Все"),
        ("pending", "⏳"),
        ("processing", "🔄"),
        ("completed", "✅"),
        ("failed", "❌"),
    ]

    filter_buttons = []
    for filter_key, filter_name in filters:
        mark = " ✓" if current_filter == filter_key else ""
        filter_buttons.append(
            InlineKeyboardButton(
                text=f"{filter_name}{mark}",
                callback_data=f"admin:orders:filter:{filter_key}",
            )
        )
    buttons.append(filter_buttons)

    # Список заказов
    total = len(orders)
    start = page * per_page
    end = start + per_page
    page_orders = orders[start:end]

    status_icons = {
        "pending": "⏳",
        "processing": "🔄",
        "completed": "✅",
        "failed": "❌",
        "cancelled": "🚫",
        "refunded": "💸",
    }

    for order in page_orders:
        icon = status_icons.get(order.status, "❓")
        created_msk = _to_moscow_time(order.created_at)
        date = created_msk.strftime("%d.%m %H:%M")
        qty_text = f"{order.quantity:,}⭐" if order.product_type == "stars" else f"{order.quantity}мес"
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {order.order_key} | @{order.recipient_username[:15]} | {qty_text} | {date}",
                callback_data=f"admin:orders:view:{order.id}",
            ),
        ])

    # Пагинация
    total_pages = max(1, (total + per_page - 1) // per_page)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀",
                    callback_data=f"admin:orders:page:{page - 1}:{current_filter}",
                )
            )
        nav_buttons.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="admin:orders:nop",
            )
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="▶",
                    callback_data=f"admin:orders:page:{page + 1}:{current_filter}",
                )
            )
        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.ORDERS,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_order_detail_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup:
    """Клавиатура детального просмотра заказа."""
    buttons = []

    # Действия в зависимости от статуса
    if status == "failed":
        buttons.append([
            InlineKeyboardButton(
                text="🔄 Повторить заказ",
                callback_data=f"admin:orders:retry:{order_id}",
            ),
        ])
        buttons.append([
            InlineKeyboardButton(
                text="💸 Вернуть деньги",
                callback_data=f"admin:orders:refund:{order_id}",
            ),
        ])
    elif status == "pending":
        buttons.append([
            InlineKeyboardButton(
                text="❌ Отменить",
                callback_data=f"admin:orders:cancel:{order_id}",
            ),
        ])
    elif status == "completed":
        buttons.append([
            InlineKeyboardButton(
                text="💸 Вернуть деньги",
                callback_data=f"admin:orders:refund:{order_id}",
            ),
        ])

    # Переход к пользователю
    buttons.append([
        InlineKeyboardButton(
            text="👤 Пользователь",
            callback_data=f"admin:orders:user:{order_id}",
        ),
    ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ К списку",
            callback_data=AdminCallback.ORDERS_LIST,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_orders_stats_keyboard(period: str = "all") -> InlineKeyboardMarkup:
    """Клавиатура статистики заказов с выбором периода."""
    periods = [
        ("24h", "24ч"),
        ("7d", "7д"),
        ("30d", "30д"),
        ("all", "Всё"),
    ]

    period_buttons = []
    for period_key, period_name in periods:
        mark = " ✓" if period == period_key else ""
        period_buttons.append(
            InlineKeyboardButton(
                text=f"{period_name}{mark}",
                callback_data=f"admin:orders:stats:{period_key}",
            )
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            period_buttons,
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=AdminCallback.ORDERS,
                ),
            ],
        ]
    )


def get_orders_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены ввода."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=AdminCallback.ORDERS_CANCEL,
                ),
            ],
        ]
    )


def get_order_confirm_action_keyboard(order_id: int, action: str) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения действия над заказом."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"admin:orders:{action}:confirm:{order_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:orders:view:{order_id}",
                ),
            ],
        ]
    )


def get_orders_problems_keyboard(orders: list, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    """Клавиатура проблемных заказов (failed + processing > 10 min)."""
    buttons = []

    total = len(orders)
    start = page * per_page
    end = start + per_page
    page_orders = orders[start:end]

    status_icons = {
        "pending": "⏳",
        "processing": "🔄",
        "failed": "❌",
    }

    for order in page_orders:
        icon = status_icons.get(order.status, "❓")
        created_msk = _to_moscow_time(order.created_at)
        date = created_msk.strftime("%d.%m %H:%M")
        qty_text = f"{order.quantity:,}⭐" if order.product_type == "stars" else f"{order.quantity}мес"
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {order.order_key} | @{order.recipient_username[:12]} | {qty_text} | {date}",
                callback_data=f"admin:orders:view:{order.id}",
            ),
        ])

    # Пагинация
    total_pages = max(1, (total + per_page - 1) // per_page)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="◀", callback_data=f"admin:orders:problems:page:{page - 1}")
            )
        nav_buttons.append(
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin:orders:nop")
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="▶", callback_data=f"admin:orders:problems:page:{page + 1}")
            )
        buttons.append(nav_buttons)

    # Массовые действия если есть failed заказы
    failed_orders = [o for o in orders if o.status == "failed"]
    if failed_orders:
        buttons.append([
            InlineKeyboardButton(
                text=f"🔄 Повторить все ({len(failed_orders)})",
                callback_data="admin:orders:retry:all",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=AdminCallback.ORDERS,
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)
