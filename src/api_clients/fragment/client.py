"""
Низкоуровневый клиент для работы с Fragment API.

Этот модуль отвечает ТОЛЬКО за:
- HTTP-запросы к Fragment API
- TON-транзакции через кошелёк
- Парсинг ответов Fragment

НЕ отвечает за:
- Управление заказами
- Работу с БД
- Бизнес-логику покупок

Оптимизации:
- Кэширование получателей (5 минут TTL)
- Connection pooling
- Circuit breaker
- Retry с exponential backoff + jitter
"""
import asyncio
import base64
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import ClientTimeout, TCPConnector
from tonsdk.boc import Cell
from tonutils.client import TonapiClient
from tonutils.wallet import WalletV4R2

from src.api_clients.fragment.config import FragmentConfig
from src.api_clients.fragment.exceptions import (
    FragmentAPIError,
    InsufficientFundsError,
    RateLimitError,
    RecipientNotFoundError,
    SessionExpiredError,
    TransactionError,
    TransactionTimeoutError,
)

logger = logging.getLogger(__name__)


@dataclass
class TransactionResult:
    """Результат выполнения транзакции."""

    address: str
    amount: float
    tx_hash: str
    currency: str = "TON"


@dataclass
class FragmentRecipient:
    """Информация о получателе из Fragment."""

    recipient_id: str
    username: str
    display_name: str | None = None


@dataclass
class FragmentRequest:
    """Инициализированный запрос на покупку."""

    req_id: str
    recipient: FragmentRecipient
    amount: float | None = None


class CircuitBreaker:
    """
    Circuit Breaker для защиты от каскадных сбоев.

    Состояния:
    - closed: нормальная работа
    - open: API недоступен, отклоняем запросы
    - half-open: пробуем восстановить связь
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        name: str = "default",
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.name = name
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.state = "closed"  # closed, open, half-open
        self._lock = asyncio.Lock()

    async def call(self, coro):
        """Выполнить корутину с защитой circuit breaker."""
        async with self._lock:
            current_time = time.time()

            # Проверяем нужно ли перейти в half-open
            if self.state == "open":
                if self.last_failure_time and current_time - self.last_failure_time > self.recovery_timeout:
                    self.state = "half-open"
                    logger.info(f"Circuit breaker [{self.name}] entering half-open state")
                else:
                    raise FragmentAPIError(
                        f"Circuit breaker [{self.name}] is open, rejecting request",
                        "circuit_breaker",
                    )

        try:
            result = await coro

            # Успех - сбрасываем счётчик
            async with self._lock:
                if self.state == "half-open":
                    self.state = "closed"
                    self.failure_count = 0
                    logger.info(f"Circuit breaker [{self.name}] reset to closed")
                elif self.state == "closed":
                    self.failure_count = 0

            return result

        except Exception as e:
            async with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()

                if self.failure_count >= self.failure_threshold:
                    self.state = "open"
                    logger.error(
                        f"Circuit breaker [{self.name}] opened after {self.failure_count} failures"
                    )
                elif self.state == "half-open":
                    self.state = "open"
                    logger.warning(f"Circuit breaker [{self.name}] back to open after half-open failure")

            raise

    def reset(self):
        """Принудительный сброс circuit breaker."""
        self.state = "closed"
        self.failure_count = 0
        self.last_failure_time = None
        logger.info(f"Circuit breaker [{self.name}] manually reset")


class FragmentClient:
    """
    Низкоуровневый клиент для Fragment API.

    Использование:
        config = FragmentConfig.from_env()
        client = FragmentClient(config)

        # Клиент инициализируется лениво при первом вызове
        recipient = await client.search_stars_recipient("username")
        request = await client.init_stars_request(recipient.recipient_id, 100)
        result = await client.execute_stars_payment(request.req_id, show_sender=False)

    Оптимизации:
        - Кэширование получателей (5 мин TTL)
        - Connection pooling
        - Circuit breaker
        - Retry с jitter
    """

    def __init__(self, config: FragmentConfig):
        self._config = config
        self._ton_client: TonapiClient | None = None
        self._wallet: WalletV4R2 | None = None
        self._public_key: bytes | None = None
        self._address: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._connector: TCPConnector | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

        # Кэш получателей: username -> (recipient, cached_at)
        self._recipient_cache: dict[str, tuple[FragmentRecipient, float]] = {}

        # Circuit breaker
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=config.circuit_breaker_failure_threshold,
            recovery_timeout=config.circuit_breaker_recovery_timeout,
            name=f"fragment_{config.account_id or 'default'}",
        )

        # Rate limit tracking
        self._rate_limit_reset_at: float | None = None

    async def _ensure_initialized(self) -> None:
        """Ленивая инициализация TON-клиента и кошелька."""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            logger.info("Initializing FragmentClient...")

            self._ton_client = TonapiClient(api_key=self._config.tonapi_key)
            wallet, public_key, *_ = WalletV4R2.from_mnemonic(
                self._ton_client, mnemonic=self._config.mnemonic
            )
            self._wallet = wallet
            self._public_key = public_key
            self._address = wallet.address.to_str()
            self._initialized = True

            logger.info(f"FragmentClient initialized. Wallet: {self._address}")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать HTTP-сессию с connection pooling."""
        if self._session is None or self._session.closed:
            # Создаём connector с пулом соединений
            self._connector = TCPConnector(
                limit=self._config.connector_limit,
                limit_per_host=self._config.connector_limit_per_host,
                keepalive_timeout=self._config.connector_keepalive_timeout,
                enable_cleanup_closed=True,
            )

            timeout = ClientTimeout(total=self._config.request_timeout)
            self._session = aiohttp.ClientSession(
                connector=self._connector,
                cookies=self._config.cookies,
                headers=self._config.headers,
                timeout=timeout,
            )
            logger.debug(
                f"Created new session with pool: limit={self._config.connector_limit}, "
                f"per_host={self._config.connector_limit_per_host}"
            )

        return self._session

    def _calculate_retry_delay(self, attempt: int) -> float:
        """Рассчитать задержку для retry с exponential backoff + jitter."""
        base_delay = self._config.retry_backoff * (2 ** attempt)
        jitter = random.uniform(0, base_delay * self._config.retry_jitter_factor)
        return base_delay + jitter

    async def _call_api(self, data: dict[str, Any], method: str) -> dict[str, Any]:
        """
        Выполнить запрос к Fragment API с retry, circuit breaker и jitter.

        Args:
            data: Данные запроса
            method: Название метода (для логирования)

        Returns:
            Ответ API

        Raises:
            SessionExpiredError: Сессия истекла
            RateLimitError: Превышен лимит запросов
            FragmentAPIError: Другие ошибки API
        """
        # Проверяем rate limit
        if self._rate_limit_reset_at and time.time() < self._rate_limit_reset_at:
            wait_time = self._rate_limit_reset_at - time.time()
            logger.warning(f"Rate limited, waiting {wait_time:.1f}s before request")
            await asyncio.sleep(wait_time)

        # Выполняем с circuit breaker
        return await self._circuit_breaker.call(
            self._call_api_impl(data, method)
        )

    async def _call_api_impl(self, data: dict[str, Any], method: str) -> dict[str, Any]:
        """Внутренняя реализация API вызова с retry и jitter."""
        url = f"{self._config.fragment_url}?hash={self._config.fragment_hash}"
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries):
            try:
                session = await self._get_session()

                logger.debug(f"Fragment API call: {method} (attempt {attempt + 1})")

                async with session.post(url, data=data) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 60))
                        self._rate_limit_reset_at = time.time() + retry_after
                        raise RateLimitError(method, retry_after)

                    raw = await resp.json()

                    # Не логируем raw response — может содержать чувствительные данные

                    error = raw.get("error")
                    if error:
                        logger.error(f"Fragment API error: {error} (method: {method})")
                        if error in ("Access denied", "Session expired"):
                            raise SessionExpiredError(method)
                        raise FragmentAPIError(f"Fragment API error: {error}", method, raw)

                    logger.debug(f"Fragment API response: {method} -> OK")
                    return raw

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                logger.warning(
                    f"Fragment API call failed: {method} (attempt {attempt + 1}): {e}"
                )

                if attempt < self._config.max_retries - 1:
                    delay = self._calculate_retry_delay(attempt)
                    logger.debug(f"Retrying in {delay:.2f}s...")
                    await asyncio.sleep(delay)

        raise FragmentAPIError(
            f"Fragment API call failed after {self._config.max_retries} attempts: {last_error}",
            method,
        )

    # ==================== КЭШИРОВАНИЕ ====================

    def _get_cached_recipient(self, username: str) -> FragmentRecipient | None:
        """Получить получателя из кэша если не истёк TTL."""
        if username not in self._recipient_cache:
            return None

        recipient, cached_at = self._recipient_cache[username]
        if time.time() - cached_at > self._config.recipient_cache_ttl:
            # TTL истёк
            del self._recipient_cache[username]
            logger.debug(f"Cache expired for recipient: {username}")
            return None

        logger.debug(f"Cache hit for recipient: {username}")
        return recipient

    def _cache_recipient(self, username: str, recipient: FragmentRecipient) -> None:
        """Добавить получателя в кэш."""
        self._recipient_cache[username] = (recipient, time.time())
        logger.debug(f"Cached recipient: {username} -> {recipient.recipient_id}")

    def invalidate_recipient_cache(self, username: str | None = None) -> None:
        """
        Инвалидировать кэш получателей.

        Args:
            username: Конкретный username или None для очистки всего кэша
        """
        if username:
            if username in self._recipient_cache:
                del self._recipient_cache[username]
                logger.debug(f"Invalidated cache for: {username}")
        else:
            self._recipient_cache.clear()
            logger.debug("Invalidated entire recipient cache")

    def _invalidate_cache_by_recipient_id(self, recipient_id: str) -> None:
        """Инвалидировать кэш по recipient_id."""
        to_remove = [
            username for username, (recipient, _) in self._recipient_cache.items()
            if recipient.recipient_id == recipient_id
        ]
        for username in to_remove:
            del self._recipient_cache[username]
            logger.debug(f"Invalidated cache by recipient_id for: {username}")

    async def get_balance(self) -> float:
        """Получить баланс TON-кошелька."""
        await self._ensure_initialized()
        nano = await self._ton_client.get_account_balance(self._address)
        balance = nano / 1e9
        logger.debug(f"Wallet balance: {balance} TON")
        return balance

    async def get_wallet_address(self) -> str:
        """Получить адрес TON-кошелька."""
        await self._ensure_initialized()
        return self._address

    # ==================== STARS ====================

    def _validate_username(self, username: str) -> str:
        """
        Валидация и нормализация username.

        Args:
            username: Username для проверки

        Returns:
            Нормализованный username (без @, lowercase)

        Raises:
            ValueError: Если username невалиден
        """
        if not username:
            raise ValueError("Username cannot be empty")

        # Убираем @ если есть
        username = username.lstrip("@").strip()

        if not username:
            raise ValueError("Username cannot be empty")

        # Telegram username: 5-32 символа, a-z, 0-9, _
        if len(username) < 5 or len(username) > 32:
            raise ValueError(f"Username must be 5-32 characters, got {len(username)}")

        if not username.replace("_", "").isalnum():
            raise ValueError("Username can only contain letters, numbers, and underscores")

        return username.lower()

    async def search_stars_recipient(
        self, username: str, use_cache: bool = True
    ) -> FragmentRecipient:
        """
        Найти получателя для отправки Stars.

        Args:
            username: Username получателя в Telegram
            use_cache: Использовать кэш (по умолчанию True)

        Returns:
            Информация о получателе

        Raises:
            RecipientNotFoundError: Получатель не найден
            ValueError: Username невалиден
        """
        # Валидируем username
        username = self._validate_username(username)

        # Проверяем кэш
        if use_cache:
            cached = self._get_cached_recipient(username)
            if cached:
                return cached

        result = await self._call_api(
            {"query": username, "quantity": "1", "method": "searchStarsRecipient"},
            "searchStarsRecipient",
        )

        found = result.get("found", {})
        recipient_id = found.get("recipient", "").strip()

        if not recipient_id:
            raise RecipientNotFoundError(username)

        recipient = FragmentRecipient(
            recipient_id=recipient_id,
            username=username,
            display_name=found.get("name"),
        )

        # Кэшируем результат
        self._cache_recipient(username, recipient)

        return recipient

    async def init_stars_request(
        self, recipient_id: str, quantity: int
    ) -> FragmentRequest:
        """
        Инициализировать запрос на покупку Stars.

        Args:
            recipient_id: ID получателя из search_stars_recipient
            quantity: Количество Stars

        Returns:
            Инициализированный запрос с req_id
        """
        try:
            await self._call_api(
                {"stars": "", "quantity": str(quantity), "method": "updateStarsPrices"},
                "updateStarsPrices",
            )

            result = await self._call_api(
                {"recipient": recipient_id, "quantity": str(quantity), "method": "initBuyStarsRequest"},
                "initBuyStarsRequest",
            )

            return FragmentRequest(
                req_id=result["req_id"],
                recipient=FragmentRecipient(recipient_id=recipient_id, username=""),
            )

        except FragmentAPIError as e:
            # Если ошибка связана с получателем - инвалидируем кэш
            error_lower = str(e).lower()
            if "recipient" in error_lower or "not found" in error_lower or "invalid" in error_lower:
                self._invalidate_cache_by_recipient_id(recipient_id)
                logger.warning(f"Invalidated cache due to recipient error: {e}")
            raise

    async def execute_stars_payment(
        self, req_id: str, show_sender: bool = False
    ) -> list[TransactionResult]:
        """
        Выполнить оплату Stars.

        Args:
            req_id: ID запроса из init_stars_request
            show_sender: Показывать отправителя

        Returns:
            Список результатов транзакций (может быть несколько)

        Raises:
            InsufficientFundsError: Недостаточно средств
            TransactionError: Ошибка транзакции
        """
        await self._ensure_initialized()

        link_data = await self._call_api(
            {
                "account": json.dumps(self._get_account_data()),
                "device": json.dumps({"platform": "python"}),
                "transaction": "1",
                "id": req_id,
                "show_sender": "1" if show_sender else "0",
                "method": "getBuyStarsLink",
            },
            "getBuyStarsLink",
        )

        return await self._execute_transactions(link_data)

    # ==================== PREMIUM ====================

    async def search_premium_recipient(
        self, username: str, use_cache: bool = True
    ) -> FragmentRecipient:
        """
        Найти получателя для отправки Premium.

        Args:
            username: Username получателя в Telegram
            use_cache: Использовать кэш (по умолчанию True)

        Returns:
            Информация о получателе

        Raises:
            RecipientNotFoundError: Получатель не найден
            ValueError: Username невалиден
        """
        # Валидируем username
        username = self._validate_username(username)

        # Проверяем кэш (общий для stars и premium)
        if use_cache:
            cached = self._get_cached_recipient(username)
            if cached:
                return cached

        result = await self._call_api(
            {"query": username, "method": "searchPremiumGiftRecipient"},
            "searchPremiumGiftRecipient",
        )

        found = result.get("found", {})
        recipient_id = found.get("recipient", "").strip()

        if not recipient_id:
            raise RecipientNotFoundError(username)

        recipient = FragmentRecipient(
            recipient_id=recipient_id,
            username=username,
            display_name=found.get("name"),
        )

        # Кэшируем результат
        self._cache_recipient(username, recipient)

        return recipient

    async def init_premium_request(
        self, recipient_id: str, months: int
    ) -> FragmentRequest:
        """
        Инициализировать запрос на покупку Premium.

        Args:
            recipient_id: ID получателя из search_premium_recipient
            months: Количество месяцев (3, 6 или 12)

        Returns:
            Инициализированный запрос с req_id
        """
        try:
            result = await self._call_api(
                {"recipient": recipient_id, "months": str(months), "method": "initGiftPremiumRequest"},
                "initGiftPremiumRequest",
            )

            return FragmentRequest(
                req_id=result["req_id"],
                recipient=FragmentRecipient(recipient_id=recipient_id, username=""),
            )

        except FragmentAPIError as e:
            # Если ошибка связана с получателем - инвалидируем кэш
            error_lower = str(e).lower()
            if "recipient" in error_lower or "not found" in error_lower or "invalid" in error_lower:
                self._invalidate_cache_by_recipient_id(recipient_id)
                logger.warning(f"Invalidated cache due to recipient error: {e}")
            raise

    async def execute_premium_payment(
        self, req_id: str, show_sender: bool = False
    ) -> list[TransactionResult]:
        """
        Выполнить оплату Premium.

        Args:
            req_id: ID запроса из init_premium_request
            show_sender: Показывать отправителя

        Returns:
            Список результатов транзакций

        Raises:
            InsufficientFundsError: Недостаточно средств
            TransactionError: Ошибка транзакции
        """
        await self._ensure_initialized()

        link_data = await self._call_api(
            {
                "account": json.dumps(self._get_account_data()),
                "device": json.dumps({"platform": "python"}),
                "transaction": "1",
                "id": req_id,
                "show_sender": "1" if show_sender else "0",
                "method": "getGiftPremiumLink",
            },
            "getGiftPremiumLink",
        )

        return await self._execute_transactions(link_data)

    # ==================== INTERNAL ====================

    async def _execute_transactions(self, link_data: dict) -> list[TransactionResult]:
        """
        Выполнить все транзакции из ответа Fragment.

        ВАЖНО: Обрабатывает ВСЕ сообщения, не игнорируя дополнительные.
        """
        messages = link_data.get("transaction", {}).get("messages", [])

        if not messages:
            raise TransactionError("No transaction messages in response", link_data)

        balance = await self.get_balance()
        total_required = sum(int(msg["amount"]) / 1e9 for msg in messages)

        if balance < total_required:
            raise InsufficientFundsError(
                required=total_required,
                available=balance,
                currency="TON",
            )

        results: list[TransactionResult] = []

        for idx, msg in enumerate(messages):
            amount = int(msg["amount"]) / 1e9
            address = msg["address"]

            logger.info(
                f"Executing transaction {idx + 1}/{len(messages)}: "
                f"{amount} TON to {address}"
            )

            try:
                cell = await self._payload_cell(msg["payload"])
                tx_hash = await self._wallet.transfer(
                    destination=address,
                    amount=amount,  # amount уже в TON (конвертировано выше)
                    body=cell,  # body содержит комментарий для Fragment
                )

                results.append(
                    TransactionResult(
                        address=address,
                        amount=amount,
                        tx_hash=tx_hash,
                    )
                )

                logger.info(f"Transaction {idx + 1} completed: {tx_hash}")

            except asyncio.TimeoutError:
                raise TransactionTimeoutError(
                    self._config.tx_timeout,
                    {"address": address, "amount": amount},
                )
            except Exception as e:
                raise TransactionError(
                    f"Transaction failed: {e}",
                    {"address": address, "amount": amount, "error": str(e)},
                )

        return results

    @staticmethod
    async def _payload_cell(payload_b64: str) -> Cell:
        """Декодировать payload из base64 в Cell."""
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return Cell.one_from_boc(base64.b64decode(padded))

    def _get_account_data(self) -> dict[str, Any]:
        """Получить данные аккаунта для Fragment API."""
        state_init = base64.b64encode(
            self._wallet.state_init.serialize().to_boc()
        ).decode()

        return {
            "address": self._address,
            "chain": "-239",
            "walletStateInit": state_init,
            "publicKey": self._public_key.hex(),
        }

    async def close(self) -> None:
        """Закрыть клиент и освободить ресурсы."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("FragmentClient session closed")

        if self._connector and not self._connector.closed:
            await self._connector.close()
            logger.debug("FragmentClient connector closed")

    async def __aenter__(self) -> "FragmentClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # ==================== HEALTH CHECK ====================

    async def health_check(self) -> dict[str, Any]:
        """Проверить состояние клиента."""
        return {
            "initialized": self._initialized,
            "session_active": self._session is not None and not self._session.closed,
            "wallet_ready": self._wallet is not None,
            "wallet_address": self._address,
            "cache_size": len(self._recipient_cache),
            "circuit_breaker_state": self._circuit_breaker.state,
            "circuit_breaker_failures": self._circuit_breaker.failure_count,
            "account_id": self._config.account_id,
        }
