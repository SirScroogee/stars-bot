"""Keyboards for giveaway administration."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def admin_giveaway_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать розыгрыш", callback_data="admin:giveaways:create", style="success")],
            [InlineKeyboardButton(text="📋 Все розыгрыши", callback_data="admin:giveaways:list:0")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")],
        ]
    )


def admin_giveaway_cancel_wizard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")]]
    )


def admin_giveaway_description_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="admin:giveaways:create:description:skip")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")],
        ]
    )


def admin_giveaway_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Одна покупка", callback_data="admin:giveaways:mode:purchase_once")],
            [InlineKeyboardButton(text="🎟 Билеты за каждый заказ", callback_data="admin:giveaways:mode:tickets_per_order")],
            [InlineKeyboardButton(text="⭐ Билеты за N Stars", callback_data="admin:giveaways:mode:tickets_per_stars")],
            [InlineKeyboardButton(text="👤 Любое действие в боте", callback_data="admin:giveaways:mode:registration_all")],
            [InlineKeyboardButton(text="🆕 Только новые пользователи", callback_data="admin:giveaways:mode:registration_new")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")],
        ]
    )


def admin_giveaway_product_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐ Stars", callback_data="admin:giveaways:product:stars"),
                InlineKeyboardButton(text="👑 Premium", callback_data="admin:giveaways:product:premium"),
            ],
            [InlineKeyboardButton(text="⭐ Stars и 👑 Premium", callback_data="admin:giveaways:product:all")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")],
        ]
    )


def admin_giveaway_prizes_keyboard(has_prizes: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Добавить призовое место", callback_data="admin:giveaways:prize:add")]]
    if has_prizes:
        rows.append([InlineKeyboardButton(text="Продолжить", callback_data="admin:giveaways:prize:done", style="success")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_giveaway_prize_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐ Stars", callback_data="admin:giveaways:prize:type:stars"),
                InlineKeyboardButton(text="👑 Premium", callback_data="admin:giveaways:prize:type:premium"),
            ],
            [InlineKeyboardButton(text="🎁 Другой приз", callback_data="admin:giveaways:prize:type:custom")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")],
        ]
    )


def admin_giveaway_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Запустить сразу", callback_data="admin:giveaways:start:now", style="success")],
            [InlineKeyboardButton(text="🗓 Запланировать", callback_data="admin:giveaways:start:scheduled")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")],
        ]
    )


def admin_giveaway_channels_keyboard(channels: list) -> InlineKeyboardMarkup:
    rows = []
    for channel in channels:
        title = channel.channel_title if len(channel.channel_title) <= 35 else channel.channel_title[:32] + "..."
        rows.append([InlineKeyboardButton(text=f"📢 {title}", callback_data=f"admin:giveaways:channel:{channel.id}")])
    rows.append([InlineKeyboardButton(text="Без публикации", callback_data="admin:giveaways:channel:none")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_giveaway_publication_keyboard(announcement: bool, results: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅' if announcement else '⬜'} Анонс",
                    callback_data="admin:giveaways:publication:announcement",
                ),
                InlineKeyboardButton(
                    text=f"{'✅' if results else '⬜'} Результаты",
                    callback_data="admin:giveaways:publication:results",
                ),
            ],
            [InlineKeyboardButton(text="Продолжить", callback_data="admin:giveaways:publication:done", style="success")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")],
        ]
    )


def admin_giveaway_photo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Без изображения", callback_data="admin:giveaways:photo:skip")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")],
        ]
    )


def admin_giveaway_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать", callback_data="admin:giveaways:create:confirm", style="success")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways:create:back")],
        ]
    )


def admin_giveaway_list_keyboard(giveaways: list, page: int, total: int, page_size: int = 8) -> InlineKeyboardMarkup:
    icons = {"scheduled": "🗓", "active": "🟢", "drawing": "🎲", "completed": "🏁", "cancelled": "🚫"}
    rows = []
    for giveaway in giveaways:
        title = giveaway.title if len(giveaway.title) <= 30 else giveaway.title[:27] + "..."
        rows.append(
            [InlineKeyboardButton(text=f"{icons.get(giveaway.status, '🎁')} #{giveaway.id} {title}", callback_data=f"admin:giveaways:view:{giveaway.id}")]
        )
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"admin:giveaways:list:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin:giveaways:nop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"admin:giveaways:list:{page + 1}"))
        rows.append(nav)
    rows.extend(
        [
            [InlineKeyboardButton(text="➕ Создать", callback_data="admin:giveaways:create", style="success")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:giveaways")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_giveaway_detail_keyboard(giveaway, winners: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="👥 Участники", callback_data=f"admin:giveaways:entries:{giveaway.id}:0")]]
    if giveaway.status == "completed":
        rows.append([InlineKeyboardButton(text="🔍 Аудит выбора", callback_data=f"admin:giveaways:audit:{giveaway.id}")])
        for winner in sorted(winners, key=lambda item: item.place):
            marker = "✅" if winner.prize.is_issued else "⬜"
            rows.append(
                [InlineKeyboardButton(text=f"{marker} Приз за {winner.place} место выдан", callback_data=f"admin:giveaways:issue:{winner.prize_id}")]
            )
    if giveaway.status in {"scheduled", "active"}:
        rows.append([InlineKeyboardButton(text="🚫 Отменить розыгрыш", callback_data=f"admin:giveaways:cancel:{giveaway.id}", style="danger")])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="admin:giveaways:list:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_giveaway_entries_keyboard(giveaway_id: int, page: int, has_next: bool) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"admin:giveaways:entries:{giveaway_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}", callback_data="admin:giveaways:nop"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"admin:giveaways:entries:{giveaway_id}:{page + 1}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[nav, [InlineKeyboardButton(text="◀️ К розыгрышу", callback_data=f"admin:giveaways:view:{giveaway_id}")]]
    )


def admin_giveaway_back_keyboard(giveaway_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ К розыгрышу", callback_data=f"admin:giveaways:view:{giveaway_id}")]]
    )
