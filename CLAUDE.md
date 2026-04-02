# Revory — Project Summary for Claude Code

> Этот файл содержит всё, что нужно чтобы продолжить разработку.
> Последнее обновление: 02.04.2026

---

## Что это

**Revory** (@Revory_bot) — Telegram-бот, AI-ассистент для продуктивности.
Позиционирование: "второй мозг" — не просто календарь, а персональный ассистент
с планированием, списками, заметками и анализом поведения.

Долгосрочная архитектура: "один мозг, много интерфейсов":
Telegram → PWA → VK/Max → iOS/Android.

---

## Стек

- **Python 3.13** + python-telegram-bot (GitHub install, не PyPI — из-за 3.13)
- **Starlette + uvicorn** — webhook сервер
- **asyncpg** → Supabase (PostgreSQL, Frankfurt) с ssl="require"
- **Together AI** (Llama-3.3-70B-Instruct-Turbo) — NLU парсинг
- **Google Calendar API** (OAuth Web Application, public callback)
- **Koyeb** (Frankfurt, Free tier, webhook mode, auto-deploy from GitHub)

### Репозиторий
- GitHub: `foxnb/second-brain-bot` (private)
- Локальный путь: `C:\Users\bear\Documents\second-brain-bot`
- IDE: Cursor на Windows (PowerShell)

### ENV переменные (в Koyeb)
- `BOT_TOKEN` — Telegram bot token
- `DATABASE_URL` — Supabase PostgreSQL connection string
- `TOGETHER_API_KEY` — Together AI
- `GOOGLE_CREDENTIALS` — OAuth credentials JSON (строкой)
- `WEBHOOK_URL` — публичный URL на Koyeb
- `PORT` — 8000 (default)

---

## Структура файлов

```
second-brain-bot/
├── main.py                  # Starlette app, webhook, команды /start /auth /timezone /colors /disconnect /logout /deletedata
├── Dockerfile               # python:3.13-slim, git для pip install
├── requirements.txt         # python-telegram-bot from GitHub, together, google-auth, asyncpg, starlette, uvicorn, httpx
├── credentials.json         # Google OAuth (в .gitignore)
├── .env                     # Локальные переменные (в .gitignore)
├── migrations/              # SQL миграции (запускать вручную в Supabase)
│   └── v10_task_destination.sql
├── handlers/
│   ├── router.py            # Точка входа: текст → AI парсит → роутинг по intent
│   ├── pending.py           # Мультишаговые диалоги (_pending_actions dict с TTL 5 мин)
│   ├── events.py            # Создание, показ, удаление, перенос, цвета, статус событий
│   ├── delete.py            # Массовое удаление событий по фильтру (bulk_delete_events)
│   ├── reminders.py         # Создание напоминаний
│   ├── lists.py             # CRUD списков (checklist + collection)
│   ├── utils.py             # resolve_user, get_user_now, extract_number, format_date_label
│   ├── search.py            # зарезервирован: поиск событий
│   └── voice.py             # зарезервирован: голосовые сообщения
├── services/
│   ├── ai.py                # Together AI — парсинг текста в JSON {intent, title, date, time, ...}
│   ├── calendar.py          # Google Calendar OAuth + create/delete/move/patch_color event
│   ├── database.py          # asyncpg CRUD: users, events, lists, color_mappings, task_destination и др.
│   └── sync.py              # Ленивая sync Google Calendar → events (syncToken, 410 fallback)
└── revory_db_schema_v9.md   # актуальная схема БД (+ v10 изменения ниже)
```

---

## Схема БД (v9 + v10 изменения)

### users — новые поля (v10)
- `task_destination TEXT CHECK IN ('calendar', 'list')` — куда по умолчанию записывать "дела" (NULL = не задано, спросить)

**SQL миграция (выполнена / нужно выполнить в Supabase):**
```sql
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS task_destination TEXT
    CHECK (task_destination IN ('calendar', 'list'));
```

### Остальные таблицы — без изменений относительно v9
- **events** — color_id теперь патчится через `update_event_color()` (новая функция)
- **color_mappings** — используются для `mark_event_done`: ищем label="сделано/done/выполнено"

---

## Архитектурные решения

### UUID вместо Telegram ID
Внутренний user_id — UUID, Telegram ID хранится в auth_methods.

### Ленивая sync с syncToken
При каждом `show_events` / `delete_event` / `reschedule_event` / `change_event_color` вызывается `sync_calendar()`.

### Pending actions (12 типов, TTL 5 минут)
| Action | Триггер | Описание |
|--------|---------|----------|
| delete_choice | delete_event | Выбор события из нескольких совпадений |
| create_list_confirm | add_to_list | Подтверждение создания нового списка |
| add_to_list_choice | add_to_list | Выбор списка для добавления |
| delete_list_choice | delete_list | Выбор списка для удаления |
| color_setup | show_events (авто) | Первичная настройка цветов |
| color_edit | /colors | Редактирование цветов |
| bulk_delete_confirm | bulk_delete_events | Подтверждение массового удаления |
| move_by_color_confirm | move_by_color | Подтверждение переноса по цвету |
| create_duplicate_confirm | create_event | Подтверждение создания дубликата |
| task_destination_choice | router (дела) | Выбор куда писать «дела» (1 раз) |
| reschedule_choice | reschedule_event | Выбор события для переноса |
| change_color_choice | change_event_color / mark_done | Выбор события для смены цвета |

**Важно про pending:** Если pending активен и приходит новая команда (длинный текст / глаголы действий) — `return False` чтобы router сбросил pending и обработал как обычное сообщение. Defer-слова ("потом", "попозже") — `clear_pending` + ответ.

### Деduplication событий
Перед `create_event` проверяется `find_duplicate_event()`: то же название ±5 минут → спрашивает подтверждение.

### task_destination
Слово "дела/задачи/задача" в запросе = неоднозначно (список или календарь?).
Router проверяет `users.task_destination`:
- `None` → спросить один раз, сохранить, выполнить
- `'calendar'` → override intent на create_event/show_events
- `'list'` → override intent на create_list/show_list
Изменить: "записывай дела в календарь" / "записывай дела в список" → intent `set_task_destination`.

### Свободный слот времени
Если `create_event` без указания времени → `_find_free_slot()`:
- Начинает с 09:00, шаг 1 час, максимум до 22:00
- Слот занят если есть перекрывающее событие в БД

### mark_event_done
"Пометь как сделано" → ищет color_mapping с label="сделано/done/выполнено" → патчит цвет.
Default: color_id=2 (шалфей/зелёный) если mapping не настроен.
Настройка через "сделано — зеленый" → intent `setup_colors`.

### Цветовая модель
Google Calendar colorId (1-11) → color_mappings (label + emoji).
При show_events: цветные кружочки перед событиями.
Автовопрос при первом обнаружении цветов (colors_asked flag).

---

## Текущие intents (25 штук)

| Intent | Описание | Пример |
|--------|----------|--------|
| create_event | Создать событие (время по умолч. 09:00) | "встреча завтра в 15:00", "запиши в календарь: X" |
| show_events | Показать расписание | "что у меня сегодня?" |
| delete_event | Удалить одно событие | "удали встречу с Аней" |
| bulk_delete_events | Удалить все события по фильтру | "удали все красные события" |
| move_by_color | Перенести события по цвету | "перенеси синие на следующую неделю" |
| reschedule_event | Перенести конкретное событие по названию | "перенеси ртутный кек на сегодня" |
| change_event_color | Изменить цвет события на конкретный | "отметь встречу красным" |
| mark_event_done | Отметить событие как выполненное | "пометь как сделано", "готово" |
| remind | Напоминание | "напомни купить молоко в 10" |
| create_list | Создать список | "список покупок: молоко, хлеб" |
| add_to_list | Добавить в список | "добавь яблоки в покупки" |
| show_list | Показать список | "что в списке покупок?" |
| check_items | Отметить элемент списка выполненным | "взяла молоко" |
| remove_from_list | Убрать элемент из списка | "удали молоко из покупок" |
| delete_list | Удалить весь список | "удали список покупок" |
| show_lists | Все списки | "мои списки" |
| setup_colors | Настройка цветов / статусов цветов | "настрой цвета", "сделано — зеленый" |
| set_task_destination | Настроить куда писать «дела» | "записывай дела в календарь" |
| change_timezone | Часовой пояс | "поменяй часовой пояс" |
| connect_calendar | Подключить календарь | "подключить гугл" |
| delete_account | Удалить аккаунт | "удали мои данные" |
| help | Помощь | "что ты умеешь?" |
| chitchat | Болтовня | "привет" |
| defer | Откладывает действие на потом | "потом", "попозже", "не сейчас" |
| unknown | Не распознано | — |

### Поля JSON от AI
```json
{
  "intent": "...",
  "title": "название события/задачи или null",
  "date": "YYYY-MM-DD или null",
  "time": "HH:MM или null (если нет — create_event ставит 09:00)",
  "end_time": "HH:MM или null",
  "period": "today|tomorrow|week или null",
  "list_name": "название списка или null (для set_task_destination: 'calendar'/'list')",
  "list_type": "checklist|collection или null",
  "items": ["элемент1"] или null,
  "color_id": 1-11 или null,
  "event_index": 1 или 2 (для move_by_color: "первое/второе") или null,
  "reply": "короткий ответ пользователю на ТЫ"
}
```

### Конфликты между похожими intent'ами (важно!)
- `check_items` (списки) vs `mark_event_done` (события) — триггер "сделано/выполнено/готово"
  → `mark_event_done` если нет активного чеклиста с таким элементом
- `change_event_color` vs `mark_event_done` — цвет указан явно → `change_event_color`; только "сделано/выполнено" без цвета → `mark_event_done`
- `setup_colors` vs `set_task_destination` — "сделано — зеленый" (цвет=значение) → `setup_colors`; "записывай дела в список" → `set_task_destination`
- `reschedule_event` vs `move_by_color` — есть название события → `reschedule_event`; только цвет → `move_by_color`

---

## Команды бота

| Команда | Описание |
|---------|----------|
| /start | Регистрация + онбординг (timezone) |
| /auth | Google OAuth flow |
| /timezone | Сменить часовой пояс |
| /colors | Настройка цветов событий |
| /disconnect | Отключить календарь |
| /logout | Удалить аккаунт и все данные |
| /deletedata | Алиас /logout (GDPR) |

---

## Фоновые процессы

1. **keep_alive** — пинг /health каждые 5 минут (Koyeb Free tier)
2. **reminder_worker** — каждые 30 секунд, pending reminders → отправить → sent

---

## Деплой

1. `git push origin claude/loving-tharp` → GitHub
2. Merge в master → Koyeb auto-deploy
3. SQL миграции — вручную в Supabase SQL Editor
4. Логи — Koyeb dashboard

### Pending SQL миграции
```sql
-- v10: task_destination (выполнить если ещё нет)
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS task_destination TEXT
    CHECK (task_destination IN ('calendar', 'list'));
```

---

## Стоимость запросов

- Системный промпт: ~1500 токенов (растёт с каждым новым intent'ом)
- История (8 сообщений): ~400 токенов
- Итого на запрос: ~2000 токенов input + ~100 output
- Цена (Together AI Llama-3.3-70B): $0.88/1M → **~$0.0018/сообщение**
- При 1000 сообщений/день: ~$1.8/день
- **⚠️ Следующий шаг:** оптимизировать системный промпт (сократить на 30-40%)

---

## Бэклог

### Закрыто в этой сессии (02.04.2026)
- [x] `defer` intent — "попозже", "потом", "не сейчас"
- [x] Дедупликация событий — проверка ±5 мин перед created_event
- [x] `event_index` в `move_by_color` — "первое/второе жёлтое"
- [x] `move_by_color_confirm` — поддержка "только одно", "первое", числа
- [x] `task_destination` preference — "дела" в календарь или список
- [x] `set_task_destination` intent
- [x] `_find_free_slot` — дефолтное время 09:00, сдвиг если занято
- [x] `reschedule_event` intent — перенос события по названию
- [x] Убран ошибочный `offset_days == 0` блок в `move_by_color`
- [x] `change_event_color` intent — смена цвета существующего события
- [x] `mark_event_done` intent — "пометь как сделано" → done-цвет из маппингов
- [x] "оба/обе/все" в `change_color_choice` pending

### Ближайшие задачи
- [ ] **Оптимизация системного промпта** — сократить на 30-40%, убрать дублирование
- [ ] **Тест-сет для AI** — файл с парами text→expected_intent, скрипт прогона
- [ ] **edit_event** — изменить название/время существующего события
- [ ] **grammar_form** — использовать m/f/n ("свободна"/"свободен")
- [ ] **Шифрование токенов** — AES-256-Fernet для calendar_connections
- [ ] **Поиск событий** — "когда следующая встреча с Аней?" — handlers/search.py
- [ ] **Голосовые** — voice messages → текст → AI — handlers/voice.py
- [ ] **Composite commands** — "удали X, а Y перенеси на пятницу"

### Средний горизонт
- [ ] PWA (email+password auth)
- [ ] Яндекс Календарь
- [ ] Утренний/вечерний дайджест (subscriptions)
- [ ] Привязка к категориям (work, personal, etc.)

### Дальний горизонт
- [ ] Группы
- [ ] Notes + entity_links
- [ ] Паттерны поведения
- [ ] 152-ФЗ data localization

---

## Принципы разработки

1. **Correct over quick** — обсуждаем архитектуру перед кодированием
2. **Итеративный деплой** — валидация через Koyeb logs + Supabase SQL queries
3. **Hardcoded timezones = неприемлемо** — timezone всегда из users
4. **Мягкое удаление** — events: is_deleted + deleted_at, cleanup через 30 дней
5. **Pending = хрупкое место** — return False для новых команд, defer-слова → clear_pending
6. **Промпт растёт = стоимость растёт** — каждый новый intent добавляет ~50-100 токенов на каждый запрос
7. **Контекст в каждом чате** — этот файл читать первым

---

## Конкурентное позиционирование

Gap: нет Telegram-бота, который совмещает AI-парсинг + Calendar sync + контекстное хранилище + поведенческие паттерны.
Существующие решения закрывают 1-2 из этих аспектов, но не все вместе.
