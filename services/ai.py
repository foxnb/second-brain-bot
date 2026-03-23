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

Текущая дата и время пользователя: {current_time}
День недели: {weekday}
Часовой пояс пользователя: {timezone}

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

КРИТИЧЕСКИ ВАЖНО — время в 24-часовом формате:
- "утра", "утром" = раннее время: "4 утра" = "04:00", "7 утра" = "07:00", "11 утра" = "11:00"
- "дня", "днём" = дневное время: "4 дня" = "16:00", "3 дня" = "15:00", "12 дня" = "12:00"
- "вечера", "вечером" = вечернее: "6 вечера" = "18:00", "8 вечера" = "20:00", "9 вечера" = "21:00"
- "ночи" = ночное: "2 ночи" = "02:00", "12 ночи" = "00:00"
- Если НЕ указано утра/дня/вечера и число от 1 до 7 — скорее всего дневное время (13:00-19:00), но если контекст неясен, используй дневное
- Если указано 15:00, 16:00 и т.д. — уже в 24-часовом формате, не меняй
- "в 4" без уточнения = "16:00" (дневное по умолчанию для чисел 1-7)
- "в 8" без уточнения = "08:00" (утреннее для чисел 8-11)

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
- Если пользователь указывает другой часовой пояс ("в 12 по мск", "в 10 по киеву") — пересчитай время в часовой пояс пользователя ({timezone}) и верни пересчитанное время
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


async def parse_message(
    text: str,
    user_now: datetime | None = None,
    tz_name: str = "Europe/Moscow",
) -> dict:
    if user_now is None:
        user_now = datetime.now()

    weekday = WEEKDAYS_RU[user_now.weekday()]

    system = SYSTEM_PROMPT.format(
        current_time=user_now.strftime("%Y-%m-%d %H:%M"),
        weekday=weekday,
        timezone=tz_name,
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