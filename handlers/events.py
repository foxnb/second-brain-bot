"""
Revory — Events handlers.
Создание, показ, удаление событий календаря.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update

from services.calendar import create_event, delete_event
from services.sync import sync_calendar
from services.database import (
    get_events_from_db,
    find_event_by_title,
    soft_delete_event,
)
from handlers.pending import set_pending

logger = logging.getLogger(__name__)


async def handle_create(update: Update, user_id, parsed: dict):
    """Создаёт событие в Google Calendar + зеркало в БД."""
    title = parsed.get("title")
    date_str = parsed.get("date")
    time_str = parsed.get("time")
    if not title:
        r = "🤔 Не понял название события. Попробуй ещё раз."
        await update.message.reply_text(r)
        return r
    if not date_str or not time_str:
        r = f"📅 {parsed.get('reply', 'Укажи дату и время для события.')}"
        await update.message.reply_text(r)
        return r
    try:
        start_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        r = "❌ Не смог разобрать дату/время. Попробуй: завтра в 15:00"
        await update.message.reply_text(r)
        return r
    end_time_str = parsed.get("end_time")
    end_time = None
    if end_time_str:
        try:
            end_time = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    result = await create_event(user_id, title, start_time, end_time)
    if result:
        start_fmt = start_time.strftime("%d.%m.%Y в %H:%M")
        r = f"✅ Создано: **{result['title']}**\n📅 {start_fmt}\n🔗 {result['link']}"
        await update.message.reply_text(r, parse_mode="Markdown")
        return r
    else:
        r = "❌ Не удалось создать событие. Попробуй позже."
        await update.message.reply_text(r)
        return r


async def handle_show(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
    """Показывает расписание из БД (после ленивой sync)."""
    period = parsed.get("period", "today")
    if period == "tomorrow":
        time_min = (user_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)
        label = "завтра"
    elif period == "week":
        time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=7)
        label = "на неделю"
    else:
        time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)
        label = "сегодня"
    date_str = parsed.get("date")
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
            time_min = day.replace(hour=0, minute=0, second=0, tzinfo=user_now.tzinfo)
            time_max = time_min + timedelta(days=1)
            label = day.strftime("%d.%m.%Y")
        except ValueError:
            pass
    try:
        await sync_calendar(user_id)
    except Exception as e:
        logger.error(f"Sync failed for user {user_id}, reading stale data: {e}")
    events = await get_events_from_db(user_id, time_min, time_max)
    if events is None:
        r = "❌ Ошибка при загрузке событий."
        await update.message.reply_text(r)
        return r
    if not events:
        r = f"📭 На {label} событий нет. Свободна как ветер!"
        await update.message.reply_text(r)
        return r
    tz = ZoneInfo(tz_name)
    lines = [f"📅 Расписание {label}:\n"]
    for e in events:
        start = e["start_time"]
        start_local = start.astimezone(tz) if start.tzinfo else start
        lines.append(f"• {start_local.strftime('%H:%M')} — {e['title']}")
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def handle_delete(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
    """Удаляет событие: ищет в БД, удаляет из Google + мягкое удаление."""
    title_query = (parsed.get("title") or "").lower()
    if not title_query:
        r = "🤔 Какое именно событие удалить?"
        await update.message.reply_text(r)
        return r
    date_str = parsed.get("date")
    period = parsed.get("period")
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
            time_min = day.replace(hour=0, minute=0, second=0, tzinfo=user_now.tzinfo)
            time_max = time_min + timedelta(days=1)
        except ValueError:
            time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
            time_max = time_min + timedelta(days=7)
    elif period == "today":
        time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)
    elif period == "tomorrow":
        time_min = (user_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)
    else:
        time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=7)
    try:
        await sync_calendar(user_id)
    except Exception as e:
        logger.error(f"Sync failed before delete for user {user_id}: {e}")
    matches = await find_event_by_title(user_id, title_query, time_min, time_max)
    if not matches:
        r = f"🔍 Не нашла событие \"{parsed.get('title')}\" на ближайшую неделю."
        await update.message.reply_text(r)
        return r
    if len(matches) == 1:
        event = matches[0]
        external_id = event.get("external_event_id")
        if external_id:
            success = await delete_event(user_id, external_id)
        else:
            await soft_delete_event(event["id"])
            success = True
        r = f"🗑️ Удалено: {event['title']}" if success else "❌ Не удалось удалить. Попробуй позже."
        await update.message.reply_text(r)
        return r
    else:
        tz = ZoneInfo(tz_name)
        lines = ["Нашла несколько совпадений. Какое удалить?\n"]
        for i, e in enumerate(matches, 1):
            start = e["start_time"]
            start_local = start.astimezone(tz) if start.tzinfo else start
            lines.append(f"{i}. {e['title']} — {start_local.strftime('%d.%m %H:%M')}")
        lines.append("\nНапиши номер или «отмена».")
        set_pending(user_id, "delete_choice", {"matches": matches})
        r = "\n".join(lines)
        await update.message.reply_text(r)
        return r
