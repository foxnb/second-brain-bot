"""
Revory — Text Router (Schema v9)
Точка входа: текст → AI парсит → роутинг по intent → handler.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from services.ai import parse_message
from services.calendar import get_credentials
from services.database import (
    load_timezone, save_message, get_recent_messages,
    get_task_destination, set_task_destination,
    get_grammar_form,
)

from handlers.utils import resolve_user, get_user_now
from handlers.pending import get_pending, clear_pending, handle_pending
from handlers.events import handle_create, handle_show, handle_delete, handle_setup_colors, handle_move_by_color, handle_reschedule, handle_change_color, handle_edit_event, handle_search_event, handle_set_event_status
from handlers.delete import handle_bulk_delete
from handlers.reminders import handle_remind
from handlers.lists import (
    handle_create_list,
    handle_add_to_list,
    handle_show_list,
    handle_check_items,
    handle_edit_list_item,
    handle_move_list_item,
    handle_set_item_status,
    handle_configure_statuses,
    handle_remove_from_list,
    handle_delete_list,
    handle_show_lists,
    handle_convert_list,
)
from handlers.notes import (
    handle_create_note,
    handle_show_notes,
    handle_find_note,
    handle_delete_note,
)

logger = logging.getLogger(__name__)

# Слова, которые делают запрос неоднозначным (список или календарь?)
_TASK_KEYWORDS = {"дела", "дело", "задачи", "задача", "задание", "задания", "todo", "дел"}


async def _dispatch_intent(update: Update, user_id, parsed: dict, user_now, tz_name: str):
    """Диспатчит один intent. Используется и в одиночном, и в composite режиме."""
    intent = parsed.get("intent", "unknown")

    CALENDAR_INTENTS = {"create_event", "show_events", "delete_event", "bulk_delete_events", "move_by_color"}
    if intent in CALENDAR_INTENTS:
        creds = await get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "🔑 Сначала подключи Google Calendar.\nНажми /auth чтобы начать."
            )
            return None

    reply_text = None

    if intent == "search_event":
        reply_text = await handle_search_event(update, user_id, parsed, user_now, tz_name)
    elif intent == "create_event":
        reply_text = await handle_create(update, user_id, parsed)
    elif intent == "show_events":
        reply_text = await handle_show(update, user_id, parsed, user_now, tz_name)
    elif intent == "delete_event":
        reply_text = await handle_delete(update, user_id, parsed, user_now, tz_name)
    elif intent == "bulk_delete_events":
        reply_text = await handle_bulk_delete(update, user_id, parsed, user_now, tz_name)
    elif intent == "move_by_color":
        reply_text = await handle_move_by_color(update, user_id, parsed, user_now, tz_name)
    elif intent == "edit_event":
        reply_text = await handle_edit_event(update, user_id, parsed, user_now, tz_name)
    elif intent == "reschedule_event":
        reply_text = await handle_reschedule(update, user_id, parsed, user_now, tz_name)
    elif intent == "set_event_status":
        reply_text = await handle_set_event_status(update, user_id, parsed, user_now, tz_name)
    elif intent == "change_event_color":
        reply_text = await handle_change_color(update, user_id, parsed, user_now, tz_name)
    elif intent == "remind":
        reply_text = await handle_remind(update, user_id, parsed, user_now, tz_name)
    elif intent == "create_list":
        reply_text = await handle_create_list(update, user_id, parsed, user_now)
    elif intent == "add_to_list":
        reply_text = await handle_add_to_list(update, user_id, parsed)
    elif intent == "show_list":
        reply_text = await handle_show_list(update, user_id, parsed)
    elif intent == "check_items":
        reply_text = await handle_check_items(update, user_id, parsed)
    elif intent == "set_item_status":
        reply_text = await handle_set_item_status(update, user_id, parsed)
    elif intent == "configure_statuses":
        reply_text = await handle_configure_statuses(update, user_id, parsed)
    elif intent == "edit_list_item":
        reply_text = await handle_edit_list_item(update, user_id, parsed)
    elif intent == "move_list_item":
        reply_text = await handle_move_list_item(update, user_id, parsed)
    elif intent == "remove_from_list":
        reply_text = await handle_remove_from_list(update, user_id, parsed)
    elif intent == "delete_list":
        reply_text = await handle_delete_list(update, user_id, parsed)
    elif intent == "convert_list":
        reply_text = await handle_convert_list(update, user_id, parsed)
    elif intent == "show_lists":
        reply_text = await handle_show_lists(update, user_id)
    elif intent == "create_note":
        reply_text = await handle_create_note(update, user_id, parsed)
    elif intent == "show_notes":
        reply_text = await handle_show_notes(update, user_id, parsed)
    elif intent == "find_note":
        reply_text = await handle_find_note(update, user_id, parsed)
    elif intent == "delete_note":
        reply_text = await handle_delete_note(update, user_id, parsed)
    elif intent == "setup_colors":
        reply_text = await handle_setup_colors(update, user_id)
    elif intent == "change_timezone":
        tz_current = await load_timezone(user_id)
        reply_text = (
            f"⏰ Текущий часовой пояс: {tz_current}\n\nХочешь сменить? Нажми /timezone"
            if tz_current
            else "⏰ Часовой пояс не установлен. Нажми /timezone"
        )
        await update.message.reply_text(reply_text)
    elif intent == "connect_calendar":
        reply_text = "🔑 Чтобы подключить календарь, используй команду /auth"
        await update.message.reply_text(reply_text)
    elif intent == "delete_account":
        reply_text = (
            "Вот команды для управления аккаунтом:\n\n"
            "🔌 /disconnect — отключить календарь (аккаунт останется)\n"
            "🚪 /logout — полное удаление аккаунта и всех данных\n"
            "🗑️ /deletedata — то же что /logout (GDPR)"
        )
        await update.message.reply_text(reply_text)
    elif intent == "help":
        reply_text = (
            "🗓️ Вот что я умею:\n\n"
            "📅 Календарь:\n"
            "• «встреча завтра в 15:00 с клиентом» — создать событие\n"
            "• «что у меня сегодня / на неделе?» — показать расписание\n"
            "• «удали встречу с клиентом» — удалить событие\n"
            "• «напомни в 10 утра купить продукты» — напоминание\n"
            "• «перенеси встречу на пятницу» — изменить дату/время\n\n"
            "🎨 Цвета событий:\n"
            "• /colors — настроить цветовые метки\n"
            "• Цвет автоматически применяется по названию события\n"
            "• «отметь встречу как сделанное» — поменять цвет-статус\n\n"
            "📋 Списки:\n"
            "• Чеклист — список дел с датой (покупки, задачи на день)\n"
            "  «список покупок: молоко, хлеб» / «дела на завтра: ...»\n"
            "• Коллекция — постоянный список (фильмы, книги, идеи, места)\n"
            "  «коллекция фильмов: Дюна, Оппенгеймер»\n"
            "• Статусы элементов: «отметь молоко купленным», «задача в работе»\n"
            "  ☐ нужно сделать  ▶ в работе  ✅ сделано\n"
            "• «переведи список в коллекцию» — изменить тип списка\n\n"
            "⚙️ Настройки:\n"
            "• «записывай дела в список» / «в календарь» — куда идут «дела»\n"
            "• /timezone — сменить часовой пояс\n\n"
            "📌 Команды:\n"
            "/auth — подключить Google Calendar\n"
            "/timezone — сменить часовой пояс\n"
            "/colors — настроить цвета событий\n"
            "/disconnect — отключить календарь\n"
            "/logout — удалить аккаунт и все данные\n\n"
            "⚠️ Если календарь уже подключён — повторный /auth не нужен и может выдать ошибку.\n\n"
            "Просто пиши как думаешь!"
        )
        await update.message.reply_text(reply_text)
    elif intent == "set_task_destination":
        dest = parsed.get("list_name")
        if dest in ("calendar", "list"):
            await set_task_destination(user_id, dest)
            label = "📅 Календарь" if dest == "calendar" else "📋 Список"
            reply_text = f"✅ Запомнила! «Дела» теперь по умолчанию → {label}\n\nИзменить: «записывай дела в список» / «в календарь»"
        else:
            reply_text = "Напиши «записывай дела в календарь» или «записывай дела в список»."
        await update.message.reply_text(reply_text)
    elif intent == "defer":
        reply_text = parsed.get("reply", "Хорошо! Напиши мне когда будешь готов(а) — всё сделаем 😊")
        await update.message.reply_text(reply_text)
    elif intent == "chitchat":
        reply_text = parsed.get("reply", "Привет! Чем могу помочь?")
        await update.message.reply_text(reply_text)
    else:
        reply_text = parsed.get("reply", "Не совсем поняла. Попробуй написать что-нибудь вроде «встреча завтра в 15:00» или «что у меня сегодня?»")
        await update.message.reply_text(reply_text)

    return reply_text


def _is_task_ambiguous(parsed: dict) -> bool:
    """True если запрос про «дела» — неоднозначно (список или календарь)."""
    intent = parsed.get("intent")
    if intent not in ("create_list", "create_event", "show_events", "show_list", "add_to_list"):
        return False
    candidate = " ".join(filter(None, [
        parsed.get("list_name") or "",
        parsed.get("title") or "",
    ])).lower()
    return any(kw in candidate for kw in _TASK_KEYWORDS)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    """Главный обработчик текстовых сообщений."""
    # Группы обрабатываются отдельным хендлером
    chat_type = update.message.chat.type if update.message else "private"
    if chat_type in ("group", "supergroup"):
        from handlers.groups import handle_group_message
        await handle_group_message(update, context, text=text)
        return

    text = (text or update.message.text).strip()
    telegram_id = update.message.from_user.id

    # --- Маппинг telegram_id → UUID ---
    user_id = await resolve_user(telegram_id)
    if not user_id:
        await update.message.reply_text("❌ Ошибка: пользователь не найден. Нажми /start")
        return

    # --- Проверяем pending action (мультишаговый диалог) ---
    pending = get_pending(user_id)
    if pending:
        handled = await handle_pending(update, user_id, text, pending)
        if handled:
            return
        clear_pending(user_id)

    # --- Получаем timezone + историю + grammar_form ---
    user_now, tz_name = await get_user_now(user_id)
    history = await get_recent_messages(user_id, limit=10)
    grammar_form = await get_grammar_form(user_id)

    # --- AI парсинг ---
    await update.message.chat.send_action("typing")
    parsed = await parse_message(
        text, user_now=user_now, tz_name=tz_name,
        history=history, grammar_form=grammar_form,
    )

    # --- Composite commands: dispatch каждый intent отдельно ---
    if "intents" in parsed and isinstance(parsed.get("intents"), list):
        await save_message(user_id, "user", text)
        for sub in parsed["intents"]:
            await _dispatch_intent(update, user_id, sub, user_now, tz_name)
        reply_text = parsed.get("reply", "")
        if reply_text:
            await save_message(user_id, "assistant", reply_text)
        return

    intent = parsed.get("intent", "unknown")

    logger.info(f"User {user_id} | Intent: {intent} | Parsed: {parsed}")
    await save_message(user_id, "user", text)

    # --- Перехват неоднозначных «дел» → preference или вопрос ---
    if _is_task_ambiguous(parsed):
        dest = await get_task_destination(user_id)
        if dest is None:
            # Спрашиваем пользователя
            from handlers.pending import set_pending
            set_pending(user_id, "task_destination_choice", {"parsed": parsed})
            reply_text = (
                "📋 Куда записывать «дела» по умолчанию?\n\n"
                "1️⃣ В 📅 Календарь\n"
                "2️⃣ В 📋 Список\n\n"
                "Напиши «календарь» или «список» — запомню и сразу выполню."
            )
            await update.message.reply_text(reply_text)
            await save_message(user_id, "assistant", reply_text)
            return
        elif dest == "calendar":
            # Переводим в calendar intent
            if intent in ("create_list", "add_to_list"):
                items = parsed.get("items") or []
                title = parsed.get("title") or parsed.get("list_name") or "Дела"
                if items:
                    # Несколько дел — создаём каждое отдельным событием или первое
                    parsed = {**parsed, "intent": "create_event", "title": ", ".join(items)}
                else:
                    parsed = {**parsed, "intent": "create_event"}
                intent = "create_event"
            elif intent == "show_list":
                parsed = {**parsed, "intent": "show_events"}
                intent = "show_events"
        else:  # dest == "list"
            if intent == "show_events":
                parsed = {**parsed, "intent": "show_list"}
                intent = "show_list"
            elif intent == "create_event":
                parsed = {**parsed, "intent": "create_list"}
                intent = "create_list"

    # --- Проверка календаря для calendar-интентов ---
    CALENDAR_INTENTS = {"create_event", "show_events", "delete_event", "bulk_delete_events", "move_by_color"}
    if intent in CALENDAR_INTENTS:
        creds = await get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "🔑 Сначала подключи Google Calendar.\nНажми /auth чтобы начать."
            )
            return

    # ─── Роутинг по intent ────────────────────────────────
    reply_text = await _dispatch_intent(update, user_id, parsed, user_now, tz_name)

    # --- Сохраняем ответ ассистента ---
    if reply_text:
        await save_message(user_id, "assistant", reply_text)