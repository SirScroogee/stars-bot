"""
Сервис логирования в Telegram группу с топиками.

Логирует события в реальном времени в соответствующие топики группы.
"""
import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError

logger = logging.getLogger(__name__)

# Московское время (UTC+3)
MOSCOW_TZ = timezone(timedelta(hours=3))


# Маппинг топиков на ключи настроек
TOPIC_KEYS = {
    "errors": "errors",
    "payments": "payments",
    "orders": "orders",
    "checks": "checks",
    "promo": "promo",
    "referrals": "referrals",
    "admin": "admin",
    "system": "system",
    "users": "users",
}


class TelegramLogger:
    """Логгер событий в Telegram группу."""

    _instance: Optional["TelegramLogger"] = None
    _bot: Optional[Bot] = None
    _settings_cache: Optional[dict] = None
    _cache_time: Optional[datetime] = None
    _cache_ttl: int = 60  # Кэш на 60 секунд

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def set_bot(cls, bot: Bot) -> None:
        """Установить экземпляр бота для логирования."""
        cls._bot = bot

    @classmethod
    def get_bot(cls) -> Optional[Bot]:
        """Получить экземпляр бота."""
        return cls._bot

    async def _get_settings(self) -> dict:
        """Получить настройки с кэшированием."""
        now = datetime.now(MOSCOW_TZ)

        # Проверяем кэш
        if (
            self._settings_cache is not None
            and self._cache_time is not None
            and (now - self._cache_time).total_seconds() < self._cache_ttl
        ):
            return self._settings_cache

        # Загружаем настройки
        try:
            from src.services.log_settings_service import get_log_settings
            self._settings_cache = await get_log_settings()
            self._cache_time = now
            return self._settings_cache
        except Exception as e:
            logger.error(f"Failed to load log settings: {e}")
            # Возвращаем дефолтные настройки
            from src.services.log_settings_service import DEFAULT_LOG_SETTINGS
            return DEFAULT_LOG_SETTINGS

    def invalidate_cache(self):
        """Сбросить кэш настроек."""
        self._settings_cache = None
        self._cache_time = None

    async def _is_enabled(self, topic_key: str, event_key: str) -> bool:
        """Проверить, включено ли логирование для события."""
        settings = await self._get_settings()

        # Глобально выключено
        if not settings.get("enabled", True):
            return False

        # Топик выключен
        topic = settings.get("topics", {}).get(topic_key, {})
        if not topic.get("enabled", True):
            return False

        # Событие выключено
        if not settings.get("events", {}).get(event_key, True):
            return False

        return True

    async def _get_topic_id(self, topic_key: str) -> Optional[int]:
        """Получить ID топика из настроек."""
        settings = await self._get_settings()
        topic = settings.get("topics", {}).get(topic_key, {})
        return topic.get("id")

    async def _get_group_id(self) -> Optional[int]:
        """Получить ID группы из настроек."""
        settings = await self._get_settings()
        return settings.get("group_id")

    async def _send(self, topic_key: str, event_key: str, text: str) -> bool:
        """Отправить сообщение в топик."""
        if not self._bot:
            logger.warning("TelegramLogger: Bot not set")
            return False

        # Проверяем, включено ли логирование
        if not await self._is_enabled(topic_key, event_key):
            return False

        group_id = await self._get_group_id()
        topic_id = await self._get_topic_id(topic_key)

        if not group_id or not topic_id:
            return False

        for attempt in range(3):
            try:
                await self._bot.send_message(
                    chat_id=group_id,
                    message_thread_id=topic_id,
                    text=text,
                    parse_mode="HTML",
                )
                return True
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                logger.error("TelegramLogger permanent error: %s", e)
                return False
            except TelegramAPIError as e:
                if attempt == 2:
                    logger.error("TelegramLogger error after retries: %s", e)
                    return False
                retry_after = float(getattr(e, "retry_after", 0) or (attempt + 1))
                await asyncio.sleep(min(retry_after, 10.0))
            except Exception as e:
                if attempt == 2:
                    logger.error("TelegramLogger unexpected error after retries: %s", e)
                    return False
                await asyncio.sleep(attempt + 1)
        return False

    def _format_user(self, user_id: int, username: Optional[str] = None) -> str:
        """Форматировать информацию о пользователе."""
        if username:
            return f"@{html.escape(username)} (<code>{user_id}</code>)"
        return f"<code>{user_id}</code>"

    def _now(self) -> str:
        """Текущее время (Москва, UTC+3)."""
        return datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S")

    async def _format_payment_amount(
        self,
        amount_usdt: Decimal,
        amount_rub: Decimal | None = None,
    ) -> str:
        if amount_rub is not None:
            return f"{amount_usdt:,.2f} USDT ({amount_rub:,.2f} RUB)"

        try:
            from src.services.rub_rate_service import format_usdt_with_rub

            return await format_usdt_with_rub(amount_usdt)
        except Exception as exc:
            logger.warning("Could not add RUB conversion to payment log: %s", exc)
            return f"{amount_usdt:,.2f} USDT"

    # ==================== ОШИБКИ ====================

    async def log_error(
        self,
        error_type: str,
        error_message: str,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        """Логировать ошибку."""
        text = f"<b>ERROR</b> | {self._now()}\n\n"
        text += f"<b>Тип:</b> {html.escape(str(error_type))}\n"
        text += f"<b>Ошибка:</b> <code>{html.escape(str(error_message))}</code>\n"

        if user_id:
            text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"

        if details:
            text += f"\n<b>Детали:</b>\n<pre>{html.escape(str(details)[:500])}</pre>"

        await self._send("errors", "error", text)

    async def log_order_error(
        self,
        order_id: int,
        error_message: str,
        user_id: int,
        username: Optional[str] = None,
    ) -> None:
        """Логировать ошибку заказа."""
        text = f"<b>ORDER ERROR</b> | {self._now()}\n\n"
        text += f"<b>Заказ:</b> #{order_id}\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Ошибка:</b> <code>{html.escape(str(error_message))}</code>"

        await self._send("errors", "order_error", text)

    async def log_order_attention(
        self,
        order_id: int,
        user_id: int,
        username: Optional[str],
        reason_code: str,
        reason: str,
        age_minutes: int,
        *,
        critical: bool = False,
    ) -> bool:
        """Notify the log topic and every admin about a delayed paid order."""
        if reason_code == "insufficient_funds":
            action = "Срочно пополните баланс рабочего кошелька Fragment."
        elif reason_code == "no_fragment_account":
            action = "Нет доступного Fragment-аккаунта. Проверьте аккаунты в админ-панели."
        elif reason_code == "session_expired":
            action = "Обновите сессию Fragment или активируйте другой аккаунт."
        elif reason_code == "access_denied":
            action = "Fragment отклонил операцию. Проверьте аккаунт и его сессию."
        elif reason_code in {"processing_timeout", "worker_exception"}:
            action = "Проверьте заказ вручную перед повторным запуском."
        else:
            action = "Проверьте состояние заказа и Fragment-сервиса."

        title = "ORDER CRITICAL" if critical else "ORDER DELAYED"
        text = (
            f"<b>{title}</b> | {self._now()}\n\n"
            f"<b>Заказ:</b> #{order_id}\n"
            f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
            f"<b>Ожидает:</b> {max(0, age_minutes)} мин.\n"
            f"<b>Код:</b> <code>{html.escape(reason_code or 'delayed')}</code>\n"
            f"<b>Причина:</b> <code>{html.escape(reason or 'Заказ выполняется дольше ожидаемого')}</code>\n\n"
            f"⚠️ {html.escape(action)}"
        )

        event_key = "order_critical" if critical else "order_delayed"
        topic_delivered = await self._send("errors", event_key, text)
        admin_delivered = await self._notify_admins(text)
        return topic_delivered or admin_delivered

    async def log_fragment_session_expired(
        self,
        account_id: int,
        account_name: str,
        error_message: str,
    ) -> None:
        """Уведомить об истекшей сессии Fragment аккаунта."""
        safe_name = html.escape(str(account_name))
        safe_error = html.escape(str(error_message))
        text = (
            f"<b>FRAGMENT SESSION EXPIRED</b> | {self._now()}\n\n"
            f"<b>Аккаунт:</b> {safe_name} (<code>{account_id}</code>)\n"
            f"<b>Ошибка:</b> <code>{safe_error}</code>\n\n"
            "⚠️ Сессия Fragment истекла. Нужно обновить cookies/tokens аккаунта в админке."
        )

        await self._send("errors", "fragment_session_expired", text)
        await self._notify_admins(text)

    async def _notify_admins(self, text: str) -> bool:
        """Отправить важное уведомление всем админам из базы."""
        if not self._bot:
            return False

        try:
            from sqlalchemy import select
            from src.db.models import User
            from src.db.session import async_session_factory

            async with async_session_factory() as session:
                result = await session.execute(
                    select(User.id)
                    .where(User.is_admin == True)
                    .where(User.is_banned == False)
                )
                admin_ids = [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.error(f"Failed to load admins for notification: {e}")
            return False

        delivered = False
        for admin_id in admin_ids:
            try:
                await self._bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                )
                delivered = True
            except TelegramAPIError as e:
                logger.warning(f"Failed to notify admin {admin_id}: {e}")
            except Exception as e:
                logger.warning(f"Unexpected notify error for admin {admin_id}: {e}")
        return delivered

    # ==================== ОПЛАТЫ ====================

    async def log_payment_created(
        self,
        user_id: int,
        username: Optional[str],
        amount: Decimal,
        currency: str,
        provider: str,
    ) -> None:
        """Логировать создание платежа."""
        text = f"<b>PAYMENT CREATED</b> | {self._now()}\n\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Сумма:</b> {amount} {currency}\n"
        text += f"<b>Провайдер:</b> {provider}"

        await self._send("payments", "payment_created", text)

    async def log_payment_completed(
        self,
        order_id: int,
        user_id: int,
        username: Optional[str],
        amount_usdt: Decimal,
        provider: str,
        product_type: str,
        quantity: int,
        recipient: str,
        amount_rub: Decimal | None = None,
        provider_amount: str | None = None,
    ) -> None:
        """Log a confirmed monetary payment for an order."""
        provider_names = {
            "cryptobot": "CryptoBot",
            "ton": "TON",
            "platega": "СБП (Platega)",
            "lava": "Lava / СБП",
            "balance": "Внутренний баланс USDT",
        }
        amount_text = await self._format_payment_amount(amount_usdt, amount_rub)

        if product_type == "stars":
            purpose = f"Покупка {quantity:,} Telegram Stars"
        else:
            purpose = f"Покупка Telegram Premium на {quantity} мес."

        text = "<b>УСПЕШНАЯ ОПЛАТА</b>\n\n"
        text += f"<b>Время (МСК):</b> {self._now()}\n"
        text += f"<b>Оплата:</b> {purpose}\n"
        text += f"<b>Заказ:</b> #{order_id}\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Получатель:</b> @{html.escape(recipient.lstrip('@'))}\n"
        text += f"<b>Сумма:</b> {amount_text}\n"
        if provider_amount:
            text += f"<b>Сумма в валюте провайдера:</b> {html.escape(provider_amount)}\n"
        text += f"<b>Способ оплаты:</b> {provider_names.get(provider, html.escape(provider))}"

        await self._send("payments", "payment_completed", text)

    async def log_deposit(
        self,
        user_id: int,
        username: Optional[str],
        amount: Decimal,
        currency: str,
        provider: str | None = None,
        amount_rub: Decimal | None = None,
        provider_amount: str | None = None,
        paid_amount_usdt: Decimal | None = None,
    ) -> None:
        """Логировать пополнение баланса."""
        payment_amount = paid_amount_usdt if paid_amount_usdt is not None else amount
        amount_text = await self._format_payment_amount(payment_amount, amount_rub)
        text = "<b>УСПЕШНАЯ ОПЛАТА</b>\n\n"
        text += f"<b>Время (МСК):</b> {self._now()}\n"
        text += "<b>Оплата:</b> Пополнение внутреннего баланса\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Сумма:</b> {amount_text}\n"
        if payment_amount != amount:
            text += f"<b>Зачислено на баланс:</b> +{amount:,.2f} USDT\n"
        if provider_amount:
            text += f"<b>Сумма в валюте провайдера:</b> {html.escape(provider_amount)}\n"
        text += f"<b>Способ оплаты:</b> {html.escape(provider or currency)}"

        await self._send("payments", "deposit", text)

    # ==================== ЗАКАЗЫ ====================

    async def log_order_created(
        self,
        order_id: int,
        user_id: int,
        username: Optional[str],
        product_type: str,
        quantity: int,
        price_usdt: Decimal,
        recipient: str,
    ) -> None:
        """Логировать создание заказа."""
        product = "Stars" if product_type == "stars" else f"Premium {quantity}м"
        qty = f"{quantity} шт" if product_type == "stars" else ""

        text = f"<b>ORDER CREATED</b> | {self._now()}\n\n"
        text += f"<b>Заказ:</b> #{order_id}\n"
        text += f"<b>Покупатель:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Товар:</b> {product} {qty}\n"
        text += f"<b>Получатель:</b> @{recipient}\n"
        text += f"<b>Сумма:</b> ${price_usdt}"

        await self._send("orders", "order_created", text)

    async def log_order_completed(
        self,
        order_id: int,
        user_id: int,
        username: Optional[str],
        product_type: str,
        quantity: int,
        recipient: str,
    ) -> None:
        """Логировать выполнение заказа."""
        product = "Stars" if product_type == "stars" else f"Premium {quantity}м"
        qty = f"{quantity} шт" if product_type == "stars" else ""

        text = f"<b>ORDER COMPLETED</b> | {self._now()}\n\n"
        text += f"<b>Заказ:</b> #{order_id}\n"
        text += f"<b>Покупатель:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Товар:</b> {product} {qty}\n"
        text += f"<b>Получатель:</b> @{recipient}"

        await self._send("orders", "order_completed", text)

    async def log_order_failed(
        self,
        order_id: int,
        user_id: int,
        username: Optional[str],
        reason: str,
    ) -> None:
        """Логировать неудачный заказ."""
        text = f"<b>ORDER FAILED</b> | {self._now()}\n\n"
        text += f"<b>Заказ:</b> #{order_id}\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Причина:</b> {html.escape(str(reason))}"

        await self._send("orders", "order_failed", text)

    async def log_order_cancelled(
        self,
        order_id: int,
        user_id: int,
        username: Optional[str],
        reason: str = "Отменён пользователем",
    ) -> None:
        """Логировать отмену заказа."""
        text = f"<b>ORDER CANCELLED</b> | {self._now()}\n\n"
        text += f"<b>Заказ:</b> #{order_id}\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Причина:</b> {reason}"

        await self._send("orders", "order_cancelled", text)

    # ==================== ЧЕКИ ====================

    async def log_check_created(
        self,
        check_code: str,
        creator_id: int,
        creator_username: Optional[str],
        amount_stars: Decimal,
        max_activations: int,
    ) -> None:
        """Логировать создание чека."""
        text = f"<b>CHECK CREATED</b> | {self._now()}\n\n"
        text += f"<b>Код:</b> <code>{check_code}</code>\n"
        text += f"<b>Создатель:</b> {self._format_user(creator_id, creator_username)}\n"
        text += f"<b>Сумма:</b> {amount_stars} Stars\n"
        text += f"<b>Активаций:</b> {max_activations}"

        await self._send("checks", "check_created", text)

    async def log_check_activated(
        self,
        check_code: str,
        user_id: int,
        username: Optional[str],
        amount_received: Decimal,
        activation_number: int,
        max_activations: int,
    ) -> None:
        """Логировать активацию чека."""
        text = f"<b>CHECK ACTIVATED</b> | {self._now()}\n\n"
        text += f"<b>Код:</b> <code>{check_code}</code>\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Получено:</b> +{amount_received} Stars\n"
        text += f"<b>Активация:</b> {activation_number}/{max_activations}"

        await self._send("checks", "check_activated", text)

    # ==================== ПРОМОКОДЫ ====================

    async def log_promo_created(
        self,
        promo_code: str,
        bonus_stars: Decimal = Decimal("0"),
        bonus_usdt: Decimal = Decimal("0"),
        bonus_premium: int = 0,
        max_uses: Optional[int] = None,
    ) -> None:
        """Логировать создание промокода."""
        uses = str(max_uses) if max_uses else "∞"

        # Определяем текст бонуса
        if bonus_stars > 0:
            bonus_text = f"+{int(bonus_stars)} ⭐ Stars"
        elif bonus_usdt > 0:
            bonus_text = f"+{bonus_usdt} 💵 USDT"
        elif bonus_premium > 0:
            bonus_text = f"+{bonus_premium} 👑 мес. Premium"
        else:
            bonus_text = "без бонуса"

        text = f"<b>PROMO CREATED</b> | {self._now()}\n\n"
        text += f"<b>Код:</b> <code>{promo_code}</code>\n"
        text += f"<b>Бонус:</b> {bonus_text}\n"
        text += f"<b>Использований:</b> {uses}"

        await self._send("promo", "promo_created", text)

    async def log_promo_used(
        self,
        promo_code: str,
        user_id: int,
        username: Optional[str],
        bonus_applied: Decimal,
        use_number: int,
        max_uses: Optional[int],
    ) -> None:
        """Логировать использование промокода."""
        uses = f"{use_number}/{max_uses}" if max_uses else f"{use_number}/∞"

        text = f"<b>PROMO USED</b> | {self._now()}\n\n"
        text += f"<b>Код:</b> <code>{promo_code}</code>\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Бонус:</b> +{bonus_applied} Stars\n"
        text += f"<b>Использование:</b> {uses}"

        await self._send("promo", "promo_used", text)

    # ==================== РЕФЕРАЛЫ ====================

    async def log_referral_joined(
        self,
        user_id: int,
        username: Optional[str],
        referrer_id: int,
        referrer_username: Optional[str],
    ) -> None:
        """Логировать регистрацию по реферальной ссылке."""
        text = f"<b>REFERRAL JOINED</b> | {self._now()}\n\n"
        text += f"<b>Новый пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Пригласил:</b> {self._format_user(referrer_id, referrer_username)}"

        await self._send("referrals", "referral_joined", text)

    async def log_referral_earning(
        self,
        referrer_id: int,
        referrer_username: Optional[str],
        referee_id: int,
        referee_username: Optional[str],
        level: int,
        amount_stars: Decimal,
        order_id: int,
    ) -> None:
        """Логировать реферальное начисление."""
        text = f"<b>REFERRAL EARNING</b> | {self._now()}\n\n"
        text += f"<b>Реферер:</b> {self._format_user(referrer_id, referrer_username)}\n"
        text += f"<b>От кого:</b> {self._format_user(referee_id, referee_username)}\n"
        text += f"<b>Уровень:</b> {level}\n"
        text += f"<b>Начислено:</b> +{amount_stars} Stars\n"
        text += f"<b>Заказ:</b> #{order_id}"

        await self._send("referrals", "referral_earning", text)

    # ==================== АДМИН ====================

    async def log_admin_login(
        self,
        admin_id: int,
        admin_username: Optional[str],
    ) -> None:
        """Логировать вход в админку."""
        text = f"<b>ADMIN LOGIN</b> | {self._now()}\n\n"
        text += f"<b>Админ:</b> {self._format_user(admin_id, admin_username)}"

        await self._send("admin", "admin_login", text)

    async def log_admin_action(
        self,
        admin_id: int,
        admin_username: Optional[str],
        action: str,
        details: Optional[str] = None,
    ) -> None:
        """Логировать действие админа."""
        text = f"<b>ADMIN ACTION</b> | {self._now()}\n\n"
        text += f"<b>Админ:</b> {self._format_user(admin_id, admin_username)}\n"
        text += f"<b>Действие:</b> {action}"

        if details:
            text += f"\n<b>Детали:</b> {details}"

        await self._send("admin", "admin_action", text)

    async def log_user_banned(
        self,
        admin_id: int,
        admin_username: Optional[str],
        user_id: int,
        user_username: Optional[str],
        reason: Optional[str] = None,
    ) -> None:
        """Логировать бан пользователя."""
        text = f"<b>USER BANNED</b> | {self._now()}\n\n"
        text += f"<b>Админ:</b> {self._format_user(admin_id, admin_username)}\n"
        text += f"<b>Забанен:</b> {self._format_user(user_id, user_username)}"

        if reason:
            text += f"\n<b>Причина:</b> {reason}"

        await self._send("admin", "user_banned", text)

    # ==================== СИСТЕМА ====================

    async def log_bot_started(self) -> None:
        """Логировать запуск бота."""
        text = f"<b>BOT STARTED</b> | {self._now()}\n\n"
        text += "Бот успешно запущен и готов к работе."

        await self._send("system", "bot_started", text)

    async def log_bot_stopped(self) -> None:
        """Логировать остановку бота."""
        text = f"<b>BOT STOPPED</b> | {self._now()}\n\n"
        text += "Бот остановлен."

        await self._send("system", "bot_stopped", text)

    async def log_system_event(
        self,
        event: str,
        details: Optional[str] = None,
    ) -> None:
        """Логировать системное событие."""
        text = f"<b>SYSTEM</b> | {self._now()}\n\n"
        text += f"<b>Событие:</b> {event}"

        if details:
            text += f"\n<b>Детали:</b> {details}"

        await self._send("system", "system_event", text)

    async def log_database_event(
        self,
        event: str,
        details: Optional[str] = None,
    ) -> None:
        """Логировать событие БД."""
        text = f"<b>DATABASE</b> | {self._now()}\n\n"
        text += f"<b>Событие:</b> {event}"

        if details:
            text += f"\n{details}"

        await self._send("system", "database_event", text)

    # ==================== ПОЛЬЗОВАТЕЛИ ====================

    async def log_user_registered(
        self,
        user_id: int,
        username: Optional[str],
        language: str,
        referrer_code: Optional[str] = None,
    ) -> bool:
        """Логировать регистрацию пользователя."""
        text = f"<b>USER REGISTERED</b> | {self._now()}\n\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Язык:</b> {html.escape(language)}"

        if referrer_code:
            text += f"\n<b>Реф. код:</b> <code>{html.escape(referrer_code)}</code>"

        return await self._send("users", "user_registered", text)

    async def log_user_started(
        self,
        user_id: int,
        username: Optional[str],
    ) -> None:
        """Логировать /start от существующего пользователя."""
        text = f"<b>USER START</b> | {self._now()}\n\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}"

        await self._send("users", "user_started", text)

    async def log_balance_change(
        self,
        user_id: int,
        username: Optional[str],
        currency: str,
        old_balance: Decimal,
        new_balance: Decimal,
        reason: str,
    ) -> None:
        """Логировать изменение баланса."""
        diff = new_balance - old_balance
        sign = "+" if diff > 0 else ""

        text = f"<b>BALANCE CHANGE</b> | {self._now()}\n\n"
        text += f"<b>Пользователь:</b> {self._format_user(user_id, username)}\n"
        text += f"<b>Валюта:</b> {currency}\n"
        text += f"<b>Было:</b> {old_balance}\n"
        text += f"<b>Стало:</b> {new_balance} ({sign}{diff})\n"
        text += f"<b>Причина:</b> {reason}"

        await self._send("users", "balance_change", text)


# Глобальный экземпляр логгера
tg_logger = TelegramLogger()
