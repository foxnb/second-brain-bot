"""
Revory — Group Chat Handler
Обработка сообщений в групповых чатах Telegram.
- Триггер: упоминание @bot или /команды
- Проекты и задачи (Projects + Tasks)
- Назначение ответственных
- Напоминания в группу
"""

import logging
from datetime import date as _date
from uuid import UUID

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.pending import (
    get_pending, clear_pending,
    set_group_text_pending, clear_group_text_pending, has_group_text_pending,
    handle_pending,
)
from handlers.utils import get_user_now
from handlers.voice import get_cached_voice, clear_voice_cache
from services.ai import parse_message
from services.database import (
    ensure_group, add_group_member,
    get_internal_user_id, ensure_user,
    get_group_projects, create_project, get_project_by_name,
    get_group_members,
    create_task, get_project_tasks,
    update_task_status, update_task_assignee, update_task_deadline, delete_task,
    get_task_by_id,
)

logger = logging.getLogger(__name__)


# ─── Bot added to group ───────────────────────────────────

async def handle_bot_joined_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Бот добавлен в группу — создаём запись, шлём приветствие."""
    result = update.my_chat_member
    chat = result.chat
    adder = result.from_user

    group = await ensure_group(chat.id, chat.title)
    group_id = group["id"]

    # Регистрируем того, кто добавил бота (если он уже есть в системе)
    adder_user_id = await get_internal_user_id(adder.id)
    if adder_user_id:
        await add_group_member(group_id, adder_user_id, adder.id)

    bot_username = context.bot.username
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"👋 Привет! Я Revory — ваш групповой ассистент.\n\n"
            f"📋 Умею вести <b>проекты и задачи</b> для команды.\n\n"
            f"Чтобы пользоваться мной, каждый участник должен зарегистрироваться:\n"
            f"→ напишите мне в личку @{bot_username} и нажмите /start\n\n"
            f"После этого тегайте меня в чате:\n"
            f"<i>@{bot_username} добавь эти дела в проект</i>"
        ),
        parse_mode="HTML",
    )


# ─── Main group message entry point ───────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    """Главный обработчик текстовых сообщений в группе."""
    message = update.message
    if not message:
        return

    telegram_id = message.from_user.id
    chat_id = message.chat_id
    msg_text = text or message.text or ""
    bot_username = context.bot.username

    bot_mentioned = f"@{bot_username}" in msg_text
    is_command = msg_text.startswith("/")
    has_text_pend = has_group_text_pending(telegram_id)

    # Ранний выход — сообщение не адресовано боту
    if not bot_mentioned and not is_command and not has_text_pend:
        return

    # Проверяем кеш голосового (если есть — используем вместо текста)
    cached_voice = get_cached_voice(telegram_id)
    if cached_voice and bot_mentioned:
        # Голосовое + тег в следующем сообщении
        working_text = cached_voice
        extra = msg_text.replace(f"@{bot_username}", "").strip()
        if extra:
            working_text = f"{cached_voice}. {extra}"
        clear_voice_cache(telegram_id)
    else:
        working_text = msg_text.replace(f"@{bot_username}", "").strip()

    if not working_text and not has_text_pend:
        await message.reply_text(
            f"Да, слушаю! Напиши что нужно сделать 😊"
        )
        return

    # Получаем/создаём группу
    group = await ensure_group(chat_id, message.chat.title)
    group_id = group["id"]

    # Получаем пользователя
    user_id = await get_internal_user_id(telegram_id)
    if not user_id:
        name = message.from_user.first_name or message.from_user.username or "пользователь"
        await message.reply_text(
            f"@{message.from_user.username or name}, "
            f"сначала зарегистрируйся — напиши мне в личку /start 👋"
        )
        return

    # Добавляем в участники группы (идемпотентно)
    await add_group_member(group_id, user_id, telegram_id)

    # ── Pending action ──
    pending = get_pending(user_id)
    if pending and pending.get("chat_id") == chat_id:
        handled = await handle_pending(update, user_id, working_text, pending)
        if handled:
            return
        clear_pending(user_id)

    if not working_text:
        return

    # ── Контекст из reply ──
    reply_context = ""
    if message.reply_to_message:
        rtext = message.reply_to_message.text or message.reply_to_message.caption or ""
        if rtext:
            reply_context = f"[Контекст сообщения, на которое ответили]:\n{rtext}\n\n"

    full_text = reply_context + working_text

    # ── AI парсинг ──
    await message.chat.send_action("typing")
    user_now, tz_name = await get_user_now(user_id)
    parsed = await parse_message(full_text, user_now=user_now, tz_name=tz_name)
    intent = parsed.get("intent", "unknown")

    logger.info(f"Group {chat_id} | User {user_id} | Intent: {intent} | Parsed: {parsed}")

    # ── Роутинг ──
    if intent == "add_tasks_to_project":
        await _handle_add_tasks_to_project(update, context, group_id, user_id, parsed, chat_id)
    elif intent == "show_project_tasks":
        await _handle_show_project_tasks(update, group_id, parsed)
    elif intent == "create_project":
        await _handle_create_project(update, group_id, parsed)
    elif intent == "complete_task":
        await _handle_complete_task(update, group_id, user_id, parsed)
    elif intent == "reschedule_task":
        await _handle_reschedule_task(update, group_id, user_id, parsed, user_now, tz_name)
    elif intent == "help":
        await _send_group_help(message)
    elif intent in ("chitchat", "defer"):
        reply = parsed.get("reply", "Да, слушаю!")
        await message.reply_text(reply)
    else:
        await message.reply_text(
            parsed.get("reply",
                "Не совсем поняла 🤔\n\n"
                "Попробуй: «добавь задачи в проект», «покажи проект X», «задача Y выполнена»"
            )
        )


# ─── Add tasks to project ─────────────────────────────────

async def _handle_add_tasks_to_project(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    group_id, user_id, parsed: dict, chat_id: int,
):
    """Добавляет задачи в проект — сначала предлагает выбор проекта."""
    tasks = parsed.get("tasks") or []
    if not tasks:
        # Пробуем извлечь из items
        items = parsed.get("items") or []
        tasks = [{"title": it, "deadline": parsed.get("date")} for it in items]

    if not tasks:
        await update.message.reply_text("Не нашла задач для добавления. Попробуй снова.")
        return

    project_name = parsed.get("project_name")
    projects = await get_group_projects(group_id)

    # Если имя проекта указано — ищем совпадение
    if project_name:
        match = await get_project_by_name(group_id, project_name)
        if match:
            await _start_task_creation(update, context, group_id, user_id, match, tasks, chat_id)
            return

    # Предлагаем выбор
    from handlers.pending import set_pending
    set_pending(user_id, "group_select_project", {
        "chat_id": chat_id,
        "tasks": tasks,
        "group_id": str(group_id),
    })

    if not projects:
        # Нет проектов — сразу предлагаем создать
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Создать новый проект", callback_data="gp_new")],
        ])
        task_list = "\n".join(f"• {t['title']}" for t in tasks)
        await update.message.reply_text(
            f"📋 <b>Задачи для добавления:</b>\n{task_list}\n\n"
            "Проектов пока нет. Создать новый?",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        buttons = [
            [InlineKeyboardButton(f"📁 {p['name']}", callback_data=f"gp_sel:{str(p['id']).replace('-', '')}")]
            for p in projects
        ]
        buttons.append([InlineKeyboardButton("➕ Новый проект", callback_data="gp_new")])
        task_list = "\n".join(f"• {t['title']}" for t in tasks)
        await update.message.reply_text(
            f"📋 <b>Задачи для добавления:</b>\n{task_list}\n\n"
            "В какой проект?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def _start_task_creation(
    update_or_query, context: ContextTypes.DEFAULT_TYPE,
    group_id, user_id, project: dict, tasks: list, chat_id: int,
):
    """Создаёт задачи и запускает цикл назначения ответственных."""
    from handlers.pending import set_pending

    created_ids = []
    for t in tasks:
        deadline = _parse_date(t.get("deadline"))
        task = await create_task(project["id"], t["title"], deadline=deadline)
        created_ids.append(str(task["id"]))

    # Запускаем назначение
    set_pending(user_id, "group_assign_tasks", {
        "chat_id": chat_id,
        "task_ids": created_ids,
        "task_titles": [t["title"] for t in tasks],
        "current_index": 0,
        "project_name": project["name"],
        "group_id": str(group_id),
    })

    await _ask_task_assignment(update_or_query, context, group_id, tasks[0]["title"], 0, len(created_ids), project["name"])


async def _ask_task_assignment(update_or_query, context, group_id, task_title: str, index: int, total: int, project_name: str):
    """Показывает кнопки назначения ответственного за задачу."""
    members = await get_group_members(group_id)

    buttons = []
    row = []
    for i, m in enumerate(members):
        name = m["display_name"] or f"Участник {i+1}"
        row.append(InlineKeyboardButton(f"👤 {name}", callback_data=f"ga:{i}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("🚫 Никого", callback_data="ga:none"),
        InlineKeyboardButton("⏭ Пропустить", callback_data="ga:skip"),
    ])

    text = (
        f"📁 <b>{project_name}</b>\n"
        f"Задача {index+1}/{total}: <i>{task_title}</i>\n\n"
        "Кого назначить ответственным?"
    )

    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    elif hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update_or_query.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# ─── Show project tasks ───────────────────────────────────

async def _handle_show_project_tasks(update: Update, group_id, parsed: dict):
    """Показывает задачи проекта."""
    project_name = parsed.get("project_name")
    if not project_name:
        projects = await get_group_projects(group_id)
        if not projects:
            await update.message.reply_text("В этой группе ещё нет проектов.")
            return
        lines = "\n".join(f"• {p['name']}" for p in projects)
        await update.message.reply_text(f"📁 <b>Проекты:</b>\n{lines}", parse_mode="HTML")
        return

    project = await get_project_by_name(group_id, project_name)
    if not project:
        await update.message.reply_text(f"Проект «{project_name}» не найден.")
        return

    tasks = await get_project_tasks(project["id"])
    if not tasks:
        await update.message.reply_text(f"В проекте «{project['name']}» пока нет задач.")
        return

    STATUS_EMOJI = {"open": "☐", "done": "✅"}
    lines = []
    for t in tasks:
        emoji = STATUS_EMOJI.get(t["status"], "☐")
        deadline_str = f" — до {t['deadline'].strftime('%d.%m')}" if t["deadline"] else ""
        assignee_str = f" ({t['assignee_name']})" if t["assignee_name"] else ""
        lines.append(f"{emoji} {t['title']}{deadline_str}{assignee_str}")

    open_count = sum(1 for t in tasks if t["status"] == "open")
    done_count = sum(1 for t in tasks if t["status"] == "done")

    await update.message.reply_text(
        f"📁 <b>{project['name']}</b> — {open_count} открытых, {done_count} выполненных\n\n"
        + "\n".join(lines),
        parse_mode="HTML",
    )


# ─── Create project ───────────────────────────────────────

async def _handle_create_project(update: Update, group_id, parsed: dict):
    """Создаёт новый проект."""
    name = parsed.get("project_name") or parsed.get("title")
    if not name:
        await update.message.reply_text("Укажи название проекта.")
        return
    project = await create_project(group_id, name)
    await update.message.reply_text(f"📁 Проект «{project['name']}» создан!")


# ─── Complete task ────────────────────────────────────────

async def _handle_complete_task(update: Update, group_id, user_id, parsed: dict):
    """Отмечает задачу выполненной."""
    title = parsed.get("title")
    project_name = parsed.get("project_name")
    if not title:
        await update.message.reply_text("Укажи название задачи.")
        return

    project = await get_project_by_name(group_id, project_name) if project_name else None
    projects = [project] if project else await get_group_projects(group_id)

    for p in projects:
        if not p:
            continue
        tasks = await get_project_tasks(p["id"])
        for t in tasks:
            if t["title"].lower() == title.lower() and t["status"] == "open":
                await update_task_status(t["id"], "done")
                await update.message.reply_text(f"✅ Задача «{t['title']}» выполнена!")
                return

    await update.message.reply_text(f"Не нашла открытую задачу «{title}».")


# ─── Reschedule task ──────────────────────────────────────

async def _handle_reschedule_task(update: Update, group_id, user_id, parsed: dict, user_now, tz_name: str):
    """Переносит задачу на другую дату."""
    title = parsed.get("title")
    new_date_str = parsed.get("date")
    project_name = parsed.get("project_name")
    chat_id = update.message.chat_id

    if not title:
        await update.message.reply_text("Укажи название задачи.")
        return

    project = await get_project_by_name(group_id, project_name) if project_name else None
    projects = [project] if project else await get_group_projects(group_id)

    for p in projects:
        if not p:
            continue
        tasks = await get_project_tasks(p["id"])
        for t in tasks:
            if t["title"].lower() == title.lower():
                new_date = _parse_date(new_date_str)
                if new_date:
                    await update_task_deadline(t["id"], new_date)
                    await update.message.reply_text(
                        f"📅 Задача «{t['title']}» перенесена на {new_date.strftime('%d.%m')}."
                    )
                else:
                    # Нужна дата — text pending
                    set_group_text_pending(user_id, update.message.from_user.id, "group_task_reschedule_date", {
                        "chat_id": chat_id,
                        "task_id": str(t["id"]),
                        "task_title": t["title"],
                    })
                    await update.message.reply_text(
                        f"📅 На какую дату перенести «{t['title']}»?\n"
                        "Например: «20 апреля», «следующий вторник», «25.04»"
                    )
                return

    await update.message.reply_text(f"Не нашла задачу «{title}».")


# ─── Group help ───────────────────────────────────────────

async def _send_group_help(message):
    await message.reply_text(
        "📋 <b>Что я умею в группе:</b>\n\n"
        "• «добавь эти дела в проект» — добавить задачи из сообщения\n"
        "• «создай проект Маркетинг» — новый проект\n"
        "• «покажи проект X» — список задач\n"
        "• «задача Y выполнена» — отметить выполненной\n"
        "• «перенеси задачу Y на пятницу» — изменить срок\n\n"
        "Тегай меня: @{bot} [команда]",
        parse_mode="HTML",
    )


# ─── Callback query handler ───────────────────────────────

async def handle_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает inline-кнопки группового интерфейса."""
    query = update.callback_query
    await query.answer()
    data = query.data
    telegram_id = query.from_user.id
    chat_id = query.message.chat_id

    user_id = await get_internal_user_id(telegram_id)
    if not user_id:
        await query.answer("Сначала зарегистрируйся — напиши /start боту в личку", show_alert=True)
        return

    # ── Выбор проекта ──
    if data.startswith("gp_sel:"):
        await _cb_project_selected(query, user_id, chat_id, data, context)

    elif data == "gp_new":
        await _cb_new_project(query, user_id, telegram_id, chat_id)

    # ── Назначение ──
    elif data.startswith("ga:"):
        await _cb_assign_task(query, user_id, chat_id, data, context)

    # ── Напоминания: задача выполнена / перенести / удалить ──
    elif data.startswith("task_done:"):
        task_id_hex = data.split(":", 1)[1]
        task_id = _hex_to_uuid(task_id_hex)
        await _cb_task_done(query, task_id)

    elif data.startswith("task_rsch:"):
        task_id_hex = data.split(":", 1)[1]
        task_id = _hex_to_uuid(task_id_hex)
        await _cb_task_reschedule(query, user_id, telegram_id, task_id, chat_id)

    elif data.startswith("task_del:"):
        task_id_hex = data.split(":", 1)[1]
        task_id = _hex_to_uuid(task_id_hex)
        await _cb_task_delete(query, task_id)


async def _cb_project_selected(query, user_id, chat_id, data, context):
    from handlers.pending import get_pending, set_pending
    pending = get_pending(user_id)
    if not pending or pending.get("action") != "group_select_project":
        await query.edit_message_text("⏰ Время ожидания истекло. Попробуй снова.")
        return

    project_id_hex = data.split(":", 1)[1]
    from uuid import UUID
    project_id = UUID(project_id_hex)

    # Получаем проект
    from services.database import get_pool
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id, name FROM projects WHERE id = $1", project_id)
    if not row:
        await query.edit_message_text("Проект не найден.")
        return

    project = dict(row)
    tasks = pending["tasks"]
    group_id = pending["group_id"]

    clear_pending(user_id)

    # Создаём задачи и начинаем назначение
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

    await _ask_task_assignment(query, context, group_id, tasks[0]["title"], 0, len(created_ids), project["name"])


async def _cb_new_project(query, user_id, telegram_id, chat_id):
    from handlers.pending import get_pending
    pending = get_pending(user_id)
    if not pending or pending.get("action") != "group_select_project":
        await query.edit_message_text("⏰ Время ожидания истекло. Попробуй снова.")
        return

    # Ждём ввода названия проекта
    set_group_text_pending(user_id, telegram_id, "group_new_project_name", {
        "chat_id": chat_id,
        "tasks": pending["tasks"],
        "group_id": pending["group_id"],
    })
    clear_pending(user_id)
    await query.edit_message_text("📁 Введи название нового проекта:")


async def _cb_assign_task(query, user_id, chat_id, data, context):
    from handlers.pending import get_pending, set_pending
    pending = get_pending(user_id)
    if not pending or pending.get("action") != "group_assign_tasks":
        await query.edit_message_text("⏰ Время ожидания истекло.")
        return

    task_ids = pending["task_ids"]
    task_titles = pending["task_titles"]
    current_index = pending["current_index"]
    project_name = pending["project_name"]
    group_id = pending["group_id"]
    task_id = task_ids[current_index]

    assignee_part = data.split(":", 1)[1]

    if assignee_part not in ("none", "skip"):
        member_index = int(assignee_part)
        members = await get_group_members(group_id)
        if member_index < len(members):
            assignee_user_id = members[member_index]["user_id"]
            await update_task_assignee(task_id, assignee_user_id)

    next_index = current_index + 1

    if next_index >= len(task_ids):
        # Всё назначено
        clear_pending(user_id)
        await query.edit_message_text(
            f"✅ Задачи добавлены в проект «{project_name}»!\n\n"
            f"Всего задач: {len(task_ids)}"
        )
    else:
        # Следующая задача
        pending["current_index"] = next_index
        set_pending(user_id, "group_assign_tasks", pending)
        await _ask_task_assignment(
            query, context, group_id,
            task_titles[next_index], next_index, len(task_ids), project_name,
        )


async def _cb_task_done(query, task_id):
    task = await get_task_by_id(task_id)
    if not task:
        await query.edit_message_text("Задача не найдена.")
        return
    await update_task_status(task_id, "done")
    await query.edit_message_text(
        f"✅ Задача «{task['title']}» отмечена выполненной!"
    )


async def _cb_task_reschedule(query, user_id, telegram_id, task_id, chat_id):
    task = await get_task_by_id(task_id)
    if not task:
        await query.edit_message_text("Задача не найдена.")
        return
    set_group_text_pending(user_id, telegram_id, "group_task_reschedule_date", {
        "chat_id": chat_id,
        "task_id": str(task_id),
        "task_title": task["title"],
    })
    await query.edit_message_text(
        f"📅 На какую дату перенести «{task['title']}»?\n"
        "Например: «20 апреля», «следующий вторник», «25.04»"
    )


async def _cb_task_delete(query, task_id):
    task = await get_task_by_id(task_id)
    if not task:
        await query.edit_message_text("Задача не найдена.")
        return
    await delete_task(task_id)
    await query.edit_message_text(f"🗑️ Задача «{task['title']}» удалена.")


# ─── Reminder keyboard builder ────────────────────────────

def make_task_reminder_keyboard(task_id) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру для напоминания о задаче."""
    task_id_hex = str(task_id).replace("-", "")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выполнено", callback_data=f"task_done:{task_id_hex}")],
        [
            InlineKeyboardButton("📅 Перенести", callback_data=f"task_rsch:{task_id_hex}"),
            InlineKeyboardButton("🗑️ Удалить", callback_data=f"task_del:{task_id_hex}"),
        ],
    ])


# ─── Voice entry point for groups ────────────────────────

async def handle_group_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает голосовые сообщения в группе."""
    from handlers.voice import _transcribe_voice
    message = update.message
    telegram_id = message.from_user.id
    bot_username = context.bot.username

    text = await _transcribe_voice(update, context)
    if not text:
        return

    # Если бот упомянут в caption → обрабатываем сразу
    caption = message.caption or ""
    if f"@{bot_username}" in caption:
        await handle_group_message(update, context, text=text)
    else:
        # Кешируем на 90 секунд
        from handlers.voice import cache_voice
        cache_voice(telegram_id, text)
        # Не отвечаем в группе чтоб не спамить


# ─── Helpers ──────────────────────────────────────────────

def _parse_date(date_str) -> _date | None:
    """Парсит строку 'YYYY-MM-DD' → date или None."""
    if not date_str:
        return None
    try:
        return _date.fromisoformat(str(date_str))
    except (ValueError, TypeError):
        return None


def _hex_to_uuid(hex_str: str) -> UUID:
    """Конвертирует 32-символьный hex обратно в UUID."""
    return UUID(hex_str)
