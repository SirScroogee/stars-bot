"""
Сервис для работы с CryptoPay API.
"""
import asyncio
import logging
from decimal import Decimal
from typing import Optional

from aiocryptopay import AioCryptoPay, Networks
from aiocryptopay.models.invoice import Invoice

logger = logging.getLogger(__name__)

# Глобальный экземпляр клиента и текущий токен
_crypto_client: Optional[AioCryptoPay] = None
_current_token: Optional[str] = None
_client_lock: asyncio.Lock = asyncio.Lock()


async def get_crypto_client() -> AioCryptoPay:
    """Получить клиент CryptoPay (токен из БД)."""
    global _crypto_client, _current_token

    from src.services.bot_settings_service import get_cryptobot_token

    token = await get_cryptobot_token()
    if not token:
        raise ValueError("CryptoBot токен не настроен. Установите токен в настройках бота.")

    # Пересоздаём клиент если токен изменился (с блокировкой для thread-safety)
    async with _client_lock:
        if _crypto_client is None or _current_token != token:
            _crypto_client = AioCryptoPay(
                token=token,
                network=Networks.MAIN_NET,
            )
            _current_token = token
            logger.info("CryptoPay client initialized/updated")

    return _crypto_client


async def reset_crypto_client():
    """Сбросить клиент (вызывается при изменении токена)."""
    global _crypto_client, _current_token
    async with _client_lock:
        _crypto_client = None
        _current_token = None
    logger.info("CryptoPay client reset")


async def create_deposit_invoice(
    amount: Decimal,
    user_id: int,
    description: Optional[str] = None,
) -> Invoice:
    """
    Создать инвойс для пополнения баланса.

    Args:
        amount: Сумма в USDT (уже с наценкой)
        user_id: ID пользователя Telegram
        description: Описание инвойса (опционально)

    Returns:
        Invoice объект с данными инвойса
    """
    crypto = await get_crypto_client()

    # Создаём инвойс в USD
    # accepted_assets - только USDT и TON
    # allow_comments=False - запрещаем комментарии
    # allow_anonymous=False - запрещаем анонимную оплату
    invoice = await crypto.create_invoice(
        amount=float(amount),
        currency_type="fiat",
        fiat="USD",
        description=description or f"Пополнение баланса на {amount} USD",
        payload=f"deposit:{user_id}:{amount}",
        expires_in=1800,  # 30 минут
        accepted_assets=["USDT", "TON"],
        allow_comments=False,
        allow_anonymous=False,
    )

    logger.info(
        f"Created invoice {invoice.invoice_id} for user {user_id}, "
        f"amount={amount} USD"
    )

    return invoice


async def check_invoice_status(invoice_id: int) -> Optional[Invoice]:
    """
    Проверить статус инвойса.

    Args:
        invoice_id: ID инвойса

    Returns:
        Invoice объект или None если не найден
    """
    crypto = await get_crypto_client()

    try:
        invoices = await crypto.get_invoices(invoice_ids=invoice_id)
        if invoices:
            invoice = invoices[0] if isinstance(invoices, list) else invoices
            logger.info(f"Invoice {invoice_id} status: {invoice.status}")
            return invoice
    except Exception as e:
        logger.error(f"Error checking invoice {invoice_id}: {e}")

    return None


async def is_invoice_paid(invoice_id: int) -> bool:
    """
    Проверить, оплачен ли инвойс.

    Args:
        invoice_id: ID инвойса

    Returns:
        True если оплачен
    """
    invoice = await check_invoice_status(invoice_id)
    if invoice:
        return invoice.status == "paid"
    return False


async def delete_invoice(invoice_id: int) -> bool:
    """
    Удалить неоплаченный инвойс.

    Args:
        invoice_id: ID инвойса

    Returns:
        True если успешно удалён
    """
    crypto = await get_crypto_client()

    try:
        await crypto.delete_invoice(invoice_id=invoice_id)
        logger.info(f"Deleted invoice {invoice_id}")
        return True
    except Exception as e:
        logger.error(f"Error deleting invoice {invoice_id}: {e}")
        return False