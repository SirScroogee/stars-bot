"""
Клавиатура календаря для выбора дат.
"""
import calendar
from datetime import date, datetime
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class CalendarCallback:
    """Callback data для календаря."""

    PREFIX = "cal"

    # Навигация
    PREV_MONTH = "cal:prev:{year}:{month}"
    NEXT_MONTH = "cal:next:{year}:{month}"

    # Выбор дня
    DAY = "cal:day:{year}:{month}:{day}:{context}"

    # Игнор (пустые ячейки)
    IGNORE = "cal:ignore"

    # Отмена
    CANCEL = "cal:cancel:{context}"

    @staticmethod
    def parse_day(callback_data: str) -> Optional[dict]:
        """Распарсить callback выбора дня."""
        if not callback_data.startswith("cal:day:"):
            return None

        parts = callback_data.split(":")
        if len(parts) < 6:
            return None

        try:
            return {
                "year": int(parts[2]),
                "month": int(parts[3]),
                "day": int(parts[4]),
                "context": parts[5],
            }
        except ValueError:
            return None


def get_calendar_keyboard(
    year: int,
    month: int,
    context: str = "stats",
    min_date: Optional[date] = None,
    max_date: Optional[date] = None,
) -> InlineKeyboardMarkup:
    """
    Создать клавиатуру календаря.

    Args:
        year: Год
        month: Месяц (1-12)
        context: Контекст использования (stats_from, stats_to, etc.)
        min_date: Минимальная доступная дата
        max_date: Максимальная доступная дата (по умолчанию сегодня)
    """
    if max_date is None:
        max_date = date.today()

    keyboard = []

    # Заголовок с месяцем и годом
    month_names = [
        "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
    ]

    # Навигация по месяцам
    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1

    # Проверяем, можно ли переключиться назад/вперёд
    can_prev = min_date is None or date(prev_year, prev_month, 1) >= min_date.replace(day=1)
    can_next = date(next_year, next_month, 1) <= max_date.replace(day=1)

    nav_row = [
        InlineKeyboardButton(
            text="◀" if can_prev else " ",
            callback_data=f"cal:prev:{prev_year}:{prev_month}:{context}" if can_prev else CalendarCallback.IGNORE,
        ),
        InlineKeyboardButton(
            text=f"{month_names[month]} {year}",
            callback_data=CalendarCallback.IGNORE,
        ),
        InlineKeyboardButton(
            text="▶" if can_next else " ",
            callback_data=f"cal:next:{next_year}:{next_month}:{context}" if can_next else CalendarCallback.IGNORE,
        ),
    ]
    keyboard.append(nav_row)

    # Дни недели
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([
        InlineKeyboardButton(text=day, callback_data=CalendarCallback.IGNORE)
        for day in weekdays
    ])

    # Дни месяца
    cal = calendar.monthcalendar(year, month)

    for week in cal:
        week_row = []
        for day in week:
            if day == 0:
                week_row.append(
                    InlineKeyboardButton(text=" ", callback_data=CalendarCallback.IGNORE)
                )
            else:
                current_date = date(year, month, day)

                # Проверяем, доступна ли дата
                is_available = True
                if min_date and current_date < min_date:
                    is_available = False
                if max_date and current_date > max_date:
                    is_available = False

                if is_available:
                    week_row.append(
                        InlineKeyboardButton(
                            text=str(day),
                            callback_data=f"cal:day:{year}:{month}:{day}:{context}",
                        )
                    )
                else:
                    week_row.append(
                        InlineKeyboardButton(text="·", callback_data=CalendarCallback.IGNORE)
                    )

        keyboard.append(week_row)

    # Быстрые кнопки
    quick_row = [
        InlineKeyboardButton(text="Сегодня", callback_data=f"cal:today:{context}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"cal:cancel:{context}"),
    ]
    keyboard.append(quick_row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_period_selection_keyboard(section: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора периода с кастомным диапазоном."""
    periods = [
        ("24h", "24ч"),
        ("7d", "7д"),
        ("30d", "30д"),
        ("all", "Всё"),
    ]

    period_buttons = [
        InlineKeyboardButton(
            text=period_name,
            callback_data=f"admin:stats:{section}:{period_key}"
        )
        for period_key, period_name in periods
    ]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            period_buttons,
            [
                InlineKeyboardButton(
                    text="📅 Выбрать период",
                    callback_data=f"admin:stats:{section}:custom",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ К разделам",
                    callback_data="admin:stats:back",
                ),
            ],
        ]
    )
