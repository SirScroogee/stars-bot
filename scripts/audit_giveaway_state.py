"""Read-only production audit for giveaway accounting and logging configuration."""
from __future__ import annotations

import asyncio
import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from src.db.models import (
    Giveaway,
    GiveawayEntry,
    GiveawayEntryOrder,
    GiveawayWinner,
    Order,
    User,
)
from src.db.session import async_session_factory, dispose_engine
from src.services.giveaway_service import (
    MODE_PURCHASE_ONCE,
    MODE_REGISTRATION_NEW,
    MODE_REGISTRATION_ALL,
    PURCHASE_MODES,
    calculate_order_ticket_award,
    is_order_eligible_for_giveaway,
)
from src.services.log_settings_service import get_log_settings


async def _audit_giveaway(session, giveaway: Giveaway) -> dict:
    entry_rows = list(
        (
            await session.execute(
                select(GiveawayEntry, User)
                .join(User, User.id == GiveawayEntry.user_id)
                .where(GiveawayEntry.giveaway_id == giveaway.id)
            )
        ).all()
    )
    actual = {
        entry.user_id: {
            "tickets": entry.tickets,
            "purchases": entry.purchase_count,
            "stars": entry.stars_purchased,
        }
        for entry, user in entry_rows
        if not user.is_banned and not user.is_admin
    }
    excluded_entries = [
        entry.user_id for entry, user in entry_rows if user.is_banned or user.is_admin
    ]

    expected: dict[int, dict[str, int]] = {}
    missing_order_records: list[int] = []
    stale_order_records: list[int] = []
    if giveaway.participation_mode in PURCHASE_MODES:
        order_rows = list(
            (
                await session.execute(
                    select(Order, User)
                    .join(User, User.id == Order.user_id)
                    .where(
                        Order.created_at >= giveaway.starts_at,
                        Order.created_at <= giveaway.ends_at,
                    )
                    .order_by(Order.user_id, Order.created_at, Order.id)
                )
            ).all()
        )
        eligible_orders = [
            order
            for order, user in order_rows
            if not user.is_banned
            and not user.is_admin
            and is_order_eligible_for_giveaway(giveaway, order)
        ]
        first_purchase_awarded: set[int] = set()
        for order in eligible_orders:
            values = expected.setdefault(order.user_id, {"tickets": 0, "purchases": 0, "stars": 0})
            first_entry = order.user_id not in first_purchase_awarded
            tickets = calculate_order_ticket_award(
                giveaway.participation_mode,
                quantity=order.quantity,
                tickets_per_order=giveaway.tickets_per_order,
                stars_per_ticket=giveaway.stars_per_ticket,
                first_entry=first_entry,
            )
            if giveaway.participation_mode == MODE_PURCHASE_ONCE:
                first_purchase_awarded.add(order.user_id)
            values["tickets"] += tickets
            values["purchases"] += 1
            if order.product_type == "stars":
                values["stars"] += order.quantity
        expected = {
            user_id: values for user_id, values in expected.items() if values["tickets"] > 0
        }

        record_rows = list(
            (
                await session.execute(
                    select(GiveawayEntryOrder, Order, User)
                    .join(Order, Order.id == GiveawayEntryOrder.order_id)
                    .join(User, User.id == GiveawayEntryOrder.user_id)
                    .where(GiveawayEntryOrder.giveaway_id == giveaway.id)
                )
            ).all()
        )
        recorded_order_ids = {record.order_id for record, _, _ in record_rows}
        missing_order_records = [
            order.id for order in eligible_orders if order.id not in recorded_order_ids
        ]
        stale_order_records = [
            record.order_id
            for record, order, user in record_rows
            if user.is_banned
            or user.is_admin
            or not is_order_eligible_for_giveaway(giveaway, order)
            or record.revoked_at is not None
        ]

    elif giveaway.participation_mode == MODE_REGISTRATION_NEW:
        filters = [
            User.is_banned.is_(False),
            User.is_admin.is_(False),
            User.created_at <= giveaway.ends_at,
        ]
        filters.append(User.created_at >= giveaway.starts_at)
        expected_ids = list((await session.execute(select(User.id).where(*filters))).scalars().all())
        expected = {
            user_id: {"tickets": 1, "purchases": 0, "stars": 0}
            for user_id in expected_ids
        }
    elif giveaway.participation_mode == MODE_REGISTRATION_ALL:
        # Activity is represented by the entry itself; User.created_at cannot
        # prove whether an existing user interacted during the campaign.
        expected = dict(actual)

    mismatches = []
    if giveaway.status != "scheduled":
        for user_id in sorted(set(expected) | set(actual)):
            expected_value = expected.get(user_id, {"tickets": 0, "purchases": 0, "stars": 0})
            actual_value = actual.get(user_id, {"tickets": 0, "purchases": 0, "stars": 0})
            if expected_value != actual_value:
                mismatches.append(
                    {"user_id": user_id, "expected": expected_value, "actual": actual_value}
                )

    winners = list(
        (
            await session.execute(
                select(GiveawayWinner)
                .where(GiveawayWinner.giveaway_id == giveaway.id)
                .order_by(GiveawayWinner.place)
            )
        ).scalars().all()
    )
    positive_participants = sum(1 for values in actual.values() if values["tickets"] > 0)
    audit_hash_valid = None
    if giveaway.audit_json:
        audit = json.loads(giveaway.audit_json)
        snapshot = audit.get("snapshot", [])
        snapshot_hash = audit.get("snapshot_sha256")
        if snapshot_hash:
            canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            audit_hash_valid = (
                hashlib.sha256(canonical.encode("ascii")).hexdigest()
                == snapshot_hash
            )

    return {
        "id": giveaway.id,
        "title": giveaway.title,
        "status": giveaway.status,
        "mode": giveaway.participation_mode,
        "prizes": len(giveaway.prizes),
        "participants": positive_participants,
        "tickets": sum(values["tickets"] for values in actual.values() if values["tickets"] > 0),
        "winners": len(winners),
        "incomplete_prize_assignment": bool(
            giveaway.status == "completed" and len(winners) != len(giveaway.prizes)
        ),
        "missing_order_records": missing_order_records[:20],
        "stale_order_records": stale_order_records[:20],
        "excluded_entries": excluded_entries[:20],
        "entry_mismatch_count": len(mismatches),
        "entry_mismatches": mismatches[:20],
        "audit_hash_valid": audit_hash_valid,
        "announcement_error": giveaway.announcement_error,
        "results_error": giveaway.results_error,
    }


async def main() -> None:
    settings = await get_log_settings()
    async with async_session_factory() as session:
        giveaways = list(
            (
                await session.execute(
                    select(Giveaway)
                    .options(selectinload(Giveaway.prizes))
                    .order_by(Giveaway.id)
                )
            ).scalars().all()
        )
        user_count = int((await session.scalar(select(func.count(User.id)))) or 0)
        audits = [await _audit_giveaway(session, giveaway) for giveaway in giveaways]

    users_topic = settings.get("topics", {}).get("users", {})
    output = {
        "logging": {
            "enabled": settings.get("enabled", True),
            "group_id": settings.get("group_id"),
            "users_topic_id": users_topic.get("id"),
            "users_topic_enabled": users_topic.get("enabled", True),
            "user_registered_enabled": settings.get("events", {}).get("user_registered", True),
        },
        "user_count": user_count,
        "giveaways": audits,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
