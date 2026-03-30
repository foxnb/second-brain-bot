"""
Revory — Pending Actions (мультишаговые диалоги).
Хранилище и обработчики для мультишаговых операций:
выбор из списка, подтверждения, очередь переносов.
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
