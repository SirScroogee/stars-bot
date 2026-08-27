import enum
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ============ ENUMS ============

class OrderStatus(enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentStatus(enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELED = "canceled"
    REFUNDED = "refunded"
    CHARGEBACKED = "chargebacked"


class PaymentProvider(enum.Enum):
    BALANCE = "balance"
    TON = "ton"
    CRYPTOBOT = "cryptobot"
    PLATEGA = "platega"
    LAVA = "lava"


class ProductType(enum.Enum):
    STARS = "stars"
    PREMIUM = "premium"


class TransactionType(enum.Enum):
    DEPOSIT = "deposit"
    PURCHASE = "purchase"
    REFUND = "refund"
    REFERRAL = "referral"
    PROMO = "promo"
    CHECK_CREATED = "check_created"
    CHECK_ACTIVATED = "check_activated"
    WITHDRAWAL = "withdrawal"


class LedgerOperation(enum.Enum):
    CREDIT = "credit"
    DEBIT = "debit"


class AdminGiftStatus(enum.Enum):
    PENDING = "pending"
    AWAITING_PAYMENT = "awaiting_payment"
    SENDING = "sending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class AdminGiftPaymentStatus(enum.Enum):
    INVOICE_PENDING = "invoice_pending"
    INVOICE_SENT = "invoice_sent"
    PRECHECKOUT = "pre_checkout"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    REFUND_FAILED = "refund_failed"
    CANCELLED = "cancelled"


# ============ MODELS ============

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str] = mapped_column(String(10), default="ru")

    balance_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    balance_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0.000000"))
    balance_premium_months: Mapped[int] = mapped_column(Integer, default=0)

    # Реферальный баланс (можно вывести на основной USDT баланс)
    referral_balance: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0.000000"))
    # Общий заработок с рефералов за всё время
    total_referral_earnings: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0.000000"))

    # Замороженные балансы (для чеков)
    frozen_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    frozen_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0.000000"))
    frozen_premium_months: Mapped[int] = mapped_column(Integer, default=0)

    # Код реферала, который использовал пользователь при регистрации
    referrer_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Собственный реферальный код пользователя
    referral_code: Mapped[str] = mapped_column(String(32), unique=True)

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)  # Забанен в боте
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="user")
    ledger_entries: Mapped[list["BalanceLedger"]] = relationship(
        "BalanceLedger", back_populates="user"
    )


class ArchivedGift(Base):
    """Built-in retired Telegram Gift hidden from the live catalog."""

    __tablename__ = "archived_gifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gift_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(100))
    emoji: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    star_count: Mapped[int] = mapped_column(Integer)
    sticker_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_admin_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("gift_id", name="uq_archived_gifts_gift_id"),
        Index("ix_archived_gifts_active_title", "is_active", "title"),
    )


class AdminGift(Base):
    """Audit record for a Telegram Gift sent by an administrator."""

    __tablename__ = "admin_gifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_key: Mapped[str] = mapped_column(String(64))

    admin_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    admin_username_snapshot: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    recipient_id: Mapped[int] = mapped_column(BigInteger)
    recipient_username_snapshot: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    recipient_was_banned: Mapped[bool] = mapped_column(Boolean, default=False)

    gift_id: Mapped[str] = mapped_column(String(255))
    gift_emoji: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    gift_star_count: Mapped[int] = mapped_column(Integer)
    gift_source: Mapped[str] = mapped_column(String(20), default="live")
    gift_title_snapshot: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    archived_gift_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("archived_gifts.id", ondelete="SET NULL"),
        nullable=True,
    )
    pay_for_upgrade: Mapped[bool] = mapped_column(Boolean, default=False)
    gift_text: Mapped[str] = mapped_column(String(128))
    bot_balance_before: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    controller_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    controller_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), default=AdminGiftStatus.PENDING.value
    )
    error_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    payments: Mapped[list["AdminGiftPayment"]] = relationship(
        "AdminGiftPayment",
        back_populates="gift_attempt",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_admin_gifts_operation_key"),
        Index("ix_admin_gifts_recipient_created", "recipient_id", "created_at"),
        Index("ix_admin_gifts_admin_created", "admin_id", "created_at"),
        Index("ix_admin_gifts_status_created", "status", "created_at"),
    )


class AdminGiftPayment(Base):
    """One Telegram Stars invoice issued to fund an administrator Gift attempt."""

    __tablename__ = "admin_gift_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gift_attempt_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("admin_gifts.id", ondelete="CASCADE")
    )
    invoice_payload: Mapped[str] = mapped_column(String(128))
    requested_stars: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(20), default=AdminGiftPaymentStatus.INVOICE_PENDING.value
    )

    invoice_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    pre_checkout_payer_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    pre_checkout_query_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    payer_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    telegram_payment_charge_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    provider_payment_charge_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    paid_stars: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    pre_checkout_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    refunded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    gift_attempt: Mapped["AdminGift"] = relationship(
        "AdminGift", back_populates="payments"
    )

    __table_args__ = (
        UniqueConstraint("invoice_payload", name="uq_admin_gift_payments_payload"),
        UniqueConstraint(
            "telegram_payment_charge_id",
            name="uq_admin_gift_payments_telegram_charge",
        ),
        Index(
            "ix_admin_gift_payments_attempt_status",
            "gift_attempt_id",
            "status",
        ),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    recipient_username: Mapped[str] = mapped_column(String(255))

    # Тип: "stars" или "premium"
    product_type: Mapped[str] = mapped_column(String(20))
    # Количество: звёзд (для stars) или месяцев (3/6/12 для premium)
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    price_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6))

    status: Mapped[str] = mapped_column(String(20), default="pending")
    payment_provider: Mapped[str] = mapped_column(String(20))

    fragment_tx_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    fragment_ton_spent: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 9), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Durable processing state. Redis is only a delivery queue; retry/accounting
    # state must survive queue recovery and application restarts.
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    processing_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_fragment_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Notification deduplication for delayed paid orders.
    admin_alerted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    critical_alerted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    user_delay_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ID сообщения для редактирования при изменении статуса
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="orders")
    payment: Mapped[Optional["Payment"]] = relationship("Payment", back_populates="order")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="order"
    )
    attempts: Mapped[list["OrderAttempt"]] = relationship(
        "OrderAttempt", back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_orders_user_status", "user_id", "status"),
        Index("ix_orders_created_at", "created_at"),
    )


class OrderAttempt(Base):
    """Persistent history of every order-processing attempt or deferral."""

    __tablename__ = "order_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    fragment_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    order: Mapped["Order"] = relationship("Order", back_populates="attempts")

    __table_args__ = (
        UniqueConstraint("order_id", "attempt_number", name="uq_order_attempt_number"),
        Index("ix_order_attempts_order_started", "order_id", "started_at"),
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), unique=True)

    provider: Mapped[str] = mapped_column(String(20))
    provider_tx_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    currency: Mapped[str] = mapped_column(String(10))

    status: Mapped[str] = mapped_column(String(20), default="pending")

    webhook_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    order: Mapped["Order"] = relationship("Order", back_populates="payment")


class PlategaPayment(Base):
    __tablename__ = "platega_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)

    operation_type: Mapped[str] = mapped_column(String(20))  # deposit / stars / premium
    provider_tx_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default=PaymentStatus.PENDING.value)

    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fee_percent: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0.0000"))

    payment_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_platega_payments_status_created", "status", "created_at"),
        Index("ix_platega_payments_user_status", "user_id", "status"),
    )


class LavaPayment(Base):
    __tablename__ = "lava_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)

    operation_type: Mapped[str] = mapped_column(String(20))  # deposit / stars / premium
    provider_order_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    provider_invoice_id: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default=PaymentStatus.PENDING.value)

    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    base_amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fee_percent: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("3.4000"))
    usdt_rub_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    rate_source: Mapped[str] = mapped_column(String(20))

    payment_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_lava_payments_status_created", "status", "created_at"),
        Index("ix_lava_payments_user_status", "user_id", "status"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=True
    )

    type: Mapped[str] = mapped_column(String(30))

    amount_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0.000000"))

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # JSON с дополнительными данными в зависимости от типа транзакции
    extra_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Внешний ID транзакции (event_id для TON, invoice_id для CryptoBot)
    # Используется для идемпотентности — предотвращает двойное зачисление
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    order: Mapped[Optional["Order"]] = relationship("Order", back_populates="transactions")

    __table_args__ = (
        Index("ix_transactions_user_type", "user_id", "type"),
        Index("ix_transactions_created_at", "created_at"),
    )


class BalanceLedger(Base):
    __tablename__ = "balance_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    transaction_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("transactions.id"), nullable=True
    )

    operation: Mapped[str] = mapped_column(String(10))

    amount_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0.000000"))
    amount_premium: Mapped[int] = mapped_column(Integer, default=0)  # Месяцы Premium

    balance_stars_before: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    balance_stars_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    balance_usdt_before: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    balance_usdt_after: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    balance_premium_before: Mapped[int] = mapped_column(Integer, default=0)
    balance_premium_after: Mapped[int] = mapped_column(Integer, default=0)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="ledger_entries")

    __table_args__ = (
        Index("ix_balance_ledger_user_id", "user_id"),
        Index("ix_balance_ledger_created_at", "created_at"),
    )


class CheckType(enum.Enum):
    SINGLE = "single"  # Одноразовый
    MULTI = "multi"    # Мульти-чек


class CheckContentType(enum.Enum):
    STARS = "stars"      # Звёзды
    PREMIUM = "premium"  # Premium подписка


class Check(Base):
    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    creator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))

    # Тип чека (одноразовый / мульти)
    check_type: Mapped[str] = mapped_column(String(20), default="single")

    # Содержимое чека (stars / premium)
    content_type: Mapped[str] = mapped_column(String(20), default="stars")

    # Сумма звёзд (на одну активацию для мульти-чека)
    amount_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))

    # Количество месяцев Premium (3/6/12)
    amount_premium_months: Mapped[int] = mapped_column(Integer, default=0)

    # Количество активаций
    max_activations: Mapped[int] = mapped_column(Integer, default=1)
    current_activations: Mapped[int] = mapped_column(Integer, default=0)

    # Описание чека (отображается при отправке)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    photo_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Ограничения
    recipient_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Username получателя
    recipient_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # Telegram ID получателя
    password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Пароль для активации
    required_channel: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Обязательная подписка (JSON список)
    require_premium: Mapped[bool] = mapped_column(Boolean, default=False)  # Только для Telegram Premium
    require_new_user: Mapped[bool] = mapped_column(Boolean, default=False)  # Только для новых пользователей бота

    # Способ оплаты и замороженные суммы
    payment_method: Mapped[str] = mapped_column(String(20), default="balance")  # usdt / stars / premium
    frozen_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0.000000"))
    frozen_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    frozen_premium_months: Mapped[int] = mapped_column(Integer, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    activations: Mapped[list["CheckActivation"]] = relationship(
        "CheckActivation", back_populates="check"
    )

    __table_args__ = (
        Index("ix_checks_creator_id", "creator_id"),
        Index("ix_checks_created_at", "created_at"),
    )


class CheckActivation(Base):
    __tablename__ = "check_activations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    check_id: Mapped[int] = mapped_column(Integer, ForeignKey("checks.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))

    amount_received: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    check: Mapped["Check"] = relationship("Check", back_populates="activations")

    __table_args__ = (
        UniqueConstraint("check_id", "user_id", name="uq_check_user"),
    )


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    bonus_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    bonus_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    bonus_premium: Mapped[int] = mapped_column(Integer, default=0)  # months
    bonus_percent: Mapped[int] = mapped_column(Integer, default=0)  # deprecated

    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_uses: Mapped[int] = mapped_column(Integer, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    uses: Mapped[list["PromoUse"]] = relationship(
        "PromoUse", back_populates="promo_code", cascade="all, delete-orphan"
    )


class PromoUse(Base):
    __tablename__ = "promo_uses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(Integer, ForeignKey("promo_codes.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=True
    )

    bonus_applied: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    promo_code: Mapped["PromoCode"] = relationship("PromoCode", back_populates="uses")

    __table_args__ = (
        UniqueConstraint("promo_id", "user_id", name="uq_promo_user"),
    )


class ReferralEarning(Base):
    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    referee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"))

    level: Mapped[int] = mapped_column(Integer)
    percent: Mapped[int] = mapped_column(Integer)

    amount_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_referral_earnings_referrer", "referrer_id"),
        Index("ix_referral_earnings_created_at", "created_at"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    admin_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_audit_logs_user_id", "user_id"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_created_at", "created_at"),
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class BotChannel(Base):
    """Каналы, где бот является администратором."""
    __tablename__ = "bot_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    channel_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    channel_title: Mapped[str] = mapped_column(String(255))

    # ID пользователя, который добавил бота в канал
    added_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_bot_channels_added_by", "added_by_user_id"),
    )


class Giveaway(Base):
    """A scheduled or active giveaway managed by bot administrators."""

    __tablename__ = "giveaways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    photo_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # scheduled / active / drawing / completed / cancelled
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    # purchase_once / tickets_per_order / tickets_per_stars /
    # registration_all (activity during campaign) / registration_new
    participation_mode: Mapped[str] = mapped_column(String(30))
    # all / stars / premium; unused for registration modes
    product_filter: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    tickets_per_order: Mapped[int] = mapped_column(Integer, default=1)
    stars_per_ticket: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    # Legacy compatibility column. Draws now happen at ends_at without a grace delay.
    grace_minutes: Mapped[int] = mapped_column(Integer, default=0)

    publish_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    publish_announcement: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_results: Mapped[bool] = mapped_column(Boolean, default=False)
    announcement_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    results_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    announcement_last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    results_last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    announcement_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    results_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    audit_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    prizes: Mapped[list["GiveawayPrize"]] = relationship(
        "GiveawayPrize", back_populates="giveaway", cascade="all, delete-orphan"
    )
    entries: Mapped[list["GiveawayEntry"]] = relationship(
        "GiveawayEntry", back_populates="giveaway", cascade="all, delete-orphan"
    )
    winners: Mapped[list["GiveawayWinner"]] = relationship(
        "GiveawayWinner", back_populates="giveaway", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_giveaways_status_starts", "status", "starts_at"),
        Index("ix_giveaways_status_ends", "status", "ends_at"),
    )


class GiveawayPrize(Base):
    __tablename__ = "giveaway_prizes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(Integer, ForeignKey("giveaways.id", ondelete="CASCADE"))
    place: Mapped[int] = mapped_column(Integer)
    # stars / premium / custom. Payout is intentionally manual.
    prize_type: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_issued: Mapped[bool] = mapped_column(Boolean, default=False)
    issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    issued_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    giveaway: Mapped["Giveaway"] = relationship("Giveaway", back_populates="prizes")
    winner: Mapped[Optional["GiveawayWinner"]] = relationship(
        "GiveawayWinner", back_populates="prize", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("giveaway_id", "place", name="uq_giveaway_prize_place"),
    )


class GiveawayEntry(Base):
    __tablename__ = "giveaway_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(Integer, ForeignKey("giveaways.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(30))
    tickets: Mapped[int] = mapped_column(Integer, default=0)
    purchase_count: Mapped[int] = mapped_column(Integer, default=0)
    stars_purchased: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    join_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    giveaway: Mapped["Giveaway"] = relationship("Giveaway", back_populates="entries")

    __table_args__ = (
        UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_entry_user"),
        Index("ix_giveaway_entries_giveaway_tickets", "giveaway_id", "tickets"),
        Index("ix_giveaway_entries_user", "user_id"),
    )


class GiveawayEntryOrder(Base):
    __tablename__ = "giveaway_entry_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(Integer, ForeignKey("giveaways.id", ondelete="CASCADE"))
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    tickets_awarded: Mapped[int] = mapped_column(Integer, default=0)
    order_quantity: Mapped[int] = mapped_column(Integer)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("giveaway_id", "order_id", name="uq_giveaway_entry_order"),
        Index("ix_giveaway_entry_orders_order", "order_id"),
        Index("ix_giveaway_entry_orders_notify", "notified_at", "tickets_awarded"),
    )


class GiveawayWinner(Base):
    __tablename__ = "giveaway_winners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(Integer, ForeignKey("giveaways.id", ondelete="CASCADE"))
    prize_id: Mapped[int] = mapped_column(Integer, ForeignKey("giveaway_prizes.id"), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    place: Mapped[int] = mapped_column(Integer)
    tickets_snapshot: Mapped[int] = mapped_column(Integer)
    random_value: Mapped[int] = mapped_column(BigInteger)
    total_weight_before: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    giveaway: Mapped["Giveaway"] = relationship("Giveaway", back_populates="winners")
    prize: Mapped["GiveawayPrize"] = relationship("GiveawayPrize", back_populates="winner")

    __table_args__ = (
        UniqueConstraint("giveaway_id", "place", name="uq_giveaway_winner_place"),
        UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_winner_user"),
        Index("ix_giveaway_winners_user", "user_id"),
    )


class FragmentAccountStatus(enum.Enum):
    """Статус аккаунта Fragment."""
    ACTIVE = "active"           # Активен, готов к использованию
    SESSION_EXPIRED = "session_expired"  # Сессия истекла, нужна повторная авторизация
    DISABLED = "disabled"       # Отключен администратором
    LOW_BALANCE = "low_balance" # Низкий баланс TON


class FragmentAccount(Base):
    """
    Аккаунты Fragment для покупки Stars и Premium.

    Чувствительные данные (mnemonic, токены) хранятся в зашифрованном виде.
    Ключ шифрования задаётся через переменную окружения ENCRYPTION_KEY.
    """
    __tablename__ = "fragment_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Понятное имя аккаунта для администратора
    name: Mapped[str] = mapped_column(String(100))

    # TON API ключ (для взаимодействия с блокчейном)
    tonapi_key: Mapped[str] = mapped_column(Text)

    # Мнемоника кошелька (24 слова, зашифровано)
    mnemonic_encrypted: Mapped[str] = mapped_column(Text)

    # Адрес TON кошелька (вычисляется из мнемоники, для отображения)
    wallet_address: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Fragment API хэш
    fragment_hash: Mapped[str] = mapped_column(String(100))

    # Сессионные куки Fragment (без шифрования, т.к. не критичные данные)
    stel_token: Mapped[str] = mapped_column(Text)
    stel_ssid: Mapped[str] = mapped_column(Text)
    stel_ton_token: Mapped[str] = mapped_column(Text)
    stel_dt: Mapped[str] = mapped_column(String(20), default="-300")
    payment_method: Mapped[str] = mapped_column(String(20), default="ton")

    # Статус аккаунта
    status: Mapped[str] = mapped_column(String(30), default="active")

    # Активен ли аккаунт (можно ли его использовать для заказов)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Приоритет (для выбора аккаунта при распределении нагрузки)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    # Баланс TON на кошельке (обновляется периодически)
    ton_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)

    # Время последней проверки сессии
    last_session_check: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Время последней проверки баланса
    last_balance_check: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Сообщение об ошибке (если статус не active)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Статистика использования
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    successful_orders: Mapped[int] = mapped_column(Integer, default=0)
    failed_orders: Mapped[int] = mapped_column(Integer, default=0)

    # Общая сумма транзакций в TON
    total_ton_spent: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0.000000"))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_fragment_accounts_status", "status"),
        Index("ix_fragment_accounts_is_active", "is_active"),
    )
