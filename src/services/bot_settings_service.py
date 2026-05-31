"""
Сервис для управления настройками бота (цены, проценты и т.д.).

Настройки кэшируются на 1 час для производительности.
При изменении через админ-панель кэш сбрасывается.
Чувствительные данные (токены, адреса кошельков) хранятся в зашифрованном виде.
"""
import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Setting
from src.core.crypto import encrypt, decrypt, EncryptionError

logger = logging.getLogger(__name__)

# Ключи настроек, которые шифруются при сохранении
ENCRYPTED_SETTINGS = {"cryptobot_token", "ton_wallet_address"}

# Ключ настроек в таблице settings
BOT_SETTINGS_KEY = "bot_settings"

# Время жизни кэша в секундах (1 час)
CACHE_TTL = 3600

DEFAULT_BOT_SETTINGS = {
    "star_price_usdt": "0.02",
    "star_cost_usdt": "0.015",
    "min_stars": 50,
    "max_stars": 10000,
    "premium_price_3m": "8.99",
    "premium_price_6m": "15.99",
    "premium_price_12m": "28.99",
    "premium_cost_3m": "6.00",
    "premium_cost_6m": "10.00",
    "premium_cost_12m": "18.00",
    "referral_percent_level1": "5",
    "referral_percent_level2": "3",
    "referral_percent_level3": "1",
    "min_withdrawal_usdt": "1.00",
    "min_referral_withdrawal": "0.50",
    "payment_fee_cryptobot": "3",
    "payment_fee_ton": "0",
    "cryptobot_token": "",
    "ton_wallet_address": "",
    "support_username": "support",
    "news_channel_url": "",
    "menu_media": {},
}


class BotSettingsService:
    """Сервис для работы с настройками бота."""

    def __init__(self, session: AsyncSession, admin_id: int | None = None):
        self._session = session
        self._admin_id = admin_id  # Для аудит-логирования

    def _decrypt_settings(self, settings: dict) -> dict:
        """Расшифровать чувствительные настройки."""
        result = settings.copy()
        for key in ENCRYPTED_SETTINGS:
            if key in result and result[key]:
                try:
                    result[key] = decrypt(result[key])
                except EncryptionError:
                    # Если расшифровка не удалась, значит это старое незашифрованное значение
                    # Оставляем как есть (для миграции)
                    logger.debug(f"Setting {key} is not encrypted, using as-is")
        return result

    def _encrypt_settings(self, settings: dict) -> dict:
        """Зашифровать чувствительные настройки перед сохранением."""
        result = settings.copy()
        for key in ENCRYPTED_SETTINGS:
            if key in result and result[key]:
                try:
                    # Проверяем, не зашифровано ли уже
                    try:
                        decrypt(result[key])
                        # Если расшифровка удалась, значит уже зашифровано
                    except EncryptionError:
                        # Не зашифровано - шифруем
                        result[key] = encrypt(result[key])
                except Exception as e:
                    logger.error(f"Failed to encrypt setting {key}: {e}")
        return result

    async def get_settings(self) -> dict:
        """Получить все настройки бота из БД (с расшифровкой)."""
        result = await self._session.execute(
            select(Setting).where(Setting.key == BOT_SETTINGS_KEY)
        )
        setting = result.scalar_one_or_none()

        if not setting:
            raise ValueError("Настройки бота не найдены в базе данных. Создайте настройки через админ-панель.")

        try:
            settings = json.loads(setting.value)
            settings = {**DEFAULT_BOT_SETTINGS, **settings}
            return self._decrypt_settings(settings)
        except json.JSONDecodeError as e:
            raise ValueError(f"Ошибка парсинга настроек: {e}")

    async def save_settings(self, settings: dict) -> bool:
        """Сохранить настройки бота (с шифрованием)."""
        try:
            result = await self._session.execute(
                select(Setting).where(Setting.key == BOT_SETTINGS_KEY)
            )
            setting = result.scalar_one_or_none()

            # Шифруем чувствительные данные перед сохранением
            encrypted_settings = self._encrypt_settings(settings)
            json_value = json.dumps(encrypted_settings, ensure_ascii=False)

            if setting:
                setting.value = json_value
            else:
                new_setting = Setting(
                    key=BOT_SETTINGS_KEY,
                    value=json_value,
                    description="Настройки бота (цены, проценты и т.д.)",
                )
                self._session.add(new_setting)

            await self._session.flush()
            return True

        except Exception as e:
            logger.error(f"Failed to save bot settings: {e}")
            return False

    async def get_setting(self, key: str) -> str | None:
        """Получить конкретную настройку."""
        settings = await self.get_settings()
        return settings.get(key)

    async def set_setting(self, key: str, value: str, old_value: str | None = None) -> bool:
        """Установить конкретную настройку с аудит-логированием."""
        settings = await self.get_settings()

        # Запоминаем старое значение для лога если не передано
        if old_value is None:
            old_value = settings.get(key, "")

        settings[key] = value
        success = await self.save_settings(settings)

        # Аудит-лог изменения настройки
        if success and self._admin_id:
            # Маскируем чувствительные данные в логе
            if key in ENCRYPTED_SETTINGS:
                old_display = "***" if old_value else "(пусто)"
                new_display = "***" if value else "(пусто)"
            else:
                old_display = old_value or "(пусто)"
                new_display = value

            logger.info(
                f"[AUDIT] Admin {self._admin_id} changed setting '{key}': "
                f"'{old_display}' -> '{new_display}'"
            )

        return success

    async def ensure_default_settings(self) -> dict:
        """Создать настройки по умолчанию если их нет."""
        result = await self._session.execute(
            select(Setting).where(Setting.key == BOT_SETTINGS_KEY)
        )
        setting = result.scalar_one_or_none()

        if setting:
            settings = json.loads(setting.value)
            settings = {**DEFAULT_BOT_SETTINGS, **settings}
            return self._decrypt_settings(settings)

        default_settings = DEFAULT_BOT_SETTINGS.copy()

        new_setting = Setting(
            key=BOT_SETTINGS_KEY,
            value=json.dumps(default_settings, ensure_ascii=False),
            description="Настройки бота (цены, проценты и т.д.)",
        )
        self._session.add(new_setting)
        await self._session.flush()

        logger.info("Created default bot settings")
        return default_settings


# Кэшированные настройки с временной меткой
_cached_settings: Optional[dict] = None
_cache_timestamp: float = 0
_cache_lock: asyncio.Lock | None = None


def _get_cache_lock() -> asyncio.Lock:
    """Получить или создать asyncio.Lock для кэша."""
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


async def get_bot_settings() -> dict:
    """
    Получить настройки бота (с кэшированием на 1 час).

    При первом запросе или после истечения кэша загружает из БД.
    Если БД недоступна и кэш есть - использует кэш с предупреждением.
    Потокобезопасно благодаря asyncio.Lock.
    """
    global _cached_settings, _cache_timestamp

    current_time = time.time()

    # Быстрая проверка кэша без блокировки
    if _cached_settings is not None and (current_time - _cache_timestamp) < CACHE_TTL:
        return _cached_settings.copy()  # Возвращаем копию для безопасности

    # Получаем блокировку для обновления кэша
    async with _get_cache_lock():
        # Перепроверяем после получения блокировки (double-check locking)
        current_time = time.time()
        if _cached_settings is not None and (current_time - _cache_timestamp) < CACHE_TTL:
            return _cached_settings.copy()

        # Пытаемся загрузить из БД
        try:
            from src.db.session import async_session_factory

            async with async_session_factory() as session:
                service = BotSettingsService(session)
                # Создаём настройки по умолчанию если их нет
                _cached_settings = await service.ensure_default_settings()
                _cache_timestamp = current_time
                logger.debug(f"Bot settings loaded from DB, cached for {CACHE_TTL}s")
                return _cached_settings.copy()

        except Exception as e:
            # Если есть старый кэш - используем его
            if _cached_settings is not None:
                logger.warning(f"Failed to refresh settings from DB, using cached values: {e}")
                return _cached_settings.copy()
            # Если кэша нет - пробрасываем ошибку
            logger.error(f"Failed to load bot settings and no cache available: {e}")
            raise


def invalidate_bot_settings_cache():
    """Сбросить кэш настроек (вызывается при изменении через админку)."""
    global _cache_timestamp
    _cache_timestamp = 0
    logger.debug("Bot settings cache invalidated")


# === Глобальные функции для удобного доступа к настройкам ===

async def get_star_price() -> Decimal:
    """Получить цену 1 звезды."""
    settings = await get_bot_settings()
    return Decimal(settings["star_price_usdt"])


async def get_min_stars() -> int:
    """Получить минимум звёзд."""
    settings = await get_bot_settings()
    return int(settings["min_stars"])


async def get_max_stars() -> int:
    """Получить максимум звёзд."""
    settings = await get_bot_settings()
    return int(settings["max_stars"])


async def get_premium_prices() -> dict[int, Decimal]:
    """Получить цены Premium."""
    settings = await get_bot_settings()
    return {
        3: Decimal(settings["premium_price_3m"]),
        6: Decimal(settings["premium_price_6m"]),
        12: Decimal(settings["premium_price_12m"]),
    }


async def get_referral_percents() -> dict[int, Decimal]:
    """Получить реферальные проценты."""
    settings = await get_bot_settings()
    return {
        1: Decimal(settings["referral_percent_level1"]),
        2: Decimal(settings["referral_percent_level2"]),
        3: Decimal(settings["referral_percent_level3"]),
    }


async def get_star_cost() -> Decimal:
    """Получить себестоимость 1 звезды."""
    settings = await get_bot_settings()
    return Decimal(settings.get("star_cost_usdt", "0.015"))


async def get_premium_costs() -> dict[int, Decimal]:
    """Получить себестоимость Premium."""
    settings = await get_bot_settings()
    return {
        3: Decimal(settings.get("premium_cost_3m", "6.00")),
        6: Decimal(settings.get("premium_cost_6m", "10.00")),
        12: Decimal(settings.get("premium_cost_12m", "18.00")),
    }


# === Способы оплаты ===

async def get_cryptobot_token() -> str | None:
    from src.core.crypto import decrypt
    settings = await get_bot_settings()
    token = settings.get("cryptobot_token", "")
    if not token:
        return None
    try:
        return decrypt(token)
    except Exception:
        return token  # если не зашифрован — вернуть как есть


async def get_cryptobot_fee() -> Decimal:
    """Получить комиссию CryptoBot (в виде множителя, например 0.03 для 3%)."""
    settings = await get_bot_settings()
    fee_percent = Decimal(settings.get("payment_fee_cryptobot", "3"))
    return fee_percent / Decimal("100")


async def get_ton_wallet_address() -> str | None:
    """Получить адрес TON кошелька из БД."""
    settings = await get_bot_settings()
    wallet = settings.get("ton_wallet_address", "")
    return wallet if wallet else None


async def get_ton_fee() -> Decimal:
    """Получить комиссию TON (в виде множителя, например 0.03 для 3%)."""
    settings = await get_bot_settings()
    fee_percent = Decimal(settings.get("payment_fee_ton", "0"))
    return fee_percent / Decimal("100")
