"""
Revory — Lists handlers.
Создание, показ, добавление, отметка, удаление списков и элементов.
"""

import logging
from datetime import datetime, timedelta

from telegram import Update

from services.database import (
    create_list,
    find_list_by_name,
    get_user_lists,
    add_list_items,
    get_list_items,
    check_list_items,
    remove_list_items,
    archive_list,
)
from handlers.pending import set_pending
from handlers.utils import format_date_label, make_checklist_name

logger = logging.getLogger(__name__)


async def handle_create_list(update: Update, user_id, parsed: dict, user_now: datetime):
    """Создаёт новый список с элементами."""
    base_name = parsed.get("list_name") or parsed.get("title") or "Список"
    list_type = parsed.get("list_type") or "checklist"
    items = parsed.get("items") or []
    date_str = parsed.get("date")
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
        archive_date = target_date + timedelta(days=1)
        auto_archive_at = datetime(
            archive_date.year, archive_date.month, archive_date.day,
            tzinfo=user_now.tzinfo,
        )
    if list_type == "checklist":
        display_name = make_checklist_name(base_name.capitalize(), target_date, user_now)
    else:
        display_name = base_name.capitalize()
    icon = "🛒" if list_type == "checklist" else "📋"
    list_id = await create_list(
        user_id=user_id, name=display_name, list_type=list_type,
        target_date=target_date, auto_archive_at=auto_archive_at, icon=icon,
    )
    if items:
        await add_list_items(list_id, items, added_by=user_id)
    date_label = format_date_label(target_date, user_now)
    item_text = f" ({len(items)} поз.)" if items else ""
    r = f"{icon} \"{display_name}\"{date_label} создан{item_text}"
    if items and len(items) <= 10:
        r += "\n" + "\n".join(f"  ☐ {item}" for item in items)
    await update.message.reply_text(r)
    return r


async def handle_add_to_list(update: Update, user_id, parsed: dict):
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
    matches = await find_list_by_name(user_id, list_name, list_type=list_type)
    if not matches:
        set_pending(user_id, "create_list_confirm", {
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
        set_pending(user_id, "add_to_list_choice", {"matches": matches, "items": items})
        r = "\n".join(lines)
        await update.message.reply_text(r)
        return r
    await add_list_items(target["id"], items, added_by=user_id)
    r = f"✅ Добавлено в \"{target['name']}\": {', '.join(items)}"
    await update.message.reply_text(r)
    return r


async def handle_show_list(update: Update, user_id, parsed: dict):
    """Показывает содержимое списка."""
    list_name = parsed.get("list_name") or ""
    if not list_name:
        return await handle_show_lists(update, user_id)
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
        mark = ("✅" if item["is_checked"] else "☐") if target["list_type"] == "checklist" else "•"
        lines.append(f"  {mark} {item['content']}")
    if target["list_type"] == "checklist":
        total = len(items)
        checked = sum(1 for i in items if i["is_checked"])
        if checked > 0:
            lines.append(f"\n{checked}/{total} выполнено")
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def handle_check_items(update: Update, user_id, parsed: dict):
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
                r += f"\nОсталось: {', '.join(i['content'] for i in remaining)}"
    else:
        r = f"🔍 Не нашла \"{', '.join(items)}\" в активных списках."
    await update.message.reply_text(r)
    return r


async def handle_remove_from_list(update: Update, user_id, parsed: dict):
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
        all_removed.extend(removed)
    if all_removed:
        r = f"🗑 Убрано: {', '.join(all_removed)}"
    else:
        r = f"🔍 Не нашла \"{', '.join(items)}\" в списках."
    await update.message.reply_text(r)
    return r


async def handle_delete_list(update: Update, user_id, parsed: dict):
    """Удаляет весь список целиком."""
    list_name = parsed.get("list_name") or ""
    if not list_name:
        r = "🤔 Какой список удалить? Напиши, например: «удали список покупок»"
        await update.message.reply_text(r)
        return r
    matches = await find_list_by_name(user_id, list_name)
    if not matches:
        r = f"📭 Список \"{list_name}\" не найден."
        await update.message.reply_text(r)
        return r
    if len(matches) == 1:
        target = matches[0]
        success = await archive_list(user_id, target["id"])
        r = f"🗑️ Список \"{target['name']}\" удалён." if success else "❌ Не удалось удалить."
        await update.message.reply_text(r)
        return r
    else:
        lines = ["Нашла несколько списков. Какой удалить?\n"]
        for i, m in enumerate(matches, 1):
            lines.append(f"{i}. {m.get('icon', '📋')} {m['name']}")
        lines.append("\nНапиши номер или «отмена».")
        set_pending(user_id, "delete_list_choice", {"matches": matches})
        r = "\n".join(lines)
        await update.message.reply_text(r)
        return r


async def handle_show_lists(update: Update, user_id):
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
