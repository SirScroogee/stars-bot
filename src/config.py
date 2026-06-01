"""
Центральная конфигурация приложения.

Все переменные окружения загружаются здесь и валидируются при старте.
"""
import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    """Основная конфигурация бота."""

    bot_token: str
    database_url: str

    # Redis (опционально - fallback на InMemoryQueue если не задан)
    redis_url: str | None = None

    # Опциональные настройки
    admin_ids: list[int] | None = None
    debug: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token:
            raise ValueError("BOT_TOKEN is required")

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL is required")

        # Парсим список админов
        admin_ids_str = os.getenv("ADMIN_IDS", "")
        admin_ids = None
        if admin_ids_str:
            admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()]

        debug = os.getenv("DEBUG", "").lower() in ("true", "1", "yes")

        # Redis URL (опционально)
        redis_url = os.getenv("REDIS_URL")

        return cls(
            bot_token=bot_token,
            database_url=database_url,
            redis_url=redis_url,
            admin_ids=admin_ids,
            debug=debug,
        )


@dataclass
class FragmentConfigData:
    """Конфигурация Fragment API (данные)."""

    tonapi_key: str
    mnemonic: list[str]
    fragment_hash: str
    stel_token: str
    stel_ssid: str
    stel_ton_token: str
    payment_method: str = "ton"

    @classmethod
    def from_env(cls) -> "FragmentConfigData":
        tonapi_key = os.getenv("TONAPI_KEY")
        if not tonapi_key:
            raise ValueError("TONAPI_KEY is required")

        mnemonic_str = os.getenv("MNEMONIC")
        if not mnemonic_str:
            raise ValueError("MNEMONIC is required")
        mnemonic = mnemonic_str.split()
        if len(mnemonic) != 24:
            raise ValueError("MNEMONIC must contain 24 words")

        fragment_hash = os.getenv("FRAGMENT_HASH", "")

        stel_token = os.getenv("STEL_TOKEN")
        if not stel_token:
            raise ValueError("STEL_TOKEN is required")

        stel_ssid = os.getenv("STEL_SSID")
        if not stel_ssid:
            raise ValueError("STEL_SSID is required")

        stel_ton_token = os.getenv("STEL_TON_TOKEN")
        if not stel_ton_token:
            raise ValueError("STEL_TON_TOKEN is required")
        payment_method = os.getenv("FRAGMENT_PAYMENT_METHOD", "ton")

        return cls(
            tonapi_key=tonapi_key,
            mnemonic=mnemonic,
            fragment_hash=fragment_hash,
            stel_token=stel_token,
            stel_ssid=stel_ssid,
            stel_ton_token=stel_ton_token,
            payment_method=payment_method,
        )


@lru_cache()
def get_config() -> Config:
    """Получить основную конфигурацию (кэшированную)."""
    return Config.from_env()


@lru_cache()
def get_fragment_config_data() -> FragmentConfigData:
    """Получить конфигурацию Fragment (кэшированную)."""
    return FragmentConfigData.from_env()


def get_fragment_config():
    """
    Получить FragmentConfig для использования в FragmentClient.

    Возвращает объект из src.api_clients.fragment.config
    """
    from src.api_clients.fragment.config import FragmentConfig

    data = get_fragment_config_data()
    return FragmentConfig(
        tonapi_key=data.tonapi_key,
        mnemonic=data.mnemonic,
        fragment_hash=data.fragment_hash,
        stel_token=data.stel_token,
        stel_ssid=data.stel_ssid,
        stel_ton_token=data.stel_ton_token,
        payment_method=data.payment_method,
    )


# Обратная совместимость
config = get_config()
