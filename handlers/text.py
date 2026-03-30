"""
Revory — Text Handler (Schema v9)
Роутер: принимает текст → AI парсит → вызывает calendar.
show_events читает из БД (после ленивой sync).
Поддержка мультишаговых диалогов (pending actions).
"""

import re
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from services.ai import parse_message
from services.calendar import (
    get_credentials,
    create_event,
    delete_event,
)
from services.sync import sync_calendar
from services.database import (
    get_internal_user_id,
    load_timezone,
    save_reminder,
    save_message,
    get_recent_messages,
    get_events_from_db,
    find_event_by_title,
    soft_delete_event,
    create_list,
    find_list_by_name,
    get_user_lists,
    add_list_items,
    get_list_items,
    check_list_items,
    remove_list_items,
)

logger = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Moscow"

# ─── Pending Actions (мультишаговые диалоги) ──────────────
# user_id → {"action": "delete_choice", "matches": [...], "expires": datetime}
_pending_actions: dict[str, dict] = {}

PENDING_TTL_MINUTES = 5  # Время жизни pending action


def _set_pending(user_id, action: str, data: dict):
    """Сохраняет pending action для пользователя."""
    _pending_actions[str(user_id)] = {
        "action": action,
        **data,
        "expires": datetime.now(dt_timezone.utc) + timedelta(minutes=PENDING_TTL_MINUTES),
    }


def _get_pending(user_id) -> dict | None:
    """Возвращает pending action если не истёк."""
    key = str(user_id)
    pending = _pending_actions.get(key)
    if not pending:
        return None
    if datetime.now(dt_timezone.utc) > pending["expires"]:
        del _pending_actions[key]
        return None
    return pending


def _clear_pending(user_id):
    """Удаляет pending action."""
    _pending_actions.pop(str(user_id), None)


def _extract_number(text: str) -> int | None:
    """Извлекает число из текста: '1', 'удали 2', 'третий'."""
    # Словесные числительные
    words_to_num = {
        "первый": 1, "первое": 1, "первая": 1, "первую": 1,
        "второй": 2, "второе": 2, "вторая": 2, "вторую": 2,
        "третий": 3, "третье": 3, "третья": 3, "третью": 3,
        "четвёртый": 4, "четвертый": 4, "четвёртое": 4, "четвертое": 4,
        "пятый": 5, "пятое": 5, "пятая": 5,
    }

    lower = text.lower().strip()

    # Сначала проверяем словесные
    for word, num in words_to_num.items():
        if word in lower:
            return num

    # Потом ищем цифры
    match = re.search(r"\d+", lower)
    if match:
        return int(match.group())

    return None


def _format_date_label(target_date, user_now: datetime) -> str:
    """Форматирует дату для отображения: 'на сегодня', 'на завтра', 'на 31.03'."""
    if target_date is None:
        return ""
    today = user_now.date()
    tomorrow = today + timedelta(days=1)
    if target_date == today:
        return " на сегодня"
    elif target_date == tomorrow:
        return " на завтра"
    else:
        return f" на {target_date.strftime('%d.%m')}"


def _make_checklist_name(base_name: str, target_date, user_now: datetime) -> str:
    """
    Добавляет дату в название чеклиста: 'Покупки' → 'Покупки 30.03'.
    Позволяет иметь несколько чеклистов на разные дни.
    """
    if target_date is None:
        return base_name
    date_str = target_date.strftime("%d.%m")
    return f"{base_name} {date_str}"


async def _resolve_user(telegram_id: int):
    """Получает UUID по telegram_id."""
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

    # --- Проверяем pending action (мультишаговый диалог) ---
    pending = _get_pending(user_id)
    if pending:
        handled = await _handle_pending(update, user_id, text, pending)
        if handled:
            return
        # Если не обработали — продолжаем обычный flow
        # (пользователь написал что-то другое вместо выбора)
        _clear_pending(user_id)

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

    # --- Интенты, требующие Google Calendar ---
    CALENDAR_INTENTS = {"create_event", "show_events", "delete_event"}
    if intent in CALENDAR_INTENTS:
        logger.info(f"Checking credentials for user_id={user_id} (telegram={telegram_id})")
        creds = await get_credentials(user_id)
        if not creds:
            logger.warning(f"No credentials found for user_id={user_id}")
            await update.message.reply_text(
                "🔑 Сначала подключи Google Calendar.\n"
                "Нажми /auth чтобы начать."
            )
            return

    # --- Роутинг по intent ---
    reply_text = None

    if intent == "create_event":
        reply_text = await _handle_create(update, user_id, parsed)

    elif intent == "show_events":
        reply_text = await _handle_show(update, user_id, parsed, user_now, tz_name)

    elif intent == "delete_event":
        reply_text = await _handle_delete(update, user_id, parsed, user_now, tz_name)

    elif intent == "remind":
        reply_text = await _handle_remind(update, user_id, parsed, user_now, tz_name)

    elif intent == "create_list":
        reply_text = await _handle_create_list(update, user_id, parsed, user_now)

    elif intent == "add_to_list":
        reply_text = await _handle_add_to_list(update, user_id, parsed)

    elif intent == "show_list":
        reply_text = await _handle_show_list(update, user_id, parsed)

    elif intent == "check_items":
        reply_text = await _handle_check_items(update, user_id, parsed)

    elif intent == "remove_from_list":
        reply_text = await _handle_remove_from_list(update, user_id, parsed)

    elif intent == "show_lists":
        reply_text = await _handle_show_lists(update, user_id)

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
            "• Списки — «список покупок: молоко, хлеб, яйца»\n\n"
            "📌 Команды:\n"
            "/auth — подключить календарь\n"
            "/timezone — сменить часовой пояс\n"
            "/disconnect — отключить календарь\n"
            "/logout — удалить аккаунт и данные\n\n"
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


# ─── Pending Actions Handler ──────────────────────────────

async def _handle_pending(update: Update, user_id, text: str, pending: dict) -> bool:
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

    return False


async def _handle_delete_choice(update: Update, user_id, text: str, pending: dict) -> bool:
    """Обрабатывает выбор номера для удаления."""
    matches = pending.get("matches", [])

    number = _extract_number(text)
    if number is None:
        lower = text.lower().strip()
        if lower in ("отмена", "отмени", "нет", "не надо", "cancel"):
            _clear_pending(user_id)
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

    _clear_pending(user_id)

    if success:
        r = f"🗑️ Удалено: {event['title']}"
    else:
        r = "❌ Не удалось удалить. Попробуй позже."

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
            user_id=user_id,
            name=list_name,
            list_type=list_type,
            icon="🛒" if list_type == "checklist" else "📋",
        )

        if items:
            await add_list_items(list_id, items, added_by=user_id)

        _clear_pending(user_id)
        r = f"✅ Создан список \"{list_name}\""
        if items:
            r += f" и добавлено: {', '.join(items)}"

        await update.message.reply_text(r)
        await save_message(user_id, "user", text)
        await save_message(user_id, "assistant", r)
        return True

    elif lower in ("нет", "no", "не надо", "отмена", "cancel"):
        _clear_pending(user_id)
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

    number = _extract_number(text)
    if number is None:
        lower = text.lower().strip()
        if lower in ("отмена", "отмени", "нет", "cancel"):
            _clear_pending(user_id)
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
    _clear_pending(user_id)

    items_str = ", ".join(items)
    r = f"✅ Добавлено в \"{target['name']}\": {items_str}"
    await update.message.reply_text(r)
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", r)
    return True


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


# ─── Показ событий (из БД, после sync) ───────────────────

async def _handle_show(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
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

    # --- Ленивая sync: синхронизируем перед чтением ---
    try:
        await sync_calendar(user_id)
    except Exception as e:
        logger.error(f"Sync failed for user {user_id}, reading stale data: {e}")

    # --- Читаем из БД ---
    events = await get_events_from_db(user_id, time_min, time_max)

    if events is None:
        r = "❌ Ошибка при загрузке событий."
        await update.message.reply_text(r)
        return r

    if not events:
        r = f"📭 На {label} событий нет. Свободна как ветер!"
        await update.message.reply_text(r)
        return r

    tz = ZoneInfo(tz_name)
    lines = [f"📅 Расписание {label}:\n"]
    for e in events:
        start = e["start_time"]
        if start.tzinfo:
            start_local = start.astimezone(tz)
        else:
            start_local = start
        time_fmt = start_local.strftime("%H:%M")
        lines.append(f"• {time_fmt} — {e['title']}")

    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


# ─── Удаление события (ищем в БД, удаляем из Google + БД) ─

async def _handle_delete(update: Update, user_id, parsed: dict, user_now: datetime, tz_name: str):
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

        if success:
            r = f"🗑️ Удалено: {event['title']}"
        else:
            r = "❌ Не удалось удалить. Попробуй позже."
        await update.message.reply_text(r)
        return r
    else:
        tz = ZoneInfo(tz_name)
        lines = ["Нашла несколько совпадений. Какое удалить?\n"]
        for i, e in enumerate(matches, 1):
            start = e["start_time"]
            if start.tzinfo:
                start_local = start.astimezone(tz)
            else:
                start_local = start
            lines.append(f"{i}. {e['title']} — {start_local.strftime('%d.%m %H:%M')}")
        lines.append("\nНапиши номер или «отмена».")

        _set_pending(user_id, "delete_choice", {"matches": matches})

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

    if not date_str:
        date_str = user_now.strftime("%Y-%m-%d")

    try:
        remind_naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        r = "❌ Не смогла разобрать дату/время."
        await update.message.reply_text(r)
        return r

    tz = ZoneInfo(tz_name)
    remind_at = remind_naive.replace(tzinfo=tz)

    if remind_at <= user_now:
        r = "⏰ Это время уже прошло. Укажи будущее время."
        await update.message.reply_text(r)
        return r

    reminder_id = await save_reminder(user_id, title, remind_at)

    remind_fmt = remind_naive.strftime("%d.%m.%Y в %H:%M")
    r = f"✅ Напоминание установлено!\n📌 {title}\n⏰ {remind_fmt}"
    await update.message.reply_text(r)
    return r


# ─── Списки ───────────────────────────────────────────────

async def _handle_create_list(update: Update, user_id, parsed: dict, user_now: datetime):
    """Создаёт новый список с элементами."""
    base_name = parsed.get("list_name") or parsed.get("title") or "Список"
    list_type = parsed.get("list_type") or "checklist"
    items = parsed.get("items") or []
    date_str = parsed.get("date")

    # Определяем target_date и auto_archive для чеклистов
    target_date = None
    auto_archive_at = None

    if list_type == "checklist":
        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                target_date = user_now.date()
        else:
            target_date = user_now.date()

        # Автоархивация: конец следующего дня
        archive_date = target_date + timedelta(days=1)
        auto_archive_at = datetime(
            archive_date.year, archive_date.month, archive_date.day,
            tzinfo=user_now.tzinfo,
        )

    # Дата в название чеклиста: "Покупки" → "Покупки 30.03"
    if list_type == "checklist":
        display_name = _make_checklist_name(base_name.capitalize(), target_date, user_now)
    else:
        display_name = base_name.capitalize()

    # Иконка по типу
    icon = "🛒" if list_type == "checklist" else "📋"

    list_id = await create_list(
        user_id=user_id,
        name=display_name,
        list_type=list_type,
        target_date=target_date,
        auto_archive_at=auto_archive_at,
        icon=icon,
    )

    if items:
        await add_list_items(list_id, items, added_by=user_id)

    # Формируем ответ
    date_label = _format_date_label(target_date, user_now)
    item_text = f" ({len(items)} поз.)" if items else ""
    r = f"{icon} \"{display_name}\"{date_label} создан{item_text}"

    if items and len(items) <= 10:
        r += "\n" + "\n".join(f"  ☐ {item}" for item in items)

    await update.message.reply_text(r)
    return r


async def _handle_add_to_list(update: Update, user_id, parsed: dict):
    """Добавляет элементы в существующий список."""
    list_name = parsed.get("list_name") or ""
    items = parsed.get("items") or []
    list_type = parsed.get("list_type")

    if not items:
        r = "🤔 Что добавить? Напиши, например: «добавь яблоки в покупки»"
        await update.message.reply_text(r)
        return r

    if not list_name:
        r = "🤔 В какой список добавить? Напиши, например: «добавь яблоки в покупки»"
        await update.message.reply_text(r)
        return r

    # Ищем список
    matches = await find_list_by_name(user_id, list_name, list_type=list_type)

    if not matches:
        # Списка нет — предлагаем создать
        _set_pending(user_id, "create_list_confirm", {
            "list_name": list_name.capitalize(),
            "list_type": list_type or "checklist",
            "items": items,
        })
        type_label = "коллекцию" if list_type == "collection" else "список"
        r = f"📝 Список \"{list_name}\" не найден. Создать {type_label} \"{list_name.capitalize()}\"?\n\nНапиши «да» или «нет»."
        await update.message.reply_text(r)
        return r

    if len(matches) == 1:
        target = matches[0]
    else:
        lines = ["Нашла несколько списков:\n"]
        for i, m in enumerate(matches, 1):
            lines.append(f"{i}. {m.get('icon', '📋')} {m['name']}")
        lines.append("\nНапиши номер.")

        _set_pending(user_id, "add_to_list_choice", {
            "matches": matches,
            "items": items,
        })
        r = "\n".join(lines)
        await update.message.reply_text(r)
        return r

    await add_list_items(target["id"], items, added_by=user_id)
    items_str = ", ".join(items)
    r = f"✅ Добавлено в \"{target['name']}\": {items_str}"
    await update.message.reply_text(r)
    return r


async def _handle_show_list(update: Update, user_id, parsed: dict):
    """Показывает содержимое списка."""
    list_name = parsed.get("list_name") or ""

    if not list_name:
        return await _handle_show_lists(update, user_id)

    matches = await find_list_by_name(user_id, list_name)

    if not matches:
        r = f"📭 Список \"{list_name}\" не найден."
        await update.message.reply_text(r)
        return r

    target = matches[0]
    items = await get_list_items(target["id"])

    if not items:
        r = f"{target.get('icon', '📋')} \"{target['name']}\" — пусто."
        await update.message.reply_text(r)
        return r

    icon = target.get("icon", "📋")
    lines = [f"{icon} {target['name']}:\n"]

    for item in items:
        if target["list_type"] == "checklist":
            mark = "✅" if item["is_checked"] else "☐"
        else:
            mark = "•"
        lines.append(f"  {mark} {item['content']}")

    if target["list_type"] == "checklist":
        total = len(items)
        checked = sum(1 for i in items if i["is_checked"])
        if checked > 0:
            lines.append(f"\n{checked}/{total} выполнено")

    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def _handle_check_items(update: Update, user_id, parsed: dict):
    """Отмечает элементы как выполненные."""
    items = parsed.get("items") or []
    list_name = parsed.get("list_name")

    if not items:
        r = "🤔 Что отметить? Напиши, например: «взяла молоко»"
        await update.message.reply_text(r)
        return r

    if list_name:
        matches = await find_list_by_name(user_id, list_name, list_type="checklist")
    else:
        matches = await get_user_lists(user_id, list_type="checklist")

    if not matches:
        r = "📭 Нет активных списков."
        await update.message.reply_text(r)
        return r

    all_checked = []
    for lst in matches:
        checked = await check_list_items(lst["id"], items, checked_by=user_id)
        for c in checked:
            all_checked.append((lst["name"], c))

    if all_checked:
        names = ", ".join(c[1] for c in all_checked)
        r = f"✅ Готово: {names}"

        if len(matches) == 1:
            remaining = await get_list_items(matches[0]["id"], include_checked=False)
            if not remaining:
                r += f"\n\n🎉 Список \"{matches[0]['name']}\" полностью выполнен!"
            elif len(remaining) <= 3:
                left = ", ".join(i["content"] for i in remaining)
                r += f"\nОсталось: {left}"
    else:
        names = ", ".join(items)
        r = f"🔍 Не нашла \"{names}\" в активных списках."

    await update.message.reply_text(r)
    return r


async def _handle_remove_from_list(update: Update, user_id, parsed: dict):
    """Удаляет элементы из списка."""
    items = parsed.get("items") or []
    list_name = parsed.get("list_name")

    if not items:
        r = "🤔 Что убрать? Напиши, например: «удали молоко из покупок»"
        await update.message.reply_text(r)
        return r

    if list_name:
        matches = await find_list_by_name(user_id, list_name)
    else:
        matches = await get_user_lists(user_id)

    if not matches:
        r = "📭 Нет активных списков."
        await update.message.reply_text(r)
        return r

    all_removed = []
    for lst in matches:
        removed = await remove_list_items(lst["id"], items)
        for rm in removed:
            all_removed.append(rm)

    if all_removed:
        names = ", ".join(all_removed)
        r = f"🗑 Убрано: {names}"
    else:
        names = ", ".join(items)
        r = f"🔍 Не нашла \"{names}\" в списках."

    await update.message.reply_text(r)
    return r


async def _handle_show_lists(update: Update, user_id):
    """Показывает все активные списки пользователя."""
    lists = await get_user_lists(user_id)

    if not lists:
        r = "📭 У тебя пока нет списков. Напиши, например: «список покупок: молоко, хлеб»"
        await update.message.reply_text(r)
        return r

    lines = ["📋 Твои списки:\n"]
    for lst in lists:
        icon = lst.get("icon") or "📋"
        name = lst["name"]
        item_count = lst.get("item_count", 0)
        checked_count = lst.get("checked_count", 0)

        if lst["list_type"] == "checklist" and item_count > 0:
            lines.append(f"  {icon} {name} — {checked_count}/{item_count} выполнено")
        elif item_count > 0:
            lines.append(f"  {icon} {name} — {item_count} элементов")
        else:
            lines.append(f"  {icon} {name}")

    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r
