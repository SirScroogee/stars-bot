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
    REFUNDED = "refunded"


class PaymentProvider(enum.Enum):
    BALANCE = "balance"
    TON = "ton"
    CRYPTOBOT = "cryptobot"


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

    __table_args__ = (
        Index("ix_orders_user_status", "user_id", "status"),
        Index("ix_orders_created_at", "created_at"),
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


