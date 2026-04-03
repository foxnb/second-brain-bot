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
    find_duplicate_event,
    soft_delete_event,
    get_color_mappings,
    get_colors_asked,
    set_colors_asked,
    get_distinct_colors_for_user,
    get_events_by_color,
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


async def _find_free_slot(user_id, date_str: str, preferred_time: str = "09:00") -> str:
    """
    Возвращает первое свободное время начиная с preferred_time с шагом 1 час.
    Слот считается занятым если в БД есть событие, которое перекрывает [slot, slot+1h).
    Перебирает не более 8 часов вперёд, после чего возвращает preferred_time.
    """
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return preferred_time

    h, m = map(int, preferred_time.split(":"))
    day_start = day.replace(hour=0, minute=0, second=0)
    day_end = day.replace(hour=23, minute=59, second=59)

    events = await get_events_from_db(user_id, day_start, day_end + timedelta(seconds=1))
    # Строим список занятых интервалов (start, end)
    busy: list[tuple[datetime, datetime]] = []
    for e in events:
        s = e["start_time"].replace(tzinfo=None) if e["start_time"].tzinfo else e["start_time"]
        en = e["end_time"].replace(tzinfo=None) if e["end_time"].tzinfo else e["end_time"]
        busy.append((s, en))

    slot_start = day.replace(hour=h, minute=m, second=0, microsecond=0)
    for _ in range(9):  # пробуем до 8 часов вперёд
        slot_end = slot_start + timedelta(hours=1)
        conflict = any(s < slot_end and en > slot_start for s, en in busy)
        if not conflict:
            return slot_start.strftime("%H:%M")
        slot_start += timedelta(hours=1)
        if slot_start.hour >= 22:
            break

    return preferred_time  # если не нашли — возвращаем исходное


async def handle_create(update: Update, user_id, parsed: dict):
    """Создаёт событие в Google Calendar + зеркало в БД."""
    title = parsed.get("title")
    date_str = parsed.get("date")
    time_str = parsed.get("time")
    if not title:
        r = "🤔 Не понял название события. Попробуй ещё раз."
        await update.message.reply_text(r)
        return r
    if not date_str:
        r = f"📅 {parsed.get('reply', 'Укажи дату для события.')}"
        await update.message.reply_text(r)
        return r
    if not time_str:
        time_str = await _find_free_slot(user_id, date_str, preferred_time="09:00")
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
    color_id = parsed.get("color_id")
    if isinstance(color_id, (int, float)):
        color_id = int(color_id)
    elif color_id is not None:
        color_id = None

    # Проверка дубликата: то же название + то же время ±5 минут
    duplicate = await find_duplicate_event(user_id, title, start_time)
    if duplicate:
        start_fmt = start_time.strftime("%d.%m.%Y в %H:%M")
        r = f"⚠️ Событие «{title}» на {start_fmt} уже существует. Создать ещё одно?"
        await update.message.reply_text(r)
        from handlers.pending import set_pending
        set_pending(user_id, "create_duplicate_confirm", {"parsed": parsed})
        return r

    result = await create_event(user_id, title, start_time, end_time, color_id=color_id)
    if result:
        start_fmt = start_time.strftime("%d.%m.%Y в %H:%M")
        color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "") if color_id else ""
        color_suffix = f"  {color_emoji}" if color_emoji else ""
        r = f"✅ Создано: **{result['title']}**{color_suffix}\n📅 {start_fmt}\n🔗 {result['link']}"
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


async def handle_move_by_color(
    update: Update,
    user_id,
    parsed: dict,
    user_now: datetime,
    tz_name: str,
):
    """
    Переносит события определённого цвета на другую дату.
    Смещение = target_date - today; применяется к каждому событию.
    """
    color_id = parsed.get("color_id")
    if isinstance(color_id, float):
        color_id = int(color_id)
    if not color_id:
        r = "🎨 Укажи цвет событий для переноса. Например: «перенеси синие на следующую неделю»"
        await update.message.reply_text(r)
        return r

    date_str = parsed.get("date")
    period = parsed.get("period")

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    if not target_date:
        if period == "tomorrow":
            target_date = (user_now + timedelta(days=1)).date()
        else:
            r = "📅 Укажи дату для переноса. Например: «перенеси синие на следующую неделю» или «на 10 апреля»"
            await update.message.reply_text(r)
            return r

    offset_days = (target_date - user_now.date()).days

    # Ищем события с этим цветом в ближайшие 90 дней
    time_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = time_min + timedelta(days=90)

    try:
        await sync_calendar(user_id)
    except Exception as e:
        logger.error(f"Sync failed before move_by_color: {e}")

    events = await get_events_by_color(user_id, color_id, time_min, time_max)

    if not events:
        color_name = GOOGLE_COLOR_NAME_RU.get(color_id, f"цвет {color_id}")
        color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "")
        r = f"🔍 Не нашла {color_emoji} {color_name} событий в ближайшие 90 дней."
        await update.message.reply_text(r)
        return r

    # Если пользователь указал конкретный индекс (первое/второе/одно)
    event_index = parsed.get("event_index")
    if isinstance(event_index, float):
        event_index = int(event_index)
    if event_index is not None and 1 <= event_index <= len(events):
        events = [events[event_index - 1]]

    color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "")
    tz = ZoneInfo(tz_name)
    direction = "вперёд" if offset_days > 0 else "назад"
    abs_days = abs(offset_days)
    target_label = target_date.strftime("%d.%m.%Y")

    count_word = f"{len(events)}" if len(events) > 1 else "1"
    lines = [
        f"📅 Найдено {count_word} {color_emoji} событий.\n"
        f"Перенести на {target_label} ({abs_days} дн. {direction})?\n"
    ]
    for i, e in enumerate(events, 1):
        start = e["start_time"].astimezone(tz)
        lines.append(f"{i}. {start.strftime('%d.%m %H:%M')} — {e['title']}")

    if len(events) > 1:
        lines.append("\nНапиши «да» (все), номер (одно) или «отмена».")
    else:
        lines.append("\nНапиши «да» или «отмена».")

    set_pending(user_id, "move_by_color_confirm", {
        "events": events,
        "offset_days": offset_days,
        "target_label": target_label,
    })
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def handle_reschedule(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
    """Переносит конкретное событие (по названию) на новую дату/время."""
    from services.calendar import move_event
    from zoneinfo import ZoneInfo

    title_query = (parsed.get("title") or "").lower()
    if not title_query:
        r = "🤔 Какое событие перенести? Напиши, например: «перенеси встречу с Аней на пятницу»"
        await update.message.reply_text(r)
        return r

    target_date_str = parsed.get("date")
    target_time_str = parsed.get("time")
    period = parsed.get("period")

    # Вычисляем целевую дату
    if target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = user_now.date()
    elif period == "tomorrow":
        target_date = (user_now + timedelta(days=1)).date()
    elif period == "today":
        target_date = user_now.date()
    else:
        r = "📅 Укажи дату для переноса. Например: «перенеси встречу на пятницу» или «на 10 апреля»"
        await update.message.reply_text(r)
        return r

    # Ищем событие в ближайшие 90 дней
    try:
        await sync_calendar(user_id)
    except Exception as e:
        logger.error(f"Sync failed before reschedule: {e}")

    search_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
    search_max = search_min + timedelta(days=90)
    matches = await find_event_by_title(user_id, title_query, search_min, search_max)

    if not matches:
        r = f"🔍 Не нашла событие «{parsed.get('title')}» в ближайшие 90 дней."
        await update.message.reply_text(r)
        return r

    if len(matches) > 1:
        tz = ZoneInfo(tz_name)
        lines = ["Нашла несколько совпадений. Какое перенести?\n"]
        for i, e in enumerate(matches, 1):
            start_local = e["start_time"].astimezone(tz)
            lines.append(f"{i}. {e['title']} — {start_local.strftime('%d.%m %H:%M')}")
        lines.append("\nНапиши номер или «отмена».")
        set_pending(user_id, "reschedule_choice", {
            "matches": matches,
            "target_date": target_date.isoformat(),
            "target_time": target_time_str,
        })
        r = "\n".join(lines)
        await update.message.reply_text(r)
        return r

    return await _do_reschedule(update, user_id, matches[0], target_date, target_time_str, tz_name)


async def _do_reschedule(update, user_id, event: dict, target_date, target_time_str, tz_name: str):
    """Выполняет перенос одного события."""
    from services.calendar import move_event
    from zoneinfo import ZoneInfo
    from datetime import date as _date

    tz = ZoneInfo(tz_name)
    old_start = event["start_time"]
    old_end = event["end_time"]
    duration = old_end - old_start

    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()

    if target_time_str:
        h, m = map(int, target_time_str.split(":"))
    else:
        # Сохраняем оригинальное время
        old_local = old_start.astimezone(tz)
        h, m = old_local.hour, old_local.minute

    new_start = datetime(target_date.year, target_date.month, target_date.day, h, m, tzinfo=tz)
    new_end = new_start + duration

    external_id = event.get("external_event_id")
    if not external_id:
        r = "❌ Не могу перенести: событие не привязано к Google Calendar."
        await update.message.reply_text(r)
        return r

    success = await move_event(user_id, external_id, new_start, new_end)
    if success:
        r = f"✅ Перенесено: «{event['title']}» → {new_start.strftime('%d.%m.%Y в %H:%M')}"
    else:
        r = "❌ Не удалось перенести событие. Попробуй позже."
    await update.message.reply_text(r)
    return r


async def handle_change_color(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
    """Меняет цвет существующего события по названию."""
    from services.calendar import patch_event_color

    title_query = (parsed.get("title") or "").lower()
    color_id = parsed.get("color_id")
    if isinstance(color_id, float):
        color_id = int(color_id)

    if not color_id:
        r = "🎨 Укажи цвет. Например: «отметь встречу красным» или «пометь обед синим»"
        await update.message.reply_text(r)
        return r

    if not title_query:
        # Попробуем взять последнее упомянутое событие из истории (ищем в ближ. 7 дней)
        search_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        search_max = search_min + timedelta(days=7)
        events = await get_events_from_db(user_id, search_min, search_max)
        if not events:
            r = "🤔 Какое событие отметить? Напиши, например: «отметь встречу с Аней красным»"
            await update.message.reply_text(r)
            return r
        # Берём ближайшее предстоящее
        event = events[0]
        matches = [event]
    else:
        try:
            await sync_calendar(user_id)
        except Exception as e:
            logger.error(f"Sync failed before change_color: {e}")

        search_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
        search_max = search_min + timedelta(days=90)
        matches = await find_event_by_title(user_id, title_query, search_min, search_max)

        if not matches:
            r = f"🔍 Не нашла событие «{parsed.get('title')}»."
            await update.message.reply_text(r)
            return r

    if len(matches) > 1:
        tz = ZoneInfo(tz_name)
        lines = ["Нашла несколько совпадений. Какое отметить?\n"]
        for i, e in enumerate(matches, 1):
            start_local = e["start_time"].astimezone(tz)
            lines.append(f"{i}. {e['title']} — {start_local.strftime('%d.%m %H:%M')}")
        lines.append("\nНапиши номер или «отмена».")
        set_pending(user_id, "change_color_choice", {"matches": matches, "color_id": color_id})
        r = "\n".join(lines)
        await update.message.reply_text(r)
        return r

    event = matches[0]
    external_id = event.get("external_event_id")
    if not external_id:
        r = "❌ Событие не привязано к Google Calendar."
        await update.message.reply_text(r)
        return r

    success = await patch_event_color(user_id, external_id, color_id)
    if success:
        color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "")
        color_name = GOOGLE_COLOR_NAME_RU.get(color_id, "")
        r = f"✅ «{event['title']}» отмечено {color_emoji} {color_name}"
    else:
        r = "❌ Не удалось изменить цвет. Попробуй позже."
    await update.message.reply_text(r)
    return r


async def handle_search_event(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
    """Ищет событие по названию и показывает когда оно запланировано."""
    title_query = (parsed.get("title") or "").lower()
    if not title_query:
        r = "🔍 Что искать? Напиши, например: «когда встреча с Аней?»"
        await update.message.reply_text(r)
        return r

    try:
        await sync_calendar(user_id)
    except Exception as e:
        logger.error(f"Sync failed before search: {e}")

    search_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
    search_max = search_min + timedelta(days=90)
    matches = await find_event_by_title(user_id, title_query, search_min, search_max)

    if not matches:
        r = f"🔍 Не нашла «{parsed.get('title')}» в ближайшие 90 дней."
        await update.message.reply_text(r)
        return r

    tz = ZoneInfo(tz_name)
    raw_mappings = await get_color_mappings(user_id)
    mappings_dict = {m["google_color_id"]: m for m in raw_mappings}

    if len(matches) == 1:
        e = matches[0]
        start_local = e["start_time"].astimezone(tz)
        color_id = e.get("color_id")
        emoji = _get_event_emoji(color_id, mappings_dict)
        r = f"🔍 {emoji} «{e['title']}» — {start_local.strftime('%d.%m.%Y в %H:%M')}"
    else:
        lines = [f"🔍 Нашла {len(matches)} совпадений:\n"]
        for e in matches:
            start_local = e["start_time"].astimezone(tz)
            color_id = e.get("color_id")
            emoji = _get_event_emoji(color_id, mappings_dict)
            lines.append(f"{emoji} {start_local.strftime('%d.%m.%Y %H:%M')} — {e['title']}")
        r = "\n".join(lines)

    await update.message.reply_text(r)
    return r


async def handle_edit_event(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
    """Переименовывает событие по названию."""
    from services.calendar import rename_event

    title_query = (parsed.get("title") or "").lower()
    new_title = (parsed.get("new_title") or "").strip()

    if not title_query:
        r = "🤔 Какое событие переименовать? Напиши, например: «переименуй встречу на совещание»"
        await update.message.reply_text(r)
        return r
    if not new_title:
        r = "🤔 Как назвать? Напиши, например: «переименуй встречу на совещание»"
        await update.message.reply_text(r)
        return r

    try:
        await sync_calendar(user_id)
    except Exception as e:
        logger.error(f"Sync failed before edit_event: {e}")

    search_min = user_now.replace(hour=0, minute=0, second=0, microsecond=0)
    search_max = search_min + timedelta(days=90)
    matches = await find_event_by_title(user_id, title_query, search_min, search_max)

    if not matches:
        r = f"🔍 Не нашла событие «{parsed.get('title')}» в ближайшие 90 дней."
        await update.message.reply_text(r)
        return r

    if len(matches) > 1:
        tz = ZoneInfo(tz_name)
        lines = ["Нашла несколько совпадений. Какое переименовать?\n"]
        for i, e in enumerate(matches, 1):
            start_local = e["start_time"].astimezone(tz)
            lines.append(f"{i}. {e['title']} — {start_local.strftime('%d.%m %H:%M')}")
        lines.append("\nНапиши номер или «отмена».")
        set_pending(user_id, "edit_event_choice", {"matches": matches, "new_title": new_title})
        r = "\n".join(lines)
        await update.message.reply_text(r)
        return r

    event = matches[0]
    external_id = event.get("external_event_id")
    if not external_id:
        r = "❌ Событие не привязано к Google Calendar."
        await update.message.reply_text(r)
        return r

    success = await rename_event(user_id, external_id, new_title)
    if success:
        r = f"✅ «{event['title']}» → «{new_title}»"
    else:
        r = "❌ Не удалось переименовать. Попробуй позже."
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