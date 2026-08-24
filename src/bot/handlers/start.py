"""
Handler для команды /start и главного меню.
"""
import json
import logging
import re
import time
from decimal import Decimal
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from src.bot.legal import PRIVACY_POLICY_URL, USER_AGREEMENT_URL, get_legal_links_text
from src.bot.keyboards.menu import MenuCallback, get_main_menu_keyboard
from src.bot.keyboards.stars import get_recipient_keyboard, StarsCallback
from src.bot.keyboards.premium import get_premium_recipient_keyboard
from src.bot.menu_media import answer_menu_message, edit_menu_message
from src.db.models import Check, CheckActivation, User
from src.db.session import async_session_factory
from src.services.user_service import UserService
from src.services.user_registration_service import finalize_new_user_registration
from src.services.bot_settings_service import get_bot_settings, get_min_stars, get_max_stars, get_premium_prices, get_star_price
from src.locales import t, get_user_locale, pluralize_months

logger = logging.getLogger(__name__)

router = Router(name="start")

# Rate limiting для брутфорса паролей чеков
# user_id -> list of timestamps of failed attempts
# TODO: Перенести в Redis для работы с несколькими инстансами бота
_password_attempts: dict[int, list[float]] = defaultdict(list)
_MAX_PASSWORD_ATTEMPTS = 5  # Максимум попыток
_PASSWORD_ATTEMPT_WINDOW = 60.0  # Окно в секундах (1 минута)
_CLEANUP_THRESHOLD = 1000  # Порог для очистки старых записей


def _check_password_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    Проверить rate limit для попыток ввода пароля.

    Returns:
        (allowed, seconds_to_wait): разрешено ли, сколько ждать если нет
    """
    now = time.time()
    attempts = _password_attempts[user_id]

    # Удаляем старые попытки
    attempts[:] = [ts for ts in attempts if now - ts < _PASSWORD_ATTEMPT_WINDOW]

    # Периодическая очистка пустых записей для предотвращения memory leak
    if len(_password_attempts) > _CLEANUP_THRESHOLD:
        _cleanup_password_attempts()

    if len(attempts) >= _MAX_PASSWORD_ATTEMPTS:
        # Вычисляем сколько ждать до разблокировки
        oldest = min(attempts)
        wait_seconds = int(_PASSWORD_ATTEMPT_WINDOW - (now - oldest)) + 1
        return False, wait_seconds

    return True, 0


def _record_failed_password_attempt(user_id: int) -> None:
    """Записать неудачную попытку ввода пароля."""
    _password_attempts[user_id].append(time.time())


def _cleanup_password_attempts() -> None:
    """Очистить пустые записи из rate limiting dict."""
    now = time.time()
    empty_keys = [
        uid for uid, attempts in _password_attempts.items()
        if not attempts or all(now - ts >= _PASSWORD_ATTEMPT_WINDOW for ts in attempts)
    ]
    for uid in empty_keys:
        del _password_attempts[uid]


def _calculate_frozen_amount(total_frozen: Decimal, max_activations: int, current_activations: int) -> Decimal:
    """
    Рассчитать сумму для списания с замороженного баланса.
    При последней активации списывается весь остаток для избежания "пыли".
    """
    if current_activations + 1 >= max_activations:
        # Последняя активация - списываем весь остаток
        return total_frozen
    else:
        # Обычная активация - делим поровну
        return total_frozen / max_activations


class CheckActivationStates(StatesGroup):
    """Состояния для активации чека с паролем."""
    waiting_password = State()


def get_welcome_message(
    balance_usdt: Decimal,
    balance_stars: Decimal,
    balance_premium_months: int,
    lang: str = "ru",
) -> str:
    """Сформировать приветственное сообщение."""
    return t(
        "menu.welcome_full",
        lang,
        balance_usdt=f"{balance_usdt:,.2f}",
        balance_stars=f"{balance_stars:,.0f}",
        balance_premium=balance_premium_months,
    ).strip()


def _get_legal_notice_text(lang: str) -> str:
    """Сформировать уведомление о принятии документов для новых пользователей."""
    if lang == "en":
        agreement_link = f'<a href="{USER_AGREEMENT_URL}">User Agreement</a>'
        return (
            "<b>Before using Dobro Star</b>\n\n"
            "By continuing to use the bot, placing orders, topping up the balance "
            "or receiving digital goods, you confirm that you have read and accept "
            f"the {agreement_link} and "
            f'<a href="{PRIVACY_POLICY_URL}">Privacy Policy</a>.\n\n'
            "If you do not agree with these terms, please stop using the bot."
        )

    agreement_link = f'<a href="{USER_AGREEMENT_URL}">Пользовательским соглашением</a>'
    return (
        "<b>Перед использованием Dobro Star</b>\n\n"
        "Продолжая пользоваться ботом, оформляя заказы, пополняя баланс "
        "или получая цифровые товары, вы подтверждаете, что ознакомились "
        f"и соглашаетесь с {agreement_link} и "
        f'<a href="{PRIVACY_POLICY_URL}">Политикой конфиденциальности</a>.\n\n'
        "Если вы не согласны с условиями документов, пожалуйста, не используйте бот."
    )


def _get_legal_notice_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Кнопки со ссылками на юридические документы."""
    if lang == "en":
        privacy_text = "Privacy Policy"
        agreement_text = "User Agreement"
    else:
        privacy_text = "Политика конфиденциальности"
        agreement_text = "Пользовательское соглашение"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=agreement_text, url=USER_AGREEMENT_URL)],
            [InlineKeyboardButton(text=privacy_text, url=PRIVACY_POLICY_URL)],
        ]
    )


async def _send_legal_notice(message: Message, lang: str) -> None:
    """Отправить юридическое уведомление новым пользователям."""
    await message.answer(
        text=_get_legal_notice_text(lang),
        reply_markup=_get_legal_notice_keyboard(lang),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _safe_parse_int(value: str, max_value: int = 10_000_000) -> Optional[int]:
    """Безопасный парсинг int с ограничением максимального значения."""
    try:
        # Ограничиваем длину строки для защиты от DoS
        if len(value) > 10:
            return None
        num = int(value)
        if num <= 0 or num > max_value:
            return None
        return num
    except (ValueError, OverflowError):
        return None


def parse_start_params(
    param: str,
) -> tuple[str | None, int | None, int | None, str | None, int | None]:
    """
    Парсит параметры /start команды.

    Форматы:
    - ref_CODE - реферальный код
    - ref_CODE_buy_100 - реферальный код + количество звёзд для покупки
    - ref_CODE_premium_3 - реферальный код + срок Premium (3, 6, 12 месяцев)
    - buy_100 - покупка звёзд (без реф. кода, из inline режима)
    - premium_3 - покупка Premium (без реф. кода, из inline режима)
    - check_CODE - код чека для активации

    Returns:
        (referrer_code, buy_stars_amount, premium_months, check_code, giveaway_id)
    """
    referrer_code = None
    buy_amount = None
    premium_months = None
    check_code = None
    giveaway_id = None

    if not param:
        return None, None, None, None, None

    giveaway_match = re.fullmatch(r"giveaway_(\d+)", param)
    if giveaway_match:
        giveaway_id = _safe_parse_int(giveaway_match.group(1), max_value=2_147_483_647)
        return None, None, None, None, giveaway_id

    # Проверяем формат check_CODE (чек для активации)
    if param.startswith("check_"):
        check_code = param[6:]  # Убираем префикс "check_"
        if check_code:  # Проверяем что код не пустой
            return None, None, None, check_code, None
        return None, None, None, None, None

    # Проверяем формат ref_CODE_buy_AMOUNT (Stars с рефералкой)
    buy_match = re.match(r"ref_(.+)_buy_(\d+)$", param)
    if buy_match:
        referrer_code = buy_match.group(1)
        buy_amount = _safe_parse_int(buy_match.group(2))
        if buy_amount:
            return referrer_code, buy_amount, None, None, None
        return None, None, None, None, None

    # Проверяем формат ref_CODE_premium_MONTHS (Premium с рефералкой)
    premium_match = re.match(r"ref_(.+)_premium_(\d+)$", param)
    if premium_match:
        referrer_code = premium_match.group(1)
        premium_months = _safe_parse_int(premium_match.group(2), max_value=120)
        if premium_months:
            return referrer_code, None, premium_months, None, None
        return None, None, None, None, None

    # Проверяем формат buy_AMOUNT (Stars без рефералки, из inline)
    buy_simple = re.match(r"buy_(\d+)$", param)
    if buy_simple:
        buy_amount = _safe_parse_int(buy_simple.group(1))
        if buy_amount:
            return None, buy_amount, None, None, None
        return None, None, None, None, None

    # Проверяем формат premium_MONTHS (Premium без рефералки, из inline)
    premium_simple = re.match(r"premium_(\d+)$", param)
    if premium_simple:
        premium_months = _safe_parse_int(premium_simple.group(1), max_value=120)
        if premium_months:
            return None, None, premium_months, None, None
        return None, None, None, None, None

    # Проверяем формат ref_CODE
    if param.startswith("ref_"):
        referrer_code = param[4:]  # Убираем префикс "ref_"
        if referrer_code:  # Проверяем что код не пустой
            return referrer_code, None, None, None, None

    return None, None, None, None, None


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_params(message: Message, state: FSMContext) -> None:
    """
    Обработка /start с параметрами.

    Форматы:
    - /start ref_CODE - реферальная ссылка
    - /start ref_CODE_buy_100 - покупка 100 звёзд по рефералке
    - /start ref_CODE_premium_3 - покупка Premium 3/6/12 месяцев по рефералке
    - /start check_CODE - активация чека
    """
    args = message.text.split(maxsplit=1)

    if len(args) > 1:
        param = args[1]
        referrer_code, buy_amount, premium_months, check_code, giveaway_id = parse_start_params(param)
    else:
        referrer_code, buy_amount, premium_months, check_code, giveaway_id = None, None, None, None, None

    await _process_start(message, state, referrer_code, buy_amount, premium_months, check_code, giveaway_id)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Обработка /start без параметров."""
    await _process_start(
        message,
        state,
        referrer_code=None,
        buy_amount=None,
        premium_months=None,
        check_code=None,
        giveaway_id=None,
    )


async def _process_start(
    message: Message,
    state: FSMContext,
    referrer_code: str | None,
    buy_amount: int | None,
    premium_months: int | None,
    check_code: str | None,
    giveaway_id: int | None,
) -> None:
    """Общая логика обработки /start."""
    user = message.from_user

    # Очищаем предыдущее состояние
    await state.clear()

    # Определяем язык пользователя из Telegram
    user_lang = get_user_locale(user.language_code)

    async with async_session_factory() as session:
        user_service = UserService(session)

        # Получаем или создаём пользователя
        db_user, created = await user_service.get_or_create_user(
            user_id=user.id,
            username=user.username,
            language_code=user_lang,  # Сохраняем определённый язык
            referrer_code_used=referrer_code,
        )

        await session.commit()

        # Используем язык из БД (мог быть изменён пользователем)
        lang = db_user.language_code or user_lang

        # Сохраняем язык в состояние
        await state.update_data(lang=lang)

        # Логируем событие
        if created:
            await finalize_new_user_registration(
                user_id=user.id,
                username=user.username,
                language=lang,
                referrer_code=referrer_code,
            )
            await _send_legal_notice(message, lang)

        # Если есть check_code - активируем чек
        if check_code:
            await _activate_check(message, state, session, db_user, check_code, lang, is_new_user=created)
            return

        if giveaway_id:
            from src.bot.handlers.giveaways import show_giveaway_from_start

            await show_giveaway_from_start(message, giveaway_id, lang)
            return

        # Если есть buy_amount - переходим к покупке звёзд
        if buy_amount:
            # Получаем лимиты из настроек
            min_stars = await get_min_stars()
            max_stars = await get_max_stars()
            if buy_amount >= min_stars and buy_amount <= max_stars:
                await _start_buy_stars_flow(message, state, db_user, buy_amount, lang)
                return

        # Если есть premium_months - переходим к покупке Premium
        if premium_months:
            # Получаем доступные периоды из настроек
            premium_prices = await get_premium_prices()
            if premium_months in premium_prices:
                await _start_buy_premium_flow(message, state, db_user, premium_months, lang)
                return

        # Формируем приветственное сообщение
        welcome_text = get_welcome_message(
            balance_usdt=db_user.balance_usdt,
            balance_stars=db_user.balance_stars,
            balance_premium_months=db_user.balance_premium_months,
            lang=lang,
        )

        bot_settings = await get_bot_settings()
        from src.services.giveaway_service import GiveawayService
        active_giveaways = await GiveawayService(session).has_active_giveaways()

        await answer_menu_message(
            message,
            "main",
            text=f"{welcome_text}\n\n{get_legal_links_text(lang)}",
            reply_markup=get_main_menu_keyboard(
                lang,
                bot_settings.get("news_channel_url"),
                is_admin=db_user.is_admin,
                has_active_giveaways=active_giveaways,
            ),
            disable_web_page_preview=True,
        )


def _get_main_menu_button_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Клавиатура с одной кнопкой 'Главное меню'."""
    btn_text = t("menu.main_menu_btn", lang)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=btn_text, callback_data=MenuCallback.BACK_TO_MENU)],
        ]
    )


def _get_cancel_activation_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Клавиатура для отмены активации чека."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("common.cancel", lang), callback_data="cancel_check_activation")],
        ]
    )


async def _activate_check(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    check_code: str,
    lang: str,
    is_new_user: bool = False,
) -> None:
    """
    Активировать чек и начислить средства пользователю.
    """
    user = message.from_user
    keyboard = _get_main_menu_button_keyboard(lang)

    # Находим чек по коду (с блокировкой для предотвращения race condition)
    result = await session.execute(
        select(Check).where(Check.code == check_code).with_for_update()
    )
    check = result.scalar_one_or_none()

    # Чек не найден
    if not check:
        await message.answer(
            t("checks.activation.errors.not_found", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Чек неактивен
    if not check.is_active:
        await message.answer(
            t("checks.activation.errors.inactive", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Чек истёк
    if check.expires_at and check.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        await message.answer(
            t("checks.activation.errors.expired", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Все активации использованы
    if check.current_activations >= check.max_activations:
        await message.answer(
            t("checks.activation.errors.exhausted", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Проверяем, не активировал ли пользователь уже этот чек
    existing_activation = await session.execute(
        select(CheckActivation).where(
            CheckActivation.check_id == check.id,
            CheckActivation.user_id == user.id,
        )
    )
    if existing_activation.scalar_one_or_none():
        await message.answer(
            t("checks.activation.errors.already_activated", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Проверяем ограничение по получателю (username)
    if check.recipient_username:
        if not user.username or user.username.lower() != check.recipient_username.lower():
            await message.answer(
                t("checks.activation.errors.recipient_restricted", lang),
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return

    # Проверяем ограничение по получателю (ID)
    if check.recipient_id:
        if user.id != check.recipient_id:
            await message.answer(
                t("checks.activation.errors.recipient_restricted", lang),
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return

    # Проверяем ограничение Telegram Premium
    if check.require_premium:
        if not getattr(user, "is_premium", False):
            await message.answer(
                t("checks.activation.errors.require_premium", lang),
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return

    # Проверяем ограничение только для новых пользователей
    if check.require_new_user:
        if not is_new_user:
            await message.answer(
                t("checks.activation.errors.require_new_user", lang),
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return

    # Проверяем обязательную подписку на канал
    if check.required_channel:
        try:
            # required_channel хранится как JSON массив (ID каналов или usernames для обратной совместимости)
            channels = json.loads(check.required_channel)
            if isinstance(channels, list):
                bot = message.bot
                not_subscribed_channels = []

                for channel_value in channels:
                    try:
                        # Определяем, это ID или username
                        try:
                            chat_id = int(channel_value)
                        except (ValueError, TypeError):
                            # Старый формат - username
                            chat_id = f"@{channel_value}" if not str(channel_value).startswith("@") else channel_value

                        member = await bot.get_chat_member(
                            chat_id=chat_id,
                            user_id=user.id,
                        )
                        if member.status in ("left", "kicked"):
                            # Получаем информацию о канале
                            try:
                                chat = await bot.get_chat(chat_id)
                                channel_title = chat.title or str(channel_value)

                                # Создаём именованную invite link для чека
                                try:
                                    invite = await bot.create_chat_invite_link(
                                        chat_id=chat_id,
                                        name=f"Check #{check.code}",
                                    )
                                    channel_link = invite.invite_link
                                except Exception:
                                    # Fallback на публичную ссылку
                                    if chat.username:
                                        channel_link = f"https://t.me/{chat.username}"
                                    else:
                                        channel_link = None
                            except Exception:
                                channel_title = str(channel_value)
                                channel_link = None

                            not_subscribed_channels.append({
                                "title": channel_title,
                                "link": channel_link,
                                "id": channel_value,
                            })
                    except Exception as e:
                        # При ошибке проверки канала - блокируем активацию
                        logger.error(f"Failed to check subscription for {channel_value}: {e}")
                        await message.answer(
                            t("checks.activation.errors.channel_check_failed", lang),
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                        return

                if not_subscribed_channels:
                    # Создаём клавиатуру с кнопками каналов
                    buttons = []
                    for ch in not_subscribed_channels:
                        if ch["link"]:
                            buttons.append([
                                InlineKeyboardButton(
                                    text=f"📢 {ch['title']}",
                                    url=ch["link"],
                                )
                            ])
                    # Кнопка активации чека (проверит подписку заново)
                    buttons.append([
                        InlineKeyboardButton(
                            text=t("checks.activation.activate_btn", lang),
                            callback_data=f"check_sub:{check.code}",
                        )
                    ])

                    await message.answer(
                        t("checks.activation.errors.require_subscription_list", lang),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                        parse_mode="HTML",
                    )
                    return
        except (json.JSONDecodeError, TypeError) as e:
            # При ошибке парсинга каналов - блокируем активацию
            logger.error(f"Failed to parse required_channel for check {check.code}: {e}")
            await message.answer(
                t("checks.activation.errors.channel_check_failed", lang),
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return

    # Проверяем пароль (если установлен)
    if check.password:
        # Пароль требует интерактивного ввода - запускаем FSM
        await state.update_data(
            check_code=check_code,
            check_id=check.id,
        )
        await state.set_state(CheckActivationStates.waiting_password)
        await message.answer(
            t("checks.activation.password_prompt", lang),
            reply_markup=_get_cancel_activation_keyboard(lang),
            parse_mode="HTML",
        )
        return

    # Создаём UserService для получения пользователей с блокировкой
    user_service = UserService(session)

    # Получаем создателя чека (с блокировкой для предотвращения race condition)
    creator = await user_service.get_user_for_update(check.creator_id)

    if not creator:
        await message.answer(
            t("checks.activation.errors.creator_not_found", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Перезагружаем пользователя с блокировкой для безопасного изменения баланса
    db_user = await user_service.get_user_for_update(user.id)
    if not db_user:
        await message.answer(
            t("common.user_not_found", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Рассчитываем сумму на одну активацию
    if check.content_type == "stars":
        amount_per_activation = check.amount_stars
    else:  # premium
        amount_per_activation = Decimal(check.amount_premium_months)

    # Получаем цены для расчёта стоимости
    star_price = await get_star_price()
    premium_prices = await get_premium_prices()

    # Выполняем активацию
    is_last_activation = check.current_activations + 1 >= check.max_activations

    if check.content_type == "stars":
        # Начисляем звёзды пользователю
        db_user.balance_stars += amount_per_activation

        # Списываем с замороженного баланса создателя
        if check.payment_method == "usdt" and check.frozen_usdt > 0:
            usdt_per_activation = _calculate_frozen_amount(
                check.frozen_usdt, check.max_activations, check.current_activations
            )
            creator.frozen_usdt -= usdt_per_activation
        elif check.payment_method == "stars" and check.frozen_stars > 0:
            stars_per_activation = _calculate_frozen_amount(
                Decimal(check.frozen_stars), check.max_activations, check.current_activations
            )
            creator.frozen_stars -= stars_per_activation

        amount_text = f"{int(amount_per_activation)} ⭐"
        price_usdt = int(amount_per_activation) * star_price

    else:  # premium
        # Начисляем месяцы премиума пользователю
        db_user.balance_premium_months += check.amount_premium_months

        # Списываем с замороженного баланса создателя
        if check.payment_method == "usdt" and check.frozen_usdt > 0:
            usdt_per_activation = _calculate_frozen_amount(
                check.frozen_usdt, check.max_activations, check.current_activations
            )
            creator.frozen_usdt -= usdt_per_activation
        elif check.payment_method == "premium" and check.frozen_premium_months > 0:
            # Для premium месяцев: при последней активации - весь остаток
            if is_last_activation:
                premium_per_activation = creator.frozen_premium_months
            else:
                premium_per_activation = check.frozen_premium_months // check.max_activations
            creator.frozen_premium_months -= premium_per_activation

        months_display = pluralize_months(check.amount_premium_months, lang)
        amount_text = f"{months_display} Premium"
        price_usdt = premium_prices.get(check.amount_premium_months, Decimal("0"))

    # Создаём запись об активации
    activation = CheckActivation(
        check_id=check.id,
        user_id=user.id,
        amount_received=amount_per_activation,
    )
    session.add(activation)

    # Увеличиваем счётчик активаций
    check.current_activations += 1

    # Если все активации использованы - деактивируем чек
    if check.current_activations >= check.max_activations:
        check.is_active = False

    await session.commit()

    # Логируем
    logger.info(
        f"User {user.id} activated check {check.code}: received {amount_text}"
    )

    # Показываем сообщение об успехе
    if check.content_type == "stars":
        success_text = t("checks.activation.success", lang, amount=amount_text, price=f"{price_usdt:.2f}")
    else:
        months_text = pluralize_months(check.amount_premium_months, lang)
        success_text = t("checks.activation.success_premium", lang, months_text=months_text, price=f"{price_usdt:.2f}")

    await message.answer(
        success_text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def _start_buy_stars_flow(
    message: Message,
    state: FSMContext,
    db_user,
    amount: int,
    lang: str,
) -> None:
    """
    Начать процесс покупки звёзд с уже выбранным количеством.

    Пользователю остаётся только выбрать получателя.
    """
    from src.bot.handlers.stars import BuyStarsStates

    # Формируем текст как в стандартной покупке
    text = (
        f"{t('common.recipient.title', lang)}\n\n"
        f"{t('stars_section.recipient.enter', lang)}"
    )

    sent_message = await message.answer(
        text=text,
        reply_markup=get_recipient_keyboard(lang),
        parse_mode="HTML",
    )

    # Сохраняем данные в состояние (включая ID сообщения для редактирования)
    await state.update_data(
        lang=lang,
        amount=amount,
        amount_preset=True,  # Флаг что количество уже выбрано
        mode="buy",
        bot_message_id=sent_message.message_id,
    )

    # Устанавливаем состояние ожидания получателя
    await state.set_state(BuyStarsStates.waiting_recipient)


async def _start_buy_premium_flow(
    message: Message,
    state: FSMContext,
    db_user,
    months: int,
    lang: str,
) -> None:
    """
    Начать процесс покупки Premium с уже выбранным сроком.

    Пользователю остаётся только выбрать получателя.
    """
    from src.bot.handlers.premium import BuyPremiumStates

    # Формируем текст как в стандартной покупке
    text = (
        f"{t('common.recipient.title', lang)}\n\n"
        f"{t('premium_section.recipient.enter', lang)}"
    )

    sent_message = await message.answer(
        text=text,
        reply_markup=get_premium_recipient_keyboard(lang),
        parse_mode="HTML",
    )

    # Сохраняем данные в состояние (включая ID сообщения для редактирования)
    await state.update_data(
        lang=lang,
        duration=months,
        duration_preset=True,  # Флаг что срок уже выбран
        mode="buy",
        bot_message_id=sent_message.message_id,
    )

    # Устанавливаем состояние ожидания получателя
    await state.set_state(BuyPremiumStates.waiting_recipient)


@router.callback_query(F.data == MenuCallback.BACK_TO_MENU)
async def callback_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в главное меню."""
    # Очищаем состояние при возврате в меню
    await state.clear()

    user = callback.from_user

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        # Определяем язык до проверки пользователя
        lang = get_user_locale(user.language_code)

        if not db_user:
            await callback.answer(t("common.user_not_found", lang), show_alert=True)
            return

        # Используем язык из БД если есть
        lang = db_user.language_code or lang

        welcome_text = get_welcome_message(
            balance_usdt=db_user.balance_usdt,
            balance_stars=db_user.balance_stars,
            balance_premium_months=db_user.balance_premium_months,
            lang=lang,
        )

        try:
            bot_settings = await get_bot_settings()
            from src.services.giveaway_service import GiveawayService
            active_giveaways = await GiveawayService(session).has_active_giveaways()
            await edit_menu_message(
                callback,
                "main",
                text=f"{welcome_text}\n\n{get_legal_links_text(lang)}",
                reply_markup=get_main_menu_keyboard(
                    lang,
                    bot_settings.get("news_channel_url"),
                    is_admin=db_user.is_admin,
                    has_active_giveaways=active_giveaways,
                ),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug(f"Failed to edit menu message: {e}")

    await callback.answer()


@router.callback_query(F.data == "subscription:check")
async def callback_required_subscription_check(callback: CallbackQuery) -> None:
    """Fallback handler so subscription middleware can process the check button."""
    await callback.answer("Проверяю подписку...")


@router.callback_query(F.data == "cancel_check_activation")
async def callback_cancel_check_activation(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена активации чека."""
    await state.clear()

    user = callback.from_user
    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        lang = db_user.language_code if db_user else get_user_locale(user.language_code)

    try:
        await callback.message.edit_text(
            t("checks.activation.cancelled", lang),
            reply_markup=_get_main_menu_button_keyboard(lang),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.debug(f"Failed to edit cancel message: {e}")
    await callback.answer()


@router.callback_query(F.data.startswith("check_sub:"))
async def callback_check_subscription(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка подписки и активация чека."""
    user = callback.from_user
    check_code = callback.data.split(":")[1]

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)
        lang = db_user.language_code if db_user else get_user_locale(user.language_code)

        # Находим чек (с блокировкой для предотвращения race condition)
        result = await session.execute(
            select(Check).where(Check.code == check_code).with_for_update()
        )
        check = result.scalar_one_or_none()

        if not check or not check.required_channel:
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)
            return

        # Проверяем подписку
        try:
            channels = json.loads(check.required_channel)
            if isinstance(channels, list):
                bot = callback.bot
                not_subscribed_channels = []

                for channel_value in channels:
                    try:
                        # Определяем, это ID или username
                        try:
                            chat_id = int(channel_value)
                        except (ValueError, TypeError):
                            # Старый формат - username
                            chat_id = f"@{channel_value}" if not str(channel_value).startswith("@") else channel_value

                        member = await bot.get_chat_member(chat_id=chat_id, user_id=user.id)
                        if member.status in ("left", "kicked"):
                            # Получаем информацию о канале для кнопки
                            try:
                                chat = await bot.get_chat(chat_id)
                                channel_title = chat.title or str(channel_value)
                                # Создаём именованную invite link для чека
                                try:
                                    invite = await bot.create_chat_invite_link(
                                        chat_id=chat_id,
                                        name=f"Check #{check.code}",
                                    )
                                    channel_link = invite.invite_link
                                except Exception:
                                    if chat.username:
                                        channel_link = f"https://t.me/{chat.username}"
                                    else:
                                        channel_link = None
                            except Exception:
                                channel_title = str(channel_value)
                                channel_link = None

                            not_subscribed_channels.append({
                                "title": channel_title,
                                "link": channel_link,
                            })
                    except Exception as e:
                        # При ошибке проверки канала - блокируем активацию
                        logger.error(f"Failed to check subscription for {channel_value}: {e}")
                        await callback.answer(
                            t("checks.activation.errors.channel_check_failed", lang),
                            show_alert=True,
                        )
                        return

                if not_subscribed_channels:
                    # Показываем обновлённые кнопки с каналами
                    buttons = []
                    for ch in not_subscribed_channels:
                        if ch["link"]:
                            buttons.append([
                                InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["link"])
                            ])
                    buttons.append([
                        InlineKeyboardButton(
                            text=t("checks.activation.activate_btn", lang),
                            callback_data=f"check_sub:{check.code}",
                        )
                    ])

                    await callback.answer(
                        t("checks.activation.not_subscribed_yet", lang),
                        show_alert=True,
                    )

                    # Обновляем сообщение с новыми кнопками
                    try:
                        await callback.message.edit_reply_markup(
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
                        )
                    except Exception as e:
                        logger.debug(f"Failed to edit reply markup: {e}")
                    return

                # Все подписки проверены - активируем чек

                # Удаляем сообщение с кнопками подписки
                try:
                    await callback.message.delete()
                except Exception as e:
                    logger.debug(f"Failed to delete subscription message: {e}")

                # Вызываем активацию
                await _activate_check_after_subscription(
                    callback, session, db_user, check, lang
                )

        except (json.JSONDecodeError, TypeError):
            await callback.answer(t("checks.errors.not_found", lang), show_alert=True)


async def _activate_check_after_subscription(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    check: Check,
    lang: str,
) -> None:
    """Активация чека после проверки подписки."""
    user = callback.from_user
    keyboard = _get_main_menu_button_keyboard(lang)

    # Проверяем, не активировал ли пользователь уже этот чек
    existing_activation = await session.execute(
        select(CheckActivation).where(
            CheckActivation.check_id == check.id,
            CheckActivation.user_id == user.id,
        )
    )
    if existing_activation.scalar_one_or_none():
        await callback.message.answer(
            t("checks.activation.errors.already_activated", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Все активации использованы
    if check.current_activations >= check.max_activations:
        await callback.message.answer(
            t("checks.activation.errors.exhausted", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Создаём UserService для получения пользователей с блокировкой
    user_service = UserService(session)

    # Получаем создателя чека (с блокировкой для предотвращения race condition)
    creator = await user_service.get_user_for_update(check.creator_id)

    if not creator:
        await callback.message.answer(
            t("checks.activation.errors.creator_not_found", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Перезагружаем пользователя с блокировкой для безопасного изменения баланса
    db_user = await user_service.get_user_for_update(user.id)
    if not db_user:
        await callback.message.answer(
            t("common.user_not_found", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Рассчитываем сумму на одну активацию
    if check.content_type == "stars":
        amount_per_activation = check.amount_stars
    else:
        amount_per_activation = Decimal(check.amount_premium_months)

    # Получаем цены для расчёта стоимости
    star_price = await get_star_price()
    premium_prices = await get_premium_prices()

    # Выполняем активацию
    is_last_activation = check.current_activations + 1 >= check.max_activations

    if check.content_type == "stars":
        db_user.balance_stars += amount_per_activation
        if check.payment_method == "usdt" and check.frozen_usdt > 0:
            usdt_per_activation = _calculate_frozen_amount(
                check.frozen_usdt, check.max_activations, check.current_activations
            )
            creator.frozen_usdt -= usdt_per_activation
        elif check.payment_method == "stars" and check.frozen_stars > 0:
            stars_per_activation = _calculate_frozen_amount(
                Decimal(check.frozen_stars), check.max_activations, check.current_activations
            )
            creator.frozen_stars -= stars_per_activation
        amount_text = f"{int(amount_per_activation)} ⭐"
        price_usdt = int(amount_per_activation) * star_price
    else:
        db_user.balance_premium_months += check.amount_premium_months
        if check.payment_method == "usdt" and check.frozen_usdt > 0:
            usdt_per_activation = _calculate_frozen_amount(
                check.frozen_usdt, check.max_activations, check.current_activations
            )
            creator.frozen_usdt -= usdt_per_activation
        elif check.payment_method == "premium" and check.frozen_premium_months > 0:
            if is_last_activation:
                premium_per_activation = creator.frozen_premium_months
            else:
                premium_per_activation = check.frozen_premium_months // check.max_activations
            creator.frozen_premium_months -= premium_per_activation
        months_display = pluralize_months(check.amount_premium_months, lang)
        amount_text = f"{months_display} Premium"
        price_usdt = premium_prices.get(check.amount_premium_months, Decimal("0"))

    # Создаём запись об активации
    activation = CheckActivation(
        check_id=check.id,
        user_id=user.id,
        amount_received=amount_per_activation,
    )
    session.add(activation)

    check.current_activations += 1
    if check.current_activations >= check.max_activations:
        check.is_active = False

    await session.commit()

    logger.info(f"User {user.id} activated check {check.code}: received {amount_text}")

    # Показываем сообщение об успехе
    if check.content_type == "stars":
        success_text = t("checks.activation.success", lang, amount=amount_text, price=f"{price_usdt:.2f}")
    else:
        months_text = pluralize_months(check.amount_premium_months, lang)
        success_text = t("checks.activation.success_premium", lang, months_text=months_text, price=f"{price_usdt:.2f}")

    await callback.message.answer(
        success_text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.message(CheckActivationStates.waiting_password)
async def message_check_password(message: Message, state: FSMContext) -> None:
    """Обработка ввода пароля для активации чека."""
    user = message.from_user
    data = await state.get_data()
    check_code = data.get("check_code")

    # Определяем язык до работы с БД
    lang = get_user_locale(user.language_code)

    # Проверяем что пользователь отправил текст
    if not message.text:
        await message.answer(
            t("checks.activation.errors.text_required", lang),
            reply_markup=_get_cancel_activation_keyboard(lang),
            parse_mode="HTML",
        )
        return

    async with async_session_factory() as session:
        user_service = UserService(session)
        db_user = await user_service.get_user(user.id)

        if not db_user:
            await message.answer(t("common.user_not_found", lang))
            await state.clear()
            return

        # Обновляем язык из БД если есть
        lang = db_user.language_code or lang
        keyboard = _get_main_menu_button_keyboard(lang)

        # Находим чек (с блокировкой для предотвращения race condition)
        result = await session.execute(
            select(Check).where(Check.code == check_code).with_for_update()
        )
        check = result.scalar_one_or_none()

        if not check:
            await message.answer(
                t("checks.activation.errors.not_found", lang),
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            await state.clear()
            return

        # Проверяем rate limit для защиты от брутфорса
        allowed, wait_seconds = _check_password_rate_limit(message.from_user.id)
        if not allowed:
            await message.answer(
                t("checks.activation.errors.too_many_attempts", lang, seconds=wait_seconds),
                reply_markup=_get_cancel_activation_keyboard(lang),
                parse_mode="HTML",
            )
            return

        # Проверяем пароль
        entered_password = message.text.strip()
        if entered_password != check.password:
            # Записываем неудачную попытку
            _record_failed_password_attempt(message.from_user.id)

            await message.answer(
                t("checks.activation.errors.wrong_password", lang),
                reply_markup=_get_cancel_activation_keyboard(lang),
                parse_mode="HTML",
            )
            return

        # Пароль верный - очищаем состояние и продолжаем активацию
        await state.clear()

        # Продолжаем активацию чека (все проверки уже пройдены кроме пароля)
        await _complete_check_activation(message, session, db_user, check, lang)


async def _complete_check_activation(
    message: Message,
    session: AsyncSession,
    db_user: User,
    check: Check,
    lang: str,
) -> None:
    """Завершить активацию чека (после всех проверок)."""
    user = message.from_user
    keyboard = _get_main_menu_button_keyboard(lang)

    # Проверяем, не активировал ли пользователь уже этот чек
    existing_activation = await session.execute(
        select(CheckActivation).where(
            CheckActivation.check_id == check.id,
            CheckActivation.user_id == user.id,
        )
    )
    if existing_activation.scalar_one_or_none():
        await message.answer(
            t("checks.activation.errors.already_activated", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Все активации использованы
    if check.current_activations >= check.max_activations:
        await message.answer(
            t("checks.activation.errors.exhausted", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Создаём UserService для получения пользователей с блокировкой
    user_service = UserService(session)

    # Получаем создателя чека (с блокировкой для предотвращения race condition)
    creator = await user_service.get_user_for_update(check.creator_id)

    if not creator:
        await message.answer(
            t("checks.activation.errors.creator_not_found", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Перезагружаем пользователя с блокировкой для безопасного изменения баланса
    db_user = await user_service.get_user_for_update(user.id)
    if not db_user:
        await message.answer(
            t("common.user_not_found", lang),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Рассчитываем сумму на одну активацию
    if check.content_type == "stars":
        amount_per_activation = check.amount_stars
    else:  # premium
        amount_per_activation = Decimal(check.amount_premium_months)

    # Получаем цены для расчёта стоимости
    star_price = await get_star_price()
    premium_prices = await get_premium_prices()

    # Выполняем активацию
    is_last_activation = check.current_activations + 1 >= check.max_activations

    if check.content_type == "stars":
        # Начисляем звёзды пользователю
        db_user.balance_stars += amount_per_activation

        # Списываем с замороженного баланса создателя
        if check.payment_method == "usdt" and check.frozen_usdt > 0:
            usdt_per_activation = _calculate_frozen_amount(
                check.frozen_usdt, check.max_activations, check.current_activations
            )
            creator.frozen_usdt -= usdt_per_activation
        elif check.payment_method == "stars" and check.frozen_stars > 0:
            stars_per_activation = _calculate_frozen_amount(
                Decimal(check.frozen_stars), check.max_activations, check.current_activations
            )
            creator.frozen_stars -= stars_per_activation

        amount_text = f"{int(amount_per_activation)} ⭐"
        price_usdt = int(amount_per_activation) * star_price

    else:  # premium
        # Начисляем месяцы премиума пользователю
        db_user.balance_premium_months += check.amount_premium_months

        # Списываем с замороженного баланса создателя
        if check.payment_method == "usdt" and check.frozen_usdt > 0:
            usdt_per_activation = _calculate_frozen_amount(
                check.frozen_usdt, check.max_activations, check.current_activations
            )
            creator.frozen_usdt -= usdt_per_activation
        elif check.payment_method == "premium" and check.frozen_premium_months > 0:
            if is_last_activation:
                premium_per_activation = creator.frozen_premium_months
            else:
                premium_per_activation = check.frozen_premium_months // check.max_activations
            creator.frozen_premium_months -= premium_per_activation

        months_display = pluralize_months(check.amount_premium_months, lang)
        amount_text = f"{months_display} Premium"
        price_usdt = premium_prices.get(check.amount_premium_months, Decimal("0"))

    # Создаём запись об активации
    activation = CheckActivation(
        check_id=check.id,
        user_id=user.id,
        amount_received=amount_per_activation,
    )
    session.add(activation)

    # Увеличиваем счётчик активаций
    check.current_activations += 1

    # Если все активации использованы - деактивируем чек
    if check.current_activations >= check.max_activations:
        check.is_active = False

    await session.commit()

    # Логируем
    logger.info(
        f"User {user.id} activated check {check.code}: received {amount_text}"
    )

    # Показываем сообщение об успехе
    if check.content_type == "stars":
        success_text = t("checks.activation.success", lang, amount=amount_text, price=f"{price_usdt:.2f}")
    else:
        months_text = pluralize_months(check.amount_premium_months, lang)
        success_text = t("checks.activation.success_premium", lang, months_text=months_text, price=f"{price_usdt:.2f}")

    await message.answer(
        success_text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
