"""Background lifecycle worker for giveaways."""
from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from src.db.models import (
    Giveaway,
    GiveawayEntry,
    GiveawayEntryOrder,
    GiveawayPrize,
    GiveawayWinner,
    User,
)
from src.db.session import async_session_factory
from src.locales import t
from src.services.giveaway_service import (
    MODE_REGISTRATION_ALL,
    MODE_REGISTRATION_NEW,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    GiveawayService,
    condition_text,
    prize_text,
)

logger = logging.getLogger(__name__)

MOSCOW_TZ = timezone(timedelta(hours=3))
PUBLICATION_RETRY_SECONDS = 60
INCOMPLETE_WINNERS_RESULT_ERROR = (
    "Полный список победителей не сформирован; автоматическая публикация итогов пропущена"
)


def _msk(value: datetime, lang: str = "ru") -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    date = value.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    return t("giveaways.date_msk", lang, date=date)


def _user_link(user: User, lang: str = "ru") -> str:
    if user.username:
        return f"@{html.escape(user.username)}"
    label = t("giveaways.posts.user_fallback", lang, user_id=user.id)
    return f'<a href="tg://user?id={user.id}">{label}</a>'


def build_announcement_text(
    giveaway: Giveaway,
    prizes: list[GiveawayPrize],
    lang: str = "ru",
) -> str:
    prize_lines = "\n".join(
        t(
            "giveaways.posts.announcement_prize_line",
            lang,
            place=prize.place,
            prize=html.escape(prize_text(prize, lang)),
        )
        for prize in sorted(prizes, key=lambda item: item.place)
    )
    description_block = f"{html.escape(giveaway.description)}\n\n" if giveaway.description else ""
    return t(
        "giveaways.posts.announcement",
        lang,
        description_block=description_block,
        prizes=prize_lines,
        condition=html.escape(condition_text(giveaway, lang)),
        starts_at=_msk(giveaway.starts_at, lang),
        ends_at=_msk(giveaway.ends_at, lang),
    )


def build_results_text(
    giveaway: Giveaway,
    winners: list[tuple[GiveawayWinner, User, GiveawayPrize]],
    participant_count: int,
    lang: str = "ru",
) -> str:
    if not winners:
        raise ValueError("A results post requires at least one winner")
    winner_lines = "\n".join(
        t(
            "giveaways.posts.winner_line",
            lang,
            place=winner.place,
            identity=_user_link(user, lang),
            prize=html.escape(prize_text(prize, lang)),
        )
        for winner, user, prize in sorted(winners, key=lambda item: item[0].place)
    )
    outcome = t("giveaways.posts.winners", lang, winner_lines=winner_lines)
    return t(
        "giveaways.posts.results",
        lang,
        participant_count=participant_count,
        outcome=outcome,
    )


class GiveawayWorker:
    def __init__(self, bot: Bot, poll_interval: float = 10.0):
        self.bot = bot
        self.poll_interval = max(2.0, poll_interval)
        self._task: asyncio.Task | None = None
        self._running = False
        self._bot_username: str | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="giveaway-worker")
        logger.info("Giveaway worker started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Giveaway worker stopped")

    async def _run(self) -> None:
        while self._running:
            started = asyncio.get_running_loop().time()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Giveaway worker cycle failed")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.2, self.poll_interval - elapsed))

    async def run_once(self) -> None:
        await self._advance_lifecycle()
        await self._publish_pending()
        await self._notify_pending_entries()
        await self._notify_pending_winners()

    async def _advance_lifecycle(self) -> None:
        async with async_session_factory() as session:
            service = GiveawayService(session)
            activated = await service.activate_due()
            registrations = await service.reconcile_registration_entries()
            orders = await service.reconcile_active_orders()
            await session.commit()
            if activated or registrations or orders:
                logger.info(
                    "Giveaway reconciliation: activated=%s registrations=%s orders=%s",
                    len(activated),
                    registrations,
                    orders,
                )

        # Reconcile first, then lock and draw each due giveaway separately.
        async with async_session_factory() as session:
            due_ids = await GiveawayService(session).list_due_for_draw()
        for giveaway_id in due_ids:
            try:
                async with async_session_factory() as session:
                    giveaway = await GiveawayService(session).draw_due(giveaway_id)
                    await session.commit()
                    if giveaway and giveaway.status == STATUS_COMPLETED:
                        logger.info("Giveaway %s completed", giveaway_id)
            except Exception:
                logger.exception("Failed to draw giveaway %s", giveaway_id)

    async def _get_bot_username(self) -> str:
        if not self._bot_username:
            self._bot_username = (await self.bot.get_me()).username or ""
        return self._bot_username

    async def _publication_keyboard(self, lang: str = "ru") -> InlineKeyboardMarkup:
        username = await self._get_bot_username()
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t("giveaways.buttons.open", lang),
                        url=f"https://t.me/{username}",
                        style="danger",
                    )
                ]
            ]
        )

    @staticmethod
    def _retry_ready(last_attempt: datetime | None) -> bool:
        return last_attempt is None or datetime.utcnow() - last_attempt >= timedelta(
            seconds=PUBLICATION_RETRY_SECONDS
        )

    async def _publish_pending(self) -> None:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Giveaway)
                .where(
                    or_(
                        (
                            (Giveaway.status == STATUS_ACTIVE)
                            & Giveaway.publish_announcement.is_(True)
                            & Giveaway.announcement_message_id.is_(None)
                        ),
                        (
                            (Giveaway.status == STATUS_COMPLETED)
                            & Giveaway.publish_results.is_(True)
                            & Giveaway.results_message_id.is_(None)
                            & or_(
                                Giveaway.results_error.is_(None),
                                Giveaway.results_error != INCOMPLETE_WINNERS_RESULT_ERROR,
                            )
                        ),
                    )
                )
                .options(
                    selectinload(Giveaway.prizes),
                    selectinload(Giveaway.winners).selectinload(GiveawayWinner.prize),
                )
            )
            giveaways = list(result.scalars().all())

        for giveaway in giveaways:
            if (
                giveaway.status == STATUS_ACTIVE
                and giveaway.publish_announcement
                and giveaway.announcement_message_id is None
                and self._retry_ready(giveaway.announcement_last_attempt_at)
            ):
                await self._publish_announcement(giveaway.id)
            if (
                giveaway.status == STATUS_COMPLETED
                and giveaway.publish_results
                and giveaway.results_message_id is None
                and self._retry_ready(giveaway.results_last_attempt_at)
            ):
                await self._publish_results(giveaway.id)

    async def _send_publication(self, giveaway: Giveaway, text: str):
        keyboard = await self._publication_keyboard()
        if giveaway.photo_file_id:
            try:
                return await self.bot.send_photo(
                    chat_id=giveaway.publish_chat_id,
                    photo=giveaway.photo_file_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.warning("Giveaway %s photo publication failed, falling back to text", giveaway.id)
        return await self.bot.send_message(
            chat_id=giveaway.publish_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _publish_announcement(self, giveaway_id: int) -> None:
        async with async_session_factory() as session:
            service = GiveawayService(session)
            giveaway = await service.get_giveaway(giveaway_id)
            if not giveaway or not giveaway.publish_chat_id or giveaway.announcement_message_id:
                return
            giveaway.announcement_last_attempt_at = datetime.utcnow()
            try:
                message = await self._send_publication(
                    giveaway,
                    build_announcement_text(giveaway, list(giveaway.prizes)),
                )
                giveaway.announcement_message_id = message.message_id
                giveaway.announcement_error = None
            except Exception as exc:
                giveaway.announcement_error = str(exc)[:1000]
                logger.exception("Failed to publish giveaway %s announcement", giveaway.id)
            await session.commit()

    async def _publish_results(self, giveaway_id: int) -> None:
        async with async_session_factory() as session:
            service = GiveawayService(session)
            giveaway = await service.get_giveaway(giveaway_id)
            if not giveaway or not giveaway.publish_chat_id or giveaway.results_message_id:
                return
            winner_rows = list(
                (
                    await session.execute(
                        select(GiveawayWinner, User, GiveawayPrize)
                        .join(User, User.id == GiveawayWinner.user_id)
                        .join(GiveawayPrize, GiveawayPrize.id == GiveawayWinner.prize_id)
                        .where(GiveawayWinner.giveaway_id == giveaway.id)
                        .order_by(GiveawayWinner.place)
                    )
                ).all()
            )
            participants, _ = await service.get_entry_stats(giveaway.id)
            giveaway.results_last_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)
            expected_winners = len(giveaway.prizes)
            if len(winner_rows) != expected_winners:
                if participants == 0:
                    reason = "ни один пользователь не выполнил условия участия"
                elif participants < expected_winners:
                    reason = (
                        f"участников ({participants}) меньше, чем призовых мест "
                        f"({expected_winners})"
                    )
                else:
                    reason = (
                        f"сформировано победителей: {len(winner_rows)} из {expected_winners}"
                    )
                try:
                    await self.bot.send_message(
                        giveaway.created_by,
                        (
                            f"⚠️ <b>Итоги розыгрыша #{giveaway.id} не опубликованы</b>\n\n"
                            f"Техническое название: <b>{html.escape(giveaway.title)}</b>\n"
                            f"Причина: {reason}.\n\n"
                            "Автоматический пост без победителя отключён. Опубликуйте итоги вручную."
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="🎁 Открыть розыгрыш в админке",
                                        callback_data=f"admin:giveaways:view:{giveaway.id}",
                                    )
                                ]
                            ]
                        ),
                    )
                    giveaway.results_error = INCOMPLETE_WINNERS_RESULT_ERROR
                except (TelegramForbiddenError, TelegramBadRequest) as exc:
                    logger.warning("Cannot notify giveaway %s creator: %s", giveaway.id, exc)
                    giveaway.results_error = INCOMPLETE_WINNERS_RESULT_ERROR
                except Exception as exc:
                    giveaway.results_error = f"Не удалось уведомить администратора: {str(exc)[:800]}"
                    logger.exception("Failed to notify creator about giveaway %s without winner", giveaway.id)
                await session.commit()
                return
            try:
                message = await self._send_publication(
                    giveaway,
                    build_results_text(giveaway, winner_rows, participants),
                )
                giveaway.results_message_id = message.message_id
                giveaway.results_error = None
            except Exception as exc:
                giveaway.results_error = str(exc)[:1000]
                logger.exception("Failed to publish giveaway %s results", giveaway.id)
            await session.commit()

    async def _notify_pending_entries(self) -> None:
        # Purchase ticket notifications.
        async with async_session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(GiveawayEntryOrder, Giveaway, GiveawayEntry, User)
                        .join(Giveaway, Giveaway.id == GiveawayEntryOrder.giveaway_id)
                        .join(
                            GiveawayEntry,
                            (GiveawayEntry.giveaway_id == GiveawayEntryOrder.giveaway_id)
                            & (GiveawayEntry.user_id == GiveawayEntryOrder.user_id),
                        )
                        .join(User, User.id == GiveawayEntryOrder.user_id)
                        .where(
                            GiveawayEntryOrder.notified_at.is_(None),
                            GiveawayEntryOrder.tickets_awarded > 0,
                        )
                        .order_by(GiveawayEntryOrder.id)
                        .limit(30)
                    )
                ).all()
            )
            for record, giveaway, entry, user in rows:
                if giveaway.status == STATUS_CANCELLED or user.is_banned or user.is_admin:
                    record.notified_at = datetime.utcnow()
                    continue
                lang = user.language_code or "ru"
                text = t(
                    "giveaways.notifications.tickets",
                    lang,
                    tickets_added=record.tickets_awarded,
                    tickets_total=entry.tickets,
                )
                if await self._send_user_notice(user.id, text, giveaway.id, lang):
                    record.notified_at = datetime.utcnow()
            await session.commit()

        # Registration-mode entry notifications.
        async with async_session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(GiveawayEntry, Giveaway, User)
                        .join(Giveaway, Giveaway.id == GiveawayEntry.giveaway_id)
                        .join(User, User.id == GiveawayEntry.user_id)
                        .where(
                            GiveawayEntry.join_notified_at.is_(None),
                            GiveawayEntry.source.in_([MODE_REGISTRATION_ALL, MODE_REGISTRATION_NEW]),
                        )
                        .order_by(GiveawayEntry.id)
                        .limit(30)
                    )
                ).all()
            )
            for entry, giveaway, user in rows:
                if giveaway.status == STATUS_CANCELLED or user.is_banned or user.is_admin:
                    entry.join_notified_at = datetime.utcnow()
                    continue
                lang = user.language_code or "ru"
                text = t(
                    "giveaways.notifications.registration",
                    lang,
                )
                if await self._send_user_notice(user.id, text, giveaway.id, lang):
                    entry.join_notified_at = datetime.utcnow()
            await session.commit()

    async def _notify_pending_winners(self) -> None:
        async with async_session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(GiveawayWinner, Giveaway, GiveawayPrize, User)
                        .join(Giveaway, Giveaway.id == GiveawayWinner.giveaway_id)
                        .join(GiveawayPrize, GiveawayPrize.id == GiveawayWinner.prize_id)
                        .join(User, User.id == GiveawayWinner.user_id)
                        .where(GiveawayWinner.notified_at.is_(None))
                        .order_by(GiveawayWinner.id)
                        .limit(30)
                    )
                ).all()
            )
            for winner, giveaway, prize, user in rows:
                lang = user.language_code or "ru"
                text = t(
                    "giveaways.notifications.winner",
                    lang,
                    place=winner.place,
                    prize=html.escape(prize_text(prize, lang)),
                )
                if await self._send_user_notice(user.id, text, giveaway.id, lang):
                    winner.notified_at = datetime.utcnow()
            await session.commit()

    async def _send_user_notice(
        self,
        user_id: int,
        text: str,
        giveaway_id: int,
        lang: str,
    ) -> bool:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t("giveaways.buttons.notice", lang),
                        callback_data=f"giveaway:view:{giveaway_id}",
                        style="danger",
                    )
                ]
            ]
        )
        try:
            await self.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=keyboard)
            return True
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.info("Giveaway notice unavailable for user %s: %s", user_id, exc)
            # Do not retry forever for users who blocked or removed the bot.
            return True
        except Exception:
            logger.exception("Failed to notify user %s about giveaway %s", user_id, giveaway_id)
            return False
