"""Access and reconciliation for the built-in retired Telegram Gift catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ArchivedGift
from src.services.archived_gift_catalog import (
    RETIRED_GIFT_CATALOG,
    ArchivedGiftCatalogItem,
)


@dataclass(frozen=True, slots=True)
class ArchivedGiftCatalogSyncResult:
    created: int
    updated: int
    removed: int


class ArchivedGiftService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_gifts(self, *, active_only: bool = False) -> list[ArchivedGift]:
        query = select(ArchivedGift)
        if active_only:
            query = query.where(ArchivedGift.is_active.is_(True))
        result = await self._session.execute(
            query.order_by(ArchivedGift.title, ArchivedGift.id)
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

    async def reconcile_catalog(
        self,
        gifts: Iterable[ArchivedGiftCatalogItem] = RETIRED_GIFT_CATALOG,
    ) -> ArchivedGiftCatalogSyncResult:
        """Make the database exactly match the curated built-in catalog."""
        catalog = {gift.gift_id: gift for gift in gifts}
        result = await self._session.execute(select(ArchivedGift))
        existing = list(result.scalars().all())
        existing_by_gift_id = {gift.gift_id: gift for gift in existing}

        removed = 0
        for gift in existing:
            if gift.gift_id not in catalog:
                await self._session.delete(gift)
                removed += 1

        created = 0
        updated = 0
        for gift_id, catalog_item in catalog.items():
            gift = existing_by_gift_id.get(gift_id)
            if gift is None:
                self._session.add(
                    ArchivedGift(
                        gift_id=catalog_item.gift_id,
                        title=catalog_item.title,
                        emoji=catalog_item.emoji,
                        star_count=catalog_item.star_count,
                        sticker_file_id=None,
                        is_active=True,
                        created_by_admin_id=None,
                    )
                )
                created += 1
                continue

            expected = {
                "title": catalog_item.title,
                "emoji": catalog_item.emoji,
                "star_count": catalog_item.star_count,
                "sticker_file_id": None,
                "is_active": True,
                "created_by_admin_id": None,
            }
            if all(getattr(gift, key) == value for key, value in expected.items()):
                continue
            for key, value in expected.items():
                setattr(gift, key, value)
            updated += 1

        if created or updated or removed:
            await self._session.commit()

        return ArchivedGiftCatalogSyncResult(
            created=created,
            updated=updated,
            removed=removed,
        )
