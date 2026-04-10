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

**Ленивая sync с syncToken** — при show_events/delete_event/search_event вызывается sync_calendar():
- syncToken есть → инкрементальный запрос; нет → полный (30 дней назад → 90 вперёд); 410 Gone → сброс

**Pending actions** — мультишаговые диалоги через `_pending_actions` dict с 5-минутным TTL.
Паттерн: set_pending() → handle_pending() → clear_pending().
Текущие pending: `delete_choice`, `create_list_confirm`, `create_list_duplicate_confirm`, `add_to_list_choice`, `delete_list_choice`, `color_setup`, `color_edit`, `bulk_delete_confirm`, `move_by_color_confirm`, `create_duplicate_confirm`, `task_destination_choice`, `reschedule_choice`, `change_color_choice`, `edit_event_choice`, `move_item_create_confirm`, `configure_statuses`, `configure_statuses_choice`, `set_event_status_choice`, `group_new_project_name`, `group_task_reschedule_date`.

**Списки** — `checklist` (чекбоксы + статусы, auto_archive_at) и `collection` (постоянные без чекбоксов).
- Имя checklist формируется как «Дела 04.04» — дата добавляется кодом, list_name из AI = только суть без слов-дат.
- Статусы элементов: `todo` ☐ / `in_progress` ▶ / `done` ✅ (колонка status в list_items, migration v11).
- Кастомные статусы хранятся в `lists.settings JSONB` → `{"statuses": [...]}`.
- **Важно:** `status` колонка имеет `DEFAULT 'todo'`. SQL-запрос `get_list_items` использует `CASE WHEN is_checked THEN 'done' ELSE COALESCE(status, 'todo') END` — иначе COALESCE всегда вернёт 'todo' даже для выполненных.
- **Редактирование списка на месте:** `_last_list_msg` (in-memory dict) хранит `{msg_id, chat_id, list_id, list_data}` последнего отправленного сообщения со списком. `_try_edit_last_list()` редактирует это сообщение через `edit_message_text`, при неудаче — отправляет новое.
- **PgBouncer lag:** параметр `just_checked` в `_try_edit_last_list` принудительно проставляет `is_checked=True, status='done'` в Python до рендера, обходя устаревшее чтение из пула соединений.
- **Приоритет поиска:** при check_items/remove_from_list без явного list_name — сначала ищем в последнем показанном списке (`_last_list_msg`), затем в остальных.

**Цветовая модель** — Google colorId (1-11) → color_mappings (label + emoji). Автовопрос при первом обнаружении (colors_asked flag).

**PostgreSQL нюансы** — COALESCE-based upserts вместо ON CONFLICT для nullable unique columns; list_type как free-text + JSONB.

**Токены** — plain JSON в calendar_connections (TODO: AES-256-Fernet).

**Заметки** — `handlers/notes.py`, таблица `notes` (migration v16). Поля: title, content, url, tags (TEXT[]). Функции DB: `create_note`, `get_user_notes(tag=, limit=)`, `find_notes_by_query`, `delete_note`.

## Деплой

`git push origin master` → GitHub → Koyeb auto-deploy (Dockerfile)
SQL миграции — вручную в Supabase SQL Editor
Схема БД: `revory_db_schema_v9.md` + `migrations/`

## Фоновые процессы

1. **keep_alive** — пинг /health каждые 5 минут (Koyeb Free tier засыпает)
2. **reminder_worker** — каждые 30 секунд, pending reminders → sent

## Принципы разработки

1. Correct over quick — обсуждаем архитектуру перед кодированием
2. Итеративный деплой — Koyeb logs + Supabase SQL queries
3. Timezone всегда из users — hardcoded недопустимы
4. Мягкое удаление — events: is_deleted + deleted_at, cleanup через 30 дней

## Текущие интенты AI (services/ai.py)

### Календарь
| Intent | Описание | Ключевые поля |
|--------|----------|---------------|
| create_event | Создать событие | title, date, time, end_time, color_id |
| show_events | Показать расписание | period, date |
| search_event | Найти когда событие | title |
| edit_event | Переименовать событие | title=старое, new_title=новое |
| reschedule_event | Перенести событие | title, date, time |
| change_event_color | Изменить цвет события | title, color_id |
| delete_event | Удалить одно событие | title |
| bulk_delete_events | Удалить по фильтру | color_id, period/date |
| move_by_color | Перенести по цвету на дату | color_id, date, event_index |
| remind | Напоминание | title, date, time |

### Списки
| Intent | Описание | Ключевые поля |
|--------|----------|---------------|
| create_list | Создать список | list_name, list_type, items, date |
| add_to_list | Добавить элементы | list_name, items |
| show_list | Показать список | list_name |
| show_lists | Все списки | — |
| edit_list_item | Переименовать элемент | old_item, new_item, list_name |
| move_list_item | Перенести элемент | items, from_list, to_list |
| check_items | Отметить выполненным | items, list_name |
| set_item_status | Поставить статус элементу | title, status, list_name |
| configure_statuses | Настроить статусы списка | list_name |
| remove_from_list | Убрать элемент (физически) | items, list_name |
| delete_list | Удалить список | list_name |
| convert_list | Конвертировать тип списка | list_name, list_type |

### Заметки
| Intent | Описание | Ключевые поля |
|--------|----------|---------------|
| create_note | Сохранить заметку | title, description, url, tags |
| show_notes | Показать заметки (все или по тегу) | tags |
| find_note | Найти конкретную заметку | title (поисковый запрос) |
| delete_note | Удалить заметку | title |

### Системные
| Intent | Описание |
|--------|----------|
| setup_colors | Настройка цветов |
| change_timezone | Часовой пояс |
| connect_calendar | Подключить Google Calendar |
| delete_account | Удалить аккаунт |
| set_task_destination | Куда записывать «дела» (calendar/list) |
| defer | Пользователь откладывает |
| help | Помощь |
| chitchat | Болтовня |
| unknown | Не распознано |

## Тест-сет

`tests/test_cases.json` — 65+ кейсов, запуск: `python tests/run_tests.py`
Фильтр: `python tests/run_tests.py --filter create_event`
**Запускать при каждом изменении промпта.**

## Бэклог

### Ближайшие задачи
- [ ] **Применить миграцию v16** — выполнить `migrations/v16_notes.sql` в Supabase SQL Editor (таблица notes)
- [ ] **Шифрование токенов** — AES-256-Fernet, ENCRYPTION_KEY env
- [ ] **grammar_form** — m/f/n для корректных ответов ("свободен"/"свободна")
- [ ] **Composite commands** — несколько интентов за раз
- [ ] **Тестирование цветов** — проверить парсер на реальных данных в проде
- [ ] **Обновить тест-сет** — добавить кейсы для новых интентов (convert_list, check_items, notes и др.)
- [ ] **`_last_list_msg` после рестарта** — сейчас in-memory, сбрасывается при деплое; рассмотреть хранение в Redis/БД

### Средний горизонт
- [ ] **PWA** — FastAPI поверх services/, JWT-auth, React/Next.js
- [ ] **Яндекс Календарь** — calendar_connections уже мультипровайдерный
- [ ] **Subscriptions** — утренний дайджест, вечерний обзор
- [ ] **Categories + Status workflow** — kanban для событий

### Дальний горизонт
- [ ] Группы, Notes/entity_links, паттерны поведения, 152-ФЗ, custom domain
