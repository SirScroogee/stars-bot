"""Curated Telegram gifts that are retired, unlimited and non-upgradable."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArchivedGiftCatalogItem:
    gift_id: str
    title: str
    emoji: str
    star_count: int = 50


RETIRED_GIFT_CATALOG = (
    ArchivedGiftCatalogItem(
        "5800655655995968830",
        "Белый мишка с сердцем",
        "🧸",
    ),
    ArchivedGiftCatalogItem(
        "5801108895304779062",
        "Сердце «I Love You»",
        "❤️",
    ),
    ArchivedGiftCatalogItem(
        "5866352046986232958",
        "Розовый мишка с цветами",
        "🌸",
    ),
    ArchivedGiftCatalogItem(
        "5893356958802511476",
        "Ирландский мишка",
        "🍀",
    ),
    ArchivedGiftCatalogItem(
        "5922558454332916696",
        "Новогодняя ёлка",
        "🎄",
    ),
    ArchivedGiftCatalogItem(
        "5935895822435615975",
        "Мишка-клоун",
        "🤡",
    ),
    ArchivedGiftCatalogItem(
        "5956217000635139069",
        "Новогодний мишка",
        "🎅",
    ),
    ArchivedGiftCatalogItem(
        "5969796561943660080",
        "Пасхальный мишка",
        "🐰",
    ),
    ArchivedGiftCatalogItem(
        "5974210632977745012",
        "Футбольный мишка",
        "⚽",
    ),
    ArchivedGiftCatalogItem(
        "6026193266406327981",
        "Первомайский мишка",
        "🔨",
    ),
    ArchivedGiftCatalogItem(
        "6046178578163303744",
        "Мишка с бомбой",
        "🧸",
    ),
)
