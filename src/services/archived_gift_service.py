"""CRUD operations for administrator-managed archived Telegram Gifts."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ArchivedGift


class ArchivedGiftAlreadyExistsError(ValueError):
    """The Telegram gift ID is already present in the archived catalog."""


class ArchivedGiftService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_gifts(self, *, active_only: bool = False) -> list[ArchivedGift]:
        query = select(ArchivedGift)
        if active_only:
            query = query.where(ArchivedGift.is_active.is_(True))
        result = await self._session.execute(
            query.order_by(
                ArchivedGift.is_active.desc(),
                ArchivedGift.title,
                ArchivedGift.id,
            )
        )
        return list(result.scalars().all())

    async def get(self, archived_gift_id: int) -> ArchivedGift | None:
        result = await self._session.execute(
            select(ArchivedGift).where(ArchivedGift.id == archived_gift_id)
        )
        return result.scalar_one_or_none()

    async def get_by_gift_id(self, gift_id: str) -> ArchivedGift | None:
        result = await self._session.execute(
            select(ArchivedGift).where(ArchivedGift.gift_id == gift_id)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        gift_id: str,
        title: str,
        emoji: str | None,
        star_count: int,
        sticker_file_id: str | None,
        admin_id: int,
    ) -> ArchivedGift:
        gift = ArchivedGift(
            gift_id=gift_id,
            title=title,
            emoji=emoji,
            star_count=star_count,
            sticker_file_id=sticker_file_id,
            is_active=True,
            created_by_admin_id=admin_id,
        )
        self._session.add(gift)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ArchivedGiftAlreadyExistsError(gift_id) from exc
        return gift

    async def update_fields(self, archived_gift_id: int, **values) -> ArchivedGift:
        gift = await self.get(archived_gift_id)
        if gift is None:
            raise LookupError("Archived gift not found")
        allowed = {"title", "emoji", "star_count", "sticker_file_id"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported archived gift fields: {sorted(unknown)}")
        for key, value in values.items():
            setattr(gift, key, value)
        gift.updated_at = datetime.utcnow()
        await self._session.commit()
        return gift

    async def set_active(
        self, archived_gift_id: int, *, is_active: bool
    ) -> ArchivedGift:
        gift = await self.get(archived_gift_id)
        if gift is None:
            raise LookupError("Archived gift not found")
        gift.is_active = is_active
        gift.updated_at = datetime.utcnow()
        await self._session.commit()
        return gift

    async def delete(self, archived_gift_id: int) -> ArchivedGift:
        gift = await self.get(archived_gift_id)
        if gift is None:
            raise LookupError("Archived gift not found")
        await self._session.delete(gift)
        await self._session.commit()
        return gift
