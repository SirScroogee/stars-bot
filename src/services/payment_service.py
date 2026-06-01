"""
PaymentService — бизнес-логика покупок Stars и Premium.

Этот сервис:
- Оркестрирует покупку (поиск получателя → инициализация → оплата)
- Маппит ошибки FragmentClient в статусы заказов
- Записывает транзакции

НЕ отвечает за:
- Прямые вызовы из handlers (только через OrderWorker)
- Управление статусами заказов (это OrderService)
- Идемпотентность (это OrderService + OrderWorker)
"""
import logging
from dataclasses import dataclass
from enum import Enum

from src.api_clients.fragment import (
    FragmentClient,
    FragmentAccessDeniedError,
    FragmentWalletLinkRequiredError,
    FragmentError,
    InsufficientFundsError,
    RecipientNotFoundError,
    SessionExpiredError,
    TransactionError,
    TransactionTimeoutError,
)
from src.api_clients.fragment.client import TransactionResult
from src.db.models import ProductType

logger = logging.getLogger(__name__)


class PaymentErrorType(Enum):
    """Типы ошибок оплаты для маппинга в статусы заказов."""

    RECIPIENT_NOT_FOUND = "recipient_not_found"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    SESSION_EXPIRED = "session_expired"
    ACCESS_DENIED = "access_denied"
    TRANSACTION_FAILED = "transaction_failed"
    TRANSACTION_TIMEOUT = "transaction_timeout"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class PaymentResult:
    """Результат попытки оплаты."""

    success: bool
    transactions: list[TransactionResult] | None = None
    error_type: PaymentErrorType | None = None
    error_message: str | None = None
    should_retry: bool = False


class PaymentService:
    """
    Сервис для выполнения платежей через Fragment.

    Использование (только из OrderWorker!):
        service = PaymentService(fragment_client)
        result = await service.purchase_stars(
            username="recipient",
            quantity=100,
            show_sender=False,
        )

        if result.success:
            # Записать транзакции, обновить баланс
        elif result.should_retry:
            # Вернуть в очередь
        else:
            # Пометить заказ как FAILED
    """

    def __init__(self, fragment_client: FragmentClient):
        self._client = fragment_client

    async def purchase_stars(
        self,
        username: str,
        quantity: int,
        show_sender: bool = False,
    ) -> PaymentResult:
        """
        Выполнить покупку Stars.

        Args:
            username: Username получателя
            quantity: Количество Stars
            show_sender: Показывать отправителя

        Returns:
            PaymentResult с результатом или ошибкой
        """
        logger.info(f"Starting Stars purchase: {quantity} stars to @{username}")

        try:
            # 1. Поиск получателя
            recipient = await self._client.search_stars_recipient(username)
            logger.debug(f"Recipient found: {recipient.recipient_id}")

            # 2. Инициализация запроса
            request = await self._client.init_stars_request(
                recipient.recipient_id, quantity
            )
            logger.debug(f"Request initialized: {request.req_id}")

            # 3. Выполнение оплаты
            transactions = await self._client.execute_stars_payment(
                request.req_id, show_sender
            )

            logger.info(
                f"Stars purchase completed: {len(transactions)} transactions, "
                f"total {sum(tx.amount for tx in transactions)} TON"
            )

            return PaymentResult(success=True, transactions=transactions)

        except RecipientNotFoundError as e:
            logger.warning(f"Recipient not found: {username}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.RECIPIENT_NOT_FOUND,
                error_message=str(e),
                should_retry=False,
            )

        except InsufficientFundsError as e:
            logger.error(f"Insufficient funds: need {e.required}, have {e.available}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.INSUFFICIENT_FUNDS,
                error_message=str(e),
                should_retry=True,  # Можно повторить после пополнения
            )

        except SessionExpiredError as e:
            logger.error(f"Fragment session expired: {e.method}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.SESSION_EXPIRED,
                error_message=str(e),
                should_retry=False,  # Требуется ручное обновление сессии
            )

        except FragmentAccessDeniedError as e:
            logger.error(f"Fragment access denied: {e.method}")
            error_message = (
                f"Fragment access denied on {e.method}. "
                "Check Fragment cookies/account status/IP or update session tokens."
            )
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.ACCESS_DENIED,
                error_message=error_message,
                should_retry=False,
            )

        except FragmentWalletLinkRequiredError as e:
            logger.error(f"Fragment wallet link required: {e.method}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.ACCESS_DENIED,
                error_message=str(e),
                should_retry=False,
            )

        except TransactionTimeoutError as e:
            logger.error(f"Transaction timeout: {e.timeout_seconds}s")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.TRANSACTION_TIMEOUT,
                error_message=str(e),
                should_retry=True,
            )

        except TransactionError as e:
            logger.error(f"Transaction failed: {e.message}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.TRANSACTION_FAILED,
                error_message=str(e),
                should_retry=True,
            )

        except FragmentError as e:
            logger.error(f"Fragment error: {e.message}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.UNKNOWN_ERROR,
                error_message=str(e),
                should_retry=True,
            )

        except Exception as e:
            logger.exception(f"Unexpected error during Stars purchase: {e}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.UNKNOWN_ERROR,
                error_message=str(e),
                should_retry=True,
            )

    async def purchase_premium(
        self,
        username: str,
        months: int,
        show_sender: bool = False,
    ) -> PaymentResult:
        """
        Выполнить покупку Premium.

        Args:
            username: Username получателя
            months: Количество месяцев (3, 6, 12)
            show_sender: Показывать отправителя

        Returns:
            PaymentResult с результатом или ошибкой
        """
        logger.info(f"Starting Premium purchase: {months} months to @{username}")

        try:
            # 1. Поиск получателя
            recipient = await self._client.search_premium_recipient(username)
            logger.debug(f"Recipient found: {recipient.recipient_id}")

            # 2. Инициализация запроса
            request = await self._client.init_premium_request(
                recipient.recipient_id, months
            )
            logger.debug(f"Request initialized: {request.req_id}")

            # 3. Выполнение оплаты
            transactions = await self._client.execute_premium_payment(
                request.req_id, show_sender
            )

            logger.info(
                f"Premium purchase completed: {len(transactions)} transactions, "
                f"total {sum(tx.amount for tx in transactions)} TON"
            )

            return PaymentResult(success=True, transactions=transactions)

        except RecipientNotFoundError as e:
            logger.warning(f"Recipient not found: {username}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.RECIPIENT_NOT_FOUND,
                error_message=str(e),
                should_retry=False,
            )

        except InsufficientFundsError as e:
            logger.error(f"Insufficient funds: need {e.required}, have {e.available}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.INSUFFICIENT_FUNDS,
                error_message=str(e),
                should_retry=True,
            )

        except SessionExpiredError as e:
            logger.error(f"Fragment session expired: {e.method}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.SESSION_EXPIRED,
                error_message=str(e),
                should_retry=False,
            )

        except FragmentAccessDeniedError as e:
            logger.error(f"Fragment access denied: {e.method}")
            error_message = (
                f"Fragment access denied on {e.method}. "
                "Check Fragment cookies/account status/IP or update session tokens."
            )
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.ACCESS_DENIED,
                error_message=error_message,
                should_retry=False,
            )

        except FragmentWalletLinkRequiredError as e:
            logger.error(f"Fragment wallet link required: {e.method}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.ACCESS_DENIED,
                error_message=str(e),
                should_retry=False,
            )

        except TransactionTimeoutError as e:
            logger.error(f"Transaction timeout: {e.timeout_seconds}s")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.TRANSACTION_TIMEOUT,
                error_message=str(e),
                should_retry=True,
            )

        except TransactionError as e:
            logger.error(f"Transaction failed: {e.message}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.TRANSACTION_FAILED,
                error_message=str(e),
                should_retry=True,
            )

        except FragmentError as e:
            logger.error(f"Fragment error: {e.message}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.UNKNOWN_ERROR,
                error_message=str(e),
                should_retry=True,
            )

        except Exception as e:
            logger.exception(f"Unexpected error during Premium purchase: {e}")
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.UNKNOWN_ERROR,
                error_message=str(e),
                should_retry=True,
            )

    async def execute_order(
        self,
        product_type: ProductType | str,
        recipient_username: str,
        quantity: int,
        show_sender: bool = False,
    ) -> PaymentResult:
        """
        Универсальный метод выполнения заказа.

        Args:
            product_type: Тип продукта (STARS или PREMIUM) - enum или строка
            recipient_username: Username получателя
            quantity: Количество (Stars для STARS, месяцы для PREMIUM)
            show_sender: Показывать отправителя

        Returns:
            PaymentResult
        """
        # Нормализуем тип продукта к строке для сравнения
        if isinstance(product_type, ProductType):
            product_type_str = product_type.value
        else:
            product_type_str = str(product_type)

        if product_type_str == ProductType.STARS.value:
            return await self.purchase_stars(recipient_username, quantity, show_sender)

        elif product_type_str == ProductType.PREMIUM.value:
            # quantity содержит количество месяцев (3, 6, или 12)
            return await self.purchase_premium(recipient_username, quantity, show_sender)

        else:
            return PaymentResult(
                success=False,
                error_type=PaymentErrorType.UNKNOWN_ERROR,
                error_message=f"Unknown product type: {product_type}",
                should_retry=False,
            )

    async def check_balance(self) -> float:
        """Проверить текущий баланс кошелька."""
        return await self._client.get_balance()
