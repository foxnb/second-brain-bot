"""
Revory — Text Handler (Schema v9)
Роутер: принимает текст → AI парсит → вызывает calendar.
Работает с UUID user_id через маппинг telegram_id → UUID.
"""

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from services.ai import parse_message
from services.calendar import (
    get_credentials,
    create_event,
    get_events,
    delete_event,
)
from services.database import get_user_id_by_telegram, load_timezone

logger = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Moscow"


async def _resolve_user(telegram_id: int):
    """Получает UUID по telegram_id. Кэш можно добавить позже."""
    return await get_user_id_by_telegram(telegram_id)


async def _get_user_now(user_id) -> tuple[datetime, str]:
    """Возвращает (текущее время пользователя, IANA timezone)."""
    tz_name = await load_timezone(user_id) or DEFAULT_TZ
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    return now, tz_name


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик текстовых сообщений."""
    text = update.message.text.strip()
    telegram_id = update.message.from_user.id

    # --- Маппинг telegram_id → UUID ---
    user_id = await _resolve_user(telegram_id)
    if not user_id:
        await update.message.reply_text("❌ Ошибка: пользователь не найден. Нажми /start")
        return

    # --- Проверка: подключён ли календарь ---
    creds = await get_credentials(user_id)
    if not creds:
        await update.message.reply_text(
            "🔑 Сначала подключи Google Calendar.\n"
            "Нажми /auth чтобы начать."
        )
        return

    # --- Получаем timezone пользователя ---
    user_now, tz_name = await _get_user_now(user_id)

    # --- Отправляем текст в AI (с timezone) ---
    await update.message.chat.send_action("typing")
    parsed = await parse_message(text, user_now=user_now, tz_name=tz_name)
    intent = parsed.get("intent", "unknown")

    logger.info(f"User {user_id} | Intent: {intent} | Parsed: {parsed}")

    # --- Роутинг по intent ---
    if intent == "create_event":
        await _handle_create(update, user_id, parsed)

    elif intent == "show_events":
        await _handle_show(update, user_id, parsed, user_now)

    elif intent == "delete_event":
        await _handle_delete(update, user_id, parsed, user_now)

    elif intent == "remind":
        reply = parsed.get("reply", "Напоминания скоро будут!")
        await update.message.reply_text(f"⏰ {reply}")

    else:
        reply = parsed.get("reply", "Не совсем понял. Попробуй по-другому?")
        await update.message.reply_text(reply)


# ─── Создание события ─────────────────────────────────────

async def _handle_create(update: Update, user_id, parsed: dict):
    title = parsed.get("title")
    date_str = parsed.get("date")
    time_str = parsed.get("time")

    if not title:
        await update.message.reply_text("🤔 Не понял название события. Попробуй ещё раз.")
        return

    if not date_str or not time_str:
        reply = parsed.get("reply", "Укажи дату и время для события.")
        await update.message.reply_text(f"📅 {reply}")
        return

    try:
        start_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text("❌ Не смог разобрать дату/время. Попробуй: завтра в 15:00")
        return

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
        await update.message.reply_text(
            f"✅ Создано: **{result['title']}**\n"
            f"📅 {start_fmt}\n"
            f"🔗 {result['link']}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Не удалось создать событие. Попробуй позже.")


# ─── Показ событий ────────────────────────────────────────

async def _handle_show(update: Update, user_id, parsed: dict, user_now: datetime):
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

    # Убираем tzinfo для передачи в calendar (он сам добавит timezone)
    time_min_naive = time_min.replace(tzinfo=None)
    time_max_naive = time_max.replace(tzinfo=None)

    events = await get_events(user_id, time_min_naive, time_max_naive)

    if events is None:
        await update.message.reply_text("❌ Ошибка при загрузке событий.")
        return

    if not events:
        await update.message.reply_text(f"📭 На {label} событий нет. Свободна как ветер!")
        return

    lines = [f"📅 **Расписание {label}:**\n"]
    for e in events:
        start = e["start"]
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            time_fmt = dt.strftime("%H:%M")
        except Exception:
            time_fmt = start
        lines.append(f"• {time_fmt} — {e['title']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Удаление события ─────────────────────────────────────

async def _handle_delete(update: Update, user_id, parsed: dict, user_now: datetime):
    title_query = (parsed.get("title") or "").lower()

    if not title_query:
        await update.message.reply_text("🤔 Какое именно событие удалить?")
        return

    time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = time_min + timedelta(days=7)

    time_min_naive = time_min.replace(tzinfo=None)
    time_max_naive = time_max.replace(tzinfo=None)

    events = await get_events(user_id, time_min_naive, time_max_naive, max_results=20)

    if not events:
        await update.message.reply_text("📭 Не нашёл событий для удаления.")
        return

    matches = [e for e in events if title_query in e["title"].lower()]

    if not matches:
        await update.message.reply_text(
            f"🔍 Не нашёл событие \"{parsed.get('title')}\" на ближайшую неделю."
        )
        return

    if len(matches) == 1:
        event = matches[0]
        success = await delete_event(user_id, event["id"])
        if success:
            await update.message.reply_text(f"🗑️ Удалено: {event['title']}")
        else:
            await update.message.reply_text("❌ Не удалось удалить. Попробуй позже.")
    else:
        lines = ["Нашёл несколько совпадений. Какое удалить?\n"]
        for i, e in enumerate(matches, 1):
            lines.append(f"{i}. {e['title']} — {e['start']}")
        await update.message.reply_text("\n".join(lines))