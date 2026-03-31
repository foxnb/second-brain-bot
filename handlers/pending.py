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
from services.calendar import delete_event
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