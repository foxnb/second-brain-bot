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

Верни ТОЛЬКО валидный JSON без markdown и ```:
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
  "reply": "короткий ответ на ТЫ"
}}

ИНТЕНТЫ:
create_event — создать событие/встречу
show_events — показать расписание
delete_event — удалить ОДНО событие по названию
bulk_delete_events — удалить ВСЕ события по фильтру (color_id и/или period/date); если фильтр по времени не указан — period="today"
move_by_color — перенести события по цвету на другую дату; обязательно color_id + date (целевая дата)
remind — поставить напоминание
create_list — создать список (с items если перечислены)
add_to_list — добавить items в существующий список
show_list — показать содержимое списка
check_items — отметить элементы выполненными: "взяла молоко", "молоко+"
remove_from_list — убрать ЭЛЕМЕНТ из списка: "удали молоко из покупок"
delete_list — удалить ВЕСЬ список целиком: "удали список покупок"
show_lists — показать все списки
setup_colors — настроить цвета ("/colors", "мои цвета")
change_timezone — узнать или сменить часовой пояс
connect_calendar — подключить Google Calendar
delete_account — удалить аккаунт/данные/выйти
help — помощь, возможности
chitchat — приветствие, болтовня
unknown — непонятный запрос

РАЗЛИЧИЕ remove_from_list vs delete_list:
"удали молоко из покупок" → remove_from_list, items=["молоко"]
"удали список покупок" → delete_list, list_name="покупки"
Правило: "удали X из Y" = remove_from_list; "удали список Y" = delete_list

СПИСКИ:
- checklist: покупки, продукты, to-do задачи
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
- help → краткое описание возможностей
- chitchat → дружелюбный ответ
- unknown → "Не совсем поняла. Попробуй написать что-нибудь вроде «встреча завтра в 15:00» или «что у меня сегодня?»"
"""

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
) -> dict:
    if user_now is None:
        user_now = datetime.now()

    weekday = WEEKDAYS_RU[user_now.weekday()]

    system = SYSTEM_PROMPT.format(
        current_time=user_now.strftime("%Y-%m-%d %H:%M"),
        weekday=weekday,
        timezone=tz_name,
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
            max_tokens=300,
        )

        raw = response.choices[0].message.content.strip()
        logger.info(f"AI raw: {raw}")

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]

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