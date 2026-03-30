"""
Revory — Утилиты для handlers.
Общие функции, используемые из нескольких модулей.
"""

import re
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.database import get_internal_user_id, load_timezone

logger = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Moscow"


def extract_number(text: str) -> int | None:
    """Извлекает число из текста: '1', 'удали 2', 'третий'."""
    words_to_num = {
        "первый": 1, "первое": 1, "первая": 1, "первую": 1,
        "второй": 2, "второе": 2, "вторая": 2, "вторую": 2,
        "третий": 3, "третье": 3, "третья": 3, "третью": 3,
        "четвёртый": 4, "четвертый": 4, "четвёртое": 4, "четвертое": 4,
        "пятый": 5, "пятое": 5, "пятая": 5,
    }
    lower = text.lower().strip()
    for word, num in words_to_num.items():
        if word in lower:
            return num
    match = re.search(r"\d+", lower)
    if match:
        return int(match.group())
    return None


def format_date_label(target_date, user_now: datetime) -> str:
    """Форматирует дату: 'на сегодня', 'на завтра', 'на 31.03'."""
    if target_date is None:
        return ""
    today = user_now.date()
    tomorrow = today + timedelta(days=1)
    if target_date == today:
        return " на сегодня"
    elif target_date == tomorrow:
        return " на завтра"
    else:
        return f" на {target_date.strftime('%d.%m')}"


def make_checklist_name(base_name: str, target_date, user_now: datetime) -> str:
    """'Покупки' → 'Покупки 30.03'."""
    if target_date is None:
        return base_name
    return f"{base_name} {target_date.strftime('%d.%m')}"


async def resolve_user(telegram_id: int):
    """Получает UUID по telegram_id."""
    return await get_internal_user_id(telegram_id)


async def get_user_now(user_id) -> tuple[datetime, str]:
    """Возвращает (текущее время пользователя, IANA timezone)."""
    tz_name = await load_timezone(user_id) or DEFAULT_TZ
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    return now, tz_name
