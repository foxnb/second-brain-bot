# Revory — Project Summary for Claude Code

> Этот файл содержит всё, что нужно чтобы продолжить разработку.
> Положить в корень репозитория как `CLAUDE.md`.
> Последнее обновление: 01.04.2026

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
├── handlers/
│   ├── router.py            # Точка входа: текст → AI парсит → роутинг по intent
│   ├── pending.py           # Мультишаговые диалоги (_pending_actions dict с TTL)
│   ├── events.py            # Создание, показ, удаление, перенос по цвету + цветовые кружочки
│   ├── delete.py            # Массовое удаление событий по фильтру (bulk_delete_events)
│   ├── reminders.py         # Создание напоминаний
│   ├── lists.py             # CRUD списков (checklist + collection)
│   ├── utils.py             # resolve_user, get_user_now, extract_number, format_date_label
│   ├── search.py            # зарезервирован: поиск событий
│   └── voice.py             # зарезервирован: голосовые сообщения
├── services/
│   ├── ai.py                # Together AI — парсинг текста в JSON {intent, title, date, time, ...}
│   ├── calendar.py          # Google Calendar OAuth + create/delete/move event (пишут в БД)
│   ├── database.py          # asyncpg CRUD: users, auth_methods, calendar_connections, events, reminders, lists, color_mappings
│   └── sync.py              # Ленивая sync Google Calendar → events (syncToken, 410 fallback)
└── revory_db_schema_v9.md   # актуальная схема БД
```

---

## Схема БД v9 (16 таблиц)

### Фаза 1 — MVP (задеплоена)
- **users** — UUID PK, email, password_hash, display_name, language, grammar_form, timezone, city, region, rate_limit_*, colors_asked (BOOLEAN)
- **auth_methods** — provider + provider_user_id (UNIQUE), metadata JSONB. Telegram, email, VK, Max
- **calendar_connections** — provider (google/yandex/apple), tokens, calendar_id, is_primary (partial unique), sync_token, status
- **messages** — user_id, role (user/assistant), content, parsed JSONB
- **events** — user_id, calendar_connection_id, external_event_id, title, start/end_time, timezone, color_id (1-11), is_deleted + deleted_at (soft delete)
- **reminders** — user_id, assigned_to, title, remind_at, status (pending/sent/cancelled), repeat_rule
- **statuses** + **status_models** — workflow (Simple: todo→done; Kanban: todo→in_progress→on_review→done)
- **categories** — personal, work, shopping, travel, health, education (is_system + translatable keys)
- **attachments** — entity_type polymorphic
- **lists** + **list_items** — list_type (checklist/collection), settings JSONB, target_date, auto_archive_at
- **color_mappings** — user_id, google_color_id (1-11), label, emoji, category_id

### Фаза 2 — Группы (будущее)
- groups, group_members, audit_log, subscriptions

### Фаза 3 — Второй мозг (будущее)
- notes, entity_links

---

## Архитектурные решения

### UUID вместо Telegram ID
Внутренний user_id — UUID, Telegram ID хранится в auth_methods. Это для мультиплатформенности: один аккаунт через Telegram + PWA + VK.

### Ленивая sync с syncToken
При каждом `show_events` / `delete_event` вызывается `sync_calendar()`:
- Если syncToken есть → инкрементальный запрос (только изменения)
- Если нет → полный запрос (30 дней назад → 90 дней вперёд)
- При 410 Gone → сброс syncToken → полная sync
- Направление: Google → БД (sync) + Бот → Google + БД (create/delete)

### Pending actions
Мультишаговые диалоги через `_pending_actions` dict с 5-минутным TTL.
Паттерн: set_pending() → пользователь отвечает → handle_pending() → clear_pending().
Используется для: delete_choice, create_list_confirm, add_to_list_choice, delete_list_choice, color_setup, color_edit, bulk_delete_confirm, move_by_color_confirm.

### Списки
Два типа: `checklist` (с чекбоксами, auto_archive_at) и `collection` (без чекбоксов, постоянные).
Чеклисты получают дату в имени ("Покупки 31.03").

### Цветовая модель
Google Calendar colorId (1-11) → color_mappings (label + emoji).
При show_events: цветные кружочки перед событиями.
Автовопрос при первом обнаружении цветов (colors_asked flag).
Парсер понимает свободный текст: "красный сделать, синий сделано", "синий — работа", "работа синим".

### Токены
Хранятся в plain JSON в calendar_connections (TODO: AES-256-Fernet).

### PostgreSQL нюансы
- NULL inequality: COALESCE-based upserts вместо ON CONFLICT для nullable unique columns
- list_type как free-text + settings JSONB вместо enum — гибкость без миграций

---

## Текущие intents (AI парсинг)

| Intent | Описание | Пример |
|--------|----------|--------|
| create_event | Создать событие | "встреча завтра в 15:00" |
| show_events | Показать расписание | "что у меня сегодня?" |
| delete_event | Удалить одно событие по названию | "удали встречу с Аней" |
| bulk_delete_events | Удалить все события по фильтру | "удали все красные события", "удали всё сегодня" |
| move_by_color | Перенести события по цвету на другую дату | "перенеси синие на следующую неделю" |
| remind | Напоминание | "напомни купить молоко в 10" |
| create_list | Создать список | "список покупок: молоко, хлеб" |
| add_to_list | Добавить в список | "добавь яблоки в покупки" |
| show_list | Показать список | "что в списке покупок?" |
| check_items | Отметить выполненным | "взяла молоко" |
| remove_from_list | Убрать элемент | "удали молоко из покупок" |
| delete_list | Удалить весь список | "удали список покупок" |
| show_lists | Все списки | "мои списки" |
| setup_colors | Настройка цветов | "настрой цвета" |
| change_timezone | Часовой пояс | "поменяй часовой пояс" |
| connect_calendar | Подключить календарь | "подключить гугл" |
| delete_account | Удалить аккаунт | "удали мои данные" |
| help | Помощь | "что ты умеешь?" |
| chitchat | Болтовня | "привет" |
| unknown | Не распознано | — |

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

## Фоновые процессы (asyncio tasks в main.py)

1. **keep_alive** — пинг /health каждые 5 минут (Koyeb Free tier засыпает)
2. **reminder_worker** — каждые 30 секунд проверяет pending reminders, отправляет и помечает sent

---

## Деплой

1. `git push` → GitHub → Koyeb auto-deploy (Dockerfile)
2. SQL миграции — вручную в Supabase SQL Editor
3. Логи — Koyeb dashboard

---

## Бэклог

### Критические баги — закрыты
- [x] Пустые файлы: `models/event.py` + папка `models/` удалены; `delete.py` реализован; `search.py`, `voice.py` зарезервированы
- [x] `revory_db_schema_v7.md` заменён на v9
- [x] `httpx` добавлен в requirements.txt

### Цветовая модель — закрыта
- [x] SQL миграция: таблица color_mappings + users.colors_asked
- [x] database.py: color_mappings CRUD + обновлённые upsert_event/get_events_from_db
- [x] sync.py: сохраняет colorId из Google API
- [x] events.py: кружочки-эмодзи в show_events + автовопрос
- [x] pending.py: color_setup + color_edit с гибким парсером
- [x] ai.py: intent setup_colors
- [x] router.py: роутинг setup_colors
- [x] main.py: /colors команда
- [ ] **Тестирование**: проверить парсер на реальных данных в проде

### Bulk operations — реализованы (01.04.2026)
- [x] **bulk_delete_events**: удаление событий по фильтру (цвет + период) с подтверждением
  - `handlers/delete.py`: handle_bulk_delete
  - `handlers/pending.py`: bulk_delete_confirm
  - `services/database.py`: get_events_by_color
- [x] **move_by_color**: перенос событий по цвету на другую дату (смещение offset_days)
  - `handlers/events.py`: handle_move_by_color
  - `handlers/pending.py`: move_by_color_confirm
  - `services/calendar.py`: move_event (patch + update DB)
  - `services/database.py`: update_event_times

### Ближайшие задачи
- [ ] **edit_event**: редактирование существующего события (название, время, цвет)
- [ ] **edit_list_item**: редактирование элементов списка (не только check/remove)
- [ ] **Шифрование токенов**: AES-256-Fernet для calendar_connections (ENCRYPTION_KEY env)
- [ ] **grammar_form**: использовать m/f/n для грамматически корректных ответов ("свободен"/"свободна"/"свободно")
- [ ] **Поиск событий**: "когда у меня следующая встреча с Аней?" — handlers/search.py
- [ ] **Голосовые**: распознавание voice messages → текст → AI парсинг — handlers/voice.py
- [ ] **Composite commands**: "удали X, а Y поменяй на Z" — сейчас бот понимает только один intent за раз

### Средний горизонт
- [ ] **PWA**: email+password auth (архитектура готова: users.email, users.password_hash, auth_methods provider='email')
- [ ] **Яндекс Календарь**: calendar_connections уже мультипровайдерный
- [ ] **Subscriptions**: утренний дайджест, вечерний обзор (таблица subscriptions в схеме)
- [ ] **Categories assignment**: привязка событий к категориям (work, personal, etc.)
- [ ] **Status workflow**: канбан для событий (todo → in_progress → done)

### Дальний горизонт
- [ ] **Группы**: groups + group_members + audit_log
- [ ] **Notes**: заметки + entity_links (связи между событиями/заметками/напоминаниями)
- [ ] **Паттерны поведения**: "ты чаще всего планируешь встречи на утро"
- [ ] **152-ФЗ**: data localization по region (RU / EU / OTHER)
- [ ] **Custom domain**: миграция с Koyeb subdomain

---

## Принципы разработки

1. **Correct over quick** — обсуждаем архитектуру перед кодированием
2. **Итеративный деплой** — валидация каждого шага через Koyeb logs + Supabase SQL queries
3. **Hardcoded timezones = неприемлемо** — timezone всегда из users, overridable per-message
4. **Мягкое удаление** — events: is_deleted + deleted_at, cleanup через 30 дней
5. **Чистая миграция** — asyncpg без Supabase SDK, DATABASE_URL swap = полная миграция
6. **Контекст в каждом чате** — саммари + бэклог для continuity

---

## Конкурентное позиционирование

Gap: нет Telegram-бота, который совмещает AI-парсинг + Calendar sync + контекстное хранилище + поведенческие паттерны.
Существующие решения закрывают 1-2 из этих аспектов, но не все вместе.