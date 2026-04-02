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
)

from handlers.utils import resolve_user, get_user_now
from handlers.pending import get_pending, clear_pending, handle_pending
from handlers.events import handle_create, handle_show, handle_delete, handle_setup_colors, handle_move_by_color, handle_reschedule
from handlers.delete import handle_bulk_delete
from handlers.reminders import handle_remind
from handlers.lists import (
    handle_create_list,
    handle_add_to_list,
    handle_show_list,
    handle_check_items,
    handle_remove_from_list,
    handle_delete_list,
    handle_show_lists,
)

logger = logging.getLogger(__name__)

# Слова, которые делают запрос неоднозначным (список или календарь?)
_TASK_KEYWORDS = {"дела", "дело", "задачи", "задача", "задание", "задания", "todo", "дел"}


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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик текстовых сообщений."""
    text = update.message.text.strip()
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

    # --- Получаем timezone + историю ---
    user_now, tz_name = await get_user_now(user_id)
    history = await get_recent_messages(user_id, limit=10)

    # --- AI парсинг ---
    await update.message.chat.send_action("typing")
    parsed = await parse_message(text, user_now=user_now, tz_name=tz_name, history=history)
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
    reply_text = None

    if intent == "create_event":
        reply_text = await handle_create(update, user_id, parsed)

    elif intent == "show_events":
        reply_text = await handle_show(update, user_id, parsed, user_now, tz_name)

    elif intent == "delete_event":
        reply_text = await handle_delete(update, user_id, parsed, user_now, tz_name)

    elif intent == "bulk_delete_events":
        reply_text = await handle_bulk_delete(update, user_id, parsed, user_now, tz_name)

    elif intent == "move_by_color":
        reply_text = await handle_move_by_color(update, user_id, parsed, user_now, tz_name)

    elif intent == "reschedule_event":
        reply_text = await handle_reschedule(update, user_id, parsed, user_now, tz_name)

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

    elif intent == "remove_from_list":
        reply_text = await handle_remove_from_list(update, user_id, parsed)

    elif intent == "delete_list":
        reply_text = await handle_delete_list(update, user_id, parsed)

    elif intent == "show_lists":
        reply_text = await handle_show_lists(update, user_id)

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
            "• Создать событие — «встреча завтра в 15:00 с клиентом»\n"
            "• Показать расписание — «что у меня сегодня?»\n"
            "• Удалить событие — «удали встречу с клиентом»\n"
            "• Напоминание — «напомни в 10 утра купить продукты»\n"
            "• Списки — «список покупок: молоко, хлеб, яйца»\n"
            "• Цвета — «настрой цвета» или /colors\n"
            "• «Дела» — «записывай дела в календарь» или «в список»\n\n"
            "📌 Команды:\n"
            "/auth — подключить календарь\n"
            "/timezone — сменить часовой пояс\n"
            "/colors — настроить цвета событий\n"
            "/disconnect — отключить календарь\n"
            "/logout — удалить аккаунт и данные\n\n"
            "Просто пиши как думаешь!"
        )
        await update.message.reply_text(reply_text)

    elif intent == "set_task_destination":
        dest = parsed.get("list_name")  # "calendar" или "list"
        if dest in ("calendar", "list"):
            await set_task_destination(user_id, dest)
            label = "📅 Календарь" if dest == "calendar" else "📋 Список"
            reply_text = f"✅ Запомнила! «Дела» теперь по умолчанию → {label}\n\nИзменить: «записывай дела в список» / «в календарь»"
        else:
            reply_text = "Напиши «записывай дела в календарь» или «записывай дела в список»."
        await update.message.reply_text(reply_text)

    elif intent == "defer":
        reply_text = parsed.get("reply", "Хорошо! Напиши мне когда будешь готова — всё сделаем 😊")
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