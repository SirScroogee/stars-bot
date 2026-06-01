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
    stel_dt: str = "-300"
    payment_method: str = "ton"

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
    fragment_base_url: str = "https://fragment.com"
    stars_page_url: str = "https://fragment.com/stars/buy"
    premium_page_url: str = "https://fragment.com/premium/gift"
    user_agent: str = (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"
    )

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
            "stel_dt": self.stel_dt,
        }

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "ru,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://fragment.com",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "Sec-Ch-Ua-Mobile": "?1",
            "Sec-Ch-Ua-Platform": '"Android"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
        }

    @property
    def tonkeeper_device(self) -> dict:
        return {
            "platform": "iphone",
            "appName": "Tonkeeper",
            "appVersion": "26.04.0",
            "maxProtocolVersion": 2,
            "features": [
                "SendTransaction",
                {"name": "SendTransaction", "maxMessages": 255},
                {"name": "SignData", "types": ["text", "binary", "cell"]},
            ],
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
            stel_dt=account.stel_dt,
            payment_method=account.payment_method,
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
        stel_dt = os.getenv("STEL_DT", "-300")
        payment_method = os.getenv("FRAGMENT_PAYMENT_METHOD", "ton")

        return cls(
            tonapi_key=tonapi_key,
            mnemonic=mnemonic,
            fragment_hash=fragment_hash,
            stel_token=stel_token,
            stel_ssid=stel_ssid,
            stel_ton_token=stel_ton_token,
            stel_dt=stel_dt,
            payment_method=payment_method,
            account_id=None,  # Нет связи с БД
        )
