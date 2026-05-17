"""
Конфигурация для Fragment API клиента.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.services.fragment_account_service import FragmentAccountData


@dataclass
class FragmentConfig:
    """Конфигурация Fragment API."""

    tonapi_key: str
    mnemonic: list[str]
    fragment_hash: str
    stel_token: str
    stel_ssid: str
    stel_ton_token: str

    # ID аккаунта в БД (для обновления статистики)
    account_id: int | None = None

    # Настройки HTTP
    request_timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 1.0  # базовая задержка между retry (секунды)

    # Настройки транзакций
    tx_timeout: float = 60.0  # таймаут ожидания подтверждения транзакции

    # Fragment API
    fragment_url: str = "https://fragment.com/api"
    user_agent: str = "Mozilla/5.0"

    # ===== КЭШИРОВАНИЕ =====
    recipient_cache_ttl: int = 300  # 5 минут - TTL кэша получателей

    # ===== CONNECTION POOLING =====
    connector_limit: int = 10  # Размер пула соединений
    connector_limit_per_host: int = 5  # Лимит на хост
    connector_keepalive_timeout: int = 30  # Keepalive таймаут

    # ===== CIRCUIT BREAKER =====
    circuit_breaker_failure_threshold: int = 5  # Порог ошибок для открытия
    circuit_breaker_recovery_timeout: float = 60.0  # Время восстановления (сек)

    # ===== RETRY С JITTER =====
    retry_jitter_factor: float = 0.1  # 10% случайности к задержке

    @property
    def cookies(self) -> dict[str, str]:
        return {
            "stel_token": self.stel_token,
            "stel_ssid": self.stel_ssid,
            "stel_ton_token": self.stel_ton_token,
        }

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }

    @classmethod
    def from_account_data(cls, account: FragmentAccountData) -> "FragmentConfig":
        """Создать конфигурацию из данных аккаунта БД."""
        return cls(
            tonapi_key=account.tonapi_key,
            mnemonic=account.mnemonic,
            fragment_hash=account.fragment_hash,
            stel_token=account.stel_token,
            stel_ssid=account.stel_ssid,
            stel_ton_token=account.stel_ton_token,
            account_id=account.id,
        )

    @classmethod
    def from_env(cls) -> "FragmentConfig":
        """
        Создать конфигурацию из переменных окружения.

        DEPRECATED: Используйте from_account_data() с аккаунтом из БД.
        Этот метод оставлен для обратной совместимости и миграции.
        """
        tonapi_key = os.getenv("TONAPI_KEY")
        if not tonapi_key:
            raise ValueError("TONAPI_KEY is required")

        mnemonic_str = os.getenv("MNEMONIC")
        if not mnemonic_str:
            raise ValueError("MNEMONIC is required")
        mnemonic = mnemonic_str.split()
        if len(mnemonic) != 24:
            raise ValueError("MNEMONIC must contain 24 words")

        fragment_hash = os.getenv("FRAGMENT_HASH")
        if not fragment_hash:
            raise ValueError("FRAGMENT_HASH is required")

        stel_token = os.getenv("STEL_TOKEN")
        if not stel_token:
            raise ValueError("STEL_TOKEN is required")

        stel_ssid = os.getenv("STEL_SSID")
        if not stel_ssid:
            raise ValueError("STEL_SSID is required")

        stel_ton_token = os.getenv("STEL_TON_TOKEN")
        if not stel_ton_token:
            raise ValueError("STEL_TON_TOKEN is required")

        return cls(
            tonapi_key=tonapi_key,
            mnemonic=mnemonic,
            fragment_hash=fragment_hash,
            stel_token=stel_token,
            stel_ssid=stel_ssid,
            stel_ton_token=stel_ton_token,
            account_id=None,  # Нет связи с БД
        )
