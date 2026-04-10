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
    create_note,
    delete_note,
    update_note,
    add_note_tags,
    get_user_note_tags,
)
from services.calendar import delete_event, move_event
from handlers.utils import extract_number

logger = logging.getLogger(__name__)

# ─── Хранилище pending actions ────────────────────────────
_pending_actions: dict[str, dict] = {}
PENDING_TTL_MINUTES = 5

# ─── Контекст последних показанных списков ────────────────
_lists_context: dict[str, list] = {}


def set_lists_context(user_id, lists: list) -> None:
    """Сохраняет последний показанный набор списков для позиционных ссылок."""
    _lists_context[str(user_id)] = lists


def get_lists_context(user_id) -> list | None:
    return _lists_context.get(str(user_id))

# Telegram IDs пользователей с активным текстовым pending в группе
# Используется для быстрой фильтрации входящих сообщений без DB-запроса
_group_text_pending_telegram: set[int] = set()


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


def set_group_text_pending(user_id, telegram_id: int, action: str, data: dict):
    """Устанавливает текстовый pending для группового диалога."""
    set_pending(user_id, action, data)
    _group_text_pending_telegram.add(telegram_id)


def has_group_text_pending(telegram_id: int) -> bool:
    """Быстрая проверка: есть ли у пользователя активный текстовый pending в группе."""
    return telegram_id in _group_text_pending_telegram


def clear_group_text_pending(user_id, telegram_id: int):
    """Удаляет групповой текстовый pending."""
    clear_pending(user_id)
    _group_text_pending_telegram.discard(telegram_id)


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
    if action == "create_list_duplicate_confirm":
        return await _handle_create_list_duplicate_confirm(update, user_id, text, pending)
    if action == "task_destination_choice":
        return await _handle_task_destination_choice(update, user_id, text, pending)
    if action == "reschedule_choice":
        return await _handle_reschedule_choice(update, user_id, text, pending)
    if action == "change_color_choice":
        return await _handle_change_color_choice(update, user_id, text, pending)
    if action == "edit_event_choice":
        return await _handle_edit_event_choice(update, user_id, text, pending)
    if action == "move_item_create_confirm":
        return await _handle_move_item_create_confirm(update, user_id, text, pending)
    if action == "configure_statuses":
        return await _handle_configure_statuses(update, user_id, text, pending)
    if action == "configure_statuses_choice":
        return await _handle_configure_statuses_choice(update, user_id, text, pending)
    if action == "set_event_status_choice":
        return await _handle_set_event_status_choice(update, user_id, text, pending)
    if action == "group_new_project_name":
        return await _handle_group_new_project_name(update, user_id, text, pending)
    if action == "group_task_reschedule_date":
        return await _handle_group_task_reschedule_date(update, user_id, text, pending)
    if action == "delete_note_choice":
        return await _handle_delete_note_choice(update, user_id, text, pending)
    if action == "rename_note_choice":
        return await _handle_rename_note_choice(update, user_id, text, pending)
    if action == "note_attachment_title":
        return await _handle_note_attachment_title(update, user_id, text, pending)
    if action == "note_after_save":
        return await _handle_note_after_save(update, user_id, text, pending)
    if action == "note_replace_attachment":
        # Ждём фото — текстовые сообщения игнорируем мягко
        r = "📎 Пришли фото или файл, который нужно прикрепить к заметке."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    return False


# ─── Обработчики (списки / удаление) ─────────────────────

async def _handle_delete_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает выбор номера для удаления события."""
    matches = pending.get("matches", [])
    lower = text.lower().strip()
    number = extract_number(text)
    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    if number is None or number < 1 or number > len(matches):
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


async def _handle_create_list_duplicate_confirm(update: Update, user_id, text: str, pending: dict) -> bool:
    """Подтверждение создания списка при уже существующем с тем же именем."""
    from datetime import date as _date, datetime as _dt
    lower = text.lower().strip()
    if lower in ("нет", "no", "не надо", "отмена", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if lower not in ("да", "yes", "ага", "давай", "создай", "ок"):
        return False

    display_name = pending["display_name"]
    list_type = pending["list_type"]
    items = pending.get("items", [])
    url = pending.get("url")
    icon = pending.get("icon", "📋")

    raw_target = pending.get("target_date")
    target_date = _date.fromisoformat(raw_target) if raw_target else None
    raw_archive = pending.get("auto_archive_at")
    auto_archive_at = _dt.fromisoformat(raw_archive) if raw_archive else None

    list_id = await create_list(
        user_id=user_id, name=display_name, list_type=list_type,
        target_date=target_date, auto_archive_at=auto_archive_at, icon=icon,
    )
    # Фолбэк: url без items → добавить url как элемент
    if url and not items:
        items = [url]
        url = None
    if items:
        await add_list_items(list_id, items, added_by=user_id, url=url)
    clear_pending(user_id)
    r = f"✅ {icon} \"{display_name}\" создан"
    if items:
        r += f" ({len(items)} поз.)"
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


async def _handle_add_to_list_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает выбор списка для добавления."""
    matches = pending.get("matches", [])
    items = pending.get("items", [])
    lower = text.lower().strip()
    number = extract_number(text)
    if lower in ("отмена", "отмени", "нет", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    if number is None or number < 1 or number > len(matches):
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
    lower = text.lower().strip()
    number = extract_number(text)

    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    # Массовое удаление: «все», «все удали», «удали все»
    if any(w in lower for w in ("все", "всё", "all", "каждый", "каждый из них")):
        clear_pending(user_id)
        deleted, failed = 0, 0
        for m in matches:
            ok = await archive_list(user_id, m["id"])
            if ok:
                deleted += 1
            else:
                failed += 1
        r = f"🗑️ Удалено списков: {deleted}."
        if failed:
            r += f" Не удалось: {failed}."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if number is None or number < 1 or number > len(matches):
        r = f"❌ Введи число от 1 до {len(matches)}, «все» или «отмена»."
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


# ─── Перенос конкретного события (disambig) ──────────────

async def _handle_reschedule_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Выбор события для переноса из нескольких совпадений."""
    matches = pending.get("matches", [])
    target_date = pending.get("target_date")
    target_time = pending.get("target_time")

    number = extract_number(text)
    lower = text.lower().strip()

    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if number is None or number < 1 or number > len(matches):
        r = f"❌ Введи число от 1 до {len(matches)}, или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    event = matches[number - 1]
    clear_pending(user_id)

    from handlers.events import _do_reschedule
    from handlers.utils import get_user_now
    from datetime import date as _date

    user_now, tz_name = await get_user_now(user_id)
    td = datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else user_now.date()
    r = await _do_reschedule(update, user_id, event, td, target_time, tz_name)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r or "")
    return True


# ─── Смена цвета события (disambig) ─────────────────────

async def _handle_change_color_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Выбор события для смены цвета из нескольких совпадений."""
    matches = pending.get("matches", [])
    color_id = pending.get("color_id")
    number = extract_number(text)
    lower = text.lower().strip()

    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if number is None or number < 1 or number > len(matches):
        r = f"❌ Введи число от 1 до {len(matches)}, или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    event = matches[number - 1]
    clear_pending(user_id)

    from services.calendar import patch_event_color
    from handlers.events import GOOGLE_COLOR_EMOJI, GOOGLE_COLOR_NAME_RU

    external_id = event.get("external_event_id")
    if not external_id:
        r = "❌ Событие не привязано к Google Calendar."
    else:
        success = await patch_event_color(user_id, external_id, color_id)
        if success:
            color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "")
            color_name = GOOGLE_COLOR_NAME_RU.get(color_id, "")
            r = f"✅ «{event['title']}» отмечено {color_emoji} {color_name}"
        else:
            r = "❌ Не удалось изменить цвет. Попробуй позже."

    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


# ─── Настройка статусов списка ───────────────────────────

async def _handle_configure_statuses(update: Update, user_id, text: str, pending: dict) -> bool:
    """Сохраняет новые статусы или подтверждает текущие."""
    lower = text.lower().strip()

    if lower in ("ок", "ok", "всё", "все", "нормально", "хорошо", "оставь", "оставить"):
        clear_pending(user_id)
        r = "✅ Статусы оставила как есть."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if lower in ("отмена", "cancel", "нет"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    # Парсим новые статусы через запятую
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        r = "Напиши минимум 2 статуса через запятую. Например: «нужно, срочно, готово»\nИли «ок» чтобы оставить текущие."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    list_id = pending.get("list_id")
    clear_pending(user_id)

    if list_id:
        from services.database import save_list_statuses
        await save_list_statuses(list_id, parts)
        r = "✅ Статусы обновлены:\n" + "\n".join(f"  • {s}" for s in parts)
    else:
        # Без конкретного списка — просто подтверждаем (глобальных статусов нет, хранятся per-list)
        r = (
            "✅ Запомнила! Новые статусы:\n" + "\n".join(f"  • {s}" for s in parts) +
            "\n\nПрименять их к конкретному списку? Напиши: «настрой статусы для покупок»"
        )

    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


# ─── Перенос элемента: создание целевого списка ─────────

async def _handle_move_item_create_confirm(update: Update, user_id, text: str, pending: dict) -> bool:
    """Создаёт целевой список и переносит элементы."""
    lower = text.lower().strip()
    if lower in ("нет", "no", "не надо", "отмена", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True
    if lower not in ("да", "yes", "ага", "давай", "ок"):
        return False

    items = pending.get("items", [])
    from_lst = pending.get("from_list", {})
    to_list_name = pending.get("to_list_name", "Список")
    clear_pending(user_id)

    removed = await remove_list_items(from_lst["id"], items)
    if not removed:
        r = f"🔍 Не нашла «{', '.join(items)}» в «{from_lst.get('name', '')}»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    new_list_id = await create_list(
        user_id=user_id, name=to_list_name.capitalize(),
        list_type=from_lst.get("list_type", "checklist"),
        icon=from_lst.get("icon", "🛒"),
    )
    await add_list_items(new_list_id, removed, added_by=user_id)
    r = f"✅ Создан список «{to_list_name.capitalize()}» и перенесено: {', '.join(removed)}"
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


# ─── Переименование события (disambig) ──────────────────

async def _handle_edit_event_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Выбор события для переименования из нескольких совпадений."""
    matches = pending.get("matches", [])
    new_title = pending.get("new_title", "")
    number = extract_number(text)
    lower = text.lower().strip()

    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if number is None or number < 1 or number > len(matches):
        r = f"❌ Введи число от 1 до {len(matches)}, или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    event = matches[number - 1]
    clear_pending(user_id)

    from services.calendar import rename_event

    external_id = event.get("external_event_id")
    if not external_id:
        r = "❌ Событие не привязано к Google Calendar."
    else:
        success = await rename_event(user_id, external_id, new_title)
        r = f"✅ «{event['title']}» → «{new_title}»" if success else "❌ Не удалось переименовать. Попробуй позже."

    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


# ─── Выбор места хранения «дел» ──────────────────────────

# Слова, означающие новую команду — не ответ на вопрос
_ACTION_VERBS = (
    "запиши", "добавь", "создай", "покажи", "запланируй", "напомни",
    "удали", "отмени", "перенеси", "сдвинь", "поставь", "забронируй",
    "отметь", "составь", "расскажи", "что у меня", "что на",
)

# Слова откладывания
_DEFER_WORDS = (
    "потом", "позже", "попозже", "позже скажу", "не сейчас",
    "позже напишу", "напишу потом", "сделаю позже", "позже выберу",
)


async def _handle_task_destination_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает выбор куда записывать «дела»: в календарь или список."""
    lower = text.lower().strip()
    from services.database import set_task_destination

    # Пользователь откладывает — снимаем pending и отпускаем
    if any(w in lower for w in _DEFER_WORDS):
        clear_pending(user_id)
        r = "Хорошо! Напиши когда будешь готова — всё сделаем 😊"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    # Новая команда (длинный текст с глаголами действий) — снимаем pending,
    # возвращаем False чтобы router обработал как обычное сообщение
    if len(text) > 20 or any(v in lower for v in _ACTION_VERBS):
        clear_pending(user_id)
        return False

    # Определяем выбор: только короткий ответ "календарь"/"список"/"1"/"2"
    if any(w in lower for w in ("календарь", "calendar", "1", "в кал", "в гугл")):
        dest = "calendar"
    elif any(w in lower for w in ("список", "list", "2", "в список", "в лист")):
        dest = "list"
    else:
        # Непонятный короткий ответ — переспрашиваем
        r = "Напиши «календарь» или «список» 😊"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    await set_task_destination(user_id, dest)
    label = "📅 Календарь" if dest == "calendar" else "📋 Список"
    r_saved = f"✅ Запомнила! «Дела» → {label}\n\nИзменить: «записывай дела в список» / «в календарь»"
    await update.message.reply_text(r_saved)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r_saved)

    # Выполняем исходный запрос с выбранным назначением
    parsed = pending.get("parsed", {})
    clear_pending(user_id)
    if not parsed:
        return True

    original_intent = parsed.get("intent")
    from handlers.utils import get_user_now

    if dest == "calendar":
        if original_intent in ("create_list", "add_to_list", "create_event"):
            items = parsed.get("items") or []
            title = ", ".join(items) if items else (parsed.get("list_name") or parsed.get("title") or "Дела")
            date_str = parsed.get("date")
            time_str = parsed.get("time") or "09:00"
            new_parsed = {**parsed, "intent": "create_event", "title": title, "time": time_str}
            from handlers.events import handle_create
            from services.calendar import get_credentials
            creds = await get_credentials(user_id)
            if creds:
                reply_text = await handle_create(update, user_id, new_parsed)
                await save_message(user_id, "assistant", reply_text or "")
            else:
                r = "🔑 Подключи Google Calendar (/auth) чтобы записывать в календарь."
                await update.message.reply_text(r)
                await save_message(user_id, "assistant", r)
        elif original_intent in ("show_list", "show_events"):
            from handlers.events import handle_show
            user_now, tz_name = await get_user_now(user_id)
            reply_text = await handle_show(update, user_id, {**parsed, "intent": "show_events"}, user_now, tz_name)
            await save_message(user_id, "assistant", reply_text or "")
    else:
        if original_intent in ("create_event", "create_list", "add_to_list"):
            from handlers.lists import handle_create_list
            user_now, tz_name = await get_user_now(user_id)
            reply_text = await handle_create_list(update, user_id, {**parsed, "intent": "create_list"}, user_now)
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


# ─── Выбор: настройка статусов — календарь или списки? ───

async def _handle_configure_statuses_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Пользователь выбирает для чего настраивать статусы: календарь или списки."""
    lower = text.lower().strip()

    if lower in ("отмена", "cancel", "нет", "не надо"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    is_calendar = any(w in lower for w in ("календарь", "calendar", "гугл", "google", "событи", "событий", "встреч"))
    is_lists = any(w in lower for w in ("список", "списки", "дела", "задачи", "checklist"))

    if is_calendar and not is_lists:
        clear_pending(user_id)
        # Переходим к настройке цветов
        from handlers.events import handle_setup_colors
        await handle_setup_colors(update, user_id)
        await save_message(user_id, "user", text)
        return True

    if is_lists and not is_calendar:
        list_name = pending.get("list_name")
        clear_pending(user_id)
        from handlers.lists import handle_configure_list_statuses
        from services.database import find_list_by_name
        list_id = None
        if list_name:
            matches = await find_list_by_name(user_id, list_name)
            if matches:
                list_id = matches[0]["id"]
                list_name = matches[0]["name"]
        await handle_configure_list_statuses(update, user_id, list_name=list_name, list_id=list_id)
        await save_message(user_id, "user", text)
        return True

    # Не распознали — просим уточнить
    r = "Напиши «календарь» или «списки» — для чего настраиваем статусы?"
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


# ─── Выбор события для смены статуса (disambig) ──────────

async def _handle_set_event_status_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Пользователь выбирает событие из нескольких для смены статуса."""
    from services.calendar import patch_event_color
    from handlers.events import GOOGLE_COLOR_EMOJI

    matches = pending.get("matches", [])
    color_id = pending.get("color_id")
    matched_label = pending.get("matched_label", "")
    number = extract_number(text)
    lower = text.lower().strip()

    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    if not number or number < 1 or number > len(matches):
        r = f"Напиши номер от 1 до {len(matches)} или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    event = matches[number - 1]
    clear_pending(user_id)

    success = await patch_event_color(user_id, event["external_id"], color_id)
    if success:
        color_emoji = GOOGLE_COLOR_EMOJI.get(color_id, "")
        r = f"✅ «{event['title']}» → {color_emoji} {matched_label}"
    else:
        r = "❌ Не удалось изменить статус. Попробуй позже."

    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True

# ─── Group pending handlers ───────────────────────────────

async def _handle_group_new_project_name(update: Update, user_id, text: str, pending: dict) -> bool:
    """Пользователь вводит название нового проекта в группе."""
    from services.database import create_project, create_task
    from handlers.groups import _ask_task_assignment, _parse_date

    telegram_id = update.message.from_user.id
    chat_id = pending.get("chat_id")
    tasks = pending.get("tasks", [])
    group_id = pending.get("group_id")
    name = text.strip()

    if not name:
        await update.message.reply_text("Введи название проекта:")
        return True

    clear_group_text_pending(user_id, telegram_id)

    project = await create_project(group_id, name)

    created_ids = []
    for t in tasks:
        deadline = _parse_date(t.get("deadline"))
        task = await create_task(project["id"], t["title"], deadline=deadline)
        created_ids.append(str(task["id"]))

    set_pending(user_id, "group_assign_tasks", {
        "chat_id": chat_id,
        "task_ids": created_ids,
        "task_titles": [t["title"] for t in tasks],
        "current_index": 0,
        "project_name": project["name"],
        "group_id": str(group_id),
    })

    await _ask_task_assignment(update, None, group_id, tasks[0]["title"], 0, len(created_ids), project["name"])
    return True


async def _handle_delete_note_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Выбор заметки для удаления из нескольких совпадений."""
    notes = pending.get("notes", [])
    lower = text.lower().strip()

    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    number = extract_number(text)
    if number is None or number < 1 or number > len(notes):
        r = f"❌ Введи число от 1 до {len(notes)}, или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    note = notes[number - 1]
    clear_pending(user_id)
    success = await delete_note(user_id, note["id"])
    r = f"🗑️ Заметка «{note['title']}» удалена." if success else "❌ Не удалось удалить."
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


async def _handle_note_attachment_title(update: Update, user_id, text: str, pending: dict) -> bool:
    """Пользователь вводит название для заметки с прикреплённым файлом."""
    raw = text.strip()
    if not raw:
        r = "📎 Введи название заметки:"
        await update.message.reply_text(r)
        return True

    file_id = pending.get("file_id")
    if not file_id:
        clear_pending(user_id)
        r = "❌ Файл потерян — попробуй загрузить заново."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    file_type = pending.get("file_type", "document")
    type_word = "фото" if file_type == "photo" else "файл"

    # Разбираем название + возможные инлайн-теги ("Ценности, поставь тег: работа")
    from handlers.notes import _parse_title_with_tags
    title, inline_tags = _parse_title_with_tags(raw)
    clear_pending(user_id)

    note_id = await create_note(
        user_id, title,
        tags=inline_tags or [],
        attachment_file_id=file_id,
        attachment_file_type=file_type,
    )

    parts = [f"📝 Заметка «{title}» сохранена с {type_word}"]
    if inline_tags:
        from handlers.notes import _format_tags
        parts.append("🏷 " + _format_tags(inline_tags))
    tag_hint = "\n\nДобавить ещё теги?" if inline_tags else "\n\nДобавить теги?"
    r = "\n".join(parts) + tag_hint
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", "\n".join(parts))

    set_pending(user_id, "note_after_save", {
        "note_id": note_id,
        "note_title": title,
        "has_tags": bool(inline_tags),
    })
    return True


_STOP_WORDS = {
    "не", "нет", "да", "и", "в", "на", "по", "с", "к", "у", "о",
    "но", "уже", "ещё", "еще", "то", "это", "так", "там", "тут",
    "всё", "все", "он", "она", "они", "мне", "мой", "моя", "их",
    "вот", "как", "что", "кто", "где", "за", "из", "от", "до",
    "нужно", "надо", "хочу", "можно", "пока", "ок", "окей",
    "спасибо", "пожалуйста", "thanks", "thank", "no", "yes",
}


def _is_cancel_phrase(text: str) -> bool:
    """Определяет, является ли текст отказом/пропуском добавления тегов."""
    lower = text.lower().strip()
    # Точное совпадение
    exact = {"нет", "не надо", "пропустить", "пропусти", "ок", "окей",
             "хорошо", "готово", "skip", "cancel", "no", "👍", "не нужно"}
    if lower in exact:
        return True
    # Фраза содержит отрицание + нужно/надо/хочу или просто «спасибо, не нужно»
    neg_words = ("не нужно", "не надо", "нет, спасибо", "не хочу",
                 "не нужны", "не нужен", "без тегов", "без тега",
                 "спасибо, не", "не, спасибо", "нет спасибо")
    if any(p in lower for p in neg_words):
        return True
    # Фраза начинается с «нет» или «не »
    if lower.startswith("нет") or lower.startswith("не "):
        return True
    return False


def _parse_tags_from_text(text: str) -> list[str]:
    """Парсит теги из произвольного текста ответа пользователя.
    Фильтрует стоп-слова и слишком короткие токены."""
    text = re.sub(r'#', '', text)
    tokens = re.split(r'[,;\s]+', text.strip())
    return [
        t.strip().lower() for t in tokens
        if t.strip()
        and len(t.strip()) >= 2
        and t.strip().lower() not in _STOP_WORDS
    ]


async def _handle_note_after_save(update: Update, user_id, text: str, pending: dict) -> bool:
    """Контекст после сохранения заметки: добавление тегов, переименование, ссылки, замена фото."""
    note_id = pending.get("note_id")
    note_title = pending.get("note_title", "заметка")
    lower = text.lower().strip()

    # Отмена / пропуск
    if _is_cancel_phrase(lower):
        clear_pending(user_id)
        r = "👌"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    # Показать существующие теги
    if any(kw in lower for kw in ("какие теги", "мои теги", "список тегов", "теги есть", "покажи теги")):
        tags = await get_user_note_tags(user_id)
        if tags:
            from handlers.notes import _format_tags
            r = "🏷 Твои теги:\n" + _format_tags(tags) + "\n\nНапиши нужные (или «нет»):"
        else:
            r = "У тебя пока нет тегов. Напиши новые (или «нет»):"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True  # Pending сохраняется — ждём выбор тега

    # Переименование
    rename_m = re.match(
        r'(?:переименуй|переименовать|назови|переименовываю|переименовываем)\s+(?:в\s+)?(.+)',
        lower,
    )
    if rename_m:
        new_title = rename_m.group(1).strip()
        # Восстанавливаем регистр из оригинала
        orig_lower_idx = lower.index(rename_m.group(1))
        new_title_orig = text[orig_lower_idx:orig_lower_idx + len(new_title)]
        await update_note(user_id, note_id, title=new_title_orig)
        clear_pending(user_id)
        r = f"✏️ Переименована: «{new_title_orig}»"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    # Добавить ссылку
    url_m = re.search(r'https?://\S+', text)
    if url_m:
        url = url_m.group(0).rstrip('.,)')
        await update_note(user_id, note_id, url=url)
        clear_pending(user_id)
        r = f"🔗 Ссылка добавлена к «{note_title}»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    # Замена фото/вложения
    if any(kw in lower for kw in (
        "замени фото", "поменяй фото", "другое фото", "новое фото",
        "замени картинку", "поменяй картинку", "замени файл", "поменяй файл",
    )):
        set_pending(user_id, "note_replace_attachment", {
            "note_id": note_id,
            "note_title": note_title,
        })
        r = "📎 Пришли новое фото или файл:"
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    # Иначе — воспринимаем как теги
    tags = _parse_tags_from_text(text)
    if tags:
        await add_note_tags(user_id, note_id, tags)
        clear_pending(user_id)
        from handlers.notes import _format_tags
        r = "🏷 Теги добавлены: " + _format_tags(tags)
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    r = "Напиши теги (например: работа проект) или «нет» чтобы пропустить."
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


async def _handle_rename_note_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Выбор заметки для переименования из нескольких совпадений."""
    notes = pending.get("notes", [])
    new_title = pending.get("new_title", "")
    lower = text.lower().strip()

    if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
        clear_pending(user_id)
        r = "👌 Отменено."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    number = extract_number(text)
    if number is None or number < 1 or number > len(notes):
        r = f"❌ Введи число от 1 до {len(notes)}, или «отмена»."
        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    note = notes[number - 1]
    clear_pending(user_id)
    success = await update_note(user_id, note["id"], title=new_title)
    r = f"✏️ Заметка переименована: «{new_title}»" if success else "❌ Не удалось переименовать."
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


async def _handle_group_task_reschedule_date(update: Update, user_id, text: str, pending: dict) -> bool:
    """Пользователь вводит дату для переноса задачи в группе."""
    from services.ai import parse_message
    from handlers.utils import get_user_now
    from services.database import update_task_deadline
    from datetime import date as _date
    from uuid import UUID

    telegram_id = update.message.from_user.id
    task_id = pending.get("task_id")
    task_title = pending.get("task_title", "задача")

    clear_group_text_pending(user_id, telegram_id)

    user_now, tz_name = await get_user_now(user_id)
    parsed = await parse_message(text, user_now=user_now, tz_name=tz_name)
    date_str = parsed.get("date")

    if not date_str:
        await update.message.reply_text("Не удалось распознать дату. Попробуй: «20 апреля» или «25.04»")
        return True

    try:
        new_date = _date.fromisoformat(date_str)
    except ValueError:
        await update.message.reply_text("Не удалось распознать дату.")
        return True

    await update_task_deadline(UUID(task_id), new_date)
    await update.message.reply_text(
        f"📅 Задача «{task_title}» перенесена на {new_date.strftime('%d.%m')}."
    )
    return True
