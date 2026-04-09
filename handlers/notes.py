"""
Revory — Notes handlers.
Создание, просмотр, поиск, удаление заметок.
"""

import logging
from telegram import Update

from services.database import (
    create_note,
    get_user_notes,
    find_notes_by_query,
    delete_note,
)
from handlers.pending import set_pending

logger = logging.getLogger(__name__)

_ATTACH_LABEL = {"photo": "фото", "document": "файл"}


def _format_tags(tags: list[str]) -> str:
    """#тег1  #тег2"""
    return "  ".join("#" + t.lstrip("#") for t in tags)


async def handle_create_note(update: Update, user_id, parsed: dict):
    """Создаёт заметку из текста."""
    title = (parsed.get("title") or "").strip()
    content = (parsed.get("description") or "").strip() or None
    url = parsed.get("url") or None
    tags = parsed.get("tags") or []

    if not title:
        r = "🤔 Как назвать заметку? Например: «заметка: Рецепт пасты — смешать фарш с томатами»"
        await update.message.reply_text(r)
        return r

    await create_note(user_id, title, content, url, tags)

    parts = [f"📝 Заметка сохранена: «{title}»"]
    if tags:
        parts.append("🏷 " + _format_tags(tags))
    r = "\n".join(parts)
    await update.message.reply_text(r)
    return r


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
        lines.append(f"{i}. {note['title']}{tag_str}{attach}")
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


async def handle_photo_or_document(update: Update, user_id):
    """Фото или документ → сохраняет как заметку или спрашивает название."""
    msg = update.message

    if msg.photo:
        file_id = msg.photo[-1].file_id
        file_type = "photo"
    elif msg.document:
        file_id = msg.document.file_id
        file_type = "document"
    else:
        return

    caption = (msg.caption or "").strip()
    type_word = "фото" if file_type == "photo" else "файл"

    if caption:
        await create_note(
            user_id, caption,
            attachment_file_id=file_id,
            attachment_file_type=file_type,
        )
        await msg.reply_text(f"📝 Заметка «{caption}» сохранена с {type_word}.")
    else:
        set_pending(user_id, "note_attachment_title", {
            "file_id": file_id,
            "file_type": file_type,
        })
        await msg.reply_text(f"📎 {type_word.capitalize()} получен! Как назвать заметку?")
