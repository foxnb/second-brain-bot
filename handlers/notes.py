"""
Revory — Notes handlers.
Создание, просмотр, поиск, удаление, переименование заметок. Поддержка папок.
"""

import re
import logging
from telegram import Update

from services.database import (
    create_note,
    get_user_notes,
    find_notes_by_query,
    delete_note,
    update_note,
    get_folder_contents,
)
from handlers.pending import set_pending, get_pending, clear_pending

logger = logging.getLogger(__name__)

_ATTACH_LABEL = {"photo": "фото", "document": "файл"}


def _format_tags(tags: list[str]) -> str:
    """#тег1  #тег2"""
    return "  ".join("#" + t.lstrip("#") for t in tags)


def _parse_title_with_tags(text: str) -> tuple[str, list[str]]:
    """
    Извлекает название и встроенные теги из текста.
    "Ценности, поставь еще тег: работа" → ("Ценности", ["работа"])
    "Мои идеи #работа #проект" → ("Мои идеи", ["работа", "проект"])
    "Название, теги: работа, проект" → ("Название", ["работа", "проект"])
    """
    # Pattern: ", поставь/добавь тег(и): X" or ", тег(и): X"
    tag_cmd = re.search(
        r',\s*(?:поставь|добавь|прибавь)?\s*(?:ещё\s+|еще\s+)?тег[и]?\s*[:\s]\s*(.+)$',
        text, re.IGNORECASE,
    )
    if tag_cmd:
        title = text[:tag_cmd.start()].strip()
        tags_raw = tag_cmd.group(1)
        tags = [t.strip().lstrip("#") for t in re.split(r'[,\s]+', tags_raw) if t.strip()]
        if title and tags:
            return title, tags

    # Pattern: hashtags anywhere in text
    if '#' in text:
        hashtags = re.findall(r'#(\w+)', text)
        title = re.sub(r'#\w+', '', text).strip().strip(',').strip()
        if title and hashtags:
            return title, hashtags

    return text.strip(), []


async def handle_create_note(update: Update, user_id, parsed: dict):
    """Создаёт заметку из текста."""
    title = (parsed.get("title") or "").strip()
    content = (parsed.get("description") or "").strip() or None
    url = parsed.get("url") or None
    tags = parsed.get("tags") or []
    folder = (parsed.get("folder") or "").strip() or None

    if not title:
        r = "🤔 Как назвать заметку? Например: «заметка: Рецепт пасты — смешать фарш с томатами»"
        await update.message.reply_text(r)
        return r

    note_id = await create_note(user_id, title, content, url, tags, folder=folder)

    parts = [f"📝 Заметка «{title}» сохранена"]
    if folder:
        parts[0] += f" в папке «{folder}»"
    if tags:
        parts.append("🏷 " + _format_tags(tags))

    tag_hint = "\n\nДобавить ещё теги?" if tags else "\n\nДобавить теги?"
    r = "\n".join(parts) + tag_hint
    await update.message.reply_text(r)

    set_pending(user_id, "note_after_save", {
        "note_id": note_id,
        "note_title": title,
        "has_tags": bool(tags),
    })
    return "\n".join(parts)


async def handle_show_notes(update: Update, user_id, parsed: dict):
    """Показывает список заметок (опционально по тегу)."""
    tags = parsed.get("tags") or []
    tag = tags[0].lstrip("#") if tags else None

    notes = await get_user_notes(user_id, tag=tag, limit=15)
    if not notes:
        hint = f" с тегом #{tag}" if tag else ""
        r = f"📭 Нет заметок{hint}.\n\nСохрани первую: «заметка Рецепт пасты — смешать фарш…»"
        await update.message.reply_text(r)
        return r

    header = f"📝 Заметки{'  #' + tag if tag else ''}:\n"
    lines = [header]
    for i, note in enumerate(notes, 1):
        tag_str = "  " + _format_tags(note["tags"]) if note.get("tags") else ""
        attach = " 📎" if note.get("attachment_file_id") else ""
        folder_str = f" 📁{note['folder']}" if note.get("folder") else ""
        lines.append(f"{i}. {note['title']}{tag_str}{attach}{folder_str}")
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def handle_find_note(update: Update, user_id, parsed: dict):
    """Находит и показывает заметку по запросу."""
    query = (parsed.get("title") or "").strip()
    if not query:
        r = "🤔 Что найти? Например: «найди заметку про рецепт»"
        await update.message.reply_text(r)
        return r

    notes = await find_notes_by_query(user_id, query)
    if not notes:
        r = f"🔍 Заметки про «{query}» не найдены."
        await update.message.reply_text(r)
        return r

    note = notes[0]
    r = f"📝 {note['title']}"
    if note.get("content"):
        r += f"\n{note['content']}"
    if note.get("url"):
        r += f"\n🔗 {note['url']}"
    if note.get("attachment_file_type"):
        r += f"\n📎 прикреплён {_ATTACH_LABEL.get(note['attachment_file_type'], 'файл')}"
    if note.get("tags"):
        r += "\n" + _format_tags(note["tags"])
    if note.get("folder"):
        r += f"\n📁 {note['folder']}"
    await update.message.reply_text(r, parse_mode="HTML")

    if note.get("attachment_file_id") and note.get("attachment_file_type"):
        try:
            if note["attachment_file_type"] == "photo":
                await update.message.reply_photo(note["attachment_file_id"])
            else:
                await update.message.reply_document(note["attachment_file_id"])
        except Exception as e:
            logger.warning(f"Could not send note attachment: {e}")

    return r


async def handle_delete_note(update: Update, user_id, parsed: dict):
    """Удаляет заметку."""
    query = (parsed.get("title") or "").strip()
    if not query:
        r = "🤔 Какую заметку удалить?"
        await update.message.reply_text(r)
        return r

    notes = await find_notes_by_query(user_id, query)
    if not notes:
        r = f"📭 Заметка «{query}» не найдена."
        await update.message.reply_text(r)
        return r

    if len(notes) == 1:
        success = await delete_note(user_id, notes[0]["id"])
        r = f"🗑️ Заметка «{notes[0]['title']}» удалена." if success else "❌ Не удалось удалить."
        await update.message.reply_text(r)
        return r

    lines = ["Нашла несколько заметок:\n"]
    for i, n in enumerate(notes[:5], 1):
        lines.append(f"{i}. {n['title']}")
    lines.append("\nНапиши номер или «отмена».")
    set_pending(user_id, "delete_note_choice", {"notes": [dict(n) for n in notes[:5]]})
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def handle_rename_note(update: Update, user_id, parsed: dict):
    """Переименовывает заметку."""
    old_title = (parsed.get("title") or "").strip()
    new_title = (parsed.get("new_title") or "").strip()

    if not old_title or not new_title:
        r = "🤔 Напиши: «переименуй заметку [старое название] в [новое название]»"
        await update.message.reply_text(r)
        return r

    notes = await find_notes_by_query(user_id, old_title)
    if not notes:
        r = f"📭 Заметка «{old_title}» не найдена."
        await update.message.reply_text(r)
        return r

    if len(notes) == 1:
        success = await update_note(user_id, notes[0]["id"], title=new_title)
        r = f"✏️ Заметка переименована: «{new_title}»" if success else "❌ Не удалось переименовать."
        await update.message.reply_text(r)
        return r

    lines = ["Нашла несколько заметок:\n"]
    for i, n in enumerate(notes[:5], 1):
        lines.append(f"{i}. {n['title']}")
    lines.append("\nНапиши номер для переименования или «отмена».")
    set_pending(user_id, "rename_note_choice", {
        "notes": [dict(n) for n in notes[:5]],
        "new_title": new_title,
    })
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def handle_show_folder(update: Update, user_id, parsed: dict):
    """Показывает содержимое папки (заметки + списки)."""
    folder = (parsed.get("folder_name") or parsed.get("folder") or "").strip()
    if not folder:
        r = "🤔 Какую папку открыть? Например: «покажи папку работа»"
        await update.message.reply_text(r)
        return r

    contents = await get_folder_contents(user_id, folder)
    notes = contents["notes"]
    lists = contents["lists"]

    if not notes and not lists:
        r = f"📁 Папка «{folder}» пуста или не существует."
        await update.message.reply_text(r)
        return r

    lines = [f"📁 Папка «{folder}»:\n"]
    if notes:
        lines.append("📝 Заметки:")
        for i, note in enumerate(notes, 1):
            tag_str = "  " + _format_tags(note["tags"]) if note.get("tags") else ""
            attach = " 📎" if note.get("attachment_file_id") else ""
            lines.append(f"  {i}. {note['title']}{tag_str}{attach}")
    if lists:
        if notes:
            lines.append("")
        lines.append("📋 Списки:")
        for lst in lists:
            icon = lst.get("icon") or "📋"
            lines.append(f"  {icon} {lst['name']}")

    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def handle_photo_or_document(update: Update, user_id, pending: dict = None):
    """Фото или документ → сохраняет как заметку или спрашивает название.
    Если есть pending note_replace_attachment — заменяет вложение у существующей заметки."""
    msg = update.message

    if msg.photo:
        file_id = msg.photo[-1].file_id
        file_type = "photo"
    elif msg.document:
        file_id = msg.document.file_id
        file_type = "document"
    else:
        return

    type_word = "фото" if file_type == "photo" else "файл"

    # Режим замены вложения в существующей заметке
    if pending and pending.get("action") == "note_replace_attachment":
        note_id = pending["note_id"]
        note_title = pending["note_title"]
        clear_pending(user_id)
        await update_note(
            user_id, note_id,
            attachment_file_id=file_id,
            attachment_file_type=file_type,
        )
        r = f"📎 {type_word.capitalize()} обновлено в заметке «{note_title}»."
        await msg.reply_text(r)
        return

    caption = (msg.caption or "").strip()

    if caption:
        # Разбираем заголовок + возможные инлайн-теги из подписи
        title, inline_tags = _parse_title_with_tags(caption)
        note_id = await create_note(
            user_id, title,
            tags=inline_tags or [],
            attachment_file_id=file_id,
            attachment_file_type=file_type,
        )
        parts = [f"📝 Заметка «{title}» сохранена с {type_word}"]
        if inline_tags:
            parts.append("🏷 " + _format_tags(inline_tags))
        tag_hint = "\n\nДобавить ещё теги?" if inline_tags else "\n\nДобавить теги?"
        await msg.reply_text("\n".join(parts) + tag_hint)
        set_pending(user_id, "note_after_save", {
            "note_id": note_id,
            "note_title": title,
            "has_tags": bool(inline_tags),
        })
    else:
        set_pending(user_id, "note_attachment_title", {
            "file_id": file_id,
            "file_type": file_type,
        })
        await msg.reply_text(f"📎 {type_word.capitalize()} получен! Как назвать заметку?")
