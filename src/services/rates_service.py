"""
Сервис для получения курсов валют через CoinGecko API.

API Documentation: https://docs.coingecko.com/v3.0.1/reference/simple-price
"""
import logging
import time
from decimal import Decimal
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# CoinGecko API
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"

# Coin IDs
COIN_IDS = {
    "ton": "the-open-network",
    "usdt": "tether",
}

# Кэш курсов
_rates_cache: dict[str, Decimal] = {}
_rates_timestamp: float = 0
RATES_CACHE_TTL = 60  # секунд

# Дефолтные значения (на случай если API недоступен)
DEFAULT_RATES = {
    "usdt": Decimal("1"),
    "ton_usd": Decimal("5.5"),    # 1 TON = $5.50
    "usd_rub": Decimal("100"),    # 1 USD = 100 RUB
}


async def fetch_rates_from_coingecko() -> Optional[dict]:
    """
    Получить курсы из CoinGecko API.

    Returns:
        Словарь с курсами или None при ошибке
    """
    try:
        async with aiohttp.ClientSession() as session:
            params = {
                "ids": "the-open-network,tether",
                "vs_currencies": "usd,rub",
                "precision": "full",
            }

            async with session.get(
                COINGECKO_API_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.debug(f"CoinGecko response: {data}")
                    return data
                else:
                    logger.warning(f"CoinGecko API returned status {response.status}")
                    return None
    except aiohttp.ClientError as e:
        logger.error(f"CoinGecko API client error: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch rates from CoinGecko: {e}")
        return None


def parse_coingecko_response(data: dict) -> dict[str, Decimal]:
    """
    Распарсить ответ CoinGecko API.

    Args:
        data: Ответ от API

    Returns:
        Словарь с курсами:
        - ton_usd: цена 1 TON в USD
        - usd_rub: цена 1 USD в RUB
    """
    rates = {}

    # TON/USD - сколько USD стоит 1 TON
    ton_data = data.get("the-open-network", {})
    if ton_data.get("usd"):
        rates["ton_usd"] = Decimal(str(ton_data["usd"]))

    # USD/RUB - сколько RUB стоит 1 USD (через tether как proxy для USD)
    tether_data = data.get("tether", {})
    if tether_data.get("rub"):
        rates["usd_rub"] = Decimal(str(tether_data["rub"]))

    return rates


async def get_rates() -> dict[str, Decimal]:
    """
    Получить актуальные курсы валют.

    Returns:
        Словарь с курсами:
        - usdt: всегда 1
        - ton_usd: цена 1 TON в USD
        - usd_rub: цена 1 USD в RUB
    """
    global _rates_cache, _rates_timestamp

    current_time = time.time()

    # Используем кэш если он свежий
    if _rates_cache and (current_time - _rates_timestamp) < RATES_CACHE_TTL:
        return _rates_cache

    # Получаем курсы из API
    data = await fetch_rates_from_coingecko()

    if data:
        parsed = parse_coingecko_response(data)

        _rates_cache = {
            "usdt": Decimal("1"),
            "ton_usd": parsed.get("ton_usd", DEFAULT_RATES["ton_usd"]),
            "usd_rub": parsed.get("usd_rub", DEFAULT_RATES["usd_rub"]),
        }
        _rates_timestamp = current_time

        logger.info(
            f"Rates updated: TON=${_rates_cache['ton_usd']:.2f}, "
            f"USD={_rates_cache['usd_rub']:.2f}₽"
        )
    elif not _rates_cache:
        # Первый запуск и API недоступен - используем дефолты
        _rates_cache = DEFAULT_RATES.copy()
        _rates_timestamp = current_time
        logger.warning("Using default rates (API unavailable)")

    return _rates_cache


def convert_usdt_to_currency(usdt_amount: Decimal, currency: str, rates: dict) -> Decimal:
    """
    Конвертировать сумму из USDT в указанную валюту.

    Args:
        usdt_amount: Сумма в USDT
        currency: Целевая валюта (usdt, ton, rub)
        rates: Словарь с курсами

    Returns:
        Сумма в целевой валюте
    """
    if currency == "usdt":
        return usdt_amount
    elif currency == "ton":
        ton_usd = rates.get("ton_usd", DEFAULT_RATES["ton_usd"])
        return usdt_amount / ton_usd
    elif currency == "rub":
        usd_rub = rates.get("usd_rub", DEFAULT_RATES["usd_rub"])
        return usdt_amount * usd_rub
    return usdt_amount


def convert_currency_to_usdt(amount: Decimal, currency: str, rates: dict) -> Decimal:
    """
    Конвертировать сумму из указанной валюты в USDT.

    Args:
        amount: Сумма в исходной валюте
        currency: Исходная валюта (usdt, ton, rub)
        rates: Словарь с курсами

    Returns:
        Сумма в USDT
    """
    if currency == "usdt":
        return amount
    elif currency == "ton":
        ton_usd = rates.get("ton_usd", DEFAULT_RATES["ton_usd"])
        return amount * ton_usd
    elif currency == "rub":
        usd_rub = rates.get("usd_rub", DEFAULT_RATES["usd_rub"])
        return amount / usd_rub
    return amount


def _format_number(amount: Decimal, decimals: int) -> str:
    """Форматировать число, убирая лишние нули после запятой."""
    formatted = f"{amount:.{decimals}f}"
    # Убираем лишние нули после запятой
    if '.' in formatted:
        formatted = formatted.rstrip('0').rstrip('.')
    return formatted


def format_currency(amount: Decimal, currency: str) -> str:
    """
    Форматировать сумму с символом валюты.

    Args:
        amount: Сумма
        currency: Валюта (usdt, ton, rub)

    Returns:
        Отформатированная строка
    """
    if currency == "usdt":
        return f"{_format_number(amount, 2)} USDT"
    elif currency == "ton":
        return f"{_format_number(amount, 4)} TON"
    elif currency == "rub":
        return f"{_format_number(amount, 2)} ₽"
    return _format_number(amount, 2)
