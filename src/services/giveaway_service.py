"""Core giveaway lifecycle, eligibility and audited winner selection."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Sequence

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import (
    Giveaway,
    GiveawayEntry,
    GiveawayEntryOrder,
    GiveawayPrize,
    GiveawayWinner,
    Order,
    OrderStatus,
    User,
)
from src.db.session import async_session_factory
from src.locales import t

logger = logging.getLogger(__name__)

STATUS_SCHEDULED = "scheduled"
STATUS_ACTIVE = "active"
STATUS_DRAWING = "drawing"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

MODE_PURCHASE_ONCE = "purchase_once"
MODE_TICKETS_PER_ORDER = "tickets_per_order"
MODE_TICKETS_PER_STARS = "tickets_per_stars"
# Keep the stored value for backwards compatibility. This mode now means that
# an existing user must interact with the bot while the giveaway is active.
MODE_REGISTRATION_ALL = "registration_all"
MODE_REGISTRATION_NEW = "registration_new"

PURCHASE_MODES = {
    MODE_PURCHASE_ONCE,
    MODE_TICKETS_PER_ORDER,
    MODE_TICKETS_PER_STARS,
}
REGISTRATION_MODES = {MODE_REGISTRATION_ALL, MODE_REGISTRATION_NEW}


@dataclass(slots=True)
class EntryAward:
    giveaway_id: int
    giveaway_title: str
    user_id: int
    tickets_added: int
    tickets_total: int
    first_entry: bool


@dataclass(slots=True)
class DrawSelection:
    user_id: int
    tickets: int
    random_value: int
    total_weight_before: int


def calculate_order_ticket_award(
    participation_mode: str,
    *,
    quantity: int,
    tickets_per_order: int = 1,
    stars_per_ticket: int | None = None,
    first_entry: bool = False,
) -> int:
    """Calculate one order independently, as configured by the administrator."""
    if participation_mode == MODE_PURCHASE_ONCE:
        return 1 if first_entry else 0
    if participation_mode == MODE_TICKETS_PER_ORDER:
        return max(1, tickets_per_order)
    if participation_mode == MODE_TICKETS_PER_STARS:
        threshold = max(1, stars_per_ticket or 1)
        return (quantity // threshold) * max(1, tickets_per_order)
    return 0


def is_order_eligible_for_giveaway(giveaway: Giveaway, order: Order) -> bool:
    """Apply the same immutable eligibility rules in hooks, reconciliation and draw."""
    completed_at = order.completed_at
    if (
        order.status != OrderStatus.COMPLETED.value
        or Decimal(order.price_usdt or 0) <= 0
        or completed_at is None
        or not giveaway.starts_at <= order.created_at <= giveaway.ends_at
        or completed_at > giveaway.ends_at
        or giveaway.product_filter not in (None, "all", order.product_type)
    ):
        return False
    return not (
        giveaway.participation_mode == MODE_TICKETS_PER_STARS
        and order.product_type != "stars"
    )


def is_giveaway_due_for_draw(giveaway: Giveaway, now: datetime) -> bool:
    """The configured end time is also the draw deadline."""
    return now >= giveaway.ends_at


def weighted_unique_draw(
    entries: Sequence[tuple[int, int]],
    prize_count: int,
    *,
    randbelow=None,
) -> list[DrawSelection]:
    """Cryptographically select unique users with probability proportional to tickets."""
    random_below = randbelow or secrets.randbelow
    pool = [(user_id, tickets) for user_id, tickets in entries if tickets > 0]
    selections: list[DrawSelection] = []
    for _ in range(max(0, prize_count)):
        if not pool:
            break
        total_weight = sum(tickets for _, tickets in pool)
        roll = random_below(total_weight)
        cursor = 0
        selected_index = len(pool) - 1
        for index, (_, tickets) in enumerate(pool):
            cursor += tickets
            if roll < cursor:
                selected_index = index
                break
        user_id, tickets = pool.pop(selected_index)
        selections.append(DrawSelection(user_id, tickets, roll, total_weight))
    return selections


def prize_text(prize: GiveawayPrize, lang: str = "ru") -> str:
    amount = Decimal(prize.amount or 0)
    if prize.prize_type == "stars":
        return t("giveaways.prize.stars", lang, amount=f"{amount:,.0f}")
    if prize.prize_type == "premium":
        return t("giveaways.prize.premium", lang, amount=f"{amount:,.0f}")
    return prize.description or t("giveaways.prize.custom_fallback", lang)


def condition_text(giveaway: Giveaway, lang: str = "ru") -> str:
    product = giveaway.product_filter or "all"
    if product not in {"all", "stars", "premium"}:
        product = "all"
    if giveaway.participation_mode == MODE_PURCHASE_ONCE:
        return t(f"giveaways.condition.purchase_once_{product}", lang)
    if giveaway.participation_mode == MODE_TICKETS_PER_ORDER:
        count = max(1, giveaway.tickets_per_order)
        return t(f"giveaways.condition.tickets_per_order_{product}", lang, count=count)
    if giveaway.participation_mode == MODE_TICKETS_PER_STARS:
        threshold = max(1, giveaway.stars_per_ticket or 1)
        count = max(1, giveaway.tickets_per_order)
        return t(
            "giveaways.condition.tickets_per_stars",
            lang,
            count=count,
            threshold=threshold,
        )
    if giveaway.participation_mode == MODE_REGISTRATION_NEW:
        return t("giveaways.condition.registration_new", lang)
    return t("giveaways.condition.registration_all", lang)


class GiveawayService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_giveaway(
        self,
        *,
        title: str,
        description: str | None,
        photo_file_id: str | None,
        participation_mode: str,
        product_filter: str | None,
        tickets_per_order: int,
        stars_per_ticket: int | None,
        starts_at: datetime,
        ends_at: datetime,
        publish_chat_id: int | None,
        publish_announcement: bool,
        publish_results: bool,
        created_by: int,
        prizes: Sequence[dict],
        grace_minutes: int = 0,
    ) -> Giveaway:
        title = title.strip()
        description = (description or "").strip() or None
        if not 2 <= len(title) <= 200:
            raise ValueError("Giveaway title must contain 2 to 200 characters")
        if description and len(description) > 1500:
            raise ValueError("Giveaway description is too long")
        if participation_mode not in PURCHASE_MODES | REGISTRATION_MODES:
            raise ValueError("Unknown participation mode")
        if ends_at <= starts_at:
            raise ValueError("Giveaway end must be later than start")
        if not prizes:
            raise ValueError("At least one prize is required")
        if participation_mode in PURCHASE_MODES and product_filter not in {"all", "stars", "premium"}:
            raise ValueError("Unknown product filter")
        if participation_mode == MODE_TICKETS_PER_STARS and product_filter != "stars":
            raise ValueError("Stars ticket mode requires the Stars product filter")
        if participation_mode == MODE_TICKETS_PER_STARS and (stars_per_ticket or 0) <= 0:
            raise ValueError("Stars per ticket must be positive")
        if not 1 <= tickets_per_order <= 1_000_000:
            raise ValueError("Tickets per order must be positive")
        if publish_chat_id is None:
            publish_announcement = False
            publish_results = False

        now = datetime.utcnow()
        status = STATUS_ACTIVE if starts_at <= now else STATUS_SCHEDULED
        giveaway = Giveaway(
            title=title,
            description=description,
            photo_file_id=photo_file_id,
            status=status,
            participation_mode=participation_mode,
            product_filter=product_filter if participation_mode in PURCHASE_MODES else None,
            tickets_per_order=tickets_per_order,
            stars_per_ticket=stars_per_ticket,
            starts_at=starts_at,
            ends_at=ends_at,
            # The administrator's end time is the actual draw deadline. Keep
            # the legacy column at zero for existing database compatibility.
            grace_minutes=0,
            publish_chat_id=publish_chat_id,
            publish_announcement=publish_announcement,
            publish_results=publish_results,
            created_by=created_by,
            activated_at=now if status == STATUS_ACTIVE else None,
        )
        self.session.add(giveaway)
        await self.session.flush()

        for place, item in enumerate(prizes, start=1):
            prize_type = str(item.get("prize_type", "custom"))
            if prize_type not in {"stars", "premium", "custom"}:
                raise ValueError("Unknown prize type")
            amount = item.get("amount")
            amount_value = Decimal(str(amount or 0))
            if prize_type in {"stars", "premium"}:
                if (
                    not amount_value.is_finite()
                    or amount_value <= 0
                    or amount_value != amount_value.to_integral_value()
                ):
                    raise ValueError("Stars and Premium prize amounts must be positive integers")
            description_value = str(item.get("description") or "").strip() or None
            if prize_type == "custom" and not description_value:
                raise ValueError("Custom prize description is required")
            if description_value and len(description_value) > 300:
                raise ValueError("Prize description is too long")
            self.session.add(
                GiveawayPrize(
                    giveaway_id=giveaway.id,
                    place=place,
                    prize_type=prize_type,
                    amount=amount_value if amount is not None else None,
                    description=description_value,
                )
            )
        await self.session.flush()
        return await self.get_giveaway(giveaway.id) or giveaway

    async def get_giveaway(self, giveaway_id: int) -> Giveaway | None:
        result = await self.session.execute(
            select(Giveaway)
            .where(Giveaway.id == giveaway_id)
            .options(
                selectinload(Giveaway.prizes),
                selectinload(Giveaway.winners).selectinload(GiveawayWinner.prize),
            )
        )
        return result.scalar_one_or_none()

    async def list_giveaways(self, *, offset: int = 0, limit: int = 20) -> list[Giveaway]:
        result = await self.session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.prizes))
            .order_by(Giveaway.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_giveaways(self) -> int:
        return int((await self.session.scalar(select(func.count(Giveaway.id)))) or 0)

    async def has_active_giveaways(self, now: datetime | None = None) -> bool:
        now = now or datetime.utcnow()
        value = await self.session.scalar(
            select(Giveaway.id)
            .where(
                Giveaway.status == STATUS_ACTIVE,
                Giveaway.starts_at <= now,
                Giveaway.ends_at > now,
            )
            .limit(1)
        )
        return value is not None

    async def list_public_active(self, now: datetime | None = None) -> list[Giveaway]:
        now = now or datetime.utcnow()
        result = await self.session.execute(
            select(Giveaway)
            .where(
                Giveaway.status == STATUS_ACTIVE,
                Giveaway.starts_at <= now,
                Giveaway.ends_at > now,
            )
            .options(selectinload(Giveaway.prizes))
            .order_by(Giveaway.ends_at, Giveaway.id)
        )
        return list(result.scalars().all())

    async def get_user_entry(self, giveaway_id: int, user_id: int) -> GiveawayEntry | None:
        return await self.session.scalar(
            select(GiveawayEntry).where(
                GiveawayEntry.giveaway_id == giveaway_id,
                GiveawayEntry.user_id == user_id,
            )
        )

    async def get_entry_stats(self, giveaway_id: int) -> tuple[int, int]:
        row = (
            await self.session.execute(
                select(
                    func.count(GiveawayEntry.id),
                    func.coalesce(func.sum(GiveawayEntry.tickets), 0),
                )
                .join(User, User.id == GiveawayEntry.user_id)
                .where(
                    GiveawayEntry.giveaway_id == giveaway_id,
                    GiveawayEntry.tickets > 0,
                    User.is_banned.is_(False),
                    User.is_admin.is_(False),
                )
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    async def list_entries(self, giveaway_id: int, *, offset: int = 0, limit: int = 20):
        result = await self.session.execute(
            select(GiveawayEntry, User)
            .join(User, User.id == GiveawayEntry.user_id)
            .where(
                GiveawayEntry.giveaway_id == giveaway_id,
                GiveawayEntry.tickets > 0,
                User.is_banned.is_(False),
                User.is_admin.is_(False),
            )
            .order_by(GiveawayEntry.tickets.desc(), GiveawayEntry.joined_at, GiveawayEntry.user_id)
            .offset(offset)
            .limit(limit)
        )
        return list(result.all())

    async def activate_due(self, now: datetime | None = None) -> list[int]:
        now = now or datetime.utcnow()
        result = await self.session.execute(
            select(Giveaway)
            .where(Giveaway.status == STATUS_SCHEDULED, Giveaway.starts_at <= now)
            .with_for_update(skip_locked=True)
        )
        activated = []
        for giveaway in result.scalars().all():
            if giveaway.ends_at <= now:
                # A missed short giveaway still activates so the draw path can audit it.
                logger.warning("Activating already ended giveaway %s after downtime", giveaway.id)
            giveaway.status = STATUS_ACTIVE
            giveaway.activated_at = now
            activated.append(giveaway.id)
        return activated

    async def add_registration_entries(self, giveaway: Giveaway, *, limit: int = 500) -> int:
        # Only newly registered users can be reconstructed from User.created_at.
        # Activity-mode entries are created exclusively by the event middleware.
        if giveaway.participation_mode != MODE_REGISTRATION_NEW:
            return 0
        filters = [
            User.is_banned.is_(False),
            User.is_admin.is_(False),
            User.created_at <= giveaway.ends_at,
            ~exists().where(
                GiveawayEntry.giveaway_id == giveaway.id,
                GiveawayEntry.user_id == User.id,
            ),
        ]
        filters.append(User.created_at >= giveaway.starts_at)
        result = await self.session.execute(
            select(User.id, User.created_at)
            .where(*filters)
            .order_by(User.id)
            .limit(limit)
        )
        rows = result.all()
        for user_id, registered_at in rows:
            self.session.add(
                GiveawayEntry(
                    giveaway_id=giveaway.id,
                    user_id=user_id,
                    source=giveaway.participation_mode,
                    tickets=1,
                    joined_at=max(registered_at, giveaway.starts_at),
                )
            )
        return len(rows)

    async def process_user_activity(
        self,
        user_id: int,
        *,
        occurred_at: datetime | None = None,
    ) -> list[EntryAward]:
        """Enter an existing user after a real bot action during the giveaway."""
        occurred_at = occurred_at or datetime.utcnow()
        user = await self.session.get(User, user_id)
        if not user or user.is_banned or user.is_admin:
            return []

        candidates = list(
            (
                await self.session.execute(
                    select(Giveaway.id)
                    .where(
                        Giveaway.status.in_([STATUS_SCHEDULED, STATUS_ACTIVE]),
                        Giveaway.participation_mode == MODE_REGISTRATION_ALL,
                        Giveaway.starts_at <= occurred_at,
                        Giveaway.ends_at >= occurred_at,
                    )
                    .order_by(Giveaway.id)
                )
            ).scalars().all()
        )
        if not candidates:
            return []

        existing_ids = set(
            (
                await self.session.execute(
                    select(GiveawayEntry.giveaway_id).where(
                        GiveawayEntry.user_id == user_id,
                        GiveawayEntry.giveaway_id.in_(candidates),
                    )
                )
            ).scalars().all()
        )
        missing_ids = [giveaway_id for giveaway_id in candidates if giveaway_id not in existing_ids]
        if not missing_ids:
            return []

        # A shared row lock allows concurrent joins while making the final draw
        # wait for every action that started before the deadline.
        giveaways = list(
            (
                await self.session.execute(
                    select(Giveaway)
                    .where(
                        Giveaway.id.in_(missing_ids),
                        Giveaway.status.in_([STATUS_SCHEDULED, STATUS_ACTIVE]),
                        Giveaway.starts_at <= occurred_at,
                        Giveaway.ends_at >= occurred_at,
                    )
                    .order_by(Giveaway.id)
                    .with_for_update(read=True)
                )
            ).scalars().all()
        )
        awards: list[EntryAward] = []
        for giveaway in giveaways:
            inserted_id = await self.session.scalar(
                pg_insert(GiveawayEntry)
                .values(
                    giveaway_id=giveaway.id,
                    user_id=user_id,
                    source=MODE_REGISTRATION_ALL,
                    tickets=1,
                    joined_at=occurred_at,
                )
                .on_conflict_do_nothing(constraint="uq_giveaway_entry_user")
                .returning(GiveawayEntry.id)
            )
            if inserted_id is not None:
                awards.append(EntryAward(giveaway.id, giveaway.title, user_id, 1, 1, True))
        return awards

    async def process_user_registration(self, user_id: int) -> list[EntryAward]:
        now = datetime.utcnow()
        user = await self.session.get(User, user_id)
        if not user or user.is_banned or user.is_admin:
            return []
        awards = await self.process_user_activity(user_id, occurred_at=now)
        result = await self.session.execute(
            select(Giveaway)
            .where(
                Giveaway.status.in_([STATUS_SCHEDULED, STATUS_ACTIVE]),
                Giveaway.participation_mode == MODE_REGISTRATION_NEW,
                Giveaway.starts_at <= user.created_at,
                Giveaway.ends_at >= user.created_at,
            )
            .order_by(Giveaway.id)
            .with_for_update(read=True)
        )
        for giveaway in result.scalars().all():
            inserted_id = await self.session.scalar(
                pg_insert(GiveawayEntry)
                .values(
                    giveaway_id=giveaway.id,
                    user_id=user_id,
                    source=MODE_REGISTRATION_NEW,
                    tickets=1,
                    joined_at=user.created_at,
                )
                .on_conflict_do_nothing(constraint="uq_giveaway_entry_user")
                .returning(GiveawayEntry.id)
            )
            if inserted_id is not None:
                awards.append(EntryAward(giveaway.id, giveaway.title, user_id, 1, 1, True))
        return awards

    async def process_completed_order(
        self,
        order_id: int,
        *,
        giveaway_id: int | None = None,
    ) -> list[EntryAward]:
        order = await self.session.get(Order, order_id)
        if (
            not order
            or order.status != OrderStatus.COMPLETED.value
            or Decimal(order.price_usdt or 0) <= 0
        ):
            return []
        user = await self.session.get(User, order.user_id)
        if not user or user.is_banned or user.is_admin:
            return []

        giveaway_query = (
            select(Giveaway)
            .where(
                Giveaway.status.in_([STATUS_ACTIVE, STATUS_DRAWING]),
                Giveaway.participation_mode.in_(PURCHASE_MODES),
                Giveaway.starts_at <= order.created_at,
                Giveaway.ends_at >= order.created_at,
            )
            .order_by(Giveaway.id)
            .with_for_update()
        )
        if giveaway_id is not None:
            giveaway_query = giveaway_query.where(Giveaway.id == giveaway_id)
        result = await self.session.execute(giveaway_query)
        awards: list[EntryAward] = []
        for giveaway in result.scalars().all():
            if not is_order_eligible_for_giveaway(giveaway, order):
                continue
            exists_order = await self.session.scalar(
                select(GiveawayEntryOrder.id).where(
                    GiveawayEntryOrder.giveaway_id == giveaway.id,
                    GiveawayEntryOrder.order_id == order.id,
                )
            )
            if exists_order is not None:
                continue

            entry = await self.get_user_entry(giveaway.id, order.user_id)
            first_entry = entry is None
            tickets = calculate_order_ticket_award(
                giveaway.participation_mode,
                quantity=order.quantity,
                tickets_per_order=giveaway.tickets_per_order,
                stars_per_ticket=giveaway.stars_per_ticket,
                first_entry=first_entry,
            )

            if entry is None and tickets > 0:
                entry = GiveawayEntry(
                    giveaway_id=giveaway.id,
                    user_id=order.user_id,
                    source=giveaway.participation_mode,
                    tickets=0,
                    joined_at=order.completed_at or datetime.utcnow(),
                )
                self.session.add(entry)
                await self.session.flush()
            if entry is not None:
                entry.tickets += tickets
                entry.purchase_count += 1
                if order.product_type == "stars":
                    entry.stars_purchased += order.quantity

            record = GiveawayEntryOrder(
                giveaway_id=giveaway.id,
                order_id=order.id,
                user_id=order.user_id,
                tickets_awarded=tickets,
                order_quantity=order.quantity,
                # Zero-ticket orders do not produce a user notification.
                notified_at=datetime.utcnow() if tickets <= 0 else None,
            )
            self.session.add(record)
            if tickets > 0 and entry is not None:
                awards.append(
                    EntryAward(
                        giveaway.id,
                        giveaway.title,
                        order.user_id,
                        tickets,
                        entry.tickets,
                        first_entry,
                    )
                )
        return awards

    async def _reconcile_giveaway_orders(
        self,
        giveaway: Giveaway,
        *,
        batch_limit: int,
        drain: bool,
    ) -> int:
        processed = 0
        while True:
            already = select(GiveawayEntryOrder.order_id).where(
                GiveawayEntryOrder.giveaway_id == giveaway.id
            )
            filters = [
                Order.status == OrderStatus.COMPLETED.value,
                Order.price_usdt > 0,
                Order.completed_at.is_not(None),
                Order.completed_at <= giveaway.ends_at,
                Order.created_at >= giveaway.starts_at,
                Order.created_at <= giveaway.ends_at,
                User.is_banned.is_(False),
                User.is_admin.is_(False),
                ~Order.id.in_(already),
            ]
            if giveaway.product_filter not in (None, "all"):
                filters.append(Order.product_type == giveaway.product_filter)
            if giveaway.participation_mode == MODE_TICKETS_PER_STARS:
                filters.append(Order.product_type == "stars")
            order_ids = list(
                (
                    await self.session.execute(
                        select(Order.id)
                        .join(User, User.id == Order.user_id)
                        .where(*filters)
                        .order_by(Order.id)
                        .limit(batch_limit)
                    )
                ).scalars().all()
            )
            if not order_ids:
                break
            for order_id in order_ids:
                await self.process_completed_order(order_id, giveaway_id=giveaway.id)
                processed += 1
            await self.session.flush()
            if not drain or len(order_ids) < batch_limit:
                break
        return processed

    async def reconcile_active_orders(self, *, per_giveaway_limit: int = 300) -> int:
        result = await self.session.execute(
            select(Giveaway).where(
                Giveaway.status.in_([STATUS_ACTIVE, STATUS_DRAWING]),
                Giveaway.participation_mode.in_(PURCHASE_MODES),
            )
        )
        processed = 0
        now = datetime.utcnow()
        for giveaway in result.scalars().all():
            if now > giveaway.ends_at and giveaway.status != STATUS_DRAWING:
                continue
            processed += await self._reconcile_giveaway_orders(
                giveaway,
                batch_limit=per_giveaway_limit,
                drain=False,
            )
            await self._recalculate_purchase_entries(giveaway)
            await self.session.flush()
        return processed

    async def reconcile_registration_entries(self, *, per_giveaway_limit: int = 500) -> int:
        result = await self.session.execute(
            select(Giveaway)
            .where(
                Giveaway.status.in_([STATUS_ACTIVE, STATUS_DRAWING]),
                Giveaway.participation_mode == MODE_REGISTRATION_NEW,
            )
            .with_for_update(skip_locked=True)
        )
        total = 0
        for giveaway in result.scalars().all():
            total += await self.add_registration_entries(giveaway, limit=per_giveaway_limit)
        return total

    async def _reconcile_all_registrations(
        self,
        giveaway: Giveaway,
        *,
        batch_limit: int = 500,
    ) -> int:
        total = 0
        while True:
            added = await self.add_registration_entries(giveaway, limit=batch_limit)
            total += added
            await self.session.flush()
            if added < batch_limit:
                return total

    async def _recalculate_purchase_entries(self, giveaway: Giveaway) -> None:
        if giveaway.participation_mode not in PURCHASE_MODES:
            return
        rows = (
            await self.session.execute(
                select(GiveawayEntryOrder, Order)
                .join(Order, Order.id == GiveawayEntryOrder.order_id)
                .where(GiveawayEntryOrder.giveaway_id == giveaway.id)
                .order_by(GiveawayEntryOrder.user_id, Order.created_at, Order.id)
            )
        ).all()
        totals: dict[int, dict[str, int]] = {}
        purchase_once_awarded: set[int] = set()
        now = datetime.utcnow()
        for record, order in rows:
            eligible = is_order_eligible_for_giveaway(giveaway, order)
            if not eligible:
                record.revoked_at = record.revoked_at or now
                record.tickets_awarded = 0
                record.notified_at = record.notified_at or now
                continue
            record.revoked_at = None
            first_entry = order.user_id not in purchase_once_awarded
            previous_tickets = record.tickets_awarded
            tickets = calculate_order_ticket_award(
                giveaway.participation_mode,
                quantity=order.quantity,
                tickets_per_order=giveaway.tickets_per_order,
                stars_per_ticket=giveaway.stars_per_ticket,
                first_entry=first_entry,
            )
            if giveaway.participation_mode == MODE_PURCHASE_ONCE:
                purchase_once_awarded.add(order.user_id)
            record.tickets_awarded = tickets
            if tickets > 0 and previous_tickets <= 0:
                record.notified_at = None
            elif tickets <= 0:
                record.notified_at = record.notified_at or now
            values = totals.setdefault(order.user_id, {"tickets": 0, "purchases": 0, "stars": 0})
            values["tickets"] += tickets
            values["purchases"] += 1
            if order.product_type == "stars":
                values["stars"] += order.quantity

        entries = list(
            (
                await self.session.execute(
                    select(GiveawayEntry).where(GiveawayEntry.giveaway_id == giveaway.id)
                )
            ).scalars().all()
        )
        for entry in entries:
            values = totals.get(entry.user_id, {"tickets": 0, "purchases": 0, "stars": 0})
            entry.tickets = values["tickets"]
            entry.purchase_count = values["purchases"]
            entry.stars_purchased = values["stars"]

    async def draw_due(self, giveaway_id: int, now: datetime | None = None) -> Giveaway | None:
        now = now or datetime.utcnow()
        giveaway = await self.session.scalar(
            select(Giveaway)
            .where(Giveaway.id == giveaway_id)
            .with_for_update()
        )
        if not giveaway or giveaway.status != STATUS_ACTIVE:
            return giveaway
        if not is_giveaway_due_for_draw(giveaway, now):
            return giveaway

        giveaway.status = STATUS_DRAWING
        await self.session.flush()
        if giveaway.participation_mode in REGISTRATION_MODES:
            await self._reconcile_all_registrations(giveaway)
        else:
            await self._reconcile_giveaway_orders(
                giveaway,
                batch_limit=500,
                drain=True,
            )
        await self._recalculate_purchase_entries(giveaway)
        await self.session.flush()

        entries = list(
            (
                await self.session.execute(
                    select(GiveawayEntry)
                    .join(User, User.id == GiveawayEntry.user_id)
                    .where(
                        GiveawayEntry.giveaway_id == giveaway.id,
                        GiveawayEntry.tickets > 0,
                        User.is_banned.is_(False),
                        User.is_admin.is_(False),
                    )
                    .order_by(GiveawayEntry.user_id)
                )
            ).scalars().all()
        )
        prizes = list(
            (
                await self.session.execute(
                    select(GiveawayPrize)
                    .where(GiveawayPrize.giveaway_id == giveaway.id)
                    .order_by(GiveawayPrize.place)
                )
            ).scalars().all()
        )

        snapshot = [{"user_id": entry.user_id, "tickets": entry.tickets} for entry in entries]
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        snapshot_hash = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        draws = []
        selections = weighted_unique_draw(
            [(entry.user_id, entry.tickets) for entry in entries],
            len(prizes),
        )
        entries_by_user = {entry.user_id: entry for entry in entries}
        for prize, selection in zip(prizes, selections):
            selected = entries_by_user[selection.user_id]
            self.session.add(
                GiveawayWinner(
                    giveaway_id=giveaway.id,
                    prize_id=prize.id,
                    user_id=selected.user_id,
                    place=prize.place,
                    tickets_snapshot=selected.tickets,
                    random_value=selection.random_value,
                    total_weight_before=selection.total_weight_before,
                )
            )
            draws.append(
                {
                    "place": prize.place,
                    "user_id": selected.user_id,
                    "tickets": selected.tickets,
                    "random_value": selection.random_value,
                    "total_weight_before": selection.total_weight_before,
                }
            )

        giveaway.audit_json = json.dumps(
            {
                "algorithm": "secrets.randbelow weighted without replacement",
                "snapshot_sha256": snapshot_hash,
                "snapshot": snapshot,
                "draws": draws,
                "drawn_at_utc": now.isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        giveaway.status = STATUS_COMPLETED
        giveaway.completed_at = now
        await self.session.flush()
        return giveaway

    async def list_due_for_draw(self, now: datetime | None = None) -> list[int]:
        now = now or datetime.utcnow()
        giveaways = list(
            (
                await self.session.execute(
                    select(Giveaway).where(Giveaway.status == STATUS_ACTIVE)
                )
            ).scalars().all()
        )
        return [
            giveaway.id
            for giveaway in giveaways
            if is_giveaway_due_for_draw(giveaway, now)
        ]

    async def cancel(self, giveaway_id: int, *, reason: str, admin_id: int) -> bool:
        giveaway = await self.session.scalar(
            select(Giveaway).where(Giveaway.id == giveaway_id).with_for_update()
        )
        if not giveaway or giveaway.status not in {STATUS_SCHEDULED, STATUS_ACTIVE}:
            return False
        giveaway.status = STATUS_CANCELLED
        giveaway.cancel_reason = reason.strip()
        giveaway.cancelled_at = datetime.utcnow()
        old_audit = json.loads(giveaway.audit_json or "{}")
        old_audit["cancelled_by"] = admin_id
        old_audit["cancelled_at_utc"] = giveaway.cancelled_at.isoformat(timespec="seconds")
        old_audit["cancel_reason"] = giveaway.cancel_reason
        giveaway.audit_json = json.dumps(old_audit, ensure_ascii=False, separators=(",", ":"))
        return True

    async def mark_prize_issued(self, prize_id: int, admin_id: int) -> GiveawayPrize | None:
        prize = await self.session.scalar(
            select(GiveawayPrize).where(GiveawayPrize.id == prize_id).with_for_update()
        )
        if not prize:
            return None
        prize.is_issued = not prize.is_issued
        prize.issued_at = datetime.utcnow() if prize.is_issued else None
        prize.issued_by = admin_id if prize.is_issued else None
        return prize


async def process_completed_order_for_giveaways(order_id: int) -> list[EntryAward]:
    """Idempotent completion hook used by the order worker callback."""
    async with async_session_factory() as session:
        service = GiveawayService(session)
        awards = await service.process_completed_order(order_id)
        await session.commit()
        return awards


async def process_registration_for_giveaways(user_id: int) -> list[EntryAward]:
    async with async_session_factory() as session:
        service = GiveawayService(session)
        awards = await service.process_user_registration(user_id)
        await session.commit()
        return awards


async def process_activity_for_giveaways(user_id: int) -> list[EntryAward]:
    """Idempotent hook for messages, callbacks and inline queries."""
    async with async_session_factory() as session:
        service = GiveawayService(session)
        awards = await service.process_user_activity(user_id)
        await session.commit()
        return awards


async def has_active_giveaways() -> bool:
    async with async_session_factory() as session:
        return await GiveawayService(session).has_active_giveaways()
