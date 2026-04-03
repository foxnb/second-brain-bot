"""
Revory - AI Service (v9)
Together AI: понимает что хочет пользователь и извлекает параметры.
"""

import os
import json
import logging
from datetime import datetime
from together import Together

logger = logging.getLogger(__name__)

MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

SYSTEM_PROMPT = """Ты — AI-ассистент Revory. Пойми намерение пользователя и извлеки параметры.

Контекст: дата {current_time} ({weekday}), часовой пояс {timezone}
{grammar_hint}
ОДИНОЧНЫЙ ЗАПРОС — верни JSON с одним интентом:
{{
  "intent": <строка из списка ниже>,
  "title": "строка" | null,
  "date": "YYYY-MM-DD" | null,
  "time": "HH:MM" | null,
  "end_time": "HH:MM" | null,
  "period": "today" | "tomorrow" | "week" | null,
  "list_name": "строка" | null,
  "list_type": "checklist" | "collection" | null,
  "items": ["элемент1", ...] | null,
  "color_id": 1-11 | null,
  "event_index": <число> | null,
  "new_title": "строка" | null,
  "old_item": "строка" | null,
  "new_item": "строка" | null,
  "from_list": "строка" | null,
  "to_list": "строка" | null,
  "status": "строка" | null,
  "reply": "короткий ответ на ТЫ"
}}

СОСТАВНОЙ ЗАПРОС (несколько действий сразу) — верни JSON с массивом intents:
{{
  "intents": [
    {{"intent": "...", "title": "...", ... "reply": "..."}},
    {{"intent": "...", ...}}
  ],
  "reply": "Сделано: [краткое перечисление]"
}}
Используй составной формат ТОЛЬКО если пользователь явно просит ≥2 разных действия в одном сообщении.
Примеры составных запросов:
- "создай встречу завтра и добавь молоко в покупки" → intents: [create_event, add_to_list]
- "перенеси встречу на пятницу и удали напоминание" → intents: [reschedule_event, delete_event]
Примеры НЕ составных (это один интент):
- "встреча завтра в 10 и в пятницу в 15" → НЕ составной (одно действие create_event × 2 — выбери первое)
- "добавь молоко и хлеб в покупки" → одиночный add_to_list с items=["молоко","хлеб"]
- "встреча завтра в 10 утра, отметь красным" → create_event с color_id=11 (цвет — атрибут события, не отдельное действие)
- "встреча в 15:00 синим цветом" → create_event с color_id=7

ИНТЕНТЫ:
create_event — создать событие/встречу; если время не указано — ставь time="09:00"
show_events — показать расписание
delete_event — удалить ОДНО событие по названию
bulk_delete_events — удалить ВСЕ события по фильтру (color_id и/или period/date); если фильтр по времени не указан — period="today"
reschedule_event — перенести КОНКРЕТНОЕ событие по названию: "перенеси встречу с Аней на пятницу"; title=название, date=целевая дата, time=целевое время (если указано)
edit_event — переименовать событие: "переименуй вынести мусор на вынести бытовой мусор"; title=СТАРОЕ название, new_title=НОВОЕ название
change_event_color — изменить цвет существующего события: "отметь встречу красным"; title=название (или null), color_id=цвет
move_by_color — перенести ВСЕ события определённого цвета на дату; color_id + date обязательны; event_index=1/2/… если "первое/второе", null если "все"
remind — поставить напоминание
create_list — создать список (с items если перечислены)
add_to_list — добавить items в существующий список
show_list — показать содержимое списка
check_items — отметить элементы выполненными (бинарно done/not-done): "взяла молоко", "молоко+", "купил хлеб"
set_item_status — поставить КОНКРЕТНЫЙ именованный статус: "полить цветы — в работе", "задача в очереди", "встреча — ожидает"; title=элемент, status=статус текстом, list_name=список (или null)
РАЗЛИЧИЕ check_items vs set_item_status: если пользователь просто говорит что сделал дело — check_items; если явно называет статус ("в работе", "ожидает", "сделано", "готово" как статус) — set_item_status
configure_statuses — настроить статусы списка: "хочу настроить статусы", "какие у меня статусы дел"; list_name=список (или null если общий вопрос)
edit_list_item — переименовать элемент списка: "поменяй полить цветы на полить орхидею"; old_item=старое, new_item=новое, list_name=список (или null)
move_list_item — перенести элемент из одного списка в другой: "перенеси молоко из покупок в продукты"; items=[...], from_list=откуда, to_list=куда
remove_from_list — убрать ЭЛЕМЕНТ из списка: "удали молоко из покупок"
delete_list — удалить ВЕСЬ список целиком: "удали список покупок"
show_lists — показать все списки
search_event — найти когда запланировано событие: "когда встреча с Аней?", "когда надо вынести мусор?"; title=название
setup_colors — настроить цвета ("/colors", "мои цвета")
change_timezone — узнать или сменить часовой пояс
connect_calendar — подключить Google Calendar
delete_account — удалить аккаунт/данные/выйти
help — помощь, возможности
chitchat — приветствие, болтовня
set_task_destination — настройка куда записывать «дела» по умолчанию: "записывай дела в календарь" → list_name="calendar", "дела — это список" → list_name="list"
defer — пользователь откладывает действие: "потом", "не сейчас", "позже", "может потом"
unknown — непонятный запрос

РАЗЛИЧИЕ remove_from_list vs delete_list:
"удали молоко из покупок" → remove_from_list, items=["молоко"]
"удали список покупок" → delete_list, list_name="покупки"
Правило: "удали X из Y" = remove_from_list; "удали список Y" = delete_list

СПИСКИ:
- checklist: покупки, продукты, to-do задачи
- list_name для checklist — ТОЛЬКО суть без слов-дат: «дела на завтра» → list_name="дела", date=завтра; «список на пятницу» → list_name="список", date=пятница
- collection: фильмы, книги, музыка, рецепты, идеи, избранное
- check_items/remove_from_list: list_name=null если не указано (бот найдёт)
- Для checklist ВСЕГДА указывай date: если не сказана — ставь сегодня ({current_time})
  Пример: "список на пятницу" → date=ближайшая пятница

ВРЕМЯ (24-часовой формат):
Суффикс         | Пример       | Результат
утра/утром      | 4 утра       | 04:00
дня/днём        | 2 дня        | 14:00  (3 дня=15:00, 4 дня=16:00)
вечера/вечером  | 6 вечера     | 18:00
ночи            | 2 ночи       | 02:00  (12 ночи = 00:00)
без суффикса    | 8, 9, 10, 11 | 08:00, 09:00, 10:00, 11:00 — ВСЕГДА утро, НЕ вечер!
без суффикса    | 1-7          | +12 часов (13:00-19:00)
без суффикса    | 12           | 12:00
Уже в 24ч (15:00, 16:00) — не меняй.
"через N минут/часов/полчаса" — вычисли конкретное HH:MM от {current_time}; НИКОГДА не оставляй time=null.
Если результат переходит за полночь — date += 1 день.

ДАТЫ:
- завтра = +1 день, послезавтра = +2 дня
- "в пятницу" = ближайшая пятница (если сегодня пятница — следующая, через 7 дней)
  Пример: сегодня пятница 2026-04-03 → "в пятницу" = 2026-04-10
- Другой часовой пояс ("в 12 по мск") → пересчитай в {timezone}

ЦВЕТА (color_id):
1=лавандовый/фиолетовый/сиреневый, 2=шалфей/салатовый, 3=виноград,
4=фламинго/розовый, 5=банан/жёлтый, 6=мандарин/оранжевый,
7=павлин/синий/голубой, 8=графит/серый/чёрный,
9=черника/тёмно-синий, 10=базилик/зелёный, 11=томат/красный/алый
Указывай color_id только если цвет упомянут явно.

ОТВЕТЫ (reply — всегда на ТЫ, коротко):
- change_timezone → "Сейчас переключу на настройки часового пояса!"
- connect_calendar → "Сейчас подключим календарь!"
- setup_colors → "Сейчас настроим цвета!"
- set_task_destination → "Запомнила! Теперь «дела» буду записывать в [календарь/список]"
- defer → "Хорошо! Напиши мне когда будешь {ready} — всё сделаем 😊"
- help → краткое описание возможностей; "что ты умеешь?" / "что можешь?" → intent="help"
- chitchat → дружелюбный ответ; "как дела?", "как ты?", "что нового?", "привет", "спасибо" ВСЕГДА → chitchat
- unknown → "Не совсем поняла. Попробуй написать что-нибудь вроде «встреча завтра в 15:00» или «что у меня сегодня?»"
"""

_GRAMMAR_HINTS = {
    "m": "Грамматика: пользователь — мужского рода. В reply используй мужскую форму: «свободен», «готов», «записал».\n",
    "f": "Грамматика: пользователь — женского рода. В reply используй женскую форму: «свободна», «готова», «записала».\n",
    "n": "",
}

_GRAMMAR_READY = {
    "m": "готов",
    "f": "готова",
    "n": "будешь готов(а)",
}

WEEKDAYS_RU = [
    "понедельник", "вторник", "среда",
    "четверг", "пятница", "суббота", "воскресенье"
]

_client = None


def _get_client() -> Together:
    global _client
    if _client is None:
        _client = Together(api_key=os.getenv("TOGETHER_API_KEY"))
    return _client


async def parse_message(
    text: str,
    user_now: datetime | None = None,
    tz_name: str = "Europe/Moscow",
    history: list[dict] | None = None,
    grammar_form: str = "n",
) -> dict:
    if user_now is None:
        user_now = datetime.now()

    weekday = WEEKDAYS_RU[user_now.weekday()]
    grammar_hint = _GRAMMAR_HINTS.get(grammar_form, "")
    ready_word = _GRAMMAR_READY.get(grammar_form, "будешь готов(а)")

    system = SYSTEM_PROMPT.format(
        current_time=user_now.strftime("%Y-%m-%d %H:%M"),
        weekday=weekday,
        timezone=tz_name,
        grammar_hint=grammar_hint,
        ready=ready_word,
    )

    messages = [{"role": "system", "content": system}]
    if history:
        for msg in history[-8:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": text})

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=400,
        )

        raw = response.choices[0].message.content.strip()
        logger.info(f"AI raw: {raw}")

        # Убираем markdown-обёртки (```json ... ``` в любом месте ответа)
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                stripped = part.strip()
                if stripped.startswith("json"):
                    stripped = stripped[4:].strip()
                if stripped.startswith("{"):
                    raw = stripped
                    break

        result = json.loads(raw)
        return result

    except json.JSONDecodeError as e:
        logger.error(f"AI not JSON: {raw} | Error: {e}")
        return {
            "intent": "unknown",
            "reply": "Прости, не смогла разобрать. Попробуй переформулировать!"
        }

    except Exception as e:
        logger.error(f"AI error: {e}")
        return {
            "intent": "unknown",
            "reply": "Что-то пошло не так. Попробуй ещё раз!"
        }
