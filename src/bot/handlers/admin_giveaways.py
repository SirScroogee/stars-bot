"""Administrative giveaway creation wizard and management screens."""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from src.bot.handlers.admin_utils import MOSCOW_TZ, check_admin, check_admin_message, to_moscow_time
from src.bot.keyboards.admin_giveaways import (
    admin_giveaway_back_keyboard,
    admin_giveaway_cancel_wizard_keyboard,
    admin_giveaway_channels_keyboard,
    admin_giveaway_confirm_keyboard,
    admin_giveaway_description_keyboard,
    admin_giveaway_detail_keyboard,
    admin_giveaway_entries_keyboard,
    admin_giveaway_list_keyboard,
    admin_giveaway_menu_keyboard,
    admin_giveaway_mode_keyboard,
    admin_giveaway_photo_keyboard,
    admin_giveaway_prize_type_keyboard,
    admin_giveaway_prizes_keyboard,
    admin_giveaway_product_keyboard,
    admin_giveaway_publication_keyboard,
    admin_giveaway_start_keyboard,
)
from src.db.models import BotChannel, Giveaway, GiveawayPrize, GiveawayWinner, User
from src.db.session import async_session_factory
from src.services.giveaway_service import GiveawayService, condition_text, prize_text

logger = logging.getLogger(__name__)
router = Router(name="admin_giveaways")


class GiveawayAdminStates(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_ticket_config = State()
    waiting_prize_value = State()
    waiting_start = State()
    waiting_end = State()
    waiting_photo = State()
    waiting_cancel_reason = State()


def _parse_moscow_datetime(value: str) -> datetime | None:
    try:
        local = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW_TZ)
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _fmt(value: datetime) -> str:
    return to_moscow_time(value).strftime("%d.%m.%Y %H:%M МСК")


def _prize_draft_text(item: dict) -> str:
    if item["prize_type"] == "stars":
        return f"{Decimal(item['amount']):,.0f} Stars"
    if item["prize_type"] == "premium":
        return f"Premium на {Decimal(item['amount']):,.0f} мес."
    return item["description"]


async def _edit_wizard(state: FSMContext, bot, text: str, reply_markup) -> None:
    data = await state.get_data()
    await bot.edit_message_text(
        chat_id=data["wizard_chat_id"],
        message_id=data["wizard_message_id"],
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _get_draft(state: FSMContext) -> dict:
    data = await state.get_data()
    return dict(data.get("giveaway_draft") or {})


async def _save_draft(state: FSMContext, draft: dict) -> None:
    await state.update_data(giveaway_draft=draft)


def _prompt(text: str, error: str | None = None) -> str:
    if not error:
        return text
    return f"❌ <b>{html.escape(error)}</b>\n\n{text}"


async def _set_wizard_step(state: FSMContext, step: str, fsm_state=None) -> None:
    await state.update_data(wizard_step=step)
    await state.set_state(fsm_state)


async def _delete_admin_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception as exc:
        logger.debug("Could not delete giveaway wizard input %s: %s", message.message_id, exc)


async def _show_title(state: FSMContext, bot, error: str | None = None) -> None:
    await _set_wizard_step(state, "title", GiveawayAdminStates.waiting_title)
    await _edit_wizard(
        state,
        bot,
        _prompt("🎉 <b>Новый розыгрыш</b>\n\nВведите техническое название (до 200 символов):", error),
        admin_giveaway_cancel_wizard_keyboard(),
    )


async def _show_description(state: FSMContext, bot, error: str | None = None) -> None:
    await _set_wizard_step(state, "description", GiveawayAdminStates.waiting_description)
    await _edit_wizard(
        state,
        bot,
        _prompt(
            "📝 <b>Описание</b>\n\nВведите текст для страницы и поста (до 1500 символов):",
            error,
        ),
        admin_giveaway_description_keyboard(),
    )


async def _show_mode(state: FSMContext, bot) -> None:
    await _set_wizard_step(state, "mode")
    await _edit_wizard(
        state,
        bot,
        "🎯 <b>Условие участия</b>\n\nВыберите один режим. Покупки засчитываются только после успешного выполнения заказа.",
        admin_giveaway_mode_keyboard(),
    )


async def _show_prizes(state: FSMContext, bot) -> None:
    await _set_wizard_step(state, "prizes")
    draft = await _get_draft(state)
    prizes = draft.get("prizes", [])
    lines = ["🏆 <b>Призовые места</b>", ""]
    if prizes:
        lines.extend(f"{index}. {html.escape(_prize_draft_text(item))}" for index, item in enumerate(prizes, 1))
    else:
        lines.append("Добавьте хотя бы один приз.")
    await _edit_wizard(state, bot, "\n".join(lines), admin_giveaway_prizes_keyboard(bool(prizes)))


async def _show_product(state: FSMContext, bot) -> None:
    await _set_wizard_step(state, "product")
    await _edit_wizard(
        state,
        bot,
        "🛍 <b>Тип покупки</b>\n\nКакие товары засчитывать?",
        admin_giveaway_product_keyboard(),
    )


async def _show_ticket_config(state: FSMContext, bot, error: str | None = None) -> None:
    draft = await _get_draft(state)
    await _set_wizard_step(state, "ticket_config", GiveawayAdminStates.waiting_ticket_config)
    if draft.get("participation_mode") == "tickets_per_order":
        text = "🎟 <b>Билеты за заказ</b>\n\nСколько билетов выдавать за каждый выполненный заказ?"
    else:
        text = (
            "🎟 <b>Билеты за Stars</b>\n\nВведите количество Stars в одном заказе, "
            "за которое выдаётся 1 билет. Например: <code>100</code>"
        )
    await _edit_wizard(
        state,
        bot,
        _prompt(text, error),
        admin_giveaway_cancel_wizard_keyboard(),
    )


async def _show_prize_type(state: FSMContext, bot) -> None:
    await _set_wizard_step(state, "prize_type")
    await _edit_wizard(
        state,
        bot,
        "🏆 <b>Тип приза</b>",
        admin_giveaway_prize_type_keyboard(),
    )


async def _show_prize_value(state: FSMContext, bot, error: str | None = None) -> None:
    data = await state.get_data()
    prize_type = data.get("current_prize_type")
    prompts = {
        "stars": "Введите количество Stars для этого места:",
        "premium": "Введите количество месяцев Premium:",
        "custom": "Введите название или описание приза:",
    }
    await _set_wizard_step(state, "prize_value", GiveawayAdminStates.waiting_prize_value)
    await _edit_wizard(
        state,
        bot,
        _prompt(f"🏆 <b>Приз</b>\n\n{prompts.get(prize_type, 'Введите значение приза:')}", error),
        admin_giveaway_cancel_wizard_keyboard(),
    )


async def _show_start_mode(state: FSMContext, bot) -> None:
    await _set_wizard_step(state, "start_mode")
    await _edit_wizard(
        state,
        bot,
        "▶️ <b>Начало розыгрыша</b>\n\nЗапустить сразу или запланировать?",
        admin_giveaway_start_keyboard(),
    )


async def _show_start_date(state: FSMContext, bot, error: str | None = None) -> None:
    await _set_wizard_step(state, "start_date", GiveawayAdminStates.waiting_start)
    await _edit_wizard(
        state,
        bot,
        _prompt(
            "🗓 <b>Запланированный старт</b>\n\nВведите дату и время по Москве в формате "
            "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>",
            error,
        ),
        admin_giveaway_cancel_wizard_keyboard(),
    )


async def _show_end_date(state: FSMContext, bot, error: str | None = None) -> None:
    await _set_wizard_step(state, "end_date", GiveawayAdminStates.waiting_end)
    await _edit_wizard(
        state,
        bot,
        _prompt(
            "⏰ <b>Окончание</b>\n\nВведите дату и время по Москве в формате "
            "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>",
            error,
        ),
        admin_giveaway_cancel_wizard_keyboard(),
    )


async def _show_channels(state: FSMContext, bot) -> None:
    await _set_wizard_step(state, "channel")
    async with async_session_factory() as session:
        channels = list(
            (
                await session.execute(
                    select(BotChannel).where(BotChannel.is_active.is_(True)).order_by(BotChannel.channel_title)
                )
            ).scalars().all()
        )
    await _edit_wizard(
        state,
        bot,
        "📢 <b>Канал или чат</b>\n\nВыберите место автоматической публикации либо продолжите без публикации.",
        admin_giveaway_channels_keyboard(channels),
    )


async def _show_publication(state: FSMContext, bot) -> None:
    draft = await _get_draft(state)
    await _set_wizard_step(state, "publication")
    await _edit_wizard(
        state,
        bot,
        "📢 <b>Автопубликация</b>\n\nВыберите, какие сообщения отправлять:",
        admin_giveaway_publication_keyboard(
            bool(draft.get("publish_announcement")),
            bool(draft.get("publish_results")),
        ),
    )


async def _show_photo(state: FSMContext, bot, error: str | None = None) -> None:
    await _set_wizard_step(state, "photo", GiveawayAdminStates.waiting_photo)
    await _edit_wizard(
        state,
        bot,
        _prompt(
            "🖼 <b>Изображение</b>\n\nОтправьте фотографию для страницы и поста или пропустите.",
            error,
        ),
        admin_giveaway_photo_keyboard(),
    )


async def _show_confirmation(state: FSMContext, bot) -> None:
    draft = await _get_draft(state)
    await _set_wizard_step(state, "confirmation")
    await _edit_wizard(state, bot, _confirmation_text(draft), admin_giveaway_confirm_keyboard())


def _previous_wizard_step(step: str | None, draft: dict) -> str:
    if not step or step == "title":
        return "menu"
    if step == "ticket_config":
        return "product" if draft.get("participation_mode") == "tickets_per_order" else "mode"
    if step == "prizes":
        mode = draft.get("participation_mode")
        if mode == "purchase_once":
            return "product"
        if mode in {"tickets_per_order", "tickets_per_stars"}:
            return "ticket_config"
        return "mode"
    if step == "end_date":
        return "start_date" if draft.get("start_mode") == "scheduled" else "start_mode"
    if step == "photo":
        return "publication" if draft.get("publish_chat_id") else "channel"
    return {
        "description": "title",
        "mode": "description",
        "product": "mode",
        "prize_type": "prizes",
        "prize_value": "prize_type",
        "start_mode": "prizes",
        "start_date": "start_mode",
        "channel": "end_date",
        "publication": "channel",
        "confirmation": "photo",
    }.get(step, "menu")


def _confirmation_text(draft: dict) -> str:
    start_text = "сразу после создания" if draft.get("start_mode") == "now" else _fmt(draft["starts_at"])
    lines = [
        "✅ <b>Подтверждение розыгрыша</b>",
        "",
        f"Название: <b>{html.escape(draft['title'])}</b>",
        f"Условие: <b>{html.escape(draft['condition_preview'])}</b>",
        f"Начало: <b>{start_text}</b>",
        f"Окончание: <b>{_fmt(draft['ends_at'])}</b>",
        "Ожидание незавершённых заказов: <b>15 минут</b>",
        "",
        "<b>Призы:</b>",
    ]
    lines.extend(f"{index}. {html.escape(_prize_draft_text(item))}" for index, item in enumerate(draft["prizes"], 1))
    lines.extend(
        [
            "",
            f"Канал: <b>{draft.get('channel_title') or 'не выбран'}</b>",
            f"Анонс: <b>{'да' if draft.get('publish_announcement') else 'нет'}</b>",
            f"Результаты: <b>{'да' if draft.get('publish_results') else 'нет'}</b>",
            f"Изображение: <b>{'да' if draft.get('photo_file_id') else 'нет'}</b>",
        ]
    )
    return "\n".join(lines)


@router.callback_query(F.data == "admin:giveaways")
async def callback_giveaway_admin_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    await state.clear()
    async with async_session_factory() as session:
        service = GiveawayService(session)
        total = await service.count_giveaways()
        active = len(await service.list_public_active())
    await callback.message.edit_text(
        f"🎉 <b>Розыгрыши</b>\n\nВсего: <b>{total}</b>\nАктивных: <b>{active}</b>",
        parse_mode="HTML",
        reply_markup=admin_giveaway_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:giveaways:create")
async def callback_giveaway_create(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    await state.clear()
    await state.update_data(
        wizard_chat_id=callback.message.chat.id,
        wizard_message_id=callback.message.message_id,
        giveaway_draft={"prizes": []},
    )
    await _show_title(state, callback.bot)
    await callback.answer()


@router.message(GiveawayAdminStates.waiting_title)
async def message_giveaway_title(message: Message, state: FSMContext) -> None:
    if not await check_admin_message(message):
        return
    title = (message.text or "").strip()
    await _delete_admin_input(message)
    if not 2 <= len(title) <= 200:
        await _show_title(state, message.bot, "Название должно содержать от 2 до 200 символов.")
        return
    draft = await _get_draft(state)
    draft["title"] = title
    await _save_draft(state, draft)
    await _show_description(state, message.bot)


@router.message(GiveawayAdminStates.waiting_description)
async def message_giveaway_description(message: Message, state: FSMContext) -> None:
    if not await check_admin_message(message):
        return
    description = (message.text or "").strip()
    await _delete_admin_input(message)
    if len(description) > 1500:
        await _show_description(state, message.bot, "Описание не должно превышать 1500 символов.")
        return
    draft = await _get_draft(state)
    draft["description"] = description or None
    await _save_draft(state, draft)
    await _show_mode(state, message.bot)


@router.callback_query(F.data == "admin:giveaways:create:description:skip")
async def callback_giveaway_description_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    draft = await _get_draft(state)
    draft["description"] = None
    await _save_draft(state, draft)
    await state.set_state(None)
    await _show_mode(state, callback.bot)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:giveaways:mode:"))
async def callback_giveaway_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    mode = callback.data.rsplit(":", 1)[-1]
    if mode not in {"purchase_once", "tickets_per_order", "tickets_per_stars", "registration_all", "registration_new"}:
        await callback.answer("Неизвестный режим", show_alert=True)
        return
    draft = await _get_draft(state)
    draft.update(participation_mode=mode, tickets_per_order=1, stars_per_ticket=None, product_filter=None)
    await _save_draft(state, draft)
    if mode in {"purchase_once", "tickets_per_order"}:
        await _show_product(state, callback.bot)
    elif mode == "tickets_per_stars":
        draft["product_filter"] = "stars"
        await _save_draft(state, draft)
        await _show_ticket_config(state, callback.bot)
    else:
        draft["condition_preview"] = (
            "Зайти в бот и совершить любое действие во время розыгрыша"
            if mode == "registration_all"
            else "Зарегистрироваться в боте во время розыгрыша"
        )
        await _save_draft(state, draft)
        await _show_prizes(state, callback.bot)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:giveaways:product:"))
async def callback_giveaway_product(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    product = callback.data.rsplit(":", 1)[-1]
    if product not in {"all", "stars", "premium"}:
        await callback.answer("Неизвестный товар", show_alert=True)
        return
    draft = await _get_draft(state)
    draft["product_filter"] = product
    if draft["participation_mode"] == "tickets_per_order":
        await _save_draft(state, draft)
        await _show_ticket_config(state, callback.bot)
    else:
        draft["condition_preview"] = {
            "all": "Совершить любую покупку в боте",
            "stars": "Купить Stars в боте",
            "premium": "Купить Telegram Premium в боте",
        }[product]
        await _save_draft(state, draft)
        await _show_prizes(state, callback.bot)
    await callback.answer()


@router.message(GiveawayAdminStates.waiting_ticket_config)
async def message_giveaway_ticket_config(message: Message, state: FSMContext) -> None:
    if not await check_admin_message(message):
        return
    try:
        value = int((message.text or "").strip())
    except ValueError:
        value = 0
    await _delete_admin_input(message)
    if not 1 <= value <= 1_000_000:
        await _show_ticket_config(state, message.bot, "Введите целое число от 1 до 1 000 000.")
        return
    draft = await _get_draft(state)
    if draft["participation_mode"] == "tickets_per_order":
        draft["tickets_per_order"] = value
        draft["condition_preview"] = {
            "all": f"За каждую покупку в боте начисляется {value} билет(а)",
            "stars": f"За каждую покупку Stars начисляется {value} билет(а)",
            "premium": f"За каждую покупку Telegram Premium начисляется {value} билет(а)",
        }[draft["product_filter"]]
    else:
        draft["stars_per_ticket"] = value
        draft["tickets_per_order"] = 1
        draft["condition_preview"] = f"За каждые {value} купленных Stars начисляется 1 билет"
    await _save_draft(state, draft)
    await _show_prizes(state, message.bot)


@router.callback_query(F.data == "admin:giveaways:prize:add")
async def callback_giveaway_prize_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    await _show_prize_type(state, callback.bot)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:giveaways:prize:type:"))
async def callback_giveaway_prize_type(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    prize_type = callback.data.rsplit(":", 1)[-1]
    if prize_type not in {"stars", "premium", "custom"}:
        return
    await state.update_data(current_prize_type=prize_type)
    await _show_prize_value(state, callback.bot)
    await callback.answer()


@router.message(GiveawayAdminStates.waiting_prize_value)
async def message_giveaway_prize_value(message: Message, state: FSMContext) -> None:
    if not await check_admin_message(message):
        return
    data = await state.get_data()
    prize_type = data.get("current_prize_type")
    raw = (message.text or "").strip()
    await _delete_admin_input(message)
    item = {"prize_type": prize_type, "amount": None, "description": None}
    if prize_type in {"stars", "premium"}:
        try:
            amount = Decimal(raw.replace(",", "."))
        except InvalidOperation:
            amount = Decimal(0)
        if (
            not amount.is_finite()
            or amount <= 0
            or amount != amount.to_integral_value()
        ):
            await _show_prize_value(
                state,
                message.bot,
                "Введите положительное целое число.",
            )
            return
        item["amount"] = str(amount)
    else:
        if not 2 <= len(raw) <= 300:
            await _show_prize_value(
                state,
                message.bot,
                "Описание приза должно содержать от 2 до 300 символов.",
            )
            return
        item["description"] = raw
    draft = await _get_draft(state)
    draft.setdefault("prizes", []).append(item)
    await _save_draft(state, draft)
    await _show_prizes(state, message.bot)


@router.callback_query(F.data == "admin:giveaways:prize:done")
async def callback_giveaway_prizes_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    draft = await _get_draft(state)
    if not draft.get("prizes"):
        await callback.answer("Добавьте хотя бы один приз", show_alert=True)
        return
    await _show_start_mode(state, callback.bot)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:giveaways:start:"))
async def callback_giveaway_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    mode = callback.data.rsplit(":", 1)[-1]
    if mode == "now":
        draft = await _get_draft(state)
        draft["start_mode"] = "now"
        draft["starts_at"] = datetime.utcnow()
        await _save_draft(state, draft)
        await _show_end_date(state, callback.bot)
    else:
        draft = await _get_draft(state)
        draft["start_mode"] = "scheduled"
        await _save_draft(state, draft)
        await _show_start_date(state, callback.bot)
    await callback.answer()


@router.message(GiveawayAdminStates.waiting_start)
async def message_giveaway_start(message: Message, state: FSMContext) -> None:
    if not await check_admin_message(message):
        return
    value = _parse_moscow_datetime(message.text or "")
    await _delete_admin_input(message)
    if not value or value <= datetime.utcnow():
        await _show_start_date(
            state,
            message.bot,
            "Введите будущую дату в формате ДД.ММ.ГГГГ ЧЧ:ММ по Москве.",
        )
        return
    draft = await _get_draft(state)
    draft["starts_at"] = value
    await _save_draft(state, draft)
    await _show_end_date(state, message.bot)


@router.message(GiveawayAdminStates.waiting_end)
async def message_giveaway_end(message: Message, state: FSMContext) -> None:
    if not await check_admin_message(message):
        return
    value = _parse_moscow_datetime(message.text or "")
    await _delete_admin_input(message)
    draft = await _get_draft(state)
    if not value or value <= draft["starts_at"] or value <= datetime.utcnow():
        await _show_end_date(
            state,
            message.bot,
            "Окончание должно быть позже начала. Формат: ДД.ММ.ГГГГ ЧЧ:ММ по Москве.",
        )
        return
    draft["ends_at"] = value
    await _save_draft(state, draft)
    await _show_channels(state, message.bot)


@router.callback_query(F.data.startswith("admin:giveaways:channel:"))
async def callback_giveaway_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    value = callback.data.rsplit(":", 1)[-1]
    draft = await _get_draft(state)
    if value == "none":
        draft.update(publish_chat_id=None, channel_title=None, publish_announcement=False, publish_results=False)
        await _save_draft(state, draft)
        await _show_photo(state, callback.bot)
    else:
        try:
            channel_pk = int(value)
        except ValueError:
            return
        async with async_session_factory() as session:
            channel = await session.scalar(
                select(BotChannel).where(BotChannel.id == channel_pk, BotChannel.is_active.is_(True))
            )
        if not channel:
            await callback.answer("Канал больше недоступен", show_alert=True)
            return
        try:
            bot_info = await callback.bot.get_me()
            membership = await callback.bot.get_chat_member(channel.channel_id, bot_info.id)
            can_post = getattr(membership, "can_post_messages", None)
            if can_post is False:
                await callback.answer(
                    "У бота нет права публиковать сообщения в этом канале",
                    show_alert=True,
                )
                return
        except Exception as exc:
            logger.warning("Could not validate giveaway channel %s: %s", channel.channel_id, exc)
            await callback.answer(
                "Не удалось проверить права бота в канале. Добавьте боту право публикации сообщений.",
                show_alert=True,
            )
            return
        draft.update(
            publish_chat_id=channel.channel_id,
            channel_title=channel.channel_title,
            publish_announcement=True,
            publish_results=True,
        )
        await _save_draft(state, draft)
        await _show_publication(state, callback.bot)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:giveaways:publication:"))
async def callback_giveaway_publication(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    action = callback.data.rsplit(":", 1)[-1]
    draft = await _get_draft(state)
    if action in {"announcement", "results"}:
        key = f"publish_{action}"
        draft[key] = not bool(draft.get(key))
        await _save_draft(state, draft)
        await callback.message.edit_reply_markup(
            reply_markup=admin_giveaway_publication_keyboard(
                bool(draft.get("publish_announcement")), bool(draft.get("publish_results"))
            )
        )
    else:
        await _show_photo(state, callback.bot)
    await callback.answer()


@router.message(GiveawayAdminStates.waiting_photo, F.photo)
async def message_giveaway_photo(message: Message, state: FSMContext) -> None:
    if not await check_admin_message(message):
        return
    draft = await _get_draft(state)
    draft["photo_file_id"] = message.photo[-1].file_id
    await _delete_admin_input(message)
    await _save_draft(state, draft)
    await _show_confirmation(state, message.bot)


@router.message(GiveawayAdminStates.waiting_photo)
async def message_giveaway_photo_invalid(message: Message, state: FSMContext) -> None:
    if await check_admin_message(message):
        await _delete_admin_input(message)
        await _show_photo(state, message.bot, "Отправьте фотографию или нажмите «Без изображения».")


@router.callback_query(F.data == "admin:giveaways:photo:skip")
async def callback_giveaway_photo_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    draft = await _get_draft(state)
    draft["photo_file_id"] = None
    await _save_draft(state, draft)
    await _show_confirmation(state, callback.bot)
    await callback.answer()


@router.callback_query(F.data == "admin:giveaways:create:confirm")
async def callback_giveaway_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    draft = await _get_draft(state)
    if draft.get("start_mode") == "now":
        draft["starts_at"] = datetime.utcnow()
        if draft["ends_at"] <= draft["starts_at"]:
            await _save_draft(state, draft)
            await _show_end_date(state, callback.bot, "Время окончания уже прошло. Укажите новое время.")
            await callback.answer("Укажите новое время окончания", show_alert=True)
            return
        await _save_draft(state, draft)
    try:
        async with async_session_factory() as session:
            service = GiveawayService(session)
            giveaway = await service.create_giveaway(
                title=draft["title"],
                description=draft.get("description"),
                photo_file_id=draft.get("photo_file_id"),
                participation_mode=draft["participation_mode"],
                product_filter=draft.get("product_filter"),
                tickets_per_order=int(draft.get("tickets_per_order", 1)),
                stars_per_ticket=draft.get("stars_per_ticket"),
                starts_at=draft["starts_at"],
                ends_at=draft["ends_at"],
                publish_chat_id=draft.get("publish_chat_id"),
                publish_announcement=bool(draft.get("publish_announcement")),
                publish_results=bool(draft.get("publish_results")),
                created_by=callback.from_user.id,
                prizes=draft["prizes"],
            )
            giveaway_id = giveaway.id
            await session.commit()
    except Exception as exc:
        logger.exception("Failed to create giveaway")
        await callback.answer(f"Ошибка создания: {str(exc)[:120]}", show_alert=True)
        return
    await state.clear()
    await callback.answer("Розыгрыш создан")
    await _render_giveaway(callback, giveaway_id)


@router.callback_query(F.data == "admin:giveaways:create:back")
@router.callback_query(F.data == "admin:giveaways:create:cancel")
async def callback_giveaway_create_back(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    data = await state.get_data()
    step = data.get("wizard_step")
    draft = await _get_draft(state)
    previous = _previous_wizard_step(step, draft)

    if previous == "menu":
        await callback_giveaway_admin_menu(callback, state)
        return
    if previous == "title":
        await _show_title(state, callback.bot)
    elif previous == "description":
        await _show_description(state, callback.bot)
    elif previous == "mode":
        await _show_mode(state, callback.bot)
    elif previous == "product":
        await _show_product(state, callback.bot)
    elif previous == "ticket_config":
        await _show_ticket_config(state, callback.bot)
    elif previous == "prizes":
        await _show_prizes(state, callback.bot)
    elif previous == "prize_type":
        await _show_prize_type(state, callback.bot)
    elif previous == "start_mode":
        await _show_start_mode(state, callback.bot)
    elif previous == "start_date":
        await _show_start_date(state, callback.bot)
    elif previous == "end_date":
        await _show_end_date(state, callback.bot)
    elif previous == "channel":
        await _show_channels(state, callback.bot)
    elif previous == "publication":
        await _show_publication(state, callback.bot)
    elif previous == "photo":
        await _show_photo(state, callback.bot)
    else:
        await callback_giveaway_admin_menu(callback, state)
        return
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:giveaways:list:\d+$"))
async def callback_giveaway_list(callback: CallbackQuery) -> None:
    if not await check_admin(callback):
        return
    page = int(callback.data.rsplit(":", 1)[-1])
    page_size = 8
    async with async_session_factory() as session:
        service = GiveawayService(session)
        total = await service.count_giveaways()
        giveaways = await service.list_giveaways(offset=page * page_size, limit=page_size)
    await callback.message.edit_text(
        f"📋 <b>Все розыгрыши</b>\n\nВсего: <b>{total}</b>",
        parse_mode="HTML",
        reply_markup=admin_giveaway_list_keyboard(giveaways, page, total, page_size),
    )
    await callback.answer()


async def _render_giveaway(callback: CallbackQuery, giveaway_id: int) -> None:
    async with async_session_factory() as session:
        service = GiveawayService(session)
        giveaway = await service.get_giveaway(giveaway_id)
        if not giveaway:
            await callback.answer("Розыгрыш не найден", show_alert=True)
            return
        participants, tickets = await service.get_entry_stats(giveaway.id)
        winners = sorted(giveaway.winners, key=lambda item: item.place)
        status = {
            "scheduled": "🗓 Запланирован",
            "active": "🟢 Активен",
            "drawing": "🎲 Подведение итогов",
            "completed": "🏁 Завершён",
            "cancelled": "🚫 Отменён",
        }.get(giveaway.status, giveaway.status)
        lines = [
            f"🎁 <b>#{giveaway.id} {html.escape(giveaway.title)}</b>",
            "",
            f"Статус: <b>{status}</b>",
            f"Условие: <b>{html.escape(condition_text(giveaway))}</b>",
            f"Начало: <b>{_fmt(giveaway.starts_at)}</b>",
            f"Окончание: <b>{_fmt(giveaway.ends_at)}</b>",
            f"Участников: <b>{participants}</b>",
            f"Билетов: <b>{tickets}</b>",
            "",
            "<b>Призы:</b>",
        ]
        lines.extend(
            f"{prize.place}. {html.escape(prize_text(prize))}"
            for prize in sorted(giveaway.prizes, key=lambda item: item.place)
        )
        if winners:
            lines.extend(["", "<b>Победители:</b>"])
            for winner in winners:
                user = await session.get(User, winner.user_id)
                identity = f"@{html.escape(user.username)}" if user and user.username else str(winner.user_id)
                issued = "✅ выдан" if winner.prize.is_issued else "⏳ ожидает выдачи"
                lines.append(f"{winner.place}. {identity} — {html.escape(prize_text(winner.prize))} ({issued})")
        if giveaway.cancel_reason:
            lines.extend(["", f"Причина отмены: {html.escape(giveaway.cancel_reason)}"])
        if giveaway.announcement_error:
            lines.extend(["", f"Ошибка анонса: <code>{html.escape(giveaway.announcement_error[:300])}</code>"])
        if giveaway.results_error:
            lines.extend(["", f"Ошибка публикации итогов: <code>{html.escape(giveaway.results_error[:300])}</code>"])
        keyboard = admin_giveaway_detail_keyboard(giveaway, winners)
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.regexp(r"^admin:giveaways:view:\d+$"))
async def callback_giveaway_view(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    await state.clear()
    await _render_giveaway(callback, int(callback.data.rsplit(":", 1)[-1]))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:giveaways:entries:\d+:\d+$"))
async def callback_giveaway_entries(callback: CallbackQuery) -> None:
    if not await check_admin(callback):
        return
    _, _, _, giveaway_id_raw, page_raw = callback.data.split(":")
    giveaway_id, page = int(giveaway_id_raw), int(page_raw)
    page_size = 15
    async with async_session_factory() as session:
        service = GiveawayService(session)
        giveaway = await service.get_giveaway(giveaway_id)
        rows = await service.list_entries(giveaway_id, offset=page * page_size, limit=page_size + 1)
        total, tickets = await service.get_entry_stats(giveaway_id)
    if not giveaway:
        await callback.answer("Розыгрыш не найден", show_alert=True)
        return
    lines = [f"👥 <b>Участники «{html.escape(giveaway.title)}»</b>", "", f"Всего: <b>{total}</b> · билетов: <b>{tickets}</b>", ""]
    for entry, user in rows[:page_size]:
        identity = f"@{html.escape(user.username)}" if user.username else str(user.id)
        lines.append(f"{identity} — <b>{entry.tickets}</b> бил. · {entry.purchase_count} пок.")
    if not rows:
        lines.append("Участников пока нет.")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_giveaway_entries_keyboard(giveaway_id, page, len(rows) > page_size),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:giveaways:audit:\d+$"))
async def callback_giveaway_audit(callback: CallbackQuery) -> None:
    if not await check_admin(callback):
        return
    giveaway_id = int(callback.data.rsplit(":", 1)[-1])
    async with async_session_factory() as session:
        giveaway = await session.get(Giveaway, giveaway_id)
    if not giveaway or not giveaway.audit_json:
        await callback.answer("Аудит ещё не сформирован", show_alert=True)
        return
    audit = json.loads(giveaway.audit_json)
    lines = [
        f"🔍 <b>Аудит розыгрыша #{giveaway_id}</b>",
        "",
        f"Алгоритм: <code>{html.escape(str(audit.get('algorithm', '—')))}</code>",
        f"Снимок SHA-256: <code>{html.escape(str(audit.get('snapshot_sha256', '—')))}</code>",
        f"Участников в снимке: <b>{len(audit.get('snapshot', []))}</b>",
        "",
        "<b>Выборы:</b>",
    ]
    for draw in audit.get("draws", []):
        lines.append(
            f"{draw['place']} место: user <code>{draw['user_id']}</code>, "
            f"билетов {draw['tickets']}, число {draw['random_value']} из {draw['total_weight_before']}"
        )
    await callback.message.edit_text(
        "\n".join(lines)[:4000], parse_mode="HTML", reply_markup=admin_giveaway_back_keyboard(giveaway_id)
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:giveaways:issue:\d+$"))
async def callback_giveaway_issue(callback: CallbackQuery) -> None:
    if not await check_admin(callback):
        return
    prize_id = int(callback.data.rsplit(":", 1)[-1])
    async with async_session_factory() as session:
        service = GiveawayService(session)
        prize = await service.mark_prize_issued(prize_id, callback.from_user.id)
        if not prize:
            await callback.answer("Приз не найден", show_alert=True)
            return
        giveaway_id = prize.giveaway_id
        issued = prize.is_issued
        await session.commit()
    await callback.answer("Приз отмечен как выданный" if issued else "Отметка выдачи снята")
    await _render_giveaway(callback, giveaway_id)


@router.callback_query(F.data.regexp(r"^admin:giveaways:cancel:\d+$"))
async def callback_giveaway_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_admin(callback):
        return
    giveaway_id = int(callback.data.rsplit(":", 1)[-1])
    await state.clear()
    await state.update_data(
        cancel_giveaway_id=giveaway_id,
        wizard_chat_id=callback.message.chat.id,
        wizard_message_id=callback.message.message_id,
    )
    await state.set_state(GiveawayAdminStates.waiting_cancel_reason)
    await callback.message.edit_text(
        "🚫 <b>Отмена розыгрыша</b>\n\nВведите причину отмены. Она сохранится в аудите:",
        parse_mode="HTML",
        reply_markup=admin_giveaway_back_keyboard(giveaway_id),
    )
    await callback.answer()


@router.message(GiveawayAdminStates.waiting_cancel_reason)
async def message_giveaway_cancel_reason(message: Message, state: FSMContext) -> None:
    if not await check_admin_message(message):
        return
    reason = (message.text or "").strip()
    await _delete_admin_input(message)
    data = await state.get_data()
    giveaway_id = int(data["cancel_giveaway_id"])
    if not 3 <= len(reason) <= 500:
        await message.bot.edit_message_text(
            chat_id=data["wizard_chat_id"],
            message_id=data["wizard_message_id"],
            text=(
                "❌ <b>Причина должна содержать от 3 до 500 символов.</b>\n\n"
                "🚫 <b>Отмена розыгрыша</b>\n\nВведите причину отмены. Она сохранится в аудите:"
            ),
            parse_mode="HTML",
            reply_markup=admin_giveaway_back_keyboard(giveaway_id),
        )
        return
    async with async_session_factory() as session:
        success = await GiveawayService(session).cancel(
            giveaway_id, reason=reason, admin_id=message.from_user.id
        )
        await session.commit()
    await state.clear()
    if not success:
        await message.bot.edit_message_text(
            chat_id=data["wizard_chat_id"],
            message_id=data["wizard_message_id"],
            text="Розыгрыш уже нельзя отменить.",
            reply_markup=admin_giveaway_back_keyboard(giveaway_id),
        )
        return
    await message.bot.edit_message_text(
        chat_id=data["wizard_chat_id"],
        message_id=data["wizard_message_id"],
        text=f"🚫 <b>Розыгрыш #{giveaway_id} отменён</b>\n\nПричина: {html.escape(reason)}",
        parse_mode="HTML",
        reply_markup=admin_giveaway_back_keyboard(giveaway_id),
    )


@router.callback_query(F.data == "admin:giveaways:nop")
async def callback_giveaway_nop(callback: CallbackQuery) -> None:
    await callback.answer()
