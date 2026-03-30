"""
Revory — Reminders handler.
Создание напоминаний.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update

from services.database import save_reminder

logger = logging.getLogger(__name__)


async def handle_remind(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
    """Создаёт напоминание и сохраняет в БД."""
    title = parsed.get("title")
    date_str = parsed.get("date")
    time_str = parsed.get("time")
    if not title:
        r = "🤔 О чём напомнить? Попробуй: «напомни позвонить маме в 18:00»"
        await update.message.reply_text(r)
        return r
    if not time_str:
        r = "⏰ Укажи время. Например: «напомни купить молоко завтра в 10:00»"
        await update.message.reply_text(r)
        return r
    if not date_str:
        date_str = user_now.strftime("%Y-%m-%d")
    try:
        remind_naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        r = "❌ Не смогла разобрать дату/время."
        await update.message.reply_text(r)
        return r
    tz = ZoneInfo(tz_name)
    remind_at = remind_naive.replace(tzinfo=tz)
    if remind_at <= user_now:
        r = "⏰ Это время уже прошло. Укажи будущее время."
        await update.message.reply_text(r)
        return r
    await save_reminder(user_id, title, remind_at)
    remind_fmt = remind_naive.strftime("%d.%m.%Y в %H:%M")
    r = f"✅ Напоминание установлено!\n📌 {title}\n⏰ {remind_fmt}"
    await update.message.reply_text(r)
    return r
