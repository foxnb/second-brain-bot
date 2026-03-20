"""
Revory - AI Service
Together AI: понимает что хочет пользователь и извлекает параметры.
"""

import os
import json
import logging
from datetime import datetime
from together import Together

logger = logging.getLogger(__name__)

MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

SYSTEM_PROMPT = """Ты - AI-ассистент Revory. Твоя задача - понять намерение пользователя и извлечь параметры.

Текущая дата и время: {current_time}
День недели: {weekday}

Верни ТОЛЬКО JSON (без markdown, без ```), строго по формату:

{{
  "intent": "create_event" | "show_events" | "delete_event" | "remind" | "unknown",
  "title": "название события" или null,
  "date": "YYYY-MM-DD" или null,
  "time": "HH:MM" или null,
  "end_time": "HH:MM" или null,
  "period": "today" | "tomorrow" | "week" или null,
  "reply": "короткий ответ пользователю"
}}

Правила:
- "завтра" = дата завтрашнего дня
- "в пятницу" = ближайшая пятница (если сегодня пятница - следующая)
- "через час" = от текущего времени
- "встреча с Иваном в 15:00" = intent=create_event, title="Встреча с Иваном", time="15:00"
- "что у меня сегодня" = intent=show_events, period="today"
- "покажи расписание на завтра" = intent=show_events, period="tomorrow"
- "удали встречу с Иваном" = intent=delete_event, title="Встреча с Иваном"
- "напомни позвонить маме в 18:00" = intent=remind, title="Позвонить маме", time="18:00"
- "напомни про встречу завтра в 9:00" = intent=remind, title="Встреча", time="09:00"
- Ключевые слова для remind: "напомни", "напоминание", "не забудь", "remind"
- Ключевые слова для create_event: "создай", "добавь", "запланируй", "поставь встречу"
- Если не понял - intent="unknown", в reply объясни что не так
- reply должен быть дружелюбным и коротким
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


async def parse_message(text: str) -> dict:
    now = datetime.now()
    weekday = WEEKDAYS_RU[now.weekday()]

    system = SYSTEM_PROMPT.format(
        current_time=now.strftime("%Y-%m-%d %H:%M"),
        weekday=weekday,
    )

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
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
            "reply": "Прости, не смогла разобрать команду. Попробуй переформулировать."
        }

    except Exception as e:
        logger.error(f"AI error: {e}")
        return {
            "intent": "unknown",
            "reply": "Произошла ошибка. Попробуй ещё раз."
        }