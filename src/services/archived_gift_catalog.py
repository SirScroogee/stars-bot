"""Load archived Telegram Gift metadata from a public, structured catalog."""
from __future__ import annotations

import json
from dataclasses import dataclass

import aiohttp


ARCHIVED_GIFT_CATALOG_URL = (
    "https://raw.githubusercontent.com/ssamy2/TelegramGiftsAssests/"
    "refs/heads/main/gifts_api_response.json"
)
ARCHIVED_GIFT_DETAILS_URL = (
    "https://raw.githubusercontent.com/ssamy2/TelegramGiftsAssests/"
    "refs/heads/main/Gifts_Details.json"
)
ARCHIVED_GIFT_CATALOG_MAX_BYTES = 5 * 1024 * 1024
ARCHIVED_GIFT_CATALOG_MAX_ITEMS = 2_000
ARCHIVED_GIFT_MAX_ID = 2**63 - 1
ARCHIVED_GIFT_MAX_PRICE = 1_000_000_000


class ArchivedGiftCatalogError(RuntimeError):
    """The external archived Gift catalog could not be loaded or validated."""


@dataclass(frozen=True, slots=True)
class ArchivedGiftCatalogItem:
    gift_id: str
    title: str
    emoji: str
    star_count: int


def _positive_int(value: object, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        return None
    return parsed if 0 < parsed <= maximum else None


def _gift_names(details: object) -> dict[str, str]:
    if not isinstance(details, dict):
        return {}
    names: dict[str, str] = {}
    collections = (
        ("upgraded", "regular_id"),
        ("unupgraded", "id"),
        ("regular_gifts", "id"),
    )
    for collection_name, id_key in collections:
        collection = details.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection[:ARCHIVED_GIFT_CATALOG_MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            gift_id = _positive_int(item.get(id_key), maximum=ARCHIVED_GIFT_MAX_ID)
            title = item.get("full_name")
            if gift_id and isinstance(title, str) and title.strip():
                names.setdefault(str(gift_id), " ".join(title.split())[:100])
    return names


def _gift_emoji(gift: dict) -> str:
    sticker = gift.get("sticker")
    if not isinstance(sticker, dict):
        return "🎁"
    attributes = sticker.get("attributes")
    if not isinstance(attributes, list):
        return "🎁"
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        alt = attribute.get("alt")
        if isinstance(alt, str) and 0 < len(alt.strip()) <= 32:
            return " ".join(alt.split())
    return "🎁"


def parse_archived_gift_catalog(
    catalog: object,
    *,
    details: object | None = None,
) -> list[ArchivedGiftCatalogItem]:
    """Validate the external payload and return only sold-out Gift entries."""
    if not isinstance(catalog, dict):
        raise ArchivedGiftCatalogError("Каталог имеет неверный формат.")
    root = catalog.get("star_gifts_full")
    gifts = root.get("gifts") if isinstance(root, dict) else None
    if not isinstance(gifts, list):
        raise ArchivedGiftCatalogError("В каталоге отсутствует список подарков.")
    if len(gifts) > ARCHIVED_GIFT_CATALOG_MAX_ITEMS:
        raise ArchivedGiftCatalogError("Каталог содержит слишком много записей.")

    names = _gift_names(details)
    parsed: dict[str, ArchivedGiftCatalogItem] = {}
    for gift in gifts:
        if not isinstance(gift, dict) or gift.get("sold_out") is not True:
            continue
        gift_id = _positive_int(gift.get("id"), maximum=ARCHIVED_GIFT_MAX_ID)
        star_count = _positive_int(
            gift.get("stars"), maximum=ARCHIVED_GIFT_MAX_PRICE
        )
        if gift_id is None or star_count is None:
            continue
        gift_id_text = str(gift_id)
        title = gift.get("title")
        if not isinstance(title, str) or not title.strip():
            title = names.get(gift_id_text) or f"Telegram Gift {gift_id_text}"
        title = " ".join(title.split())[:100]
        parsed[gift_id_text] = ArchivedGiftCatalogItem(
            gift_id=gift_id_text,
            title=title,
            emoji=_gift_emoji(gift),
            star_count=star_count,
        )

    if not parsed:
        raise ArchivedGiftCatalogError("Каталог не содержит удалённых подарков.")
    return sorted(parsed.values(), key=lambda item: (item.title.casefold(), item.gift_id))


async def _download_json(session: aiohttp.ClientSession, url: str) -> object:
    try:
        async with session.get(url) as response:
            if response.status != 200:
                raise ArchivedGiftCatalogError(
                    f"Источник каталога вернул HTTP {response.status}."
                )
            payload = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                payload.extend(chunk)
                if len(payload) > ARCHIVED_GIFT_CATALOG_MAX_BYTES:
                    raise ArchivedGiftCatalogError("Файл каталога слишком большой.")
    except ArchivedGiftCatalogError:
        raise
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise ArchivedGiftCatalogError("Источник каталога недоступен.") from exc

    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchivedGiftCatalogError("Источник вернул повреждённый JSON.") from exc


async def fetch_archived_gift_catalog() -> list[ArchivedGiftCatalogItem]:
    """Download catalog data; optional names never make the main import fail."""
    timeout = aiohttp.ClientTimeout(total=20, connect=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        catalog = await _download_json(session, ARCHIVED_GIFT_CATALOG_URL)
        try:
            details = await _download_json(session, ARCHIVED_GIFT_DETAILS_URL)
        except ArchivedGiftCatalogError:
            details = None
    return parse_archived_gift_catalog(catalog, details=details)
