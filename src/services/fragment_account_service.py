"""
FragmentAccountService — управление аккаунтами Fragment для покупки Stars и Premium.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.crypto import decrypt_mnemonic, encrypt_mnemonic
from src.db.models import FragmentAccount, FragmentAccountStatus

logger = logging.getLogger(__name__)


@dataclass
class FragmentAccountData:
    """Расшифрованные данные аккаунта Fragment."""

    id: int
    name: str
    tonapi_key: str
    mnemonic: list[str]
    wallet_address: Optional[str]
    fragment_hash: str
    stel_token: str
    stel_ssid: str
    stel_ton_token: str
    stel_dt: str
    status: str
    is_active: bool
    priority: int
    ton_balance: Optional[Decimal]


class FragmentAccountService:
    """Сервис для работы с аккаунтами Fragment."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_account(
        self,
        name: str,
        tonapi_key: str,
        mnemonic: list[str],
        fragment_hash: str,
        stel_token: str,
        stel_ssid: str,
        stel_ton_token: str,
        stel_dt: str = "-300",
        wallet_address: Optional[str] = None,
        priority: int = 0,
    ) -> FragmentAccount:
        """
        Создать новый аккаунт Fragment.

        Args:
            name: Понятное имя аккаунта
            tonapi_key: API ключ для TON
            mnemonic: Мнемоника кошелька (24 слова)
            fragment_hash: Hash для Fragment API
            stel_token: Токен сессии Fragment
            stel_ssid: ID сессии Fragment
            stel_ton_token: TON токен сессии Fragment
            wallet_address: Адрес кошелька (опционально)
            priority: Приоритет (0 = самый высокий)

        Returns:
            Созданный аккаунт
        """
        if len(mnemonic) != 24:
            raise ValueError("Mnemonic must contain 24 words")

        account = FragmentAccount(
            name=name,
            tonapi_key=tonapi_key,
            mnemonic_encrypted=encrypt_mnemonic(mnemonic),
            wallet_address=wallet_address,
            fragment_hash=fragment_hash,
            stel_token=stel_token,
            stel_ssid=stel_ssid,
            stel_ton_token=stel_ton_token,
            stel_dt=stel_dt,
            status=FragmentAccountStatus.ACTIVE.value,
            is_active=True,
            priority=priority,
        )

        self._session.add(account)
        await self._session.flush()

        logger.info(f"Fragment account created: {account.id} ({name})")
        return account

    async def get_account(self, account_id: int) -> Optional[FragmentAccount]:
        """Получить аккаунт по ID."""
        result = await self._session.execute(
            select(FragmentAccount).where(FragmentAccount.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_account_data(self, account_id: int) -> Optional[FragmentAccountData]:
        """
        Получить расшифрованные данные аккаунта.

        Returns:
            FragmentAccountData с расшифрованными токенами и мнемоникой
        """
        account = await self.get_account(account_id)
        if not account:
            return None

        return FragmentAccountData(
            id=account.id,
            name=account.name,
            tonapi_key=account.tonapi_key,
            mnemonic=decrypt_mnemonic(account.mnemonic_encrypted),
            wallet_address=account.wallet_address,
            fragment_hash=account.fragment_hash,
            stel_token=account.stel_token,
            stel_ssid=account.stel_ssid,
            stel_ton_token=account.stel_ton_token,
            stel_dt=account.stel_dt or "-300",
            status=account.status,
            is_active=account.is_active,
            priority=account.priority,
            ton_balance=account.ton_balance,
        )

    async def get_all_accounts(self) -> list[FragmentAccount]:
        """Получить все аккаунты."""
        result = await self._session.execute(
            select(FragmentAccount).order_by(FragmentAccount.priority, FragmentAccount.id)
        )
        return list(result.scalars().all())

    async def get_active_accounts(self) -> list[FragmentAccount]:
        """Получить только активные аккаунты."""
        result = await self._session.execute(
            select(FragmentAccount)
            .where(FragmentAccount.is_active == True)
            .where(FragmentAccount.status == FragmentAccountStatus.ACTIVE.value)
            .order_by(FragmentAccount.priority, FragmentAccount.id)
        )
        return list(result.scalars().all())

    async def get_best_account(self) -> Optional[FragmentAccountData]:
        """
        Получить лучший аккаунт для выполнения заказа.

        Выбирает активный аккаунт с наивысшим приоритетом и достаточным балансом.

        Returns:
            FragmentAccountData или None если нет доступных аккаунтов
        """
        result = await self._session.execute(
            select(FragmentAccount)
            .where(FragmentAccount.is_active == True)
            .where(FragmentAccount.status == FragmentAccountStatus.ACTIVE.value)
            .order_by(FragmentAccount.priority, FragmentAccount.id)
            .limit(1)
        )
        account = result.scalar_one_or_none()

        if not account:
            return None

        # Сразу возвращаем данные без повторного запроса
        return FragmentAccountData(
            id=account.id,
            name=account.name,
            tonapi_key=account.tonapi_key,
            mnemonic=decrypt_mnemonic(account.mnemonic_encrypted),
            wallet_address=account.wallet_address,
            fragment_hash=account.fragment_hash,
            stel_token=account.stel_token,
            stel_ssid=account.stel_ssid,
            stel_ton_token=account.stel_ton_token,
            stel_dt=account.stel_dt or "-300",
            status=account.status,
            is_active=account.is_active,
            priority=account.priority,
            ton_balance=account.ton_balance,
        )

    async def get_all_active_accounts(self) -> list[FragmentAccountData]:
        """
        Получить все активные аккаунты для балансировки нагрузки.

        Используется OrderWorker для warmth-based выбора аккаунта.

        Returns:
            Список FragmentAccountData всех активных аккаунтов
        """
        result = await self._session.execute(
            select(FragmentAccount)
            .where(FragmentAccount.is_active == True)
            .where(FragmentAccount.status == FragmentAccountStatus.ACTIVE.value)
            .order_by(FragmentAccount.priority, FragmentAccount.id)
        )
        accounts = result.scalars().all()

        account_data_list = []
        for account in accounts:
            data = await self.get_account_data(account.id)
            if data:
                account_data_list.append(data)

        return account_data_list

    async def update_account(
        self,
        account_id: int,
        name: Optional[str] = None,
        tonapi_key: Optional[str] = None,
        mnemonic: Optional[list[str]] = None,
        fragment_hash: Optional[str] = None,
        stel_token: Optional[str] = None,
        stel_ssid: Optional[str] = None,
        stel_ton_token: Optional[str] = None,
        stel_dt: Optional[str] = None,
        wallet_address: Optional[str] = None,
        priority: Optional[int] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[FragmentAccount]:
        """
        Обновить данные аккаунта.

        Передайте только те поля, которые нужно обновить.
        """
        account = await self.get_account(account_id)
        if not account:
            return None

        if name is not None:
            account.name = name
        if tonapi_key is not None:
            account.tonapi_key = tonapi_key
        if mnemonic is not None:
            if len(mnemonic) != 24:
                raise ValueError("Mnemonic must contain 24 words")
            account.mnemonic_encrypted = encrypt_mnemonic(mnemonic)
        if fragment_hash is not None:
            account.fragment_hash = fragment_hash
        if stel_token is not None:
            account.stel_token = stel_token
        if stel_ssid is not None:
            account.stel_ssid = stel_ssid
        if stel_ton_token is not None:
            account.stel_ton_token = stel_ton_token
        if stel_dt is not None:
            account.stel_dt = stel_dt
        if wallet_address is not None:
            account.wallet_address = wallet_address
        if priority is not None:
            account.priority = priority
        if is_active is not None:
            account.is_active = is_active

        account.updated_at = datetime.utcnow()

        logger.info(f"Fragment account updated: {account_id}")
        return account

    async def update_session_tokens(
        self,
        account_id: int,
        stel_token: str,
        stel_ssid: str,
        stel_ton_token: str,
        stel_dt: str = "-300",
    ) -> bool:
        """
        Обновить только сессионные токены аккаунта.

        Удобно для обновления сессии без изменения остальных данных.
        """
        account = await self.get_account(account_id)
        if not account:
            return False

        account.stel_token = stel_token
        account.stel_ssid = stel_ssid
        account.stel_ton_token = stel_ton_token
        account.stel_dt = stel_dt
        account.status = FragmentAccountStatus.ACTIVE.value
        account.is_active = True
        account.error_message = None
        account.last_session_check = datetime.utcnow()
        account.updated_at = datetime.utcnow()

        logger.info(f"Fragment account {account_id}: session tokens updated")
        return True

    async def set_status(
        self,
        account_id: int,
        status: FragmentAccountStatus,
        error_message: Optional[str] = None,
    ) -> bool:
        """Установить статус аккаунта."""
        account = await self.get_account(account_id)
        if not account:
            return False

        account.status = status.value
        account.error_message = error_message
        account.updated_at = datetime.utcnow()

        if status == FragmentAccountStatus.SESSION_EXPIRED:
            account.is_active = False

        logger.info(f"Fragment account {account_id}: status changed to {status.value}")
        return True

    async def mark_session_expired(self, account_id: int, error: str = None) -> bool:
        """Пометить сессию как истёкшую."""
        return await self.set_status(
            account_id,
            FragmentAccountStatus.SESSION_EXPIRED,
            error or "Session expired",
        )

    async def update_balance(
        self, account_id: int, ton_balance: Decimal
    ) -> bool:
        """Обновить баланс TON на аккаунте."""
        account = await self.get_account(account_id)
        if not account:
            return False

        account.ton_balance = ton_balance
        account.last_balance_check = datetime.utcnow()
        account.updated_at = datetime.utcnow()

        # Если баланс слишком низкий, помечаем статус
        min_balance = Decimal("0.5")  # Минимум 0.5 TON для операций
        if ton_balance < min_balance:
            account.status = FragmentAccountStatus.LOW_BALANCE.value
            logger.warning(
                f"Fragment account {account_id}: low balance ({ton_balance} TON)"
            )
        elif account.status == FragmentAccountStatus.LOW_BALANCE.value:
            # Восстанавливаем статус если баланс пополнен
            account.status = FragmentAccountStatus.ACTIVE.value
            account.is_active = True

        return True

    async def increment_order_stats(
        self,
        account_id: int,
        success: bool,
        ton_spent: Decimal = Decimal("0"),
    ) -> bool:
        """
        Обновить статистику заказов аккаунта.

        Args:
            account_id: ID аккаунта
            success: True если заказ успешен
            ton_spent: Потрачено TON
        """
        account = await self.get_account(account_id)
        if not account:
            return False

        account.total_orders += 1
        if success:
            account.successful_orders += 1
            account.total_ton_spent += ton_spent
        else:
            account.failed_orders += 1

        account.updated_at = datetime.utcnow()
        return True

    async def delete_account(self, account_id: int) -> bool:
        """Удалить аккаунт."""
        account = await self.get_account(account_id)
        if not account:
            return False

        await self._session.delete(account)
        logger.info(f"Fragment account deleted: {account_id}")
        return True

    async def toggle_account(self, account_id: int) -> Optional[bool]:
        """
        Переключить активность аккаунта.

        Returns:
            Новое значение is_active или None если аккаунт не найден
        """
        account = await self.get_account(account_id)
        if not account:
            return None

        account.is_active = not account.is_active
        account.updated_at = datetime.utcnow()

        logger.info(
            f"Fragment account {account_id}: is_active = {account.is_active}"
        )
        return account.is_active

    async def get_account_count(self) -> int:
        """Получить общее количество аккаунтов."""
        result = await self._session.execute(
            select(func.count(FragmentAccount.id))
        )
        return result.scalar() or 0

    async def get_active_account_count(self) -> int:
        """Получить количество активных аккаунтов."""
        result = await self._session.execute(
            select(func.count(FragmentAccount.id))
            .where(FragmentAccount.is_active == True)
            .where(FragmentAccount.status == FragmentAccountStatus.ACTIVE.value)
        )
        return result.scalar() or 0

    async def get_stats(self) -> dict:
        """
        Получить общую статистику по аккаунтам.

        Returns:
            {
                "total": int,
                "active": int,
                "session_expired": int,
                "low_balance": int,
                "total_orders": int,
                "successful_orders": int,
                "total_ton_spent": Decimal,
            }
        """
        accounts = await self.get_all_accounts()

        stats = {
            "total": len(accounts),
            "active": 0,
            "session_expired": 0,
            "low_balance": 0,
            "disabled": 0,
            "total_orders": 0,
            "successful_orders": 0,
            "failed_orders": 0,
            "total_ton_spent": Decimal("0"),
        }

        for acc in accounts:
            if acc.status == FragmentAccountStatus.ACTIVE.value and acc.is_active:
                stats["active"] += 1
            elif acc.status == FragmentAccountStatus.SESSION_EXPIRED.value:
                stats["session_expired"] += 1
            elif acc.status == FragmentAccountStatus.LOW_BALANCE.value:
                stats["low_balance"] += 1
            elif acc.status == FragmentAccountStatus.DISABLED.value:
                stats["disabled"] += 1

            stats["total_orders"] += acc.total_orders
            stats["successful_orders"] += acc.successful_orders
            stats["failed_orders"] += acc.failed_orders
            stats["total_ton_spent"] += acc.total_ton_spent or Decimal("0")

        return stats
