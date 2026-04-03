# Revory — Project Context

**Revory** (@Revory_bot) — Telegram-бот, AI-ассистент для продуктивности ("второй мозг").
Долгосрочная архитектура: Telegram → PWA → VK/Max → iOS/Android.

## Стек

- Python 3.13 + python-telegram-bot (GitHub install, не PyPI)
- Starlette + uvicorn — webhook сервер
- asyncpg → Supabase (PostgreSQL, Frankfurt, ssl="require")
- Together AI (Llama-3.3-70B-Instruct-Turbo) — NLU парсинг (`services/ai.py`)
- Google Calendar API (OAuth Web Application)
- Koyeb (Frankfurt, Free tier, auto-deploy from GitHub)

**Репозиторий:** `foxnb/second-brain-bot` (private)
**ENV:** BOT_TOKEN, DATABASE_URL, TOGETHER_API_KEY, GOOGLE_CREDENTIALS, WEBHOOK_URL, PORT=8000

## Архитектурные решения

**UUID вместо Telegram ID** — внутренний user_id UUID, Telegram ID в auth_methods. Для мультиплатформенности.

**Ленивая sync с syncToken** — при show_events/delete_event вызывается sync_calendar():
- syncToken есть → инкрементальный запрос; нет → полный (30 дней назад → 90 вперёд); 410 Gone → сброс

**Pending actions** — мультишаговые диалоги через `_pending_actions` dict с 5-минутным TTL.
Паттерн: set_pending() → handle_pending() → clear_pending().

**Списки** — `checklist` (чекбоксы, auto_archive_at) и `collection` (постоянные без чекбоксов).

**Цветовая модель** — Google colorId (1-11) → color_mappings (label + emoji). Автовопрос при первом обнаружении (colors_asked flag).

**PostgreSQL нюансы** — COALESCE-based upserts вместо ON CONFLICT для nullable unique columns; list_type как free-text + JSONB.

**Токены** — plain JSON в calendar_connections (TODO: AES-256-Fernet).

## Деплой

`git push` → GitHub → Koyeb auto-deploy (Dockerfile)
SQL миграции — вручную в Supabase SQL Editor
Схема БД: `revory_db_schema_v9.md`

## Фоновые процессы

1. **keep_alive** — пинг /health каждые 5 минут (Koyeb Free tier засыпает)
2. **reminder_worker** — каждые 30 секунд, pending reminders → sent

## Принципы разработки

1. Correct over quick — обсуждаем архитектуру перед кодированием
2. Итеративный деплой — Koyeb logs + Supabase SQL queries
3. Timezone всегда из users — hardcoded недопустимы
4. Мягкое удаление — events: is_deleted + deleted_at, cleanup через 30 дней

## Бэклог

### Ближайшие задачи
- [ ] **Шифрование токенов** — AES-256-Fernet, ENCRYPTION_KEY env
- [ ] **grammar_form** — m/f/n для корректных ответов ("свободен"/"свободна")
- [ ] **Голосовые** — voice → текст → AI парсинг — handlers/voice.py
- [ ] **Composite commands** — несколько интентов за раз
- [ ] **Тестирование цветов** — проверить парсер на реальных данных в проде
- [ ] **Статусы в show_list** — обновить тест-сет, добавить кейсы для set_item_status
- [x] **edit_event** — переименование события (calendar.py + database.py + events.py)
- [x] **edit_list_item** — переименование элемента списка
- [x] **move_list_item** — перенос элемента между списками
- [x] **search_event** — поиск события по названию в 90 дней
- [x] **Статусная модель** — todo/in_progress/done, диалог настройки (migration v11)

### Средний горизонт
- [ ] **PWA** — FastAPI поверх services/, JWT-auth, React/Next.js
- [ ] **Яндекс Календарь** — calendar_connections уже мультипровайдерный
- [ ] **Subscriptions** — утренний дайджест, вечерний обзор
- [ ] **Categories + Status workflow** — kanban для событий

### Дальний горизонт
- [ ] Группы, Notes/entity_links, паттерны поведения, 152-ФЗ, custom domain
