"""User-facing giveaway list and details."""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from src.bot.keyboards.giveaways import get_giveaway_detail_keyboard, get_giveaway_list_keyboard
from src.bot.keyboards.menu import MenuCallback, get_back_button
from src.bot.menu_media import edit_menu_message
from src.db.models import GiveawayPrize, GiveawayWinner, User
from src.db.session import async_session_factory
from src.locales import get_user_locale, t
from src.services.giveaway_service import STATUS_COMPLETED, GiveawayService, condition_text, prize_text

router = Router(name="giveaways")
MOSCOW_TZ = timezone(timedelta(hours=3))


def _msk(value: datetime, lang: str) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    date = value.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    return t("giveaways.date_msk", lang, date=date)


async def _language(session, telegram_user) -> str:
    user = await session.get(User, telegram_user.id)
    return (user.language_code if user else None) or get_user_locale(telegram_user.language_code)


async def _detail_text(service: GiveawayService, giveaway, user_id: int, lang: str) -> str:
    entry = await service.get_user_entry(giveaway.id, user_id)
    participants, _ = await service.get_entry_stats(giveaway.id)
    prizes = sorted(giveaway.prizes, key=lambda item: item.place)
    prize_lines = "\n".join(
        t(
            "giveaways.detail.prize_line",
            lang,
            place=prize.place,
            prize=html.escape(prize_text(prize, lang)),
        )
        for prize in prizes
    )
    if entry and entry.tickets > 0:
        participation_status = t("giveaways.detail.participating", lang, tickets=entry.tickets)
    else:
        participation_status = t("giveaways.detail.not_participating", lang)

    results_block = ""
    if giveaway.status == STATUS_COMPLETED:
        winner_rows = list(
            (
                await service.session.execute(
                    select(GiveawayWinner, User, GiveawayPrize)
                    .join(User, User.id == GiveawayWinner.user_id)
                    .join(GiveawayPrize, GiveawayPrize.id == GiveawayWinner.prize_id)
                    .where(GiveawayWinner.giveaway_id == giveaway.id)
                    .order_by(GiveawayWinner.place)
                )
            ).all()
        )
        winner_lines = []
        for winner, winner_user, prize in winner_rows:
            identity = f"@{html.escape(winner_user.username)}" if winner_user.username else str(winner_user.id)
            winner_lines.append(
                t(
                    "giveaways.detail.winner_line",
                    lang,
                    place=winner.place,
                    identity=identity,
                    prize=html.escape(prize_text(prize, lang)),
                )
            )
        if not winner_lines:
            winner_lines.append(t("giveaways.detail.no_winners", lang))
        results_block = "\n\n" + t(
            "giveaways.detail.results",
            lang,
            winner_lines="\n".join(winner_lines),
        )

    description_block = f"{html.escape(giveaway.description)}\n\n" if giveaway.description else ""
    return t(
        "giveaways.detail.template",
        lang,
        description_block=description_block,
        prizes=prize_lines,
        condition=html.escape(condition_text(giveaway, lang)),
        ends_at=_msk(giveaway.ends_at, lang),
        participant_count=participants,
        participation_status=participation_status,
        results_block=results_block,
    )


@router.callback_query(F.data == MenuCallback.GIVEAWAYS)
async def callback_giveaway_list(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        service = GiveawayService(session)
        lang = await _language(session, callback.from_user)
        giveaways = await service.list_public_active()
    if giveaways:
        text = t("giveaways.list.title", lang)
        keyboard = get_giveaway_list_keyboard(giveaways, lang)
    else:
        text = t("giveaways.list.empty", lang)
        keyboard = get_back_button(lang)
    await edit_menu_message(callback, "giveaways", text=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^giveaway:view:\d+$"))
async def callback_giveaway_view(callback: CallbackQuery) -> None:
    giveaway_id = int(callback.data.rsplit(":", 1)[-1])
    async with async_session_factory() as session:
        service = GiveawayService(session)
        lang = await _language(session, callback.from_user)
        giveaway = await service.get_giveaway(giveaway_id)
        if not giveaway or giveaway.status not in {"active", "completed"}:
            await callback.answer(t("giveaways.detail.unavailable", lang), show_alert=True)
            return
        text = await _detail_text(service, giveaway, callback.from_user.id, lang)
    await edit_menu_message(
        callback,
        "giveaways",
        text=text,
        reply_markup=get_giveaway_detail_keyboard(lang),
    )
    await callback.answer()


async def show_giveaway_from_start(message: Message, giveaway_id: int, lang: str) -> None:
    """Open a giveaway from a channel deep link."""
    async with async_session_factory() as session:
        service = GiveawayService(session)
        giveaway = await service.get_giveaway(giveaway_id)
        if not giveaway or giveaway.status not in {"active", "completed"}:
            await message.answer(t("giveaways.detail.unavailable", lang))
            return
        text = await _detail_text(service, giveaway, message.from_user.id, lang)
    await message.answer(text, parse_mode="HTML", reply_markup=get_giveaway_detail_keyboard(lang))
