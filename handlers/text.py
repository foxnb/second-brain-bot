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
from services.database import get_internal_user_id, load_timezone, save_reminder, save_message, get_recent_messages

logger = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Moscow"


async def _resolve_user(telegram_id: int):
    """Получает UUID по telegram_id. Кэш можно добавить позже."""
    return await get_internal_user_id(telegram_id)


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
    logger.info(f"Checking credentials for user_id={user_id} (telegram={telegram_id})")
    creds = await get_credentials(user_id)
    if not creds:
        logger.warning(f"No credentials found for user_id={user_id}")
        await update.message.reply_text(
            "🔑 Сначала подключи Google Calendar.\n"
            "Нажми /auth чтобы начать."
        )
        return

    # --- Получаем timezone пользователя ---
    user_now, tz_name = await _get_user_now(user_id)

    # --- Загружаем историю диалога ---
    history = await get_recent_messages(user_id, limit=10)

    # --- Отправляем текст в AI (с timezone + историей) ---
    await update.message.chat.send_action("typing")
    parsed = await parse_message(text, user_now=user_now, tz_name=tz_name, history=history)
    intent = parsed.get("intent", "unknown")

    logger.info(f"User {user_id} | Intent: {intent} | Parsed: {parsed}")

    # --- Сохраняем сообщение пользователя ---
    await save_message(user_id, "user", text)

    # --- Роутинг по intent ---
    reply_text = None

    if intent == "create_event":
        reply_text = await _handle_create(update, user_id, parsed)

    elif intent == "show_events":
        reply_text = await _handle_show(update, user_id, parsed, user_now)

    elif intent == "delete_event":
        reply_text = await _handle_delete(update, user_id, parsed, user_now)

    elif intent == "remind":
        reply_text = await _handle_remind(update, user_id, parsed, user_now, tz_name)

    elif intent == "change_timezone":
        tz_name_current = await load_timezone(user_id)
        if tz_name_current:
            reply_text = f"⏰ Текущий часовой пояс: {tz_name_current}\n\nХочешь сменить? Нажми /timezone"
        else:
            reply_text = "⏰ Часовой пояс не установлен. Нажми /timezone"
        await update.message.reply_text(reply_text)

    elif intent == "connect_calendar":
        reply_text = "🔑 Чтобы подключить календарь, используй команду /auth"
        await update.message.reply_text(reply_text)

    elif intent == "help":
        reply_text = (
            "🗓️ Вот что я умею:\n\n"
            "• Создать событие — «встреча завтра в 15:00 с клиентом»\n"
            "• Показать расписание — «что у меня сегодня?»\n"
            "• Удалить событие — «удали встречу с клиентом»\n"
            "• Напоминание — «напомни в 10 утра купить продукты»\n\n"
            "📌 Команды:\n"
            "/auth — подключить календарь\n"
            "/timezone — сменить часовой пояс\n\n"
            "Просто пиши как думаешь!"
        )
        await update.message.reply_text(reply_text)

    elif intent == "chitchat":
        reply_text = parsed.get("reply", "Привет! Чем могу помочь?")
        await update.message.reply_text(reply_text)

    else:
        reply_text = parsed.get("reply", "Не совсем поняла. Попробуй написать что-нибудь вроде «встреча завтра в 15:00» или «что у меня сегодня?»")
        await update.message.reply_text(reply_text)

    # --- Сохраняем ответ ассистента ---
    if reply_text:
        await save_message(user_id, "assistant", reply_text)


# ─── Создание события ─────────────────────────────────────

async def _handle_create(update: Update, user_id, parsed: dict):
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
        r = "❌ Ошибка при загрузке событий."
        await update.message.reply_text(r)
        return r

    if not events:
        r = f"📭 На {label} событий нет. Свободна как ветер!"
        await update.message.reply_text(r)
        return r

    lines = [f"📅 **Расписание {label}:**\n"]
    for e in events:
        start = e["start"]
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            time_fmt = dt.strftime("%H:%M")
        except Exception:
            time_fmt = start
        lines.append(f"• {time_fmt} — {e['title']}")

    r = "\n".join(lines)
    await update.message.reply_text(r, parse_mode="Markdown")
    return r


# ─── Удаление события ─────────────────────────────────────

async def _handle_delete(update: Update, user_id, parsed: dict, user_now: datetime):
    title_query = (parsed.get("title") or "").lower()

    if not title_query:
        r = "🤔 Какое именно событие удалить?"
        await update.message.reply_text(r)
        return r

    time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = time_min + timedelta(days=7)

    time_min_naive = time_min.replace(tzinfo=None)
    time_max_naive = time_max.replace(tzinfo=None)

    events = await get_events(user_id, time_min_naive, time_max_naive, max_results=20)

    if not events:
        r = "📭 Не нашёл событий для удаления."
        await update.message.reply_text(r)
        return r

    matches = [e for e in events if title_query in e["title"].lower()]

    if not matches:
        r = f"🔍 Не нашёл событие \"{parsed.get('title')}\" на ближайшую неделю."
        await update.message.reply_text(r)
        return r

    if len(matches) == 1:
        event = matches[0]
        success = await delete_event(user_id, event["id"])
        if success:
            r = f"🗑️ Удалено: {event['title']}"
        else:
            r = "❌ Не удалось удалить. Попробуй позже."
        await update.message.reply_text(r)
        return r
    else:
        lines = ["Нашёл несколько совпадений. Какое удалить?\n"]
        for i, e in enumerate(matches, 1):
            lines.append(f"{i}. {e['title']} — {e['start']}")
        r = "\n".join(lines)
        await update.message.reply_text(r)
        return r


# ─── Напоминание ──────────────────────────────────────────

async def _handle_remind(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
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

    # Собираем дату+время
    if not date_str:
        date_str = user_now.strftime("%Y-%m-%d")

    try:
        remind_naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        r = "❌ Не смогла разобрать дату/время."
        await update.message.reply_text(r)
        return r

    # Делаем aware в timezone пользователя
    tz = ZoneInfo(tz_name)
    remind_at = remind_naive.replace(tzinfo=tz)

    # Проверяем что не в прошлом
    if remind_at <= user_now:
        r = "⏰ Это время уже прошло. Укажи будущее время."
        await update.message.reply_text(r)
        return r

    # Сохраняем в БД
    reminder_id = await save_reminder(user_id, title, remind_at)

    # Форматируем подтверждение
    remind_fmt = remind_naive.strftime("%d.%m.%Y в %H:%M")
    r = f"✅ Напоминание установлено!\n📌 {title}\n⏰ {remind_fmt}"
    await update.message.reply_text(r)
    return r