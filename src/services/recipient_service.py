"""
Сервис для проверки получателей через Fragment API.

Использует Fragment аккаунты из базы данных с fallback на .env.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.api_clients.fragment import FragmentClient
from src.api_clients.fragment.config import FragmentConfig
from src.api_clients.fragment.exceptions import (
    FragmentAPIError,
    RecipientNotFoundError,
    SessionExpiredError,
)
from src.db.session import async_session_factory
from src.locales import t
from src.services.fragment_account_service import FragmentAccountService, FragmentAccountData

logger = logging.getLogger(__name__)

# Кэш клиентов Fragment по account_id (для переиспользования сессий)
_fragment_clients: dict[int, FragmentClient] = {}
# Fallback клиент из .env
_fallback_client: Optional[FragmentClient] = None
# Кэш данных лучшего аккаунта: (account_data, cached_at)
_best_account_cache: Optional[tuple[FragmentAccountData, float]] = None
# TTL кэша аккаунта (5 минут)
_ACCOUNT_CACHE_TTL = 300


async def get_fragment_client() -> FragmentClient:
    """
    Получить FragmentClient.

    Приоритет:
    1. Кэшированный клиент (если есть)
    2. Лучший активный аккаунт из БД (с кэшированием)
    3. Fallback на .env если нет аккаунтов в БД
    """
    global _fragment_clients, _fallback_client, _best_account_cache

    # Проверяем кэш аккаунта
    account_data = None
    if _best_account_cache:
        cached_data, cached_at = _best_account_cache
        if time.time() - cached_at < _ACCOUNT_CACHE_TTL:
            account_data = cached_data
            # Если клиент уже создан - возвращаем сразу
            if account_data.id in _fragment_clients:
                return _fragment_clients[account_data.id]

    # Кэш пуст или истёк - запрашиваем из БД
    if account_data is None:
        async with async_session_factory() as session:
            service = FragmentAccountService(session)
            account_data = await service.get_best_account()

        if account_data:
            _best_account_cache = (account_data, time.time())

    if account_data:
        # Используем кэшированный клиент или создаём новый
        if account_data.id not in _fragment_clients:
            config = FragmentConfig.from_account_data(account_data)
            _fragment_clients[account_data.id] = FragmentClient(config)
            logger.info(f"Created FragmentClient for account {account_data.id} ({account_data.name})")
        return _fragment_clients[account_data.id]

    # Fallback на .env
    if _fallback_client is None:
        try:
            from src.config import get_fragment_config
            config = get_fragment_config()
            _fallback_client = FragmentClient(config)
            logger.info("Using fallback FragmentClient from .env")
        except Exception as e:
            logger.error(f"Failed to create fallback client: {e}")
            raise FragmentAPIError("No Fragment accounts available", "get_client")

    return _fallback_client


def invalidate_account_cache() -> None:
    """Инвалидировать кэш аккаунта (при изменении настроек)."""
    global _best_account_cache
    _best_account_cache = None
    logger.debug("Account cache invalidated")


async def clear_client_cache(account_id: Optional[int] = None) -> None:
    """
    Очистить кэш клиентов.

    Args:
        account_id: ID аккаунта для очистки, или None для очистки всего кэша
    """
    global _fragment_clients, _fallback_client
    if account_id is not None:
        clients = [_fragment_clients.pop(account_id, None)]
    else:
        clients = list(_fragment_clients.values())
        _fragment_clients.clear()
        clients.append(_fallback_client)
        _fallback_client = None

    for client in clients:
        if client:
            try:
                await client.close()
            except Exception:
                logger.exception("Failed to close cached Fragment client")


@dataclass
class RecipientInfo:
    """Информация о получателе."""

    recipient_id: str
    username: str
    display_name: Optional[str] = None


async def validate_stars_recipient(
    username: str,
    lang: str = "ru",
) -> tuple[bool, Optional[RecipientInfo], Optional[str]]:
    """
    Проверить получателя для Stars.

    Args:
        username: Username получателя
        lang: Язык для сообщений об ошибках

    Returns:
        (success, recipient_info, error_message)
    """
    try:
        client = await get_fragment_client()
        recipient = await client.search_stars_recipient(username)

        return True, RecipientInfo(
            recipient_id=recipient.recipient_id,
            username=recipient.username,
            display_name=recipient.display_name,
        ), None

    except RecipientNotFoundError:
        logger.info(f"Stars recipient not found: {username}")
        return False, None, t("common.recipient.errors.not_found", lang, username=username)

    except SessionExpiredError:
        logger.error("Fragment session expired")
        return False, None, t("common.recipient.errors.service_unavailable", lang)

    except FragmentAPIError as e:
        logger.error(f"Fragment API error for @{username}: {e}", exc_info=True)
        error_str = str(e).lower()

        # Пользователь не может получить Stars (ограничения аккаунта)
        if "cannot receive" in error_str or "not eligible" in error_str:
            return False, None, t("common.recipient.errors.cannot_receive_stars", lang, username=username)

        # Пользователь не найден
        if "not found" in error_str or "recipient" in error_str:
            return False, None, t("common.recipient.errors.not_found", lang, username=username)

        # Bad request - общая ошибка
        if "bad request" in error_str:
            return False, None, t("common.recipient.errors.unavailable_stars", lang, username=username)

        # Rate limit
        if "rate limit" in error_str or "too many" in error_str:
            return False, None, t("common.recipient.errors.rate_limit", lang)

        # Timeout
        if "timeout" in error_str:
            return False, None, t("common.recipient.errors.timeout", lang)

        # Неизвестная ошибка
        logger.warning(f"Unknown Fragment API error for stars recipient @{username}: {e}")
        return False, None, t("common.recipient.errors.unknown", lang)

    except Exception as e:
        logger.error(f"Unexpected error validating stars recipient @{username}: {e}", exc_info=True)
        return False, None, t("common.recipient.errors.validation_error", lang)


async def validate_premium_recipient(
    username: str,
    lang: str = "ru",
) -> tuple[bool, Optional[RecipientInfo], Optional[str]]:
    """
    Проверить получателя для Premium.

    Args:
        username: Username получателя
        lang: Язык для сообщений об ошибках

    Returns:
        (success, recipient_info, error_message)
    """
    try:
        client = await get_fragment_client()
        recipient = await client.search_premium_recipient(username)

        return True, RecipientInfo(
            recipient_id=recipient.recipient_id,
            username=recipient.username,
            display_name=recipient.display_name,
        ), None

    except RecipientNotFoundError:
        logger.info(f"Premium recipient not found: {username}")
        return False, None, t("common.recipient.errors.not_found", lang, username=username)

    except SessionExpiredError:
        logger.error("Fragment session expired")
        return False, None, t("common.recipient.errors.service_unavailable", lang)

    except FragmentAPIError as e:
        logger.error(f"Fragment API error for @{username}: {e}", exc_info=True)
        error_str = str(e).lower()

        # Пользователь уже имеет Premium
        if "already" in error_str and "premium" in error_str:
            return False, None, t("common.recipient.errors.already_has_premium", lang, username=username)
        if "has premium" in error_str or "active premium" in error_str:
            return False, None, t("common.recipient.errors.already_has_premium", lang, username=username)

        # Пользователь не может получить Premium (ограничения аккаунта)
        if "cannot receive" in error_str or "not eligible" in error_str:
            return False, None, t("common.recipient.errors.cannot_receive_premium", lang, username=username)

        # Пользователь не найден
        if "not found" in error_str or "recipient" in error_str:
            return False, None, t("common.recipient.errors.not_found", lang, username=username)

        # Bad request - общая ошибка, часто означает что получатель недоступен
        if "bad request" in error_str:
            return False, None, t("common.recipient.errors.unavailable_premium", lang, username=username)

        # Rate limit
        if "rate limit" in error_str or "too many" in error_str:
            return False, None, t("common.recipient.errors.rate_limit", lang)

        # Timeout
        if "timeout" in error_str:
            return False, None, t("common.recipient.errors.timeout", lang)

        # Неизвестная ошибка - логируем полностью для анализа
        logger.warning(f"Unknown Fragment API error for premium recipient @{username}: {e}")
        return False, None, t("common.recipient.errors.unknown", lang)

    except Exception as e:
        logger.error(f"Unexpected error validating premium recipient @{username}: {e}", exc_info=True)
        return False, None, t("common.recipient.errors.validation_error", lang)
