"""
Сервис для приёма платежей в TON напрямую.
"""
import hashlib
import logging
import time
from decimal import Decimal
from typing import Optional
from urllib.parse import quote

import aiohttp

from src.services.rates_service import get_rates, DEFAULT_RATES

logger = logging.getLogger(__name__)


async def _get_tonapi_key() -> Optional[str]:
    """
    Получить TONAPI ключ.

    Приоритет:
    1. Из активного Fragment аккаунта в БД
    2. Из .env (fallback)

    Returns:
        TONAPI ключ или None если не найден
    """
    # Сначала пробуем из БД
    try:
        from src.db.session import async_session_factory
        from src.services.fragment_account_service import FragmentAccountService

        async with async_session_factory() as session:
            service = FragmentAccountService(session)
            account = await service.get_best_account()
            if account and account.tonapi_key:
                return account.tonapi_key
    except Exception as e:
        logger.debug(f"Could not get TONAPI key from DB: {e}")

    # Fallback на .env
    try:
        from src.config import get_fragment_config_data
        fragment_config = get_fragment_config_data()
        return fragment_config.tonapi_key
    except Exception as e:
        logger.debug(f"Could not get TONAPI key from .env: {e}")

    return None


async def get_ton_wallet() -> str:
    """Получить адрес TON кошелька из БД."""
    from src.services.bot_settings_service import get_ton_wallet_address

    wallet = await get_ton_wallet_address()
    if not wallet:
        raise ValueError("TON кошелёк не настроен. Установите адрес в настройках бота.")
    return wallet


async def get_ton_usd_rate() -> Optional[Decimal]:
    """
    Получить текущий курс TON/USD.

    Returns:
        Курс TON в USD или None при ошибке
    """
    rates = await get_rates()
    ton_usd = rates.get("ton_usd")

    if ton_usd:
        return ton_usd

    # Fallback на дефолтное значение
    return DEFAULT_RATES.get("ton_usd")


def generate_payment_comment(
    user_id: int,
    amount: Decimal,
    product_type: str = "deposit",
    quantity: int = 0,
) -> str:
    """
    Генерировать уникальный комментарий для платежа.

    Args:
        user_id: ID пользователя Telegram
        amount: Сумма пополнения в USDT
        product_type: Тип продукта (deposit, stars, premium)
        quantity: Количество (звёзд или месяцев)

    Returns:
        Уникальный комментарий для отслеживания платежа
    """
    # Создаём хэш из user_id, amount и timestamp
    timestamp = int(time.time())
    data = f"{user_id}:{amount}:{timestamp}"
    hash_part = hashlib.sha256(data.encode()).hexdigest()[:6]

    # Формат: buy_100stars_a1b2c3 или buy_3premium_a1b2c3 или dep_a1b2c3
    if product_type == "stars" and quantity > 0:
        return f"buy_{quantity}stars_{hash_part}"
    elif product_type == "premium" and quantity > 0:
        return f"buy_{quantity}premium_{hash_part}"
    else:
        return f"dep_{hash_part}"


async def create_ton_payment_url(
    amount_ton: Decimal,
    comment: str,
) -> str:
    """
    Создать URL для оплаты в TON.

    Args:
        amount_ton: Сумма в TON
        comment: Комментарий к платежу

    Returns:
        URL для оплаты (ton://transfer/...)
    """
    wallet_address = await get_ton_wallet()

    # Конвертируем TON в нано-TON (1 TON = 10^9 нано-TON)
    amount_nano = int(amount_ton * Decimal("1000000000"))

    # Формируем URL
    url = (
        f"ton://transfer/{wallet_address}"
        f"?amount={amount_nano}"
        f"&text={quote(comment)}"
    )

    return url


async def check_ton_payment(
    comment: str,
    expected_amount_ton: Decimal,
    since_timestamp: int,
) -> Optional[dict]:
    """
    Проверить, была ли получена оплата в TON.

    Args:
        comment: Ожидаемый комментарий платежа
        expected_amount_ton: Ожидаемая сумма в TON
        since_timestamp: Проверять транзакции после этого времени

    Returns:
        Данные транзакции если найдена, иначе None
    """
    # Получаем адрес кошелька из БД
    try:
        wallet_address = await get_ton_wallet()
    except ValueError as e:
        logger.error(f"TON wallet not configured: {e}")
        return None

    # Получаем TONAPI ключ (из БД или .env)
    tonapi_key = await _get_tonapi_key()
    if not tonapi_key:
        logger.error("TONAPI_KEY not configured (neither in DB accounts nor in .env)")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            # Получаем транзакции кошелька через TONAPI
            url = f"https://tonapi.io/v2/accounts/{wallet_address}/events"
            headers = {"Authorization": f"Bearer {tonapi_key}"}
            params = {"limit": 50}

            logger.info(f"Checking TON payment: comment={comment}, expected={expected_amount_ton} TON")

            async with session.get(url, headers=headers, params=params, timeout=15) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(f"TONAPI error: {response.status}, {text}")
                    return None

                data = await response.json()
                events = data.get("events", [])

                logger.info(f"Found {len(events)} events to check")

                for event in events:
                    # Проверяем время транзакции
                    event_time = event.get("timestamp", 0)
                    if event_time < since_timestamp:
                        continue

                    # Ищем переводы TON
                    actions = event.get("actions", [])
                    for action in actions:
                        if action.get("type") != "TonTransfer":
                            continue

                        ton_transfer = action.get("TonTransfer", {})

                        # Проверяем комментарий (это главный критерий)
                        tx_comment = ton_transfer.get("comment", "")
                        if tx_comment != comment:
                            continue

                        # Нашли транзакцию с нужным комментарием!
                        # Проверяем сумму (в нано-TON)
                        amount_nano = int(ton_transfer.get("amount", 0))
                        amount_ton_received = Decimal(amount_nano) / Decimal("1000000000")

                        # Допускаем погрешность 5% (на комиссию сети)
                        min_amount = expected_amount_ton * Decimal("0.95")
                        if amount_ton_received >= min_amount:
                            logger.info(
                                f"Found TON payment: {amount_ton_received} TON, "
                                f"comment={comment}, event_id={event.get('event_id')}"
                            )
                            return {
                                "amount_ton": amount_ton_received,
                                "comment": comment,
                                "timestamp": event_time,
                                "event_id": event.get("event_id"),
                            }
                        else:
                            logger.warning(
                                f"Found comment but amount too low: {amount_ton_received} < {min_amount}"
                            )

    except Exception as e:
        logger.error(f"Error checking TON payment: {e}", exc_info=True)

    return None


async def debug_get_recent_transactions() -> list:
    """
    Получить последние транзакции для отладки.
    """
    # Получаем адрес кошелька из БД
    try:
        wallet_address = await get_ton_wallet()
    except ValueError:
        return []

    tonapi_key = await _get_tonapi_key()
    if not tonapi_key:
        return []

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://tonapi.io/v2/accounts/{wallet_address}/events"
            headers = {"Authorization": f"Bearer {tonapi_key}"}
            params = {"limit": 10}

            async with session.get(url, headers=headers, params=params, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    events = data.get("events", [])

                    result = []
                    for event in events:
                        actions = event.get("actions", [])
                        for action in actions:
                            if action.get("type") == "TonTransfer":
                                ton_transfer = action.get("TonTransfer", {})
                                result.append({
                                    "timestamp": event.get("timestamp"),
                                    "amount": int(ton_transfer.get("amount", 0)) / 1e9,
                                    "comment": ton_transfer.get("comment", ""),
                                    "sender": ton_transfer.get("sender", {}).get("address", ""),
                                    "recipient": ton_transfer.get("recipient", {}).get("address", ""),
                                })
                    return result
    except Exception as e:
        logger.error(f"Debug error: {e}")

    return []
