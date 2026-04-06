"""
Revory — Lists handlers.
Создание, показ, добавление, отметка, удаление списков и элементов.
"""

import logging
from datetime import datetime, timedelta

from telegram import Update

from services.database import (
    create_list,
    find_duplicate_list,
    find_list_by_name,
    get_user_lists,
    add_list_items,
    get_list_items,
    check_list_items,
    remove_list_items,
    rename_list_item,
    set_list_item_status,
    set_list_item_status_across_lists,
    get_list_statuses,
    get_color_mappings,
    archive_list,
    update_list_type,
)
from handlers.pending import set_pending, set_lists_context, get_lists_context
from handlers.utils import extract_number, format_date_label, make_checklist_name

logger = logging.getLogger(__name__)


async def handle_create_list(update: Update, user_id, parsed: dict, user_now: datetime):
    """Создаёт новый список с элементами."""
    base_name = parsed.get("list_name") or parsed.get("title") or "Список"
    list_type = parsed.get("list_type") or "checklist"
    description = parsed.get("description") or None
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

    # Проверка дубликата: список с тем же именем уже существует
    duplicate = await find_duplicate_list(user_id, display_name)
    if duplicate:
        dup_icon = duplicate.get("icon", "📋")
        r = f"⚠️ Список {dup_icon} \"{duplicate['name']}\" уже существует. Создать ещё один?"
        await update.message.reply_text(r)
        set_pending(user_id, "create_list_duplicate_confirm", {
            "display_name": display_name, "list_type": list_type,
            "target_date": target_date.isoformat() if target_date else None,
            "auto_archive_at": auto_archive_at.isoformat() if auto_archive_at else None,
            "icon": icon, "items": items,
            "url": parsed.get("url"),
        })
        return r

    list_id = await create_list(
        user_id=user_id, name=display_name, list_type=list_type,
        target_date=target_date, auto_archive_at=auto_archive_at, icon=icon,
        description=description,
    )
    url = parsed.get("url")
    if items:
        await add_list_items(list_id, items, added_by=user_id, url=url)
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
    url = parsed.get("url")
    await add_list_items(target["id"], items, added_by=user_id, url=url)
    r = f"✅ Добавлено в \"{target['name']}\": {', '.join(items)}"
    if url:
        r += f" 🔗"
    await update.message.reply_text(r)
    return r


async def handle_show_list(update: Update, user_id, parsed: dict):
    """Показывает содержимое списка."""
    list_name = parsed.get("list_name") or ""
    if not list_name:
        return await handle_show_lists(update, user_id)
    resolved = _resolve_list_name_from_context(user_id, list_name)
    if resolved:
        list_name = resolved
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
    header = f"{icon} {target['name']}"
    if target.get("description"):
        header += f"\n<i>{target['description']}</i>"
    lines = [header + ":\n"]
    for item in items:
        if target["list_type"] == "checklist":
            status = item.get("status") or ("done" if item["is_checked"] else "todo")
            mark = _STATUS_EMOJI.get(status, "☐")
        else:
            mark = "•"
        item_url = item.get("url")
        if item_url:
            lines.append(f"  {mark} <a href=\"{item_url}\">{item['content']}</a>")
        else:
            lines.append(f"  {mark} {item['content']}")
    if target["list_type"] == "checklist":
        total = len(items)
        done = sum(1 for i in items if (i.get("status") or ("done" if i["is_checked"] else "todo")) == "done")
        if done > 0:
            lines.append(f"\n{done}/{total} выполнено")
    r = "\n".join(lines)
    await update.message.reply_text(r, parse_mode="HTML")
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


async def handle_edit_list_item(update: Update, user_id, parsed: dict):
    """Переименовывает элемент в списке."""
    old_item = (parsed.get("old_item") or "").strip()
    new_item = (parsed.get("new_item") or "").strip()
    list_name = parsed.get("list_name") or ""

    if not old_item or not new_item:
        r = "🤔 Напиши, например: «поменяй полить цветы на полить орхидею»"
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

    for lst in matches:
        result = await rename_list_item(lst["id"], old_item, new_item)
        if result:
            r = f"✅ «{old_item}» → «{new_item}» в списке «{lst['name']}»"
            await update.message.reply_text(r)
            return r

    r = f"🔍 Не нашла «{old_item}» в списках."
    await update.message.reply_text(r)
    return r


async def handle_move_list_item(update: Update, user_id, parsed: dict):
    """Переносит элемент из одного списка в другой."""
    items = parsed.get("items") or []
    from_list_name = parsed.get("from_list") or parsed.get("list_name") or ""
    to_list_name = parsed.get("to_list") or ""

    if not items:
        r = "🤔 Что перенести? Напиши, например: «перенеси молоко из покупок в продукты»"
        await update.message.reply_text(r)
        return r
    if not from_list_name or not to_list_name:
        r = "🤔 Укажи оба списка. Например: «перенеси молоко из покупок в продукты»"
        await update.message.reply_text(r)
        return r

    from_matches = await find_list_by_name(user_id, from_list_name)
    if not from_matches:
        r = f"📭 Список «{from_list_name}» не найден."
        await update.message.reply_text(r)
        return r

    to_matches = await find_list_by_name(user_id, to_list_name)
    if not to_matches:
        set_pending(user_id, "move_item_create_confirm", {
            "items": items,
            "from_list": from_matches[0],
            "to_list_name": to_list_name,
        })
        r = f"📝 Список «{to_list_name}» не найден. Создать и перенести?\n\nНапиши «да» или «нет»."
        await update.message.reply_text(r)
        return r

    from_lst = from_matches[0]
    to_lst = to_matches[0]

    removed = await remove_list_items(from_lst["id"], items)
    if not removed:
        r = f"🔍 Не нашла «{', '.join(items)}» в «{from_lst['name']}»."
        await update.message.reply_text(r)
        return r

    await add_list_items(to_lst["id"], removed, added_by=user_id)
    r = f"✅ «{', '.join(removed)}» перенесено из «{from_lst['name']}» в «{to_lst['name']}»"
    await update.message.reply_text(r)
    return r


async def handle_convert_list(update: Update, user_id, parsed: dict):
    """Конвертирует список из checklist в collection или наоборот."""
    list_name = parsed.get("list_name") or ""
    target_type = parsed.get("list_type")  # желаемый тип
    if not list_name:
        r = "🤔 Какой список конвертировать?"
        await update.message.reply_text(r)
        return r
    resolved = _resolve_list_name_from_context(user_id, list_name)
    if resolved:
        list_name = resolved
    matches = await find_list_by_name(user_id, list_name)
    if not matches:
        r = f"📭 Список \"{list_name}\" не найден."
        await update.message.reply_text(r)
        return r
    target = matches[0]
    current_type = target["list_type"]
    if target_type and target_type == current_type:
        r = f"ℹ️ \"{target['name']}\" уже является {('коллекцией' if current_type == 'collection' else 'чеклистом')}."
        await update.message.reply_text(r)
        return r
    new_type = target_type or ("collection" if current_type == "checklist" else "checklist")
    new_icon = "📋" if new_type == "collection" else "🛒"
    await update_list_type(target["id"], new_type, new_icon)
    type_label = "коллекцию" if new_type == "collection" else "чеклист"
    r = f"✅ \"{target['name']}\" переведён в {type_label}."
    await update.message.reply_text(r)
    return r


def _resolve_list_name_from_context(user_id, list_name: str):
    """Если list_name — порядковое слово/число, возвращает имя из контекста или None."""
    idx = extract_number(list_name)
    if idx is None:
        return None
    ctx = get_lists_context(user_id)
    if ctx and 1 <= idx <= len(ctx):
        return ctx[idx - 1]["name"]
    return None


async def handle_delete_list(update: Update, user_id, parsed: dict):
    """Удаляет весь список целиком."""
    list_name = parsed.get("list_name") or ""
    if not list_name:
        r = "🤔 Какой список удалить? Напиши, например: «удали список покупок»"
        await update.message.reply_text(r)
        return r
    resolved = _resolve_list_name_from_context(user_id, list_name)
    if resolved:
        list_name = resolved
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


_DEFAULT_STATUSES = ["нужно сделать", "в работе", "сделано"]

# Ключевые слова → внутренний статус
_STATUS_KEYWORDS: list[tuple[list[str], str]] = [
    (["в работе", "делаю", "взяла", "взял", "начала", "начал", "в процессе"], "in_progress"),
    (["сделано", "готово", "выполнено", "готова", "сделал", "сделала", "✅", "done"], "done"),
    (["нужно", "надо", "todo", "не сделано", "отложено"], "todo"),
]

_STATUS_EMOJI = {"todo": "☐", "in_progress": "▶", "done": "✅"}
_STATUS_LABEL = {"todo": "нужно сделать", "in_progress": "в работе", "done": "сделано"}


def _parse_status(text: str) -> str | None:
    lower = text.lower()
    for keywords, status in _STATUS_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return status
    return None


async def handle_set_item_status(update: Update, user_id, parsed: dict):
    """Устанавливает статус элемента списка."""
    items = parsed.get("items") or []
    item_query = parsed.get("title") or (items[0] if items else "")
    status_str = parsed.get("status") or ""
    list_name = parsed.get("list_name")

    if not item_query:
        r = "🤔 Что именно? Напиши, например: «полить цветы — в работе»"
        await update.message.reply_text(r)
        return r

    status = _parse_status(status_str) or _parse_status(item_query)
    if not status:
        r = "🤔 Какой статус? Например: «в работе», «сделано», «нужно сделать»"
        await update.message.reply_text(r)
        return r

    if list_name:
        matches = await find_list_by_name(user_id, list_name)
        if not matches:
            r = f"📭 Список «{list_name}» не найден."
            await update.message.reply_text(r)
            return r
        for lst in matches:
            result = await set_list_item_status(lst["id"], item_query, status)
            if result:
                emoji = _STATUS_EMOJI[status]
                label = _STATUS_LABEL[status]
                r = f"{emoji} «{result}» — {label}"
                await update.message.reply_text(r)
                return r
        r = f"🔍 Не нашла «{item_query}» в «{list_name}»."
    else:
        results = await set_list_item_status_across_lists(user_id, item_query, status)
        if results:
            emoji = _STATUS_EMOJI[status]
            label = _STATUS_LABEL[status]
            r = f"{emoji} «{results[0][1]}» — {label}"
        else:
            r = f"🔍 Не нашла «{item_query}» в активных списках."

    await update.message.reply_text(r)
    return r


async def handle_configure_statuses(update: Update, user_id, parsed: dict):
    """Спрашивает: настраиваем статусы для календаря или списков?"""
    from handlers.events import GOOGLE_COLOR_NAME_RU, GOOGLE_COLOR_EMOJI

    list_name = parsed.get("list_name")

    # Собираем текущие настройки для обоих разделов
    color_mappings = await get_color_mappings(user_id)
    list_statuses = _DEFAULT_STATUSES

    lines = ["⚙️ Давай настроим статусы!\n"]

    # Текущие статусы календаря
    if color_mappings:
        lines.append("🗓 Статусы в календаре:")
        for m in color_mappings:
            emoji = m.get("emoji") or GOOGLE_COLOR_EMOJI.get(m["google_color_id"], "•")
            color_name = GOOGLE_COLOR_NAME_RU.get(m["google_color_id"], "")
            lines.append(f"  {emoji} {color_name} → {m['label']}")
    else:
        lines.append("🗓 Статусы в календаре: не настроены")

    lines.append("")

    # Текущие статусы списков
    lines.append("📋 Статусы в списках:")
    emojis = ["☐", "▶", "✅"]
    for i, s in enumerate(list_statuses):
        em = emojis[i] if i < len(emojis) else "•"
        lines.append(f"  {em} {s}")

    lines.append("\nДля чего хочешь настроить статусы — для 📅 календаря или 📋 списков?")

    set_pending(user_id, "configure_statuses_choice", {"list_name": list_name})
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r


async def handle_configure_list_statuses(update: Update, user_id, list_name=None, list_id=None):
    """Настройка статусов для конкретного списка."""
    if list_id:
        custom = await get_list_statuses(list_id)
        statuses = custom or _DEFAULT_STATUSES
        list_label = f" для «{list_name}»"
    else:
        statuses = _DEFAULT_STATUSES
        list_label = ""

    lines = [f"📋 Текущие статусы{list_label}:\n"]
    emojis = ["☐", "▶", "✅"]
    for i, s in enumerate(statuses):
        em = emojis[i] if i < len(emojis) else "•"
        lines.append(f"  {em} {s}")
    lines.append(
        "\nНапиши новые статусы через запятую или «ок» если всё устраивает.\n"
        "Например: «нужно, срочно, готово»"
    )

    set_pending(user_id, "configure_statuses", {"list_id": list_id, "list_name": list_name})
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
    set_lists_context(user_id, lists)
    lines = ["📋 Твои списки:\n"]
    for i, lst in enumerate(lists, 1):
        icon = lst.get("icon") or "📋"
        name = lst["name"]
        item_count = lst.get("item_count", 0)
        checked_count = lst.get("checked_count", 0)
        if lst["list_type"] == "checklist" and item_count > 0:
            lines.append(f"  {i}. {icon} {name} — {checked_count}/{item_count} выполнено")
        elif item_count > 0:
            lines.append(f"  {i}. {icon} {name} — {item_count} элементов")
        else:
            lines.append(f"  {i}. {icon} {name}")
    r = "\n".join(lines)
    await update.message.reply_text(r)
    return r
