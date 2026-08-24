"""
Handlers для inline-режима бота (калькулятор звёзд и Premium).
"""
import html
import logging
import re
from decimal import Decimal

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import select

from src.db.models import Check
from src.db.session import async_session_factory
from src.locales import t, get_user_locale
from src.services.user_service import UserService
from src.services.user_registration_service import finalize_new_user_registration
from src.services.bot_settings_service import get_bot_settings
from src.services.rates_service import (
    get_rates as get_rates_from_service,
    convert_usdt_to_currency as convert_usdt_service,
    convert_currency_to_usdt as convert_currency_service,
    format_currency,
)

logger = logging.getLogger(__name__)

router = Router(name="inline")

# Допустимые сроки Premium
PREMIUM_DURATIONS = [3, 6, 12]

# Валюты
CURRENCIES = ["usdt", "ton", "rub"]


async def get_price_settings() -> dict:
    """Получить настройки цен из БД."""
    settings = await get_bot_settings()
    return {
        "min_stars": int(settings.get("min_stars", 50)),
        "max_stars": int(settings.get("max_stars", 10000)),
        "star_price_usdt": Decimal(str(settings.get("star_price_usdt", "0.02"))),
        "premium_prices": {
            3: Decimal(str(settings.get("premium_price_3m", "8.99"))),
            6: Decimal(str(settings.get("premium_price_6m", "15.99"))),
            12: Decimal(str(settings.get("premium_price_12m", "28.99"))),
        },
    }


async def get_rates() -> dict[str, Decimal]:
    """Получить курсы валют и преобразовать к формату для inline."""
    rates = await get_rates_from_service()
    # Преобразуем ключи из rates_service в формат inline
    return {
        "usdt": rates.get("usdt", Decimal("1")),
        "ton": rates.get("ton_usd", Decimal("5.5")),
        "rub": rates.get("usd_rub", Decimal("100")),
    }


def convert_usdt_to_currency(usdt_amount: Decimal, currency: str, rates: dict) -> Decimal:
    """Конвертировать USDT в другую валюту."""
    if currency == "usdt":
        return usdt_amount
    elif currency == "ton":
        ton_rate = rates.get("ton", Decimal("5.5"))
        return usdt_amount / ton_rate
    elif currency == "rub":
        rub_rate = rates.get("rub", Decimal("100"))
        return usdt_amount * rub_rate
    return usdt_amount


def convert_currency_to_usdt(amount: Decimal, currency: str, rates: dict) -> Decimal:
    """Конвертировать валюту в USDT."""
    if currency == "usdt":
        return amount
    elif currency == "ton":
        ton_rate = rates.get("ton", Decimal("5.5"))
        return amount * ton_rate
    elif currency == "rub":
        rub_rate = rates.get("rub", Decimal("100"))
        return amount / rub_rate
    return amount


@router.inline_query()
async def inline_calculator(inline_query: InlineQuery) -> None:
    """
    Inline калькулятор звёзд и Premium.

    Форматы запросов:
    - Stars: 100s, 100 stars, 100 stars usdt/ton/rub
    - Premium: 3p, 3 premium, 3 premium usdt/ton/rub
    - Currency to Stars: 100 usdt/ton/rub stars
    """
    user = inline_query.from_user
    query = inline_query.query.strip().lower()

    # Определяем язык пользователя
    lang = get_user_locale(user.language_code)

    # Получаем язык из БД если пользователь существует
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if db_user:
            lang = db_user.language_code or lang
        else:
            # Если пользователя нет в БД, создаём его
            db_user, created = await user_service.get_or_create_user(
                user_id=user.id,
                username=user.username,
                language_code=lang,
            )
            await session.commit()
            if created:
                await finalize_new_user_registration(
                    user_id=user.id,
                    username=user.username,
                    language=db_user.language_code or lang,
                )

    # Получаем username бота для ссылок
    bot_info = await inline_query.bot.get_me()
    bot_username = bot_info.username

    # Получаем настройки цен из БД
    price_settings = await get_price_settings()
    min_stars = price_settings["min_stars"]
    max_stars = price_settings["max_stars"]

    # Получаем курсы валют (нужны для всех запросов)
    rates = await get_rates()

    # Если пустой запрос - показываем курсы 1 звезды
    if not query:
        await _show_instruction(inline_query, bot_username, lang, price_settings, rates)
        return

    # 0. Check sharing: код чека напрямую (только буквы и цифры, 6-16 символов)
    # Проверяем что это не другой формат (stars, premium, currency)
    if re.match(r"^[A-Za-z0-9]{6,16}$", query) and not re.match(r"^\d+\s*(s|p|stars?|premium|prem|usdt|ton|rub)", query, re.IGNORECASE):
        check_code = query.upper()
        await _show_check_result(inline_query, bot_username, check_code, lang, price_settings)
        return

    # Паттерны для парсинга
    # Звёзды: stars, star, s, звезд, звёзд, звезды, звёзды, звезда
    STARS_PATTERN = r"(?:stars?|s|звезд(?:а|ы|ов)?|звёзд(?:ы)?)"
    # Premium: premium, prem, p, премиум, прем, тгп, месяц, месяца, месяцев
    PREMIUM_PATTERN = r"(?:premium|prem|p|премиум|прем|тгп|месяц(?:а|ев)?)"
    # Валюты с русскими алиасами (включая символы $ и ₽)
    CURRENCY_PATTERN = r"(usdt|юсдт|ton|rub|руб|₽|\$|доллар(?:ы|ов)?|бакс(?:ы|ов)?|рубл(?:и|ей|ь)?|тон(?:ы|ов)?)"

    def normalize_currency(cur: str) -> str:
        """Нормализовать валюту к usdt/ton/rub."""
        cur = cur.lower()
        if cur in ("usdt", "юсдт", "$", "доллар", "доллары", "долларов", "бакс", "баксы", "баксов"):
            return "usdt"
        elif cur in ("ton", "тон", "тоны", "тонов"):
            return "ton"
        elif cur in ("rub", "руб", "₽", "рубль", "рубли", "рублей"):
            return "rub"
        return cur

    # 1. Currency to Stars: "100 usdt stars", "100 ton s", "100 rub звезд"
    currency_to_stars = re.match(rf"^(\d+(?:\.\d+)?)\s*{CURRENCY_PATTERN}\s*{STARS_PATTERN}$", query)
    if currency_to_stars:
        amount = Decimal(currency_to_stars.group(1))
        currency = normalize_currency(currency_to_stars.group(2))
        await _show_currency_to_stars_result(inline_query, bot_username, amount, currency, rates, lang, price_settings)
        return

    # 2. Stars with currency: "100 stars usdt", "100s ton", "100 звезд рубли"
    stars_with_currency = re.match(rf"^(\d+)\s*{STARS_PATTERN}\s+{CURRENCY_PATTERN}$", query)
    if stars_with_currency:
        amount = int(stars_with_currency.group(1))
        currency = normalize_currency(stars_with_currency.group(2))
        if amount < min_stars:
            await _show_min_error(inline_query, lang, min_stars, max_stars)
            return
        await _show_stars_result(inline_query, bot_username, amount, lang, rates, currency, price_settings)
        return

    # 3. Stars without currency (all currencies): "100 stars", "100s", "100 звезд"
    stars_match = re.match(rf"^(\d+)\s*{STARS_PATTERN}$", query)
    if stars_match:
        amount = int(stars_match.group(1))
        if amount < min_stars:
            await _show_min_error(inline_query, lang, min_stars, max_stars)
            return
        await _show_stars_result(inline_query, bot_username, amount, lang, rates, None, price_settings)
        return

    # 4. Premium with currency: "3 premium usdt", "3p ton", "6 месяцев рубли"
    premium_with_currency = re.match(rf"^(\d+)\s*{PREMIUM_PATTERN}\s+{CURRENCY_PATTERN}$", query)
    if premium_with_currency:
        months = int(premium_with_currency.group(1))
        currency = normalize_currency(premium_with_currency.group(2))
        if months in PREMIUM_DURATIONS:
            await _show_premium_result(inline_query, bot_username, months, lang, rates, currency, price_settings)
            return
        else:
            await _show_premium_invalid(inline_query, lang)
            return

    # 5. Premium without currency (all currencies): "3 premium", "3p", "3 месяца"
    premium_match = re.match(rf"^(\d+)\s*{PREMIUM_PATTERN}$", query)
    if premium_match:
        months = int(premium_match.group(1))
        if months in PREMIUM_DURATIONS:
            await _show_premium_result(inline_query, bot_username, months, lang, rates, None, price_settings)
            return
        else:
            await _show_premium_invalid(inline_query, lang)
            return

    # 6. Число + валюта (без указания типа): "50 usdt", "3 rub" - показываем несколько вариантов
    number_with_currency = re.match(rf"^(\d+(?:\.\d+)?)\s*{CURRENCY_PATTERN}$", query)
    if number_with_currency:
        num = Decimal(number_with_currency.group(1))
        currency = normalize_currency(number_with_currency.group(2))
        await _show_combined_results(inline_query, bot_username, num, currency, rates, lang, price_settings)
        return

    # 7. Просто число: 3/6/12 = Premium, >= min_stars = Stars
    just_number = re.match(r"^(\d+)$", query)
    if just_number:
        num = int(just_number.group(1))
        if num in PREMIUM_DURATIONS:
            await _show_premium_result(inline_query, bot_username, num, lang, rates, None, price_settings)
            return
        elif num >= min_stars:
            await _show_stars_result(inline_query, bot_username, num, lang, rates, None, price_settings)
            return

    # Если ничего не подошло - показываем подсказку
    await _show_invalid_input(inline_query, lang)


async def _show_stars_result(
    inline_query: InlineQuery,
    bot_username: str,
    amount: int,
    lang: str,
    rates: dict,
    currency: str | None,
    price_settings: dict,
) -> None:
    """Показать результат расчёта звёзд."""
    star_price = price_settings["star_price_usdt"]
    max_stars = price_settings["max_stars"]
    total_usdt = amount * star_price

    # Проверяем превышение максимума
    exceeds_max = amount > max_stars
    buy_amount = min(amount, max_stars)

    # Формируем ссылки без реферального кода
    buy_link = f"https://t.me/{bot_username}?start=buy_{buy_amount}"
    bot_link = f"https://t.me/{bot_username}"

    # Эмодзи валют
    currency_emoji = {"usdt": "💵", "ton": "💎", "rub": "🇷🇺"}.get(currency, "💰") if currency else ""

    # Названия валют
    currency_names = {"usdt": "USDT", "ton": "TON", "rub": "RUB"}

    # Слово "звезд" для локализации
    stars_word = t("inline.stars_word", lang)
    max_warning = t("inline.max_warning", lang, max=max_stars)

    # Формируем цены
    if currency:
        # Одна валюта - короткий формат
        price = convert_usdt_to_currency(total_usdt, currency, rates)
        price_text = format_currency(price, currency)
        title = t("inline.stars.title_with_price", lang, amount=amount, price=price_text)
        # Извлекаем только число (без валюты)
        price_num = price_text.rsplit(" ", 1)[0]
        currency_name = currency_names.get(currency, "")
        message_text = f"⭐️ <b>{amount}</b> {stars_word} = {currency_emoji} <b>{price_num}</b> {currency_name}"
    else:
        # Все валюты - извлекаем только числа
        usdt_num = f"{float(total_usdt):.2f}"
        ton_num = f"{float(convert_usdt_to_currency(total_usdt, 'ton', rates)):.4f}"
        rub_num = f"{float(convert_usdt_to_currency(total_usdt, 'rub', rates)):.2f}"
        title = t("inline.stars.title", lang, amount=amount)
        message_text = f"⭐️ <b>{amount}</b> {stars_word} = 💵 <b>{usdt_num}</b> USDT | 💎 <b>{ton_num}</b> TON | 🇷🇺 <b>{rub_num}</b> RUB"

    # Добавляем предупреждение о максимуме
    if exceeds_max:
        message_text += f"\n\n{max_warning}"

    description = t("inline.click_to_send", lang)
    buy_btn_text = t("inline.buy_btn", lang, amount=buy_amount)
    bot_btn_text = t("inline.go_to_bot_btn", lang)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=buy_btn_text, url=buy_link)],
            [InlineKeyboardButton(text=bot_btn_text, url=bot_link)],
        ]
    )

    result = InlineQueryResultArticle(
        id=f"stars_{amount}_{currency or 'all'}",
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=message_text,
            parse_mode="HTML",
        ),
        reply_markup=keyboard,
        thumbnail_url="https://img.icons8.com/emoji/96/star-emoji.png",
    )

    await inline_query.answer(
        results=[result],
        cache_time=1,
        is_personal=True,
    )


async def _show_currency_to_stars_result(
    inline_query: InlineQuery,
    bot_username: str,
    amount: Decimal,
    currency: str,
    rates: dict,
    lang: str,
    price_settings: dict,
) -> None:
    """Показать сколько звёзд можно купить за указанную валюту."""
    star_price = price_settings["star_price_usdt"]
    min_stars = price_settings["min_stars"]
    max_stars = price_settings["max_stars"]

    # Конвертируем в USDT
    usdt_amount = convert_currency_to_usdt(amount, currency, rates)

    # Рассчитываем количество звёзд (реальное, без ограничений)
    real_stars_amount = int(usdt_amount / star_price)

    # Форматируем входную сумму (с RUB вместо ₽)
    currency_emoji = {"usdt": "💵", "ton": "💎", "rub": "🇷🇺"}.get(currency, "💰")
    currency_names = {"usdt": "USDT", "ton": "TON", "rub": "RUB"}
    currency_name = currency_names.get(currency, "")
    if currency == "usdt":
        amount_str = f"{float(amount):.2f}"
    elif currency == "ton":
        amount_str = f"{float(amount):.4f}"
    else:
        amount_str = f"{float(amount):.2f}"

    # Проверяем минимум
    if real_stars_amount < min_stars:
        min_label = t("inline.min_label", lang)
        title = f"{currency_emoji} {amount_str} {currency_name} = {real_stars_amount} ⭐ ({min_label} {min_stars})"
        description = t("inline.converter.min_error_desc", lang, min=min_stars)

        result = InlineQueryResultArticle(
            id=f"error_{currency}_min",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(message_text=title),
        )
        await inline_query.answer(results=[result], cache_time=1, is_personal=True)
        return

    # Для кнопки покупки ограничиваем максимумом
    buy_stars_amount = min(real_stars_amount, max_stars)
    exceeds_max = real_stars_amount > max_stars

    # Формируем ссылки без реферального кода
    buy_link = f"https://t.me/{bot_username}?start=buy_{buy_stars_amount}"
    bot_link = f"https://t.me/{bot_username}"

    stars_word = t("inline.stars_word", lang)
    max_warning = t("inline.max_warning", lang, max=max_stars)

    title = f"{currency_emoji} {amount_str} {currency_name} = {real_stars_amount} ⭐"
    description = t("inline.click_to_send", lang)

    # Формируем короткое сообщение
    if exceeds_max:
        message_text = (
            f"{currency_emoji} <b>{amount_str}</b> {currency_name} = ⭐️ <b>{real_stars_amount}</b> {stars_word}\n\n"
            f"{max_warning}"
        )
    else:
        message_text = f"{currency_emoji} <b>{amount_str}</b> {currency_name} = ⭐️ <b>{real_stars_amount}</b> {stars_word}"

    buy_btn_text = t("inline.buy_btn", lang, amount=buy_stars_amount)
    bot_btn_text = t("inline.go_to_bot_btn", lang)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=buy_btn_text, url=buy_link)],
            [InlineKeyboardButton(text=bot_btn_text, url=bot_link)],
        ]
    )

    result = InlineQueryResultArticle(
        id=f"{currency}_to_stars_{amount}",
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=message_text,
            parse_mode="HTML",
        ),
        reply_markup=keyboard,
        thumbnail_url="https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f4b1.png",
    )

    await inline_query.answer(
        results=[result],
        cache_time=1,
        is_personal=True,
    )


async def _show_premium_result(
    inline_query: InlineQuery,
    bot_username: str,
    months: int,
    lang: str,
    rates: dict,
    currency: str | None,
    price_settings: dict,
) -> None:
    """Показать результат расчёта Premium."""
    premium_prices = price_settings["premium_prices"]
    price_usdt = premium_prices[months]

    # Формируем ссылки без реферального кода
    buy_link = f"https://t.me/{bot_username}?start=premium_{months}"
    bot_link = f"https://t.me/{bot_username}"

    # Эмодзи валют
    currency_emoji = {"usdt": "💵", "ton": "💎", "rub": "🇷🇺"}.get(currency, "💰") if currency else ""

    # Названия валют
    currency_names = {"usdt": "USDT", "ton": "TON", "rub": "RUB"}

    # Сокращение для месяцев
    months_abbr = t("inline.months_abbr", lang)

    # Формируем цены
    if currency:
        # Одна валюта - короткий формат
        price = convert_usdt_to_currency(price_usdt, currency, rates)
        price_text = format_currency(price, currency)
        title = t("inline.premium.title_with_price", lang, months=months, price=price_text)
        # Извлекаем только число (без валюты)
        price_num = price_text.rsplit(" ", 1)[0]
        currency_name = currency_names.get(currency, "")
        message_text = f"👑 <b>{months} {months_abbr}</b> Premium = {currency_emoji} <b>{price_num}</b> {currency_name}"
    else:
        # Все валюты - извлекаем только числа
        usdt_num = f"{float(price_usdt):.2f}"
        ton_num = f"{float(convert_usdt_to_currency(price_usdt, 'ton', rates)):.4f}"
        rub_num = f"{float(convert_usdt_to_currency(price_usdt, 'rub', rates)):.2f}"
        title = t("inline.premium.title", lang, months=months)
        message_text = f"👑 <b>{months} {months_abbr}</b> Premium = 💵 <b>{usdt_num}</b> USDT | 💎 <b>{ton_num}</b> TON | 🇷🇺 <b>{rub_num}</b> RUB"

    description = t("inline.click_to_send", lang)
    buy_btn_text = t("inline.buy_premium_btn", lang, months=months)
    bot_btn_text = t("inline.go_to_bot_btn", lang)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=buy_btn_text, url=buy_link)],
            [InlineKeyboardButton(text=bot_btn_text, url=bot_link)],
        ]
    )

    result = InlineQueryResultArticle(
        id=f"premium_{months}_{currency or 'all'}",
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=message_text,
            parse_mode="HTML",
        ),
        reply_markup=keyboard,
        thumbnail_url="https://img.icons8.com/emoji/96/crown-emoji.png",
    )

    await inline_query.answer(
        results=[result],
        cache_time=1,
        is_personal=True,
    )


async def _show_premium_invalid(inline_query: InlineQuery, lang: str) -> None:
    """Показать ошибку неверного срока Premium."""
    title = t("inline.premium.invalid_title", lang)
    description = t("inline.premium.invalid_desc", lang)

    result = InlineQueryResultArticle(
        id="error_premium_invalid",
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=title,
        ),
    )

    await inline_query.answer(
        results=[result],
        cache_time=1,
        is_personal=True,
    )


async def _show_combined_results(
    inline_query: InlineQuery,
    bot_username: str,
    amount: Decimal,
    currency: str,
    rates: dict,
    lang: str,
    price_settings: dict,
) -> None:
    """Показать комбинированные результаты для запроса 'число валюта'."""
    star_price = price_settings["star_price_usdt"]
    min_stars = price_settings["min_stars"]
    max_stars = price_settings["max_stars"]
    premium_prices = price_settings["premium_prices"]

    results = []
    bot_link = f"https://t.me/{bot_username}"
    bot_btn_text = t("inline.go_to_bot_btn", lang)

    # Эмодзи и названия валют
    currency_emoji = {"usdt": "💵", "ton": "💎", "rub": "🇷🇺"}.get(currency, "💰")
    currency_names = {"usdt": "USDT", "ton": "TON", "rub": "RUB"}
    currency_name = currency_names.get(currency, "")

    # Локализованные строки
    stars_word = t("inline.stars_word", lang)
    months_abbr = t("inline.months_abbr", lang)
    max_warning = t("inline.max_warning", lang, max=max_stars)

    int_amount = int(amount)

    # 1. Если число это Premium месяц (3/6/12) - показать Premium
    if int_amount in PREMIUM_DURATIONS:
        price_usdt = premium_prices[int_amount]
        price = convert_usdt_to_currency(price_usdt, currency, rates)
        price_text = format_currency(price, currency)
        price_num = price_text.rsplit(" ", 1)[0]

        buy_link = f"https://t.me/{bot_username}?start=premium_{int_amount}"
        buy_btn_text = t("inline.buy_premium_btn", lang, months=int_amount)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=buy_btn_text, url=buy_link)],
                [InlineKeyboardButton(text=bot_btn_text, url=bot_link)],
            ]
        )

        results.append(InlineQueryResultArticle(
            id=f"premium_{int_amount}_{currency}",
            title=f"👑 {int_amount} {months_abbr} Premium = {price_num} {currency_name}",
            description=t("inline.click_to_send", lang),
            input_message_content=InputTextMessageContent(
                message_text=f"👑 <b>{int_amount} {months_abbr}</b> Premium = {currency_emoji} <b>{price_num}</b> {currency_name}",
                parse_mode="HTML",
            ),
            reply_markup=keyboard,
            thumbnail_url="https://img.icons8.com/emoji/96/crown-emoji.png",
        ))

    # 2. Если число >= min_stars - показать Stars
    if int_amount >= min_stars:
        total_usdt = int_amount * star_price
        price = convert_usdt_to_currency(total_usdt, currency, rates)
        price_text = format_currency(price, currency)
        price_num = price_text.rsplit(" ", 1)[0]

        exceeds_max = int_amount > max_stars
        buy_amount = min(int_amount, max_stars)
        buy_link = f"https://t.me/{bot_username}?start=buy_{buy_amount}"
        buy_btn_text = t("inline.buy_btn", lang, amount=buy_amount)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=buy_btn_text, url=buy_link)],
                [InlineKeyboardButton(text=bot_btn_text, url=bot_link)],
            ]
        )

        message_text = f"⭐️ <b>{int_amount}</b> {stars_word} = {currency_emoji} <b>{price_num}</b> {currency_name}"
        if exceeds_max:
            message_text += f"\n\n{max_warning}"

        results.append(InlineQueryResultArticle(
            id=f"stars_{int_amount}_{currency}",
            title=f"⭐️ {int_amount} {stars_word} = {price_num} {currency_name}",
            description=t("inline.click_to_send", lang),
            input_message_content=InputTextMessageContent(
                message_text=message_text,
                parse_mode="HTML",
            ),
            reply_markup=keyboard,
            thumbnail_url="https://img.icons8.com/emoji/96/star-emoji.png",
        ))

    # 3. Конвертер: сколько звёзд можно купить за эту сумму
    usdt_amount = convert_currency_to_usdt(amount, currency, rates)
    real_stars_amount = int(usdt_amount / star_price)

    if real_stars_amount >= min_stars:
        buy_stars_amount = min(real_stars_amount, max_stars)
        exceeds_max = real_stars_amount > max_stars

        buy_link = f"https://t.me/{bot_username}?start=buy_{buy_stars_amount}"
        buy_btn_text = t("inline.buy_btn", lang, amount=buy_stars_amount)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=buy_btn_text, url=buy_link)],
                [InlineKeyboardButton(text=bot_btn_text, url=bot_link)],
            ]
        )

        # Форматируем сумму
        if currency == "usdt":
            amount_str = f"{float(amount):.2f}"
        elif currency == "ton":
            amount_str = f"{float(amount):.4f}"
        else:
            amount_str = f"{float(amount):.2f}"

        if exceeds_max:
            message_text = (
                f"{currency_emoji} <b>{amount_str}</b> {currency_name} = ⭐️ <b>{real_stars_amount}</b> {stars_word}\n\n"
                f"{max_warning}"
            )
        else:
            message_text = f"{currency_emoji} <b>{amount_str}</b> {currency_name} = ⭐️ <b>{real_stars_amount}</b> {stars_word}"

        results.append(InlineQueryResultArticle(
            id=f"convert_{currency}_{amount}",
            title=f"🔄 {amount_str} {currency_name} = {real_stars_amount} ⭐",
            description=t("inline.click_to_send", lang),
            input_message_content=InputTextMessageContent(
                message_text=message_text,
                parse_mode="HTML",
            ),
            reply_markup=keyboard,
            thumbnail_url="https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f4b1.png",
        ))

    if results:
        await inline_query.answer(results=results, cache_time=1, is_personal=True)
    else:
        await _show_invalid_input(inline_query, lang)


async def _show_instruction(
    inline_query: InlineQuery,
    bot_username: str,
    lang: str,
    price_settings: dict,
    rates: dict,
) -> None:
    """Показать курсы 1 звезды к валютам."""
    star_price = price_settings["star_price_usdt"]

    bot_link = f"https://t.me/{bot_username}"
    bot_btn_text = t("inline.go_to_bot_btn", lang)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=bot_btn_text, url=bot_link)],
        ]
    )

    results = []

    # Локализованное слово "звезда"
    star_singular = t("inline.star_singular", lang)

    # Курс 1 звезды к USDT (3 цифры)
    usdt_price = f"{float(star_price):.3f}"
    results.append(InlineQueryResultArticle(
        id="rate_usdt",
        title=f"⭐️ 1 {star_singular} = {usdt_price} USDT",
        description=t("inline.click_to_send", lang),
        input_message_content=InputTextMessageContent(
            message_text=f"⭐️ <b>1</b> {star_singular} = 💵 <b>{usdt_price}</b> USDT",
            parse_mode="HTML",
        ),
        reply_markup=keyboard,
        thumbnail_url="https://img.icons8.com/emoji/96/star-emoji.png",
    ))

    # Курс 1 звезды к TON (5 цифр)
    ton_price = f"{float(convert_usdt_to_currency(star_price, 'ton', rates)):.5f}"
    results.append(InlineQueryResultArticle(
        id="rate_ton",
        title=f"⭐️ 1 {star_singular} = {ton_price} TON",
        description=t("inline.click_to_send", lang),
        input_message_content=InputTextMessageContent(
            message_text=f"⭐️ <b>1</b> {star_singular} = 💎 <b>{ton_price}</b> TON",
            parse_mode="HTML",
        ),
        reply_markup=keyboard,
        thumbnail_url="https://img.icons8.com/emoji/96/star-emoji.png",
    ))

    # Курс 1 звезды к RUB
    rub_price = f"{float(convert_usdt_to_currency(star_price, 'rub', rates)):.2f}"
    results.append(InlineQueryResultArticle(
        id="rate_rub",
        title=f"⭐️ 1 {star_singular} = {rub_price} RUB",
        description=t("inline.click_to_send", lang),
        input_message_content=InputTextMessageContent(
            message_text=f"⭐️ <b>1</b> {star_singular} = 🇷🇺 <b>{rub_price}</b> RUB",
            parse_mode="HTML",
        ),
        reply_markup=keyboard,
        thumbnail_url="https://img.icons8.com/emoji/96/star-emoji.png",
    ))

    await inline_query.answer(
        results=results,
        cache_time=60,
        is_personal=True,
    )


async def _show_invalid_input(inline_query: InlineQuery, lang: str) -> None:
    """Показать ошибку неверного ввода."""
    title = t("inline.errors.invalid_title", lang)
    description = t("inline.errors.invalid_desc", lang)
    message_text = t("inline.errors.invalid_message", lang).strip()

    result = InlineQueryResultArticle(
        id="error_invalid",
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=message_text,
            parse_mode="HTML",
        ),
    )

    await inline_query.answer(
        results=[result],
        cache_time=1,
        is_personal=True,
    )


async def _show_min_error(inline_query: InlineQuery, lang: str, min_stars: int, max_stars: int) -> None:
    """Показать ошибку минимума."""
    title = t("inline.errors.min_stars", lang, min=min_stars)
    description = t("inline.errors.range_desc", lang, min=min_stars, max=max_stars)

    result = InlineQueryResultArticle(
        id="error_min",
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=title,
        ),
    )

    await inline_query.answer(
        results=[result],
        cache_time=1,
        is_personal=True,
    )


async def _show_check_result(
    inline_query: InlineQuery,
    bot_username: str,
    check_code: str,
    lang: str,
    price_settings: dict,
) -> None:
    """Показать результат поиска чека для отправки."""
    star_price = price_settings["star_price_usdt"]
    premium_prices = price_settings["premium_prices"]
    # Ищем чек
    async with async_session_factory() as session:
        result = await session.execute(
            select(Check).where(Check.code == check_code)
        )
        check = result.scalar_one_or_none()

    # Чек не найден
    if not check:
        title = t("inline.check.not_found_title", lang)
        description = t("inline.check.not_found_desc", lang, code=check_code)

        error_result = InlineQueryResultArticle(
            id=f"check_error_{check_code}",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(
                message_text=title,
            ),
        )
        await inline_query.answer(results=[error_result], cache_time=1, is_personal=True)
        return

    # Чек неактивен
    if not check.is_active:
        title = t("inline.check.inactive_title", lang)
        description = t("inline.check.inactive_desc", lang)

        error_result = InlineQueryResultArticle(
            id=f"check_inactive_{check_code}",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(
                message_text=title,
            ),
        )
        await inline_query.answer(results=[error_result], cache_time=1, is_personal=True)
        return

    # Все активации использованы
    remaining = check.max_activations - check.current_activations
    if remaining <= 0:
        title = t("inline.check.exhausted_title", lang)
        description = t("inline.check.exhausted_desc", lang)

        error_result = InlineQueryResultArticle(
            id=f"check_exhausted_{check_code}",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(
                message_text=title,
            ),
        )
        await inline_query.answer(results=[error_result], cache_time=1, is_personal=True)
        return

    # Формируем ссылку для активации чека
    activate_link = f"https://t.me/{bot_username}?start=check_{check_code}"
    max_activations = check.max_activations
    is_multi = max_activations > 1

    # Формируем текст получателя, если чек для конкретного пользователя
    # Экранируем HTML для безопасности
    recipient_text = ""
    if check.recipient_username:
        recipient_text = t("inline.check.for_user", lang, username=html.escape(check.recipient_username))
    elif check.recipient_id:
        recipient_text = t("inline.check.for_id", lang, id=check.recipient_id)

    # Описание для всех чеков одинаковое
    description = t("inline.check.share_desc", lang)

    if check.content_type == "stars":
        amount_per_activation = int(check.amount_stars)
        price_per_activation = amount_per_activation * star_price
        total_amount = amount_per_activation * max_activations
        total_price = price_per_activation * max_activations

        if is_multi:
            title = t("inline.check.stars_multi_title", lang, amount=total_amount, price=f"{total_price:.2f}", recipient=recipient_text)
            message_text = t("inline.check.stars_multi_message", lang,
                total=total_amount, price=f"{total_price:.2f}", recipient=recipient_text,
                amount=amount_per_activation, per_price=f"{price_per_activation:.2f}", count=max_activations).strip()
        else:
            title = t("inline.check.stars_title", lang, amount=amount_per_activation, price=f"{price_per_activation:.2f}", recipient=recipient_text)
            message_text = t("inline.check.stars_single_message", lang, amount=amount_per_activation, price=f"{price_per_activation:.2f}", recipient=recipient_text)
        activate_btn = t("inline.check.activate_stars_btn", lang, amount=amount_per_activation)
    else:
        # Premium
        months_per_activation = check.amount_premium_months
        price_per_activation = premium_prices.get(months_per_activation, Decimal("0"))
        total_months = months_per_activation * max_activations
        total_price = price_per_activation * max_activations

        if is_multi:
            title = t("inline.check.premium_multi_title", lang, months=total_months, price=f"{total_price:.2f}", recipient=recipient_text)
            message_text = t("inline.check.premium_multi_message", lang,
                total=total_months, price=f"{total_price:.2f}", recipient=recipient_text,
                months=months_per_activation, per_price=f"{price_per_activation:.2f}", count=max_activations).strip()
        else:
            title = t("inline.check.premium_title", lang, months=months_per_activation, price=f"{price_per_activation:.2f}", recipient=recipient_text)
            message_text = t("inline.check.premium_single_message", lang, months=months_per_activation, price=f"{price_per_activation:.2f}", recipient=recipient_text)
        activate_btn = t("inline.check.activate_premium_btn", lang, months=months_per_activation)

    # Добавляем описание чека, если оно есть (экранируем HTML для безопасности)
    if check.description:
        safe_description = html.escape(check.description)
        message_text += f"\n\n💬 {safe_description}"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=activate_btn, url=activate_link)],
        ]
    )

    if check.photo_file_id:
        check_result = InlineQueryResultCachedPhoto(
            id=f"check_{check_code}",
            photo_file_id=check.photo_file_id,
            title=title,
            description=description,
            caption=message_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        check_result = InlineQueryResultArticle(
            id=f"check_{check_code}",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(
                message_text=message_text,
                parse_mode="HTML",
            ),
            reply_markup=keyboard,
        )

    await inline_query.answer(
        results=[check_result],
        cache_time=1,
        is_personal=True,
    )
