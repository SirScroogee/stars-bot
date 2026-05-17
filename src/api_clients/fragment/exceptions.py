"""
Доменные исключения для работы с Fragment API.

Иерархия:
    FragmentError (базовый)
    ├── FragmentAPIError (ошибки API)
    │   └── SessionExpiredError (сессия истекла)
    ├── RecipientNotFoundError (получатель не найден)
    ├── InsufficientFundsError (недостаточно средств)
    └── TransactionError (ошибки транзакций)
        └── TransactionTimeoutError (таймаут транзакции)
"""


class FragmentError(Exception):
    """Базовое исключение для всех ошибок Fragment."""

    def __init__(self, message: str, details: dict | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class FragmentAPIError(FragmentError):
    """Ошибка при вызове Fragment API."""

    def __init__(self, message: str, method: str, response: dict | None = None):
        self.method = method
        self.response = response
        super().__init__(message, {"method": method, "response": response})


class SessionExpiredError(FragmentAPIError):
    """Сессия Fragment истекла, требуется повторная авторизация."""

    def __init__(self, method: str):
        super().__init__(
            "Fragment session expired. Re-authentication required.",
            method=method,
            response={"error": "Session expired"},
        )


class RecipientNotFoundError(FragmentError):
    """Получатель не найден в Telegram."""

    def __init__(self, username: str):
        self.username = username
        super().__init__(
            f"Recipient not found: {username}",
            {"username": username},
        )


class InsufficientFundsError(FragmentError):
    """Недостаточно средств на кошельке для выполнения транзакции."""

    def __init__(self, required: float, available: float, currency: str = "TON"):
        self.required = required
        self.available = available
        self.currency = currency
        super().__init__(
            f"Insufficient funds: need {required} {currency}, have {available} {currency}",
            {"required": required, "available": available, "currency": currency},
        )


class TransactionError(FragmentError):
    """Ошибка при выполнении TON-транзакции."""

    def __init__(self, message: str, tx_details: dict | None = None):
        self.tx_details = tx_details
        super().__init__(message, {"transaction": tx_details})


class TransactionTimeoutError(TransactionError):
    """Таймаут при выполнении транзакции."""

    def __init__(self, timeout_seconds: float, tx_details: dict | None = None):
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Transaction timed out after {timeout_seconds}s",
            tx_details,
        )


class RateLimitError(FragmentAPIError):
    """Превышен лимит запросов к Fragment API."""

    def __init__(self, method: str, retry_after: int | None = None):
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded. Retry after {retry_after}s" if retry_after else "Rate limit exceeded",
            method=method,
            response={"error": "Rate limit", "retry_after": retry_after},
        )
