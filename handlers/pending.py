"""
Revory — Pending Actions (мультишаговые диалоги).
Хранилище и обработчики для мультишаговых операций:
выбор из списка, подтверждения, настройка цветов.
"""

import re
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from telegram import Update

from services.database import (
    save_message,
    soft_delete_event,
    add_list_items,
    create_list,
    archive_list,
    save_color_mapping,
    delete_color_mappings,
    get_color_mappings,
)
from services.calendar import delete_event, move_event
from handlers.utils import extract_number

logger = logging.getLogger(__name__)

# ─── Хранилище pending actions ────────────────────────────
_pending_actions: dict[str, dict] = {}
PENDING_TTL_MINUTES = 5


def set_pending(user_id, action: str, data: dict):
    """Сохраняет pending action для пользователя."""
    _pending_actions[str(user_id)] = {
        "action": action,
        **data,
        "expires": datetime.now(dt_timezone.utc) + timedelta(minutes=PENDING_TTL_MINUTES),
    }


def get_pending(user_id) -> dict | None:
    """Возвращает pending action если не истёк."""
    key = str(user_id)
    pending = _pending_actions.get(key)
    if not pending:
        return None
    if datetime.now(dt_timezone.utc) > pending["expires"]:
        del _pending_actions[key]
        return None
    return pending


def clear_pending(user_id):
    """Удаляет pending action."""
    _pending_actions.pop(str(user_id), None)


# ─── Диспетчер ────────────────────────────────────────────

async def handle_pending(update: Update, user_id, text: str, pending: dict) -> bool:
    """
    Обрабатывает ответ на pending action.
    Возвращает True если обработали, False если это не ответ на pending.
    """
    action = pending.get("action")
    if action == "delete_choice":
        return await _handle_delete_choice(update, user_id, text, pending)
    if action == "create_list_confirm":
        return await _handle_create_list_confirm(update, user_id, text, pending)
    if action == "add_to_list_choice":
        return await _handle_add_to_list_choice(update, user_id, text, pending)
    if action == "delete_list_choice":
        return await _handle_delete_list_choice(update, user_id, text, pending)
    if action == "color_setup":
        return await _handle_color_setup(update, user_id, text, pending)
    if action == "color_edit":
        return await _handle_color_edit(update, user_id, text, pending)
    if action == "bulk_delete_confirm":
        return await _handle_bulk_delete_confirm(update, user_id, text, pending)
    if action == "move_by_color_confirm":
        return await _handle_move_by_color_confirm(update, user_id, text, pending)
    if action == "create_duplicate_confirm":
        return await _handle_create_duplicate_confirm(update, user_id, text, pending)
    if action == "task_destination_choice":
        return await _handle_task_destination_choice(update, user_id, text, pending)
    return False


# ─── Обработчики (списки / удаление) ─────────────────────

async def _handle_delete_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает выбор номера для удаления события."""
    matches = pending.get("matches", [])
    number = extract_number(text)
    if number is None:
        lower = text.lower().strip()
        if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
            clear_pending(user_id)
            r = "👌 Отменено."
            await update.message.reply_text(r)
            await save_message(user_id, "user", text)
            await save_message(user_id, "assistant", r)
            return True
        return False
    if number < 1 or number > len(matches):
        r = f"❌ Введи число от 1 до {len(matches)}, или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    event = matches[number - 1]
    external_id = event.get("external_event_id")
    if external_id:
        success = await delete_event(user_id, external_id)
    else:
        await soft_delete_event(event["id"])
        success = True
    clear_pending(user_id)
    r = f"🗑️ Удалено: {event['title']}" if success else "❌ Не удалось удалить. Попробуй позже."
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


async def _handle_create_list_confirm(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает подтверждение создания нового списка."""
    lower = text.lower().strip()
    if lower in ("да", "yes", "ага", "давай", "создай", "ок"):
        list_name = pending["list_name"]
        list_type = pending["list_type"]
        items = pending.get("items", [])
        list_id = await create_list(
            user_id=user_id, name=list_name, list_type=list_type,
            icon="🛒" if list_type == "checklist" else "📋",
        )
        if items:
            await add_list_items(list_id, items, added_by=user_id)
        clear_pending(user_id)
        r = f"✅ Создан список \"{list_name}\""
        if items:
            r += f" и добавлено: {', '.join(items)}"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    elif lower in ("нет", "no", "не надо", "отмена", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    return False


async def _handle_add_to_list_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает выбор списка для добавления."""
    matches = pending.get("matches", [])
    items = pending.get("items", [])
    number = extract_number(text)
    if number is None:
        lower = text.lower().strip()
        if lower in ("отмена", "отмени", "нет", "cancel"):
            clear_pending(user_id)
            r = "👌 Отменено."
            await update.message.reply_text(r)
            await save_message(user_id, "user", text)
            await save_message(user_id, "assistant", r)
            return True
        return False
    if number < 1 or number > len(matches):
        r = f"❌ Введи число от 1 до {len(matches)}, или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    target = matches[number - 1]
    await add_list_items(target["id"], items, added_by=user_id)
    clear_pending(user_id)
    r = f"✅ Добавлено в \"{target['name']}\": {', '.join(items)}"
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


async def _handle_delete_list_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает выбор списка для удаления."""
    matches = pending.get("matches", [])
    number = extract_number(text)
    if number is None:
        lower = text.lower().strip()
        if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
            clear_pending(user_id)
            r = "👌 Отменено."
            await update.message.reply_text(r)
            await save_message(user_id, "user", text)
            await save_message(user_id, "assistant", r)
            return True
        return False
    if number < 1 or number > len(matches):
        r = f"❌ Введи число от 1 до {len(matches)}, или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    target = matches[number - 1]
    success = await archive_list(user_id, target["id"])
    clear_pending(user_id)
    r = f"🗑️ Список \"{target['name']}\" удалён." if success else "❌ Не удалось удалить."
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


# ─── Выбор места хранения «дел» ──────────────────────────

async def _handle_task_destination_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает выбор куда записывать «дела»: в календарь или список."""
    lower = text.lower().strip()
    from services.database import set_task_destination

    if any(w in lower for w in ("календарь", "calendar", "1", "в кал", "в гугл")):
        dest = "calendar"
    elif any(w in lower for w in ("список", "list", "2", "в список", "в лист")):
        dest = "list"
    else:
        r = "Напиши «календарь» или «список» 😊"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    await set_task_destination(user_id, dest)
    label = "📅 Календарь" if dest == "calendar" else "📋 Список"
    r_saved = f"✅ Запомнила! «Дела» → {label}\n\nИзменить потом: «записывай дела в список» / «в календарь»"
    await update.message.reply_text(r_saved)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r_saved)

    # Теперь выполняем исходный запрос с выбранным назначением
    parsed = pending.get("parsed", {})
    clear_pending(user_id)
    if not parsed:
        return True

    original_intent = parsed.get("intent")
    from handlers.utils import get_user_now

    if dest == "calendar":
        # Выполняем как create_event или show_events
        if original_intent in ("create_list", "add_to_list"):
            items = parsed.get("items") or []
            title = ", ".join(items) if items else (parsed.get("list_name") or parsed.get("title") or "Дела")
            date_str = parsed.get("date")
            time_str = parsed.get("time")
            if date_str and time_str:
                from handlers.events import handle_create
                new_parsed = {**parsed, "intent": "create_event", "title": title}
                from services.calendar import get_credentials
                creds = await get_credentials(user_id)
                if creds:
                    reply_text = await handle_create(update, user_id, new_parsed)
                    await save_message(user_id, "assistant", reply_text or "")
                else:
                    r = "🔑 Подключи Google Calendar (/auth) чтобы записывать в календарь."
                    await update.message.reply_text(r)
                    await save_message(user_id, "assistant", r)
            else:
                r = "📅 Укажи дату и время для записи в календарь."
                await update.message.reply_text(r)
                await save_message(user_id, "assistant", r)
        elif original_intent == "show_list":
            from handlers.events import handle_show
            user_now, tz_name = await get_user_now(user_id)
            new_parsed = {**parsed, "intent": "show_events"}
            reply_text = await handle_show(update, user_id, new_parsed, user_now, tz_name)
            await save_message(user_id, "assistant", reply_text or "")
    else:
        # Выполняем как create_list или show_list
        if original_intent in ("create_event", "create_list", "add_to_list"):
            from handlers.lists import handle_create_list
            user_now, tz_name = await get_user_now(user_id)
            new_parsed = {**parsed, "intent": "create_list"}
            reply_text = await handle_create_list(update, user_id, new_parsed, user_now)
            await save_message(user_id, "assistant", reply_text or "")
        elif original_intent in ("show_events", "show_list"):
            from handlers.lists import handle_show_list
            reply_text = await handle_show_list(update, user_id, parsed)
            await save_message(user_id, "assistant", reply_text or "")

    return True


# ─── Дедупликация создания событий ──────────────────────

async def _handle_create_duplicate_confirm(update: Update, user_id, text: str, pending: dict) -> bool:
    """Подтверждение создания дублирующего события."""
    lower = text.lower().strip()
    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if lower not in ("да", "yes", "ага", "давай", "создай", "ок"):
        return False

    parsed = pending.get("parsed", {})
    clear_pending(user_id)

    # Повторно вызываем create через events handler, минуя проверку дубликата
    from services.calendar import create_event
    from handlers.events import GOOGLE_COLOR_EMOJI
    from datetime import datetime as _dt

    title = parsed.get("title")
    date_str = parsed.get("date")
    time_str = parsed.get("time")
    end_time_str = parsed.get("end_time")
    color_id = parsed.get("color_id")
    if isinstance(color_id, (int, float)):
        color_id = int(color_id)
    elif color_id is not None:
        color_id = None

    try:
        start_time = _dt.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_time = None
        if end_time_str:
            try:
                end_time = _dt.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                pass
    except Exception:
        r = "❌ Не смог разобрать дату/время."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    result = await create_event(user_id, title, start_time, end_time, color_id=color_id)
    if result:
        start_fmt = start_time.strftime("%d.%m.%Y в %H:%M")
        color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "") if color_id else ""
        color_suffix = f"  {color_emoji}" if color_emoji else ""
        r = f"✅ Создано: **{result['title']}**{color_suffix}\n📅 {start_fmt}\n🔗 {result['link']}"
        await update.message.reply_text(r, parse_mode="Markdown")
    else:
        r = "❌ Не удалось создать событие. Попробуй позже."
        await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


# ─── Bulk delete / Move by color ─────────────────────────

async def _handle_bulk_delete_confirm(update: Update, user_id, text: str, pending: dict) -> bool:
    """Подтверждение массового удаления событий."""
    lower = text.lower().strip()
    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if lower not in ("да", "yes", "ага", "давай", "удали", "ок"):
        return False

    events = pending.get("events", [])
    deleted = 0
    failed = 0

    for event in events:
        external_id = event.get("external_event_id")
        if external_id:
            success = await delete_event(user_id, external_id)
        else:
            await soft_delete_event(event["id"])
            success = True
        if success:
            deleted += 1
        else:
            failed += 1

    clear_pending(user_id)
    r = f"🗑️ Удалено: {deleted} событий."
    if failed:
        r += f" Не удалось: {failed}."
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


async def _handle_move_by_color_confirm(update: Update, user_id, text: str, pending: dict) -> bool:
    """Подтверждение переноса событий по цвету."""
    lower = text.lower().strip()

    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    events = pending.get("events", [])
    offset_days = pending.get("offset_days", 0)
    target_label = pending.get("target_label", "")

    # Определяем какие события переносить
    events_to_move = None

    if lower in ("да", "yes", "ага", "давай", "перенеси", "ок", "все", "всё"):
        events_to_move = events
    else:
        # Проверяем "только одно" / "первое" / "первый" / "одно"
        _first_words = ("первое", "первый", "первую", "одно", "только одно", "одно из них", "одну")
        if any(w in lower for w in _first_words):
            events_to_move = events[:1]
        else:
            # Проверяем числовой выбор
            number = extract_number(text)
            if number is not None and 1 <= number <= len(events):
                events_to_move = [events[number - 1]]

    if events_to_move is None:
        return False

    moved = 0
    failed = 0

    for event in events_to_move:
        external_id = event.get("external_event_id")
        if not external_id:
            failed += 1
            continue
        new_start = event["start_time"] + timedelta(days=offset_days)
        new_end = event["end_time"] + timedelta(days=offset_days)
        success = await move_event(user_id, external_id, new_start, new_end)
        if success:
            moved += 1
        else:
            failed += 1

    clear_pending(user_id)
    r = f"✅ Перенесено: {moved} событий на {target_label}."
    if failed:
        r += f" Не удалось перенести: {failed}."
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


# ─── Цвета: парсинг ──────────────────────────────────────

# Ключевые слова → google_color_id
# Порядок: более длинные/специфичные первыми, короткие последними
_COLOR_PATTERNS: list[tuple[str, int]] = [
    ("тёмно-син", 9), ("темно-син", 9),
    ("лаванд", 1), ("сирен", 1), ("фиолет", 1),
    ("шалфей", 2), ("салатов", 2),
    ("виноград", 3),
    ("фламинго", 4), ("розов", 4),
    ("банан", 5), ("жёлт", 5), ("желт", 5),
    ("мандарин", 6), ("оранж", 6),
    ("павлин", 7), ("голуб", 7),
    ("графит", 8), ("чёрн", 8), ("черн", 8),
    ("черник", 9),
    ("базилик", 10),
    ("томат", 11), ("алый", 11), ("алая", 11),
    # Короткие — последними
    ("син", 7),
    ("зелён", 10), ("зелен", 10),
    ("красн", 11),
    ("сер", 8),
    ("роз", 4),
]


def _find_color_in_text(text: str) -> tuple[int | None, int, int]:
    """
    Находит цветовое слово в тексте.
    Возвращает (color_id, start_index, end_index).
    start/end — позиции полного слова, содержащего ключ.
    """
    lower = text.lower()
    for keyword, cid in _COLOR_PATTERNS:
        idx = lower.find(keyword)
        if idx != -1:
            # Расширяем до полного слова
            start = idx
            while start > 0 and lower[start - 1].isalpha():
                start -= 1
            end = idx + len(keyword)
            while end < len(lower) and lower[end].isalpha():
                end += 1
            return cid, start, end
    return None, -1, -1


def _parse_color_assignments(text: str, available_colors: list[int]) -> list[tuple[int, str]]:
    """
    Гибкий парсер цветовых назначений. Понимает:
    - "синий — работа, зелёный — личное"
    - "красный - сделать, синий сделано"
    - "Красный -сделать, синий сделано"
    - "синий=работа, красный=срочное"
    - "работа красным, личное зелёным"
    - "красный сделать, синий сделано"
    
    Логика: разбиваем на фрагменты, в каждом ищем цветовое слово,
    всё остальное (после очистки разделителей) = label.
    """
    results = []

    # Разбиваем на фрагменты по запятой, ;, переводу строки
    parts = re.split(r'[,;\n]+', text)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Ищем цветовое слово
        color_id, start, end = _find_color_in_text(part)
        if color_id is None:
            continue

        if available_colors and color_id not in available_colors:
            continue

        # Label — всё кроме цветового слова
        before = part[:start].strip()
        after = part[end:].strip()

        # Убираем разделители по краям: — - = :
        before = before.strip("-—=: ")
        after = after.strip("-—=: ")

        # Label — непустая часть (before или after)
        if after and before:
            # Оба непустые — label = более длинный (вероятнее осмысленный)
            label = after if len(after) >= len(before) else before
        elif after:
            label = after
        elif before:
            label = before
        else:
            continue

        if label:
            results.append((color_id, label))

    return results


# ─── Цвета: обработчики pending ──────────────────────────

async def _handle_color_setup(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает первичную настройку цветов (после автовопроса)."""
    lower = text.lower().strip()
    if lower in ("пропустить", "пропусти", "не надо", "нет", "skip"):
        clear_pending(user_id)
        r = "👌 Пропущено. Настроить потом: /colors"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    available_colors = pending.get("colors", [])
    assignments = _parse_color_assignments(text, available_colors)

    if not assignments:
        r = (
            "🤔 Не поняла. Напиши, например:\n"
            "«синий — работа, зелёный — личное»\n"
            "или «красный сделать, синий сделано»\n\n"
            "Или «пропустить»."
        )
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    from handlers.events import GOOGLE_COLOR_EMOJI, GOOGLE_COLOR_NAME_RU

    saved_lines = []
    for color_id, label in assignments:
        await save_color_mapping(user_id, color_id, label)
        emoji = GOOGLE_COLOR_EMOJI.get(color_id, "•")
        name = GOOGLE_COLOR_NAME_RU.get(color_id, "?")
        saved_lines.append(f"{emoji} {name} → {label}")

    clear_pending(user_id)
    r = "✅ Цвета настроены:\n\n" + "\n".join(saved_lines) + "\n\nИзменить: /colors"
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


async def _handle_color_edit(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает редактирование цветов (через /colors)."""
    lower = text.lower().strip()

    if lower in ("сбросить", "сброс", "очистить", "удалить", "reset"):
        await delete_color_mappings(user_id)
        from services.database import set_colors_asked
        await set_colors_asked(user_id, False)
        clear_pending(user_id)
        r = "✅ Маппинг цветов сброшен. При следующем показе расписания спрошу заново."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if lower in ("отмена", "cancel", "нет", "не надо"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    available_colors = pending.get("colors", [])
    if not available_colors:
        from services.database import get_distinct_colors_for_user
        available_colors = await get_distinct_colors_for_user(user_id)
    if not available_colors:
        available_colors = list(range(1, 12))

    assignments = _parse_color_assignments(text, available_colors)

    if not assignments:
        r = (
            "🤔 Не поняла. Напиши, например:\n"
            "«синий — работа, зелёный — личное»\n"
            "или «красный сделать, синий сделано»\n\n"
            "Или «сбросить» / «отмена»."
        )
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    from handlers.events import GOOGLE_COLOR_EMOJI, GOOGLE_COLOR_NAME_RU

    saved_lines = []
    for color_id, label in assignments:
        await save_color_mapping(user_id, color_id, label)
        emoji = GOOGLE_COLOR_EMOJI.get(color_id, "•")
        name = GOOGLE_COLOR_NAME_RU.get(color_id, "?")
        saved_lines.append(f"{emoji} {name} → {label}")

    clear_pending(user_id)
    r = "✅ Цвета обновлены:\n\n" + "\n".join(saved_lines)
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True