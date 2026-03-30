"""
Revory — Events handlers.
Создание, показ, удаление событий календаря.
Цветовые кружочки из Google Calendar.
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
    get_color_mappings,
    get_colors_asked,
    set_colors_asked,
    get_distinct_colors_for_user,
)
from handlers.pending import set_pending

logger = logging.getLogger(__name__)

# ─── Google colorId → дефолтный эмодзи ───────────────────

GOOGLE_COLOR_EMOJI = {
    1: "\U0001f7e3",   # 🟣 Lavender
    2: "\U0001f7e2",   # 🟢 Sage
    3: "\U0001f347",   # 🍇 Grape
    4: "\U0001fa77",   # 🩷 Flamingo
    5: "\U0001f7e1",   # 🟡 Banana
    6: "\U0001f7e0",   # 🟠 Tangerine
    7: "\U0001f535",   # 🔵 Peacock
    8: "\u26ab",       # ⚫ Graphite
    9: "\U0001fad0",   # 🫐 Blueberry
    10: "\U0001f33f",  # 🌿 Basil
    11: "\U0001f534",  # 🔴 Tomato
}

GOOGLE_COLOR_NAME_RU = {
    1: "лавандовый",
    2: "шалфей",
    3: "виноград",
    4: "фламинго",
    5: "банан",
    6: "мандарин",
    7: "павлин (синий)",
    8: "графит",
    9: "черника",
    10: "базилик (зелёный)",
    11: "томат (красный)",
}


def _get_event_emoji(color_id, mappings_dict):
    """Возвращает эмодзи для события: пользовательский или дефолтный."""
    if color_id is None:
        return "•"
    mapping = mappings_dict.get(color_id)
    if mapping and mapping.get("emoji"):
        return mapping["emoji"]
    return GOOGLE_COLOR_EMOJI.get(color_id, "•")


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
    """Показывает расписание из БД (после ленивой sync) с цветовыми кружочками."""
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

    # Загружаем маппинг цветов пользователя
    raw_mappings = await get_color_mappings(user_id)
    mappings_dict = {m["google_color_id"]: m for m in raw_mappings}

    tz = ZoneInfo(tz_name)
    lines = [f"📅 Расписание {label}:\n"]
    has_colors = False

    for e in events:
        start = e["start_time"]
        start_local = start.astimezone(tz) if start.tzinfo else start
        color_id = e.get("color_id")
        emoji = _get_event_emoji(color_id, mappings_dict)

        if color_id is not None:
            has_colors = True

        # Если есть маппинг с label — показываем label
        mapping = mappings_dict.get(color_id) if color_id else None
        if mapping:
            lines.append(f"{emoji} {start_local.strftime('%H:%M')} — {e['title']}  ({mapping['label']})")
        else:
            lines.append(f"{emoji} {start_local.strftime('%H:%M')} — {e['title']}")

    r = "\n".join(lines)
    await update.message.reply_text(r)

    # Автовопрос: если есть цвета, нет маппингов, и ещё не спрашивали
    if has_colors and not raw_mappings:
        colors_asked = await get_colors_asked(user_id)
        if not colors_asked:
            user_colors = await get_distinct_colors_for_user(user_id)
            if user_colors:
                await _ask_about_colors(update, user_id, user_colors)
                await set_colors_asked(user_id, True)

    return r


async def _ask_about_colors(update: Update, user_id, color_ids: list[int]):
    """Спрашивает пользователя, что означают его цвета."""
    color_list = []
    for cid in color_ids:
        emoji = GOOGLE_COLOR_EMOJI.get(cid, "•")
        name = GOOGLE_COLOR_NAME_RU.get(cid, f"цвет {cid}")
        color_list.append(f"{emoji} {name}")

    colors_text = ", ".join(color_list)
    msg = (
        f"\n\n💡 Вижу, что события помечены цветами: {colors_text}\n\n"
        "Что они означают? Напиши, например:\n"
        "«синий — работа, зелёный — личное»\n\n"
        "Или напиши «пропустить» если не нужно."
    )
    set_pending(user_id, "color_setup", {"colors": color_ids})
    await update.message.reply_text(msg)


async def handle_setup_colors(update: Update, user_id):
    """Показывает текущие маппинги цветов и предлагает изменить."""
    mappings = await get_color_mappings(user_id)
    user_colors = await get_distinct_colors_for_user(user_id)

    if mappings:
        lines = ["🎨 Твои цвета:\n"]
        for m in mappings:
            emoji = m.get("emoji") or GOOGLE_COLOR_EMOJI.get(m["google_color_id"], "•")
            lines.append(f"{emoji} {GOOGLE_COLOR_NAME_RU.get(m['google_color_id'], '?')} → {m['label']}")
        lines.append("\nХочешь изменить? Напиши новые значения:")
        lines.append("«синий — работа, зелёный — личное»")
        lines.append("Или «сбросить» чтобы убрать маппинг.")
        r = "\n".join(lines)
    elif user_colors:
        color_list = []
        for cid in user_colors:
            emoji = GOOGLE_COLOR_EMOJI.get(cid, "•")
            name = GOOGLE_COLOR_NAME_RU.get(cid, f"цвет {cid}")
            color_list.append(f"{emoji} {name}")
        r = (
            "🎨 У тебя пока не настроены цвета.\n\n"
            f"В событиях используются: {', '.join(color_list)}\n\n"
            "Напиши что они означают:\n«синий — работа, зелёный — личное»"
        )
    else:
        r = "🎨 В твоих событиях пока нет цветов. Когда появятся — я спрошу что они означают."

    set_pending(user_id, "color_edit", {"colors": user_colors or []})
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