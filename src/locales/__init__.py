"""
Система локализации бота.
"""
import os
from pathlib import Path
from typing import Any

import yaml


# Поддерживаемые языки
SUPPORTED_LANGUAGES = ["ru", "en"]
DEFAULT_LANGUAGE = "ru"

# Кэш загруженных переводов
_translations: dict[str, dict] = {}


def _load_translations() -> None:
    """Загрузить все переводы из YAML файлов."""
    global _translations

    locales_dir = Path(__file__).parent

    for lang in SUPPORTED_LANGUAGES:
        file_path = locales_dir / f"{lang}.yml"
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as f:
                _translations[lang] = yaml.safe_load(f) or {}
        else:
            _translations[lang] = {}


def get_text(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """
    Получить текст по ключу для указанного языка.

    Args:
        key: Ключ в формате "section.subsection.key" (например, "menu.welcome")
        lang: Код языка (ru, en)
        **kwargs: Параметры для форматирования строки

    Returns:
        Переведённая строка или ключ, если перевод не найден
    """
    # Загружаем переводы если ещё не загружены
    if not _translations:
        _load_translations()

    # Нормализуем язык
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE

    # Получаем словарь переводов для языка
    translations = _translations.get(lang, {})

    # Разбираем ключ по точкам
    keys = key.split(".")
    value: Any = translations

    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            # Если ключ не найден, пробуем дефолтный язык
            if lang != DEFAULT_LANGUAGE:
                return get_text(key, DEFAULT_LANGUAGE, **kwargs)
            return key

    # Если значение не строка, возвращаем ключ
    if not isinstance(value, str):
        return key

    # Форматируем строку если есть параметры
    if kwargs:
        try:
            value = value.format(**kwargs)
        except KeyError:
            pass

    return value.strip()


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Сокращённый алиас для get_text."""
    return get_text(key, lang, **kwargs)


class Localization:
    """Класс для работы с локализацией для конкретного пользователя."""

    def __init__(self, lang: str = DEFAULT_LANGUAGE):
        self.lang = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    def get(self, key: str, **kwargs) -> str:
        """Получить текст для текущего языка."""
        return get_text(key, self.lang, **kwargs)

    def __call__(self, key: str, **kwargs) -> str:
        """Позволяет использовать экземпляр как функцию."""
        return self.get(key, **kwargs)


def get_user_locale(language_code: str | None) -> str:
    """
    Определить язык пользователя по коду из Telegram.

    Args:
        language_code: Код языка из Telegram (message.from_user.language_code)

    Returns:
        Код поддерживаемого языка
    """
    if not language_code:
        return DEFAULT_LANGUAGE

    # Берём первые 2 символа (ru-RU -> ru)
    lang = language_code[:2].lower()

    if lang in SUPPORTED_LANGUAGES:
        return lang

    return DEFAULT_LANGUAGE


def pluralize_months(n: int, lang: str = DEFAULT_LANGUAGE) -> str:
    """
    Склонение слова "месяц" в зависимости от числа.

    Args:
        n: Количество месяцев
        lang: Код языка

    Returns:
        Строка вида "3 месяца", "6 месяцев" и т.д.
    """
    if lang == "en":
        if n == 1:
            return f"{n} month"
        return f"{n} months"

    # Русский язык
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} месяц"
    elif 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return f"{n} месяца"
    else:
        return f"{n} месяцев"


# Загружаем переводы при импорте модуля
_load_translations()
