"""
Revory — Pending Actions (мультишаговые диалоги).
Хранилище и обработчики для мультишаговых операций:
выбор из списка, подтверждения, настройка цветов.
"""

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


# ─── Обработчики ──────────────────────────────────────────

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


# ─── Цвета ────────────────────────────────────────────────

# Ключевые слова → google_color_id для парсинга ответа пользователя
_COLOR_KEYWORDS = {
    "лаванд": 1, "сирен": 1, "фиолет": 1,
    "шалфей": 2, "салат": 2,
    "виноград": 3,
    "фламинго": 4, "роз": 4, "розов": 4,
    "банан": 5, "жёлт": 5, "желт": 5,
    "мандарин": 6, "оранж": 6,
    "павлин": 7, "син": 7, "голуб": 7,
    "графит": 8, "сер": 8, "чёрн": 8, "черн": 8,
    "черник": 9, "тёмно-син": 9, "темно-син": 9,
    "базилик": 10, "зелён": 10, "зелен": 10,
    "томат": 11, "красн": 11, "алый": 11,
}


def _parse_color_assignments(text: str, available_colors: list[int]) -> list[tuple[int, str]]:
    """
    Парсит текст вида "синий — работа, зелёный — личное".
    Возвращает [(google_color_id, label), ...].
    """
    results = []
    # Разбиваем по запятой, точке с запятой, переводу строки
    parts = []
    for sep in [",", ";", "\n"]:
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            break
    if not parts:
        parts = [text.strip()]

    for part in parts:
        # Разбиваем по " — ", " - ", " = ", " это "
        pair = None
        for sep in [" — ", " - ", " = ", " это ", ": "]:
            if sep in part:
                left, right = part.split(sep, 1)
                pair = (left.strip().lower(), right.strip())
                break
        if not pair:
            continue

        color_word, label = pair

        # Определяем color_id по ключевым словам
        matched_id = None
        for keyword, cid in _COLOR_KEYWORDS.items():
            if keyword in color_word:
                if cid in available_colors:
                    matched_id = cid
                    break
        if matched_id is None:
            # Попробуем обратный порядок (label — цвет)
            for keyword, cid in _COLOR_KEYWORDS.items():
                if keyword in label.lower():
                    if cid in available_colors:
                        matched_id = cid
                        label = pair[0]  # обратный порядок
                        break

        if matched_id and label:
            results.append((matched_id, label.strip()))

    return results


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
            "🤔 Не поняла. Напиши в формате:\n"
            "«синий — работа, зелёный — личное»\n\n"
            "Или «пропустить»."
        )
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    # Сохраняем маппинги
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
    # Если нет информации о доступных цветах, берём все
    if not available_colors:
        from services.database import get_distinct_colors_for_user
        available_colors = await get_distinct_colors_for_user(user_id)
    if not available_colors:
        available_colors = list(range(1, 12))

    assignments = _parse_color_assignments(text, available_colors)

    if not assignments:
        r = (
            "🤔 Не поняла. Напиши в формате:\n"
            "«синий — работа, зелёный — личное»\n\n"
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