"""Production-safe giveaway integration check; all database changes are rolled back."""
import asyncio
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.db.models import GiveawayEntry, GiveawayEntryOrder, GiveawayWinner, Order, User
from src.db.session import async_session_factory, dispose_engine
from src.services.giveaway_service import GiveawayService, STATUS_COMPLETED


async def main() -> None:
    async with async_session_factory() as session:
        admin = await session.scalar(select(User).where(User.is_admin.is_(True)).limit(1))
        users = list(
            (
                await session.execute(
                    select(User)
                    .where(User.is_admin.is_(False), User.is_banned.is_(False))
                    .order_by(User.id)
                    .limit(2)
                )
            ).scalars().all()
        )
        if not admin or len(users) < 2:
            raise RuntimeError("Integration check requires one admin and two regular users")

        now = datetime.utcnow()
        service = GiveawayService(session)
        synthetic_order_id = -int(uuid.uuid4().int % 1_000_000_000) - 1_000_000

        def next_synthetic_order_id() -> int:
            nonlocal synthetic_order_id
            synthetic_order_id -= 1
            return synthetic_order_id

        giveaway = await service.create_giveaway(
            title="Integration check",
            description=None,
            photo_file_id=None,
            participation_mode="tickets_per_order",
            product_filter="all",
            tickets_per_order=2,
            stars_per_ticket=None,
            starts_at=now - timedelta(minutes=10),
            ends_at=now - timedelta(minutes=1),
            publish_chat_id=None,
            publish_announcement=False,
            publish_results=False,
            created_by=admin.id,
            prizes=[
                {"prize_type": "stars", "amount": "100"},
                {"prize_type": "premium", "amount": "3"},
            ],
            grace_minutes=0,
        )

        for index, user in enumerate(users, start=1):
            order = Order(
                id=next_synthetic_order_id(),
                order_key=f"GWTEST-{uuid.uuid4().hex}",
                user_id=user.id,
                recipient_username=user.username or f"test_user_{index}",
                product_type="stars" if index == 1 else "premium",
                quantity=100 if index == 1 else 3,
                price_usdt=Decimal("1.00"),
                status="completed",
                payment_provider="balance",
                created_at=now - timedelta(minutes=5),
                completed_at=now - timedelta(minutes=4),
            )
            session.add(order)
            await session.flush()
            awards = await service.process_completed_order(order.id)
            assert len(awards) == 1 and awards[0].tickets_added == 2

        entries = list(
            (
                await session.execute(
                    select(GiveawayEntry).where(GiveawayEntry.giveaway_id == giveaway.id)
                )
            ).scalars().all()
        )
        assert len(entries) == 2 and all(entry.tickets == 2 for entry in entries)

        drawn = await service.draw_due(giveaway.id, now=now)
        assert drawn and drawn.status == STATUS_COMPLETED
        winners = list(
            (
                await session.execute(
                    select(GiveawayWinner).where(GiveawayWinner.giveaway_id == giveaway.id)
                )
            ).scalars().all()
        )
        assert len(winners) == 2
        assert len({winner.user_id for winner in winners}) == 2
        assert drawn.audit_json and "snapshot_sha256" in drawn.audit_json

        registration_giveaway = await service.create_giveaway(
            title="Registration integration check",
            description=None,
            photo_file_id=None,
            participation_mode="registration_new",
            product_filter=None,
            tickets_per_order=1,
            stars_per_ticket=None,
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=5),
            publish_chat_id=None,
            publish_announcement=False,
            publish_results=False,
            created_by=admin.id,
            prizes=[{"prize_type": "stars", "amount": "50"}],
        )
        synthetic_id = -int(uuid.uuid4().int % 8_000_000_000_000_000_000) - 1
        synthetic_user = User(
            id=synthetic_id,
            username=None,
            language_code="ru",
            referral_code=f"gw{uuid.uuid4().hex[:28]}",
            created_at=now,
        )
        session.add(synthetic_user)
        await session.flush()
        registration_awards = await service.process_user_registration(synthetic_user.id)
        assert len(registration_awards) == 1
        registration_entry = await service.get_user_entry(registration_giveaway.id, synthetic_user.id)
        assert registration_entry and registration_entry.tickets == 1

        activity_giveaway = await service.create_giveaway(
            title="Activity integration check",
            description=None,
            photo_file_id=None,
            participation_mode="registration_all",
            product_filter=None,
            tickets_per_order=1,
            stars_per_ticket=None,
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=5),
            publish_chat_id=None,
            publish_announcement=False,
            publish_results=False,
            created_by=admin.id,
            prizes=[{"prize_type": "stars", "amount": "50"}],
        )
        assert await service.add_registration_entries(activity_giveaway) == 0
        assert await service.get_user_entry(activity_giveaway.id, users[0].id) is None
        assert await service.get_user_entry(activity_giveaway.id, users[1].id) is None
        activity_awards = await service.process_user_activity(users[0].id, occurred_at=now)
        assert len(activity_awards) == 1
        assert await service.process_user_activity(users[0].id, occurred_at=now) == []
        assert await service.get_user_entry(activity_giveaway.id, users[0].id)
        assert await service.get_user_entry(activity_giveaway.id, users[1].id) is None

        # If the first qualifying order is refunded, purchase_once must move the
        # ticket to the next successful order for that user.
        refund_giveaway = await service.create_giveaway(
            title="Refund recalculation check",
            description=None,
            photo_file_id=None,
            participation_mode="purchase_once",
            product_filter="all",
            tickets_per_order=1,
            stars_per_ticket=None,
            starts_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(minutes=10),
            publish_chat_id=None,
            publish_announcement=False,
            publish_results=False,
            created_by=admin.id,
            prizes=[{"prize_type": "stars", "amount": "10"}],
        )
        refund_orders = []
        for index in range(2):
            order = Order(
                id=next_synthetic_order_id(),
                order_key=f"GWREFUND-{uuid.uuid4().hex}",
                user_id=users[0].id,
                recipient_username=users[0].username or "refund_test",
                product_type="stars",
                quantity=50,
                price_usdt=Decimal("1.00"),
                status="completed",
                payment_provider="balance",
                created_at=now - timedelta(minutes=5 - index),
                completed_at=now - timedelta(minutes=4 - index),
            )
            session.add(order)
            await session.flush()
            await service.process_completed_order(order.id, giveaway_id=refund_giveaway.id)
            refund_orders.append(order)
        refund_orders[0].status = "refunded"
        await service._recalculate_purchase_entries(refund_giveaway)
        refund_records = list(
            (
                await session.execute(
                    select(GiveawayEntryOrder)
                    .where(GiveawayEntryOrder.giveaway_id == refund_giveaway.id)
                    .order_by(GiveawayEntryOrder.order_id)
                )
            ).scalars().all()
        )
        assert [record.tickets_awarded for record in refund_records] == [0, 1]

        # Final reconciliation must drain more than one internal batch before drawing.
        batch_size = 505
        batch_start = now - timedelta(minutes=10)
        batch_end = now - timedelta(minutes=1)
        batch_giveaway = await service.create_giveaway(
            title="Purchase batch reconciliation check",
            description=None,
            photo_file_id=None,
            participation_mode="tickets_per_order",
            product_filter="stars",
            tickets_per_order=1,
            stars_per_ticket=None,
            starts_at=batch_start,
            ends_at=batch_end,
            publish_chat_id=None,
            publish_announcement=False,
            publish_results=False,
            created_by=admin.id,
            prizes=[{"prize_type": "stars", "amount": "25"}],
            grace_minutes=0,
        )
        registration_batch = await service.create_giveaway(
            title="Registration batch reconciliation check",
            description=None,
            photo_file_id=None,
            participation_mode="registration_new",
            product_filter=None,
            tickets_per_order=1,
            stars_per_ticket=None,
            starts_at=batch_start,
            ends_at=batch_end,
            publish_chat_id=None,
            publish_announcement=False,
            publish_results=False,
            created_by=admin.id,
            prizes=[{"prize_type": "stars", "amount": "25"}],
            grace_minutes=0,
        )

        base_id = -int(uuid.uuid4().int % 7_000_000_000_000_000_000) - 1
        synthetic_ids = [base_id - index for index in range(batch_size)]
        session.add_all(
            [
                User(
                    id=user_id,
                    username=None,
                    language_code="ru",
                    referral_code=f"B{uuid.uuid4().hex[:30]}",
                    created_at=now - timedelta(minutes=5),
                )
                for user_id in synthetic_ids
            ]
        )
        await session.flush()
        session.add_all(
            [
                Order(
                    id=next_synthetic_order_id(),
                    order_key=f"GWBATCH-{uuid.uuid4().hex}",
                    user_id=user_id,
                    recipient_username=f"batch_{index}",
                    product_type="stars",
                    quantity=100,
                    price_usdt=Decimal("1.00"),
                    status="completed",
                    payment_provider="balance",
                    created_at=now - timedelta(minutes=5),
                    completed_at=now - timedelta(minutes=4),
                )
                for index, user_id in enumerate(synthetic_ids)
            ]
        )
        await session.flush()

        await service.draw_due(batch_giveaway.id, now=now)
        batch_entries = list(
            (
                await session.execute(
                    select(GiveawayEntry.id).where(
                        GiveawayEntry.giveaway_id == batch_giveaway.id,
                        GiveawayEntry.user_id.in_(synthetic_ids),
                    )
                )
            ).scalars().all()
        )
        batch_records = list(
            (
                await session.execute(
                    select(GiveawayEntryOrder.id).where(
                        GiveawayEntryOrder.giveaway_id == batch_giveaway.id,
                        GiveawayEntryOrder.user_id.in_(synthetic_ids),
                    )
                )
            ).scalars().all()
        )
        assert len(batch_entries) == batch_size
        assert len(batch_records) == batch_size

        await service.draw_due(registration_batch.id, now=now)
        registration_entries = list(
            (
                await session.execute(
                    select(GiveawayEntry.id).where(
                        GiveawayEntry.giveaway_id == registration_batch.id,
                        GiveawayEntry.user_id.in_(synthetic_ids),
                    )
                )
            ).scalars().all()
        )
        assert len(registration_entries) == batch_size

        await session.rollback()
        print("GIVEAWAY_INTEGRATION_OK")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
