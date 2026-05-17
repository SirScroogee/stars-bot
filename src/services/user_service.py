"""
UserService — управление пользователями и реферальной системой.
"""
import logging
import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Order, PromoCode, PromoUse, ReferralEarning, Transaction, User, BalanceLedger
from src.services.bot_settings_service import get_bot_settings
from src.services.telegram_logger import tg_logger

logger = logging.getLogger(__name__)

# Дефолтные реферальные проценты по уровням
DEFAULT_REFERRAL_PERCENTS = {
    1: Decimal("5"),   # 5% за прямого реферала
    2: Decimal("3"),   # 3% за реферала 2 уровня
    3: Decimal("1"),   # 1% за реферала 3 уровня
}
MAX_REFERRAL_LEVELS = 3


async def get_referral_percents() -> dict[int, Decimal]:
    """Получить реферальные проценты из настроек."""
    try:
        settings = await get_bot_settings()
        return {
            1: Decimal(settings.get("referral_percent_level1", "5")),
            2: Decimal(settings.get("referral_percent_level2", "3")),
            3: Decimal(settings.get("referral_percent_level3", "1")),
        }
    except Exception:
        return DEFAULT_REFERRAL_PERCENTS.copy()


class UserService:
    """Сервис для работы с пользователями."""

    def __init__(self, session: AsyncSession):
        self._session = session

    @staticmethod
    def generate_referral_code() -> str:
        """Генерировать уникальный реферальный код (7 символов, только буквы и цифры)."""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # Без 0, O, 1, I для избежания путаницы
        return "".join(secrets.choice(alphabet) for _ in range(7))

    async def get_or_create_user(
        self,
        user_id: int,
        username: str | None = None,
        language_code: str = "ru",
        referrer_code_used: str | None = None,
    ) -> tuple[User, bool]:
        """
        Получить или создать пользователя.

        Args:
            user_id: Telegram ID
            username: Username
            language_code: Код языка
            referrer_code_used: Реферальный код, который использовал пользователь при регистрации

        Returns:
            (User, created): Пользователь и флаг создания
        """
        # Проверяем существующего пользователя
        result = await self._session.execute(
            select(User).where(User.id == user_id)
        )
        user = result.scalar_one_or_none()

        if user:
            # Обновляем username если изменился
            if username and user.username != username:
                user.username = username
                user.updated_at = datetime.utcnow()

            return user, False

        # Проверяем валидность реферального кода
        valid_referrer_code = None
        if referrer_code_used:
            referrer_result = await self._session.execute(
                select(User.id).where(User.referral_code == referrer_code_used)
            )
            referrer_row = referrer_result.first()
            if referrer_row:
                referrer_id = referrer_row[0]
                # Нельзя быть своим рефералом
                if referrer_id != user_id:
                    valid_referrer_code = referrer_code_used
                    logger.info(f"User {user_id} registered with referrer code {referrer_code_used}")

        # Генерируем уникальный реферальный код
        new_referral_code = self.generate_referral_code()
        while True:
            existing = await self._session.execute(
                select(User.id).where(User.referral_code == new_referral_code)
            )
            if not existing.first():
                break
            new_referral_code = self.generate_referral_code()

        # Создаём пользователя
        user = User(
            id=user_id,
            username=username,
            language_code=language_code,
            referrer_code=valid_referrer_code,  # Код реферала, который использовал при регистрации
            referral_code=new_referral_code,    # Собственный реферальный код
        )

        self._session.add(user)
        await self._session.flush()

        logger.info(f"User created: {user_id} (ref_code={new_referral_code})")
        return user, True

    async def get_user(self, user_id: int) -> User | None:
        """Получить пользователя по ID."""
        result = await self._session.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_user_for_update(self, user_id: int) -> User | None:
        """Получить пользователя по ID с блокировкой строки (FOR UPDATE)."""
        result = await self._session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_user_by_username(self, username: str) -> User | None:
        """Получить пользователя по username."""
        result = await self._session.execute(
            select(User).where(User.username == username)
        )
        return result.scalar_one_or_none()

    async def get_user_by_referral_code(self, referral_code: str) -> User | None:
        """Получить пользователя по его реферальному коду."""
        result = await self._session.execute(
            select(User).where(User.referral_code == referral_code)
        )
        return result.scalar_one_or_none()

    async def get_referral_chain(self, user_id: int, max_levels: int = 3) -> list[User]:
        """
        Получить цепочку рефереров (вверх по дереву).

        Returns:
            Список рефереров от ближайшего к дальнему
        """
        chain = []
        current_id = user_id

        for _ in range(max_levels):
            result = await self._session.execute(
                select(User).where(User.id == current_id)
            )
            user = result.scalar_one_or_none()

            if not user or not user.referrer_code:
                break

            # Получаем реферера по его referral_code
            referrer_result = await self._session.execute(
                select(User).where(User.referral_code == user.referrer_code)
            )
            referrer = referrer_result.scalar_one_or_none()

            if referrer:
                chain.append(referrer)
                current_id = referrer.id
            else:
                break

        return chain

    async def process_referral_earnings(
        self,
        buyer_id: int,
        order_id: int,
        amount_stars: Decimal,
    ) -> list[ReferralEarning]:
        """
        Начислить реферальные бонусы за покупку.

        Args:
            buyer_id: ID покупателя
            order_id: ID заказа
            amount_stars: Сумма покупки в Stars

        Returns:
            Список созданных начислений
        """
        earnings = []
        referrers = await self.get_referral_chain(buyer_id, MAX_REFERRAL_LEVELS)

        # Получаем проценты из настроек
        referral_percents = await get_referral_percents()

        for level, referrer in enumerate(referrers, start=1):
            percent = referral_percents.get(level, Decimal("0"))
            if percent <= 0:
                continue

            bonus = (amount_stars * percent / Decimal("100")).quantize(Decimal("0.01"))
            if bonus <= 0:
                continue

            # Сохраняем балансы до изменений
            balance_usdt_before = referrer.balance_usdt
            referral_balance_before = referrer.referral_balance

            # Создаём запись о начислении
            earning = ReferralEarning(
                referrer_id=referrer.id,
                referee_id=buyer_id,
                order_id=order_id,
                level=level,
                percent=int(percent),
                amount_stars=bonus,
            )
            self._session.add(earning)

            # Начисляем на реферальный баланс (можно вывести на USDT)
            referrer.referral_balance += bonus
            referrer.total_referral_earnings += bonus

            # Запись в ledger (реферальный бонус как USDT)
            ledger = BalanceLedger(
                user_id=referrer.id,
                operation="credit",
                amount_stars=Decimal("0"),
                amount_usdt=bonus,  # Реферальный баланс в USDT
                amount_premium=0,
                balance_stars_before=referrer.balance_stars,
                balance_stars_after=referrer.balance_stars,
                balance_usdt_before=balance_usdt_before,
                balance_usdt_after=referrer.balance_usdt,
                balance_premium_before=referrer.balance_premium_months,
                balance_premium_after=referrer.balance_premium_months,
                description=f"Реферальный бонус L{level} ({percent}%): +{bonus} USDT от заказа #{order_id}",
            )
            self._session.add(ledger)

            earnings.append(earning)

            logger.info(
                f"Referral earning: {referrer.id} got {bonus} stars "
                f"(level {level}, {percent}%) from order {order_id}"
            )

        return earnings

    async def get_referral_stats(self, user_id: int) -> dict:
        """
        Получить статистику рефералов пользователя.

        Returns:
            {
                "total_referrals": int,
                "level_1": int,
                "level_2": int,
                "level_3": int,
                "referral_balance": Decimal,
                "total_earnings": Decimal,
            }
        """
        # Получаем пользователя
        user = await self.get_user(user_id)
        if not user:
            return {
                "total_referrals": 0,
                "level_1": 0,
                "level_2": 0,
                "level_3": 0,
                "referral_balance": Decimal("0"),
                "total_earnings": Decimal("0"),
            }

        my_code = user.referral_code

        # Количество прямых рефералов (уровень 1) - те, кто использовал мой код
        level_1_result = await self._session.execute(
            select(func.count(User.id)).where(User.referrer_code == my_code)
        )
        level_1 = level_1_result.scalar() or 0

        # Рефералы уровня 2 (рефералы рефералов)
        level_2_result = await self._session.execute(
            select(func.count(User.id))
            .where(User.referrer_code.in_(
                select(User.referral_code).where(User.referrer_code == my_code)
            ))
        )
        level_2 = level_2_result.scalar() or 0

        # Рефералы уровня 3
        level_3_result = await self._session.execute(
            select(func.count(User.id))
            .where(User.referrer_code.in_(
                select(User.referral_code).where(User.referrer_code.in_(
                    select(User.referral_code).where(User.referrer_code == my_code)
                ))
            ))
        )
        level_3 = level_3_result.scalar() or 0

        return {
            "total_referrals": level_1 + level_2 + level_3,
            "level_1": level_1,
            "level_2": level_2,
            "level_3": level_3,
            "referral_balance": user.referral_balance,
            "total_earnings": user.total_referral_earnings,
        }

    async def get_referral_count(self, user_id: int) -> int:
        """Получить количество прямых рефералов."""
        user = await self.get_user(user_id)
        if not user:
            return 0

        result = await self._session.execute(
            select(func.count(User.id)).where(User.referrer_code == user.referral_code)
        )
        return result.scalar() or 0

    async def withdraw_referral_balance(self, user_id: int) -> tuple[bool, Decimal, str]:
        """
        Вывести реферальный баланс на основной USDT баланс.

        Args:
            user_id: ID пользователя

        Returns:
            (success, amount, message)
        """
        # Блокируем строку для предотвращения race condition
        user = await self.get_user_for_update(user_id)
        if not user:
            return False, Decimal("0"), "user_not_found"

        if user.referral_balance <= Decimal("0"):
            return False, Decimal("0"), "no_balance"

        amount = user.referral_balance

        # Сохраняем балансы до изменений
        balance_usdt_before = user.balance_usdt

        # Переводим с реферального на основной USDT баланс
        user.balance_usdt += amount
        user.referral_balance = Decimal("0")
        user.updated_at = datetime.utcnow()

        # Создаём транзакцию
        tx = Transaction(
            user_id=user_id,
            type="referral_withdrawal",
            amount_usdt=amount,
            description=f"Вывод реферального баланса: {amount} USDT",
        )
        self._session.add(tx)

        # Запись в ledger
        ledger = BalanceLedger(
            user_id=user_id,
            operation="credit",
            amount_stars=Decimal("0"),
            amount_usdt=amount,
            amount_premium=0,
            balance_stars_before=user.balance_stars,
            balance_stars_after=user.balance_stars,
            balance_usdt_before=balance_usdt_before,
            balance_usdt_after=user.balance_usdt,
            balance_premium_before=user.balance_premium_months,
            balance_premium_after=user.balance_premium_months,
            description=f"Вывод реферального баланса: +{amount} USDT",
        )
        self._session.add(ledger)

        logger.info(f"User {user_id} withdrew referral balance: {amount} USDT")

        return True, amount, "success"

    async def get_user_balance(self, user_id: int) -> dict:
        """Получить баланс пользователя."""
        user = await self.get_user(user_id)
        if not user:
            return {"stars": Decimal("0"), "usdt": Decimal("0")}

        return {
            "stars": user.balance_stars,
            "usdt": user.balance_usdt,
        }

    async def is_admin(self, user_id: int) -> bool:
        """Проверить, является ли пользователь админом."""
        user = await self.get_user(user_id)
        return user.is_admin if user else False

    async def is_banned(self, user_id: int) -> bool:
        """Проверить, забанен ли пользователь."""
        user = await self.get_user(user_id)
        return user.is_banned if user else False

    async def get_purchase_stats(self, user_id: int) -> dict:
        """
        Получить статистику покупок пользователя.

        Returns:
            {
                "total_deposited_usdt": Decimal,
                "total_stars_bought": int,
                "total_stars_usdt": Decimal,
                "total_premium_months": int,
                "total_premium_usdt": Decimal,
            }
        """
        # Всего пополнено USDT (из транзакций типа deposit)
        from src.db.models import Transaction
        deposited_result = await self._session.execute(
            select(func.sum(Transaction.amount_usdt))
            .where(Transaction.user_id == user_id)
            .where(Transaction.type == "deposit")
        )
        total_deposited = deposited_result.scalar() or Decimal("0")

        # Всего куплено Stars (количество и сумма USDT)
        stars_result = await self._session.execute(
            select(func.sum(Order.quantity), func.sum(Order.price_usdt))
            .where(Order.user_id == user_id)
            .where(Order.status == "completed")
            .where(Order.product_type == "stars")
        )
        stars_row = stars_result.one()
        total_stars = stars_row[0] or 0
        total_stars_usdt = stars_row[1] or Decimal("0")

        # Всего куплено месяцев Premium (количество и сумма USDT)
        premium_result = await self._session.execute(
            select(func.sum(Order.quantity), func.sum(Order.price_usdt))
            .where(Order.user_id == user_id)
            .where(Order.status == "completed")
            .where(Order.product_type == "premium")
        )
        premium_row = premium_result.one()
        total_premium_months = premium_row[0] or 0
        total_premium_usdt = premium_row[1] or Decimal("0")

        return {
            "total_deposited_usdt": total_deposited,
            "total_stars_bought": total_stars,
            "total_stars_usdt": total_stars_usdt,
            "total_premium_months": total_premium_months,
            "total_premium_usdt": total_premium_usdt,
        }

    async def update_language(self, user_id: int, language_code: str) -> bool:
        """
        Обновить язык пользователя.

        Args:
            user_id: ID пользователя
            language_code: Код языка (ru/en)

        Returns:
            True если успешно
        """
        user = await self.get_user(user_id)
        if not user:
            return False

        user.language_code = language_code
        user.updated_at = datetime.utcnow()
        return True

    async def get_user_orders(
        self,
        user_id: int,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[Order], int]:
        """
        Получить заказы пользователя с пагинацией.

        Args:
            user_id: ID пользователя
            limit: Количество записей
            offset: Смещение

        Returns:
            (список заказов, общее количество)
        """
        # Общее количество заказов
        count_result = await self._session.execute(
            select(func.count(Order.id)).where(Order.user_id == user_id)
        )
        total_count = count_result.scalar() or 0

        # Получаем заказы
        result = await self._session.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        orders = list(result.scalars().all())

        return orders, total_count

    async def get_order_by_id(self, order_id: int, user_id: int) -> Order | None:
        """
        Получить заказ по ID (только если принадлежит пользователю).

        Args:
            order_id: ID заказа
            user_id: ID пользователя

        Returns:
            Order или None
        """
        result = await self._session.execute(
            select(Order)
            .where(Order.id == order_id)
            .where(Order.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_user_history(
        self,
        user_id: int,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """
        Получить историю заказов пользователя (только покупки stars/premium).

        Returns:
            (список записей, общее количество)
            Каждая запись: {"type": "order", "data": Order, "date": datetime}
        """
        # Получаем только заказы (покупки stars/premium)
        orders_result = await self._session.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
        )
        orders = list(orders_result.scalars().all())

        # Формируем историю
        history = []
        for order in orders:
            history.append({
                "type": "order",
                "data": order,
                "date": order.created_at,
            })

        total_count = len(history)

        # Пагинация
        paginated = history[offset:offset + limit]

        return paginated, total_count

    async def get_transaction_by_id(self, tx_id: int, user_id: int) -> Transaction | None:
        """Получить транзакцию по ID."""
        result = await self._session.execute(
            select(Transaction)
            .where(Transaction.id == tx_id)
            .where(Transaction.user_id == user_id)
        )
        return result.scalar_one_or_none()

    # ==================== ПРОМОКОДЫ ====================

    async def get_promo_code(self, code: str, for_update: bool = False) -> PromoCode | None:
        """Получить промокод по коду."""
        query = select(PromoCode).where(PromoCode.code == code.upper())
        if for_update:
            query = query.with_for_update()
        result = await self._session.execute(query)
        return result.scalar_one_or_none()

    async def has_used_promo(self, user_id: int, promo_id: int) -> bool:
        """Проверить, использовал ли пользователь промокод."""
        result = await self._session.execute(
            select(PromoUse)
            .where(PromoUse.user_id == user_id)
            .where(PromoUse.promo_id == promo_id)
        )
        return result.scalar_one_or_none() is not None

    async def activate_promo_code(
        self, user_id: int, code: str
    ) -> tuple[bool, str, Decimal | None]:
        """
        Активировать промокод для пользователя.

        Args:
            user_id: ID пользователя
            code: Код промокода

        Returns:
            (success, message_key, bonus_amount)
            message_key: ключ для локализации (not_found, expired, limit_reached,
                        already_used, success_stars, user_not_found)
        """
        # 1. Найти промокод с блокировкой строки (FOR UPDATE) для предотвращения race condition
        promo = await self.get_promo_code(code, for_update=True)
        if not promo:
            return False, "not_found", None

        # 2. Проверить активность
        if not promo.is_active:
            return False, "not_active", None

        # 3. Проверить срок действия
        if promo.expires_at and promo.expires_at < datetime.utcnow():
            return False, "expired", None

        # 4. Проверить лимит использований (теперь безопасно благодаря FOR UPDATE)
        if promo.max_uses is not None and promo.current_uses >= promo.max_uses:
            return False, "limit_reached", None

        # 5. Проверить что пользователь не использовал
        if await self.has_used_promo(user_id, promo.id):
            return False, "already_used", None

        # 6. Получить пользователя
        user = await self.get_user(user_id)
        if not user:
            return False, "user_not_found", None

        # 7. Определить тип бонуса и начислить
        bonus_applied = Decimal("0")
        message_key = "success"
        bonus_value = None

        # Сохраняем балансы до изменений для ledger
        balance_stars_before = user.balance_stars
        balance_usdt_before = user.balance_usdt
        balance_premium_before = user.balance_premium_months

        if promo.bonus_stars > 0:
            # Начислить звёзды на баланс
            user.balance_stars += promo.bonus_stars
            bonus_applied = promo.bonus_stars
            bonus_value = int(promo.bonus_stars)
            message_key = "success_stars"

            tx = Transaction(
                user_id=user_id,
                type="promo",
                amount_stars=promo.bonus_stars,
                amount_usdt=Decimal("0"),
                description=f"Промокод {promo.code}: +{int(promo.bonus_stars)} Stars",
                extra_data=f'{{"code": "{promo.code}", "type": "stars"}}',
            )
            self._session.add(tx)

            # Запись в ledger
            ledger = BalanceLedger(
                user_id=user_id,
                operation="credit",
                amount_stars=promo.bonus_stars,
                amount_usdt=Decimal("0"),
                amount_premium=0,
                balance_stars_before=balance_stars_before,
                balance_stars_after=user.balance_stars,
                balance_usdt_before=balance_usdt_before,
                balance_usdt_after=user.balance_usdt,
                balance_premium_before=balance_premium_before,
                balance_premium_after=user.balance_premium_months,
                description=f"Промокод {promo.code}: +{int(promo.bonus_stars)} Stars",
            )
            self._session.add(ledger)
            logger.info(f"User {user_id} activated promo {promo.code}: +{promo.bonus_stars} Stars")

        elif promo.bonus_usdt > 0:
            # Начислить USDT на баланс
            user.balance_usdt += promo.bonus_usdt
            bonus_applied = promo.bonus_usdt
            bonus_value = float(promo.bonus_usdt)
            message_key = "success_usdt"

            tx = Transaction(
                user_id=user_id,
                type="promo",
                amount_stars=Decimal("0"),
                amount_usdt=promo.bonus_usdt,
                description=f"Промокод {promo.code}: +{promo.bonus_usdt} USDT",
                extra_data=f'{{"code": "{promo.code}", "type": "usdt"}}',
            )
            self._session.add(tx)

            # Запись в ledger
            ledger = BalanceLedger(
                user_id=user_id,
                operation="credit",
                amount_stars=Decimal("0"),
                amount_usdt=promo.bonus_usdt,
                amount_premium=0,
                balance_stars_before=balance_stars_before,
                balance_stars_after=user.balance_stars,
                balance_usdt_before=balance_usdt_before,
                balance_usdt_after=user.balance_usdt,
                balance_premium_before=balance_premium_before,
                balance_premium_after=user.balance_premium_months,
                description=f"Промокод {promo.code}: +{promo.bonus_usdt} USDT",
            )
            self._session.add(ledger)
            logger.info(f"User {user_id} activated promo {promo.code}: +{promo.bonus_usdt} USDT")

        elif promo.bonus_premium > 0:
            # Начислить Premium месяцы
            user.balance_premium_months += promo.bonus_premium
            bonus_applied = Decimal(str(promo.bonus_premium))
            bonus_value = promo.bonus_premium
            message_key = "success_premium"

            tx = Transaction(
                user_id=user_id,
                type="promo",
                amount_stars=Decimal("0"),
                amount_usdt=Decimal("0"),
                description=f"Промокод {promo.code}: +{promo.bonus_premium} мес. Premium",
                extra_data=f'{{"code": "{promo.code}", "type": "premium", "months": {promo.bonus_premium}}}',
            )
            self._session.add(tx)

            # Запись в ledger
            ledger = BalanceLedger(
                user_id=user_id,
                operation="credit",
                amount_stars=Decimal("0"),
                amount_usdt=Decimal("0"),
                amount_premium=promo.bonus_premium,
                balance_stars_before=balance_stars_before,
                balance_stars_after=user.balance_stars,
                balance_usdt_before=balance_usdt_before,
                balance_usdt_after=user.balance_usdt,
                balance_premium_before=balance_premium_before,
                balance_premium_after=user.balance_premium_months,
                description=f"Промокод {promo.code}: +{promo.bonus_premium} мес. Premium",
            )
            self._session.add(ledger)
            logger.info(f"User {user_id} activated promo {promo.code}: +{promo.bonus_premium} Premium months")

        else:
            return False, "no_bonus", None

        # 8. Создать запись использования
        promo_use = PromoUse(
            promo_id=promo.id,
            user_id=user_id,
            bonus_applied=bonus_applied,
        )
        self._session.add(promo_use)

        # 9. Увеличить счётчик использований
        promo.current_uses += 1

        # 10. Обновить время
        user.updated_at = datetime.utcnow()

        return True, message_key, bonus_value

    async def create_promo_code(
        self,
        code: str,
        bonus_stars: Decimal = Decimal("0"),
        bonus_usdt: Decimal = Decimal("0"),
        bonus_premium: int = 0,
        max_uses: int | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[PromoCode | None, str]:
        """
        Создать промокод на звёзды, USDT или Premium.

        Returns:
            (PromoCode, message_key)
            message_key: "success" | "already_exists" | "invalid"
        """
        code = code.upper().strip()

        if len(code) < 3:
            return None, "invalid"

        # Проверить существование
        existing = await self.get_promo_code(code)
        if existing:
            return None, "already_exists"

        promo = PromoCode(
            code=code,
            bonus_stars=bonus_stars,
            bonus_usdt=bonus_usdt,
            bonus_premium=bonus_premium,
            bonus_percent=0,  # deprecated
            max_uses=max_uses,
            expires_at=expires_at,
            is_active=True,
        )
        self._session.add(promo)
        await self._session.flush()

        # Формируем текст бонуса для лога
        if bonus_stars > 0:
            bonus_text = f"+{int(bonus_stars)} Stars"
        elif bonus_usdt > 0:
            bonus_text = f"+{bonus_usdt} USDT"
        elif bonus_premium > 0:
            bonus_text = f"+{bonus_premium} мес. Premium"
        else:
            bonus_text = "без бонуса"

        logger.info(f"Promo code created: {code} ({bonus_text})")

        # Логируем в Telegram
        await tg_logger.log_promo_created(
            promo_code=code,
            bonus_stars=bonus_stars,
            bonus_usdt=bonus_usdt,
            bonus_premium=bonus_premium,
            max_uses=max_uses,
        )

        return promo, "success"

    async def get_all_promo_codes(self) -> list[PromoCode]:
        """Получить все промокоды."""
        from sqlalchemy import desc as sql_desc
        result = await self._session.execute(
            select(PromoCode).order_by(sql_desc(PromoCode.created_at))
        )
        return list(result.scalars().all())

    async def get_promo_by_id(self, promo_id: int) -> PromoCode | None:
        """Получить промокод по ID."""
        result = await self._session.execute(
            select(PromoCode).where(PromoCode.id == promo_id)
        )
        return result.scalar_one_or_none()

    async def toggle_promo_code(self, promo_id: int) -> bool:
        """Переключить активность промокода."""
        promo = await self.get_promo_by_id(promo_id)
        if not promo:
            return False

        promo.is_active = not promo.is_active
        return True

    async def delete_promo_code(self, promo_id: int) -> bool:
        """Удалить промокод и все его использования."""
        promo = await self.get_promo_by_id(promo_id)
        if not promo:
            return False

        # Удаляем все использования промокода
        await self._session.execute(
            delete(PromoUse).where(PromoUse.promo_id == promo_id)
        )

        # Удаляем сам промокод
        await self._session.delete(promo)
        logger.info(f"Promo code deleted: {promo.code}")
        return True
