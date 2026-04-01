"""
Revory — Bulk Delete handler.
Массовое удаление событий по фильтру (цвет, дата, период).
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update

from services.sync import sync_calendar
from services.database import get_events_by_color
from handlers.pending import set_pending
from handlers.events import GOOGLE_COLOR_NAME_RU, GOOGLE_COLOR_EMOJI

logger = logging.getLogger(__name__)


async def handle_bulk_delete(
    update: Update,
    user_id,
    parsed: dict,
    user_now: datetime,
    tz_name: str,
):
    """
    Массовое удаление событий по фильтру (цвет + период).
    Показывает список найденных событий и запрашивает подтверждение через pending.
    """
    color_id = parsed.get("color_id")
    if isinstance(color_id, float):
        color_id = int(color_id)

    period = parsed.get("period")
    date_str = parsed.get("date")

    # Вычисляем временной диапазон
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
            time_min = day.replace(hour=0, minute=0, second=0, tzinfo=user_now.tzinfo)
            time_max = time_min + timedelta(days=1)
            range_label = day.strftime("%d.%m.%Y")
        except ValueError:
            time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
            time_max = time_min + timedelta(days=7)
            range_label = "ближайшие 7 дней"
    elif period == "today":
        time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)
        range_label = "сегодня"
    elif period == "tomorrow":
        time_min = (user_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)
        range_label = "завтра"
    elif period == "week":
        time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=7)
        range_label = "на неделю"
    else:
        time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=7)
        range_label = "ближайшие 7 дней"

    try:
        await sync_calendar(user_id)
    except Exception as e:
        logger.error(f"Sync failed before bulk delete: {e}")

    events = await get_events_by_color(user_id, color_id, time_min, time_max)

    if not events:
        if color_id:
            color_name = GOOGLE_COLOR_NAME_RU.get(color_id, f"цвет {color_id}")
            color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "")
            r = f"🔍 Не нашла {color_emoji} {color_name} событий за {range_label}."
        else:
            r = f"🔍 Не нашла событий за {range_label}."
        await update.message.reply_text(r)
        return r

    tz = ZoneInfo(tz_name)
    if color_id:
        color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "")
        header = f"🗑️ Найдено {len(events)} {color_emoji} событий за {range_label}:\n"
    else:
        header = f"🗑️ Найдено {len(events)} событий за {range_label}:\n"

    lines = [header]
    for e in events:
        start = e["start_time"].astimezone(tz)
        lines.append(f"• {start.strftime('%d.%m %H:%M')} — {e['title']}")
    lines.append("\nУдалить все? Напиши «да» или «отмена».")

    set_pending(user_id, "bulk_delete_confirm", {"events": events})
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r
