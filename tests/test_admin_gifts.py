from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendGift
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.bot.handlers.admin_gifts import (
    TELEGRAM_GIFT_TEXT_LIMIT,
    _get_live_gifts_and_balance,
    _issue_payment_invoice,
    _preflight_selected_gift,
    _refund_paid_topups,
    build_gift_text,
    max_comment_length,
    telegram_text_length,
    validate_archived_gift_id,
    validate_archived_gift_price,
    validate_archived_gift_title,
    validate_gift_pre_checkout,
)
from src.bot.keyboards.admin import AdminCallback, get_admin_menu_keyboard
from src.bot.keyboards.admin_gifts import (
    _archived_gift_button_title,
    admin_gift_catalog_keyboard,
    archived_gift_item_keyboard,
    archived_gift_manage_keyboard,
)
from src.db.models import (
    AdminGift,
    AdminGiftPayment,
    AdminGiftPaymentStatus,
    AdminGiftStatus,
    ArchivedGift,
    Base,
    User,
)
from src.services.admin_gift_service import (
    AdminGiftService,
    GiftInvoiceAlreadyPaidError,
)
from src.services.archived_gift_service import (
    ArchivedGiftAlreadyExistsError,
    ArchivedGiftService,
)
from src.services.archived_gift_catalog import (
    ArchivedGiftCatalogError,
    ArchivedGiftCatalogItem,
    parse_archived_gift_catalog,
)


def _callback_values(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_admin_menu_contains_native_gift_action() -> None:
    markup = get_admin_menu_keyboard()
    assert AdminCallback.GIFTS in _callback_values(markup)
    assert "🎁 Подарить подарок" in _button_texts(markup)


@pytest.mark.asyncio
async def test_read_only_gift_preflight_retries_one_network_timeout() -> None:
    timeout = TelegramNetworkError(
        method=SimpleNamespace(),
        message="Request timeout error",
    )
    bot = SimpleNamespace(
        get_available_gifts=AsyncMock(side_effect=[timeout, "gifts"]),
        get_my_star_balance=AsyncMock(side_effect=["old-balance", "balance"]),
    )

    with patch("src.bot.handlers.admin_gifts.asyncio.sleep", AsyncMock()) as sleep:
        gifts, balance = await _get_live_gifts_and_balance(bot)

    assert (gifts, balance) == ("gifts", "balance")
    assert bot.get_available_gifts.await_count == 2
    assert bot.get_my_star_balance.await_count == 2
    sleep.assert_awaited_once_with(0.5)


def test_gift_text_contains_only_optional_admin_comment() -> None:
    assert build_gift_text(None) == ""
    assert build_gift_text("") == ""
    assert build_gift_text("С праздником!") == "С праздником!"
    assert build_gift_text("  С праздником!  ") == "С праздником!"


def test_gift_comment_limit_uses_full_telegram_utf16_budget() -> None:
    comment = "a" * max_comment_length()
    full_text = build_gift_text(comment)

    assert telegram_text_length(full_text) == TELEGRAM_GIFT_TEXT_LIMIT
    assert telegram_text_length(build_gift_text(comment + "🎁")) > TELEGRAM_GIFT_TEXT_LIMIT


def test_catalog_is_dynamic_and_has_no_upgrade_action() -> None:
    gifts = [
        {
            "id": "gift-1",
            "emoji": "🌹",
            "star_count": 25,
            "remaining_count": 3,
        },
        {
            "id": "gift-2",
            "emoji": "💝",
            "star_count": 50,
            "remaining_count": None,
        },
    ]
    markup = admin_gift_catalog_keyboard(gifts)
    text = " ".join(_button_texts(markup)).lower()
    callbacks = _callback_values(markup)

    assert "🌹 25 ⭐ · осталось 3" in _button_texts(markup)
    assert "💝 50 ⭐" in _button_texts(markup)
    assert "улучш" not in text
    assert "upgrade" not in " ".join(callbacks).lower()
    assert "admin:gifts:archive:choose" in callbacks


def test_archived_gift_input_validation() -> None:
    assert validate_archived_gift_id(" 5170233102089322756 ") == "5170233102089322756"
    assert validate_archived_gift_price("250") == 250
    assert validate_archived_gift_title("  Новогодний подарок  ") == "Новогодний подарок"

    for invalid in ("", "-1", "1.5", "abc", str(2**63)):
        with pytest.raises(ValueError):
            validate_archived_gift_id(invalid)
    for invalid in ("", "0", "-50", "1.5"):
        with pytest.raises(ValueError):
            validate_archived_gift_price(invalid)


def test_archived_gift_management_keyboards_use_stable_database_ids() -> None:
    gifts = [
        SimpleNamespace(
            id=41,
            title="Ёлка",
            emoji="🎄",
            star_count=50,
            is_active=True,
        ),
        SimpleNamespace(
            id=99,
            title="Сердце",
            emoji="💝",
            star_count=100,
            is_active=False,
        ),
    ]
    markup = archived_gift_manage_keyboard(gifts, has_recipient=True)
    callbacks = _callback_values(markup)

    assert "admin:gifts:archive:item:41" in callbacks
    assert "admin:gifts:archive:item:99" in callbacks
    assert "admin:gifts:archive:add" in callbacks
    assert "admin:gifts:archive:sync" in callbacks
    assert "admin:gifts:archive:choose" in callbacks

    active_item = archived_gift_item_keyboard(gifts[0], has_recipient=True)
    inactive_item = archived_gift_item_keyboard(gifts[1], has_recipient=True)
    assert "admin:gifts:archive:select:41" in _callback_values(active_item)
    assert "admin:gifts:archive:set:0:41" in _callback_values(active_item)
    assert "admin:gifts:archive:set:1:99" in _callback_values(inactive_item)
    assert not any(
        value.startswith("admin:gifts:archive:select:")
        for value in _callback_values(inactive_item)
    )


def test_archived_gift_button_title_is_bounded() -> None:
    compact = _archived_gift_button_title("Очень длинное название " * 10)

    assert len(compact) == 32
    assert compact.endswith("…")


@pytest.mark.asyncio
async def test_archived_preflight_skips_live_catalog_and_uses_saved_snapshot() -> None:
    archived = SimpleNamespace(
        id=17,
        gift_id="5170233102089322756",
        title="Архивный подарок",
        emoji="🎁",
        star_count=50,
        sticker_file_id="sticker-file-id",
        is_active=True,
    )
    balance = SimpleNamespace(amount=500)
    bot = SimpleNamespace(get_my_star_balance=AsyncMock(return_value=balance))

    with patch(
        "src.bot.handlers.admin_gifts._get_archived_gift",
        AsyncMock(return_value=archived),
    ):
        verified, returned_balance = await _preflight_selected_gift(
            bot,
            {
                "source": "archive",
                "archived_gift_id": archived.id,
                "id": archived.gift_id,
            },
        )

    assert returned_balance is balance
    assert verified == {
        "id": archived.gift_id,
        "source": "archive",
        "archived_gift_id": archived.id,
        "title": archived.title,
        "emoji": archived.emoji,
        "star_count": archived.star_count,
        "total_count": None,
        "remaining_count": None,
        "sticker_file_id": archived.sticker_file_id,
    }
    bot.get_my_star_balance.assert_awaited_once()


@pytest.mark.asyncio
async def test_archived_gift_crud_is_shared_and_enforces_unique_gift_id() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[User.__table__, ArchivedGift.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            session.add(
                User(
                    id=42,
                    username="admin",
                    language_code="ru",
                    referral_code="ARCHIVE_ADMIN",
                    is_admin=True,
                )
            )
            await session.commit()
            service = ArchivedGiftService(session)
            gift = await service.create(
                gift_id="5170233102089322756",
                title="Праздничный подарок",
                emoji="🎄",
                star_count=50,
                sticker_file_id=None,
                admin_id=42,
            )
            gift_database_id = gift.id
            telegram_gift_id = gift.gift_id

            assert (await service.list_gifts(active_only=True))[0].id == gift_database_id
            with pytest.raises(ArchivedGiftAlreadyExistsError):
                await service.create(
                    gift_id=telegram_gift_id,
                    title="Дубликат",
                    emoji="🎁",
                    star_count=50,
                    sticker_file_id=None,
                    admin_id=42,
                )

            updated = await service.update_fields(
                gift_database_id,
                title="Новое название",
                star_count=75,
            )
            assert updated.title == "Новое название"
            assert updated.star_count == 75
            assert (
                await service.set_active(gift_database_id, is_active=False)
            ).is_active is False
            assert (
                await service.set_active(gift_database_id, is_active=False)
            ).is_active is False
            assert await service.list_gifts(active_only=True) == []
            deleted = await service.delete(gift_database_id)
            assert deleted.gift_id == "5170233102089322756"
            assert await service.get(gift_database_id) is None
    finally:
        await engine.dispose()


def test_external_archived_catalog_parses_only_valid_sold_out_gifts() -> None:
    catalog = {
        "star_gifts_full": {
            "gifts": [
                {
                    "id": 123,
                    "stars": 50,
                    "sold_out": True,
                    "title": None,
                    "sticker": {"attributes": [{"alt": "🎄"}]},
                },
                {
                    "id": 456,
                    "stars": 100,
                    "sold_out": False,
                    "title": "Still available",
                },
                {"id": "invalid", "stars": 10, "sold_out": True},
            ]
        }
    }
    details = {
        "upgraded": [
            {"regular_id": "123", "full_name": "Holiday Tree"},
        ]
    }

    gifts = parse_archived_gift_catalog(catalog, details=details)

    assert gifts == [
        ArchivedGiftCatalogItem(
            gift_id="123",
            title="Holiday Tree",
            emoji="🎄",
            star_count=50,
        )
    ]


def test_external_archived_catalog_rejects_invalid_or_empty_payload() -> None:
    with pytest.raises(ArchivedGiftCatalogError):
        parse_archived_gift_catalog({})
    with pytest.raises(ArchivedGiftCatalogError):
        parse_archived_gift_catalog(
            {"star_gifts_full": {"gifts": [{"id": 1, "stars": 1}]}}
        )


@pytest.mark.asyncio
async def test_external_import_preserves_existing_gift_and_adds_missing() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[User.__table__, ArchivedGift.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            session.add(
                User(
                    id=42,
                    username="admin",
                    language_code="ru",
                    referral_code="IMPORT_ADMIN",
                    is_admin=True,
                )
            )
            await session.commit()
            service = ArchivedGiftService(session)
            existing = await service.create(
                gift_id="100",
                title="Ручное название",
                emoji="🎁",
                star_count=999,
                sticker_file_id="manual-preview",
                admin_id=42,
            )

            imported, preserved = await service.import_missing(
                [
                    ArchivedGiftCatalogItem("100", "External", "❌", 1),
                    ArchivedGiftCatalogItem("200", "Imported", "🎄", 50),
                ],
                admin_id=42,
            )

            assert (imported, preserved) == (1, 1)
            assert existing.title == "Ручное название"
            assert existing.star_count == 999
            added = await service.get_by_gift_id("200")
            assert added is not None
            assert added.title == "Imported"
            assert added.star_count == 50
            assert added.is_active is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_gift_attempt_keeps_archived_source_snapshot() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    ArchivedGift.__table__,
                    AdminGift.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            session.add_all(
                [
                    User(
                        id=42,
                        username="admin",
                        language_code="ru",
                        referral_code="ARCHIVE_ATTEMPT_ADMIN",
                        is_admin=True,
                    ),
                    User(
                        id=123456,
                        username="recipient",
                        language_code="ru",
                        referral_code="ARCHIVE_ATTEMPT_USER",
                    ),
                ]
            )
            await session.commit()
            archived = await ArchivedGiftService(session).create(
                gift_id="5170233102089322756",
                title="Старый праздник",
                emoji="🎄",
                star_count=50,
                sticker_file_id=None,
                admin_id=42,
            )
            service = AdminGiftService(session, bot=SimpleNamespace())
            attempt, created = await service.create_or_get_attempt(
                operation_key="archived-source-snapshot",
                admin_id=42,
                admin_username="admin",
                recipient_id=123456,
                recipient_username="recipient",
                recipient_was_banned=False,
                gift_id=archived.gift_id,
                gift_emoji=archived.emoji,
                gift_star_count=archived.star_count,
                gift_text="",
                bot_balance_before=100,
                gift_source="archive",
                gift_title=archived.title,
                archived_gift_id=archived.id,
            )

            assert created is True
            assert attempt.gift_source == "archive"
            assert attempt.gift_title_snapshot == archived.title
            assert attempt.archived_gift_id == archived.id
    finally:
        await engine.dispose()


class FakeBot:
    def __init__(self, result=True, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def send_gift(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class FakeGiftService(AdminGiftService):
    def __init__(self, attempt, bot, *, claimed=True):
        super().__init__(session=None, bot=bot)
        self.attempt = attempt
        self.claimed = claimed
        self.finalized: list[tuple[AdminGiftStatus, BaseException | None]] = []

    async def _claim_attempt(self, attempt_id: int, admin_id: int):
        if self.claimed:
            self.attempt.status = AdminGiftStatus.SENDING.value
        return self.attempt, self.claimed

    async def _finalize(self, attempt_id: int, *, status, error=None):
        self.attempt.status = status.value
        self.attempt.error_message = str(error) if error else None
        self.finalized.append((status, error))
        return self.attempt


def _attempt(status=AdminGiftStatus.PENDING.value):
    return SimpleNamespace(
        id=7,
        recipient_id=123456,
        gift_id="gift-id",
        gift_text="Поздравляю!",
        status=status,
        error_message=None,
    )


@pytest.mark.asyncio
async def test_send_attempt_is_claimed_once_and_never_pays_for_upgrade() -> None:
    bot = FakeBot()
    service = FakeGiftService(_attempt(), bot)

    outcome = await service.send_attempt(7, 42)

    assert outcome.status == AdminGiftStatus.SUCCEEDED.value
    assert outcome.performed is True
    assert len(bot.calls) == 1
    assert bot.calls[0]["user_id"] == 123456
    assert bot.calls[0]["pay_for_upgrade"] is False
    assert bot.calls[0]["text"] == "Поздравляю!"


@pytest.mark.asyncio
async def test_gift_without_comment_is_sent_without_text() -> None:
    bot = FakeBot()
    attempt = _attempt()
    attempt.gift_text = ""
    service = FakeGiftService(attempt, bot)

    outcome = await service.send_attempt(7, 42)

    assert outcome.status == AdminGiftStatus.SUCCEEDED.value
    assert bot.calls[0]["text"] is None


@pytest.mark.asyncio
async def test_duplicate_send_attempt_does_not_call_telegram() -> None:
    bot = FakeBot()
    attempt = _attempt(AdminGiftStatus.SUCCEEDED.value)
    service = FakeGiftService(attempt, bot, claimed=False)

    outcome = await service.send_attempt(7, 42)

    assert outcome.performed is False
    assert outcome.status == AdminGiftStatus.SUCCEEDED.value
    assert bot.calls == []


@pytest.mark.asyncio
async def test_unknown_error_is_not_retried_and_is_marked_unknown() -> None:
    bot = FakeBot(error=RuntimeError("connection ended after request"))
    service = FakeGiftService(_attempt(), bot)

    outcome = await service.send_attempt(7, 42)

    assert outcome.status == AdminGiftStatus.UNKNOWN.value
    assert len(bot.calls) == 1
    assert service.finalized[0][0] == AdminGiftStatus.UNKNOWN


@pytest.mark.asyncio
async def test_telegram_rejection_is_marked_failed() -> None:
    error = TelegramBadRequest(
        method=SendGift(gift_id="gift-id", user_id=123456),
        message="gift is unavailable",
    )
    bot = FakeBot(error=error)
    service = FakeGiftService(_attempt(), bot)

    outcome = await service.send_attempt(7, 42)

    assert outcome.status == AdminGiftStatus.FAILED.value
    assert len(bot.calls) == 1
    assert service.finalized[0][0] == AdminGiftStatus.FAILED


def test_pre_checkout_accepts_any_payer_with_exact_xtr_amount() -> None:
    payment = SimpleNamespace(
        status=AdminGiftPaymentStatus.INVOICE_SENT.value,
        requested_stars=10,
    )
    attempt = SimpleNamespace(
        admin_id=42,
        status=AdminGiftStatus.AWAITING_PAYMENT.value,
    )

    assert validate_gift_pre_checkout(
        payment=payment,
        attempt=attempt,
        currency="XTR",
        total_amount=10,
    ) is None
    assert validate_gift_pre_checkout(
        payment=payment,
        attempt=attempt,
        currency="XTR",
        total_amount=10,
    ) is None
    assert "Сумма счёта" in validate_gift_pre_checkout(
        payment=payment,
        attempt=attempt,
        currency="XTR",
        total_amount=9,
    )


class FakeInvoiceBot:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_invoice(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=321)


class FakeInvoiceService:
    def __init__(self):
        self.payment = SimpleNamespace(
            id=11,
            invoice_payload="agift:payload",
            requested_stars=10,
            status=AdminGiftPaymentStatus.INVOICE_PENDING.value,
            invoice_message_id=None,
        )

    async def create_payment_request(self, **kwargs):
        assert kwargs["requested_stars"] == 10
        return self.payment, True

    async def mark_invoice_sent(self, payment_id, invoice_message_id):
        assert payment_id == self.payment.id
        self.payment.status = AdminGiftPaymentStatus.INVOICE_SENT.value
        self.payment.invoice_message_id = invoice_message_id
        return self.payment

    async def mark_invoice_failed(self, payment_id, error):
        raise AssertionError(f"Invoice unexpectedly failed: {error}")


@pytest.mark.asyncio
async def test_missing_balance_invoice_uses_exact_xtr_difference() -> None:
    bot = FakeInvoiceBot()
    service = FakeInvoiceService()
    attempt = SimpleNamespace(
        id=7,
        admin_id=42,
        recipient_id=123456,
        recipient_username_snapshot="recipient",
    )

    payment, created = await _issue_payment_invoice(bot, service, attempt, 10)

    assert created is True
    assert payment.invoice_message_id == 321
    assert len(bot.calls) == 1
    call = bot.calls[0]
    assert call["chat_id"] == 42
    assert call["currency"] == "XTR"
    assert call["provider_token"] == ""
    assert call["payload"] == "agift:payload"
    assert "start_parameter" not in call
    assert "protect_content" not in call
    assert len(call["prices"]) == 1
    assert call["prices"][0].amount == 10


@pytest.mark.asyncio
async def test_pending_invoice_is_resent_with_same_payload_after_restart() -> None:
    bot = FakeInvoiceBot()
    service = FakeInvoiceService()

    async def existing_request(**kwargs):
        assert kwargs["requested_stars"] == 10
        return service.payment, False

    service.create_payment_request = existing_request
    attempt = SimpleNamespace(
        id=7,
        admin_id=42,
        recipient_id=123456,
        recipient_username_snapshot="recipient",
    )

    payment, sent = await _issue_payment_invoice(bot, service, attempt, 10)

    assert sent is True
    assert payment.invoice_payload == "agift:payload"
    assert bot.calls[0]["payload"] == "agift:payload"
    assert bot.calls[0]["prices"][0].amount == payment.requested_stars


@pytest.mark.asyncio
async def test_paid_invoice_is_idempotent_and_reactivates_gift() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    AdminGift.__table__,
                    AdminGiftPayment.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            session.add_all(
                [
                    User(
                        id=42,
                        username="admin",
                        language_code="ru",
                        referral_code="ADMIN42",
                        is_admin=True,
                    ),
                    User(
                        id=123456,
                        username="recipient",
                        language_code="ru",
                        referral_code="USER123",
                    ),
                ]
            )
            await session.commit()
            attempt = AdminGift(
                operation_key="test-operation",
                admin_id=42,
                recipient_id=123456,
                gift_id="gift-id",
                gift_star_count=15,
                gift_text="",
                bot_balance_before=5,
                status=AdminGiftStatus.PENDING.value,
            )
            session.add(attempt)
            await session.commit()

            service = AdminGiftService(session, bot=SimpleNamespace())
            payment, created = await service.create_payment_request(
                attempt_id=attempt.id,
                admin_id=42,
                requested_stars=10,
            )
            duplicate, duplicate_created = await service.create_payment_request(
                attempt_id=attempt.id,
                admin_id=42,
                requested_stars=10,
            )

            assert created is True
            assert duplicate_created is False
            assert duplicate.id == payment.id
            assert (await service.get_attempt(attempt.id)).status == (
                AdminGiftStatus.AWAITING_PAYMENT.value
            )

            assert await service.claim_pre_checkout(
                payment.id, payer_id=99, query_id="checkout-1"
            ) is True
            assert await service.claim_pre_checkout(
                payment.id, payer_id=99, query_id="checkout-1"
            ) is True
            assert await service.claim_pre_checkout(
                payment.id, payer_id=99, query_id="checkout-2"
            ) is False
            assert await service.claim_pre_checkout(
                payment.id, payer_id=100, query_id="checkout-3"
            ) is False

            paid, paid_attempt, claimed = await service.record_successful_payment(
                invoice_payload=payment.invoice_payload,
                payer_id=99,
                currency="XTR",
                total_amount=10,
                telegram_payment_charge_id="charge-1",
                provider_payment_charge_id="",
            )
            repeated, repeated_attempt, repeated_claimed = (
                await service.record_successful_payment(
                    invoice_payload=payment.invoice_payload,
                    payer_id=99,
                    currency="XTR",
                    total_amount=10,
                    telegram_payment_charge_id="charge-1",
                    provider_payment_charge_id="",
                )
            )
            with pytest.raises(GiftInvoiceAlreadyPaidError):
                await service.record_successful_payment(
                    invoice_payload=payment.invoice_payload,
                    payer_id=99,
                    currency="XTR",
                    total_amount=10,
                    telegram_payment_charge_id="charge-2",
                    provider_payment_charge_id="",
                )

            assert claimed is True
            assert repeated_claimed is False
            assert paid.id == repeated.id
            assert paid.status == AdminGiftPaymentStatus.PAID.value
            assert paid.pre_checkout_payer_id == 99
            assert paid.payer_id == 99
            assert paid_attempt.status == AdminGiftStatus.PENDING.value
            assert repeated_attempt.status == AdminGiftStatus.PENDING.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_pre_checkout_reservation_can_be_reclaimed() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    AdminGift.__table__,
                    AdminGiftPayment.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            session.add_all(
                [
                    User(
                        id=42,
                        username="admin",
                        language_code="ru",
                        referral_code="ADMIN42",
                        is_admin=True,
                    ),
                    User(
                        id=123456,
                        username="recipient",
                        language_code="ru",
                        referral_code="USER123",
                    ),
                ]
            )
            await session.commit()
            attempt = AdminGift(
                operation_key="stale-reservation",
                admin_id=42,
                recipient_id=123456,
                gift_id="gift-id",
                gift_star_count=15,
                gift_text="",
                status=AdminGiftStatus.AWAITING_PAYMENT.value,
            )
            session.add(attempt)
            await session.commit()
            payment = AdminGiftPayment(
                gift_attempt_id=attempt.id,
                invoice_payload="agift:stale",
                requested_stars=10,
                status=AdminGiftPaymentStatus.PRECHECKOUT.value,
                pre_checkout_payer_id=99,
                pre_checkout_at=datetime.utcnow() - timedelta(minutes=11),
            )
            session.add(payment)
            await session.commit()

            service = AdminGiftService(session, bot=SimpleNamespace())
            assert await service.claim_pre_checkout(
                payment.id, payer_id=100, query_id="checkout-new"
            ) is True
            refreshed, _ = await service.get_payment_context(payment.invoice_payload)
            assert refreshed.status == AdminGiftPaymentStatus.PRECHECKOUT.value
            assert refreshed.pre_checkout_payer_id == 100
            assert refreshed.pre_checkout_query_id == "checkout-new"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_gift_refunds_the_actual_unregistered_payer() -> None:
    class RefundBot:
        def __init__(self):
            self.calls = []

        async def refund_star_payment(self, **kwargs):
            self.calls.append(kwargs)
            return True

    class RefundService:
        def __init__(self):
            self.marked = []

        async def get_refundable_payments(self, attempt_id):
            assert attempt_id == 7
            return [
                SimpleNamespace(
                    id=11,
                    payer_id=999999,
                    telegram_payment_charge_id="charge-public-payer",
                    paid_stars=10,
                    requested_stars=10,
                )
            ]

        async def mark_payment_refunded(self, payment_id, error=None):
            self.marked.append((payment_id, error))

    bot = RefundBot()
    service = RefundService()
    refunded, failed = await _refund_paid_topups(
        bot,
        service,
        SimpleNamespace(id=7, admin_id=42),
    )

    assert (refunded, failed) == (10, 0)
    assert bot.calls[0]["user_id"] == 999999
    assert service.marked == [(11, None)]


@pytest.mark.asyncio
async def test_already_refunded_response_repairs_refund_audit_state() -> None:
    class AlreadyRefundedBot:
        async def refund_star_payment(self, **kwargs):
            raise RuntimeError("Bad Request: CHARGE_ALREADY_REFUNDED")

    class RefundService:
        def __init__(self):
            self.marked = []

        async def get_refundable_payments(self, attempt_id):
            return [
                SimpleNamespace(
                    id=12,
                    payer_id=777,
                    telegram_payment_charge_id="already-refunded",
                    paid_stars=6,
                    requested_stars=6,
                )
            ]

        async def mark_payment_refunded(self, payment_id, error=None):
            self.marked.append((payment_id, error))

    service = RefundService()
    refunded, failed = await _refund_paid_topups(
        AlreadyRefundedBot(),
        service,
        SimpleNamespace(id=8, admin_id=42),
    )

    assert (refunded, failed) == (6, 0)
    assert service.marked == [(12, None)]
