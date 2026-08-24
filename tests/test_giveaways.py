"""Focused tests for giveaway rules, draw behavior and navigation."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.bot.handlers.admin_giveaways import _delete_admin_input, _previous_wizard_step
from src.bot.handlers.start import parse_start_params
from src.bot.main import register_user_middlewares
from src.bot.middlewares.ban_check import BanCheckMiddleware
from src.bot.middlewares.giveaway_activity import GiveawayActivityMiddleware
from src.bot.middlewares.subscription_check import SubscriptionCheckMiddleware
from src.bot.keyboards.admin_giveaways import (
    admin_giveaway_cancel_wizard_keyboard,
    admin_giveaway_channels_keyboard,
    admin_giveaway_confirm_keyboard,
    admin_giveaway_description_keyboard,
    admin_giveaway_mode_keyboard,
    admin_giveaway_photo_keyboard,
    admin_giveaway_prize_type_keyboard,
    admin_giveaway_prizes_keyboard,
    admin_giveaway_product_keyboard,
    admin_giveaway_publication_keyboard,
    admin_giveaway_start_keyboard,
)
from src.bot.keyboards.menu import MenuCallback, get_main_menu_keyboard
from src.locales import t
from src.services.giveaway_service import (
    MODE_PURCHASE_ONCE,
    MODE_REGISTRATION_ALL,
    MODE_TICKETS_PER_ORDER,
    MODE_TICKETS_PER_STARS,
    calculate_order_ticket_award,
    condition_text,
    GiveawayService,
    is_giveaway_due_for_draw,
    is_order_eligible_for_giveaway,
    prize_text,
    weighted_unique_draw,
)
from src.workers.giveaway_worker import (
    INCOMPLETE_WINNERS_RESULT_ERROR,
    GiveawayWorker,
    build_announcement_text,
    build_results_text,
)


class GiveawayTicketTests(unittest.TestCase):
    def test_stars_threshold_is_calculated_per_order(self):
        self.assertEqual(
            calculate_order_ticket_award(
                MODE_TICKETS_PER_STARS,
                quantity=40,
                stars_per_ticket=100,
            ),
            0,
        )
        self.assertEqual(
            calculate_order_ticket_award(
                MODE_TICKETS_PER_STARS,
                quantity=60,
                stars_per_ticket=100,
            ),
            0,
        )
        self.assertEqual(
            calculate_order_ticket_award(
                MODE_TICKETS_PER_STARS,
                quantity=250,
                stars_per_ticket=100,
            ),
            2,
        )

    def test_purchase_once_only_awards_first_order(self):
        self.assertEqual(calculate_order_ticket_award(MODE_PURCHASE_ONCE, quantity=1, first_entry=True), 1)
        self.assertEqual(calculate_order_ticket_award(MODE_PURCHASE_ONCE, quantity=1, first_entry=False), 0)

    def test_tickets_per_order_uses_configured_weight(self):
        self.assertEqual(
            calculate_order_ticket_award(MODE_TICKETS_PER_ORDER, quantity=1, tickets_per_order=5),
            5,
        )

    def test_order_must_complete_by_configured_end_time(self):
        end = datetime(2026, 7, 29, 10, 0)
        giveaway = SimpleNamespace(
            starts_at=end - timedelta(hours=1),
            ends_at=end,
            grace_minutes=15,
            product_filter="all",
            participation_mode=MODE_TICKETS_PER_ORDER,
        )
        order = SimpleNamespace(
            status="completed",
            price_usdt=1,
            product_type="stars",
            created_at=end - timedelta(minutes=1),
            completed_at=end,
        )
        self.assertTrue(is_order_eligible_for_giveaway(giveaway, order))
        order.completed_at = end + timedelta(microseconds=1)
        self.assertFalse(is_order_eligible_for_giveaway(giveaway, order))

    def test_draw_is_due_at_configured_end_even_with_legacy_grace(self):
        end = datetime(2026, 7, 29, 10, 0)
        giveaway = SimpleNamespace(ends_at=end, grace_minutes=15)
        self.assertFalse(is_giveaway_due_for_draw(giveaway, end - timedelta(microseconds=1)))
        self.assertTrue(is_giveaway_due_for_draw(giveaway, end))

    def test_activity_condition_is_user_facing(self):
        giveaway = SimpleNamespace(
            participation_mode=MODE_REGISTRATION_ALL,
            product_filter=None,
            tickets_per_order=1,
            stars_per_ticket=None,
        )
        self.assertIn("действие", condition_text(giveaway, "ru"))
        self.assertIn("any action", condition_text(giveaway, "en"))


class GiveawayDrawTests(unittest.TestCase):
    def test_draw_is_weighted_and_removes_each_winner(self):
        rolls = iter([3, 0, 0])
        result = weighted_unique_draw(
            [(10, 1), (20, 3), (30, 6)],
            3,
            randbelow=lambda total: next(rolls),
        )
        self.assertEqual([item.user_id for item in result], [20, 10, 30])
        self.assertEqual(len({item.user_id for item in result}), 3)
        self.assertEqual([item.total_weight_before for item in result], [10, 7, 6])

    def test_draw_stops_when_prizes_exceed_participants(self):
        result = weighted_unique_draw([(10, 2)], 5, randbelow=lambda total: 0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].user_id, 10)


class GiveawayNavigationTests(unittest.TestCase):
    def test_giveaway_deep_link_is_parsed(self):
        self.assertEqual(parse_start_params("giveaway_42")[-1], 42)
        self.assertIsNone(parse_start_params("giveaway_bad")[-1])

    def test_main_menu_button_only_appears_when_active(self):
        without = get_main_menu_keyboard("ru", has_active_giveaways=False)
        with_active = get_main_menu_keyboard("ru", has_active_giveaways=True)
        callbacks_without = [button.callback_data for row in without.inline_keyboard for button in row]
        callbacks_with = [button.callback_data for row in with_active.inline_keyboard for button in row]
        self.assertNotIn(MenuCallback.GIVEAWAYS, callbacks_without)
        self.assertIn(MenuCallback.GIVEAWAYS, callbacks_with)


class GiveawayLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.giveaway = SimpleNamespace(
            title="Летний розыгрыш",
            description="Описание",
            product_filter="all",
            participation_mode=MODE_TICKETS_PER_STARS,
            tickets_per_order=2,
            stars_per_ticket=100,
            starts_at=datetime(2026, 7, 29, 9, 0),
            ends_at=datetime(2026, 7, 30, 9, 0),
            completed_at=datetime(2026, 7, 30, 9, 1),
        )
        self.prize = SimpleNamespace(prize_type="premium", amount=3, description=None, place=1)

    def test_conditions_and_prizes_follow_user_locale(self):
        self.assertIn("билет(а)", condition_text(self.giveaway, "ru"))
        self.assertIn("ticket(s)", condition_text(self.giveaway, "en"))
        self.assertEqual(prize_text(self.prize, "ru"), "Telegram Premium на 3 мес.")
        self.assertEqual(prize_text(self.prize, "en"), "Telegram Premium for 3 months")

    def test_posts_are_built_from_localized_templates(self):
        ru_announcement = build_announcement_text(self.giveaway, [self.prize], "ru")
        en_announcement = build_announcement_text(self.giveaway, [self.prize], "en")
        self.assertIn("<b>Призы:</b>", ru_announcement)
        self.assertIn("<b>Prizes:</b>", en_announcement)
        self.assertNotIn(self.giveaway.title, ru_announcement)

        winner = SimpleNamespace(place=1)
        user = SimpleNamespace(id=42, username="winner")
        rows = [(winner, user, self.prize)]
        ru_results = build_results_text(self.giveaway, rows, 4, "ru")
        en_results = build_results_text(self.giveaway, rows, 4, "en")
        self.assertIn("<b>Победители:</b>", ru_results)
        self.assertIn("<b>Winners:</b>", en_results)
        self.assertNotIn(self.giveaway.title, ru_results)

    def test_results_post_requires_a_winner(self):
        with self.assertRaises(ValueError):
            build_results_text(self.giveaway, [], 0, "ru")

    def test_user_message_keys_exist_in_both_locales(self):
        keys = (
            "giveaways.list.title",
            "giveaways.detail.template",
            "giveaways.posts.announcement",
            "giveaways.posts.results",
            "giveaways.notifications.tickets",
            "giveaways.notifications.registration",
            "giveaways.notifications.winner",
            "giveaways.buttons.open",
        )
        for lang in ("ru", "en"):
            for key in keys:
                self.assertNotEqual(t(key, lang), key)

    def test_creation_wizard_keyboards_use_step_back_navigation(self):
        keyboards = (
            admin_giveaway_cancel_wizard_keyboard(),
            admin_giveaway_description_keyboard(),
            admin_giveaway_mode_keyboard(),
            admin_giveaway_product_keyboard(),
            admin_giveaway_prizes_keyboard(True),
            admin_giveaway_prize_type_keyboard(),
            admin_giveaway_start_keyboard(),
            admin_giveaway_channels_keyboard([]),
            admin_giveaway_publication_keyboard(True, True),
            admin_giveaway_photo_keyboard(),
            admin_giveaway_confirm_keyboard(),
        )
        for keyboard in keyboards:
            buttons = [button for row in keyboard.inline_keyboard for button in row]
            self.assertTrue(any(button.callback_data == "admin:giveaways:create:back" for button in buttons))
            self.assertFalse(any("Отмена" in button.text for button in buttons))

    def test_creation_wizard_previous_step_covers_all_branches(self):
        self.assertEqual(_previous_wizard_step("title", {}), "menu")
        self.assertEqual(_previous_wizard_step("description", {}), "title")
        self.assertEqual(_previous_wizard_step("mode", {}), "description")
        self.assertEqual(_previous_wizard_step("product", {}), "mode")
        self.assertEqual(
            _previous_wizard_step("ticket_config", {"participation_mode": "tickets_per_order"}),
            "product",
        )
        self.assertEqual(
            _previous_wizard_step("ticket_config", {"participation_mode": "tickets_per_stars"}),
            "mode",
        )
        self.assertEqual(
            _previous_wizard_step("prizes", {"participation_mode": "tickets_per_order"}),
            "ticket_config",
        )
        self.assertEqual(_previous_wizard_step("prize_type", {}), "prizes")
        self.assertEqual(_previous_wizard_step("prize_value", {}), "prize_type")
        self.assertEqual(_previous_wizard_step("start_mode", {}), "prizes")
        self.assertEqual(_previous_wizard_step("start_date", {}), "start_mode")
        self.assertEqual(
            _previous_wizard_step("end_date", {"start_mode": "scheduled"}),
            "start_date",
        )
        self.assertEqual(_previous_wizard_step("channel", {}), "end_date")
        self.assertEqual(_previous_wizard_step("publication", {}), "channel")
        self.assertEqual(_previous_wizard_step("photo", {"publish_chat_id": -1001}), "publication")
        self.assertEqual(_previous_wizard_step("confirmation", {}), "photo")


class GiveawayAdminInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_wizard_input_is_deleted(self):
        message = SimpleNamespace(message_id=10, delete=AsyncMock())
        await _delete_admin_input(message)
        message.delete.assert_awaited_once()

    async def test_missing_winner_skips_post_and_notifies_creator(self):
        giveaway = SimpleNamespace(
            id=7,
            title="Техническое название",
            publish_chat_id=-1001,
            results_message_id=None,
            results_last_attempt_at=None,
            results_error=None,
            created_by=42,
            prizes=[SimpleNamespace(place=1)],
        )
        session = MagicMock()
        session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
        session.commit = AsyncMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=None)
        factory = MagicMock(return_value=context)
        bot = SimpleNamespace(send_message=AsyncMock())
        worker = GiveawayWorker(bot)

        with (
            patch("src.workers.giveaway_worker.async_session_factory", factory),
            patch.object(GiveawayService, "get_giveaway", AsyncMock(return_value=giveaway)),
            patch.object(GiveawayService, "get_entry_stats", AsyncMock(return_value=(0, 0))),
        ):
            await worker._publish_results(giveaway.id)

        bot.send_message.assert_awaited_once()
        self.assertEqual(giveaway.results_error, INCOMPLETE_WINNERS_RESULT_ERROR)
        session.commit.assert_awaited_once()


class GiveawayActivityMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_user_is_eligible_after_an_action(self):
        occurred_at = datetime(2026, 8, 4, 9, 0)
        user = SimpleNamespace(
            id=42,
            created_at=occurred_at - timedelta(days=30),
            is_banned=False,
            is_admin=False,
        )
        giveaway = SimpleNamespace(id=32, title="Activity giveaway")

        def scalar_result(values):
            result = MagicMock()
            result.scalars.return_value.all.return_value = values
            return result

        session = MagicMock()
        session.get = AsyncMock(return_value=user)
        session.execute = AsyncMock(
            side_effect=[
                scalar_result([giveaway.id]),
                scalar_result([]),
                scalar_result([giveaway]),
            ]
        )
        session.scalar = AsyncMock(return_value=1)

        awards = await GiveawayService(session).process_user_activity(
            user.id,
            occurred_at=occurred_at,
        )

        self.assertEqual([award.giveaway_id for award in awards], [giveaway.id])
        session.scalar.assert_awaited_once()

    def test_user_middlewares_are_outer_and_keep_gate_order(self):
        from aiogram import Dispatcher

        dispatcher = Dispatcher()
        register_user_middlewares(dispatcher)

        expected_types = [
            BanCheckMiddleware,
            SubscriptionCheckMiddleware,
            GiveawayActivityMiddleware,
        ]
        for observer in (
            dispatcher.message,
            dispatcher.callback_query,
            dispatcher.inline_query,
        ):
            self.assertEqual(
                [type(item) for item in observer.outer_middleware._middlewares],
                expected_types,
            )
            self.assertEqual(observer.middleware._middlewares, [])

    async def test_activity_is_recorded_before_the_handler(self):
        middleware = GiveawayActivityMiddleware()
        event = object()
        calls = []

        async def activity(user_id):
            calls.append(("activity", user_id))

        async def handler(received_event, data):
            calls.append(("handler", received_event))
            return "handled"

        with (
            patch.object(middleware, "_get_user_id", return_value=42),
            patch(
                "src.bot.middlewares.giveaway_activity.process_activity_for_giveaways",
                side_effect=activity,
            ),
        ):
            result = await middleware(handler, event, {})

        self.assertEqual(result, "handled")
        self.assertEqual(calls, [("activity", 42), ("handler", event)])

    async def test_activity_failure_does_not_block_the_bot_action(self):
        middleware = GiveawayActivityMiddleware()
        handler = AsyncMock(return_value="handled")
        with (
            patch.object(middleware, "_get_user_id", return_value=42),
            patch(
                "src.bot.middlewares.giveaway_activity.process_activity_for_giveaways",
                AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
        ):
            result = await middleware(handler, object(), {})

        self.assertEqual(result, "handled")
        handler.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
