# Revory — Схема БД v9 (29.03.2026)

## 16 таблиц, 3 фазы реализации

### Ключевые изменения v8 → v9
- **user_id**: BIGINT (Telegram ID) → UUID (внутренний, платформо-независимый)
- **Новая таблица `auth_methods`**: Telegram, email+пароль, VK/Max — способы входа в один аккаунт
- **Новая таблица `calendar_connections`**: мультипровайдер (Google, Яндекс, Apple), один `is_primary`, остальные подключены
- **Убрано из `users`**: `telegram_username`, `google_token_encrypted`, `default_calendar_id`
- **Добавлено в `users`**: `email`, `password_hash`, `region`
- **events**: `google_event_id` → `external_event_id` + `calendar_connection_id`
- **sync_token** в `calendar_connections` — инкрементальная синхронизация Google Calendar
- OAuth scope расширен: `openid` + `userinfo.email` (id_token для email)

---

## Фаза 1 — MVP (текущая, задеплоена)

### users
| Поле | Тип | Описание |
|------|-----|----------|
| id | UUID PK DEFAULT gen_random_uuid() | Внутренний ID пользователя |
| email | TEXT UNIQUE NULL | Email (заполняется из Google OAuth или вручную для PWA) |
| password_hash | TEXT NULL | bcrypt хеш (NULL если вход только через Telegram/OAuth) |
| display_name | TEXT | Имя для отображения |
| language | TEXT DEFAULT 'ru' | Язык интерфейса (ru, en...) |
| grammar_form | TEXT DEFAULT 'n' | Грамматический род: m / f / n |
| timezone | TEXT NULL | IANA timezone |
| city | TEXT NULL | Город для погоды |
| region | TEXT NOT NULL DEFAULT 'EU' | Регион хранения данных: RU / EU / OTHER (для 152-ФЗ, deferred) |
| rate_limit_count | INT DEFAULT 0 | Счётчик запросов (сбрасывается раз в час) |
| rate_limit_reset | TIMESTAMPTZ NULL | Когда сбросить счётчик |
| created_at | TIMESTAMPTZ DEFAULT now() | Дата регистрации |

**Constraint**: CHECK (email IS NOT NULL OR EXISTS auth_method) — реализуется на уровне приложения: у пользователя должен быть хотя бы один способ входа.

### auth_methods
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users ON DELETE CASCADE | Владелец |
| provider | TEXT NOT NULL | 'telegram', 'email', 'vk', 'max' |
| provider_user_id | TEXT NOT NULL | Telegram user_id, VK id и т.д. (для email = email) |
| metadata | JSONB NULL | Доп. данные: {"username": "@nick", "first_name": "..."} |
| created_at | TIMESTAMPTZ DEFAULT now() | |

**Constraints**:
- UNIQUE (provider, provider_user_id) — один Telegram-аккаунт = один пользователь
- INDEX (user_id) — быстрый поиск всех методов входа

**Логика**: При `/start` в Telegram → ищем auth_methods WHERE provider='telegram' AND provider_user_id=telegram_id. Если нет — создаём users + auth_methods. Если есть — возвращаем user_id.

### calendar_connections
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users ON DELETE CASCADE | Владелец |
| provider | TEXT NOT NULL | 'google', 'yandex', 'apple' |
| provider_email | TEXT NULL | Email аккаунта провайдера |
| access_token_encrypted | TEXT NOT NULL | OAuth access token (TODO: AES-256-Fernet) |
| refresh_token_encrypted | TEXT NULL | OAuth refresh token (TODO: AES-256-Fernet) |
| token_expires_at | TIMESTAMPTZ NULL | Когда истекает access token |
| calendar_id | TEXT DEFAULT 'primary' | ID календаря у провайдера |
| is_primary | BOOLEAN DEFAULT FALSE | Основной для записи |
| status | TEXT DEFAULT 'active' | 'active', 'expired', 'revoked' |
| sync_token | TEXT NULL | Google syncToken для инкрементальной sync |
| connected_at | TIMESTAMPTZ DEFAULT now() | |
| updated_at | TIMESTAMPTZ DEFAULT now() | |

**Constraints**:
- UNIQUE (user_id, provider, provider_email) — один Google-аккаунт подключается один раз
- Partial UNIQUE INDEX: (user_id) WHERE is_primary = TRUE — только один primary на пользователя
- INDEX (user_id, status) — быстрый поиск активных подключений

**Логика `is_primary`**:
- Первое подключение автоматически становится primary
- Переключение: `/calendar switch yandex` — снимает primary со старого, ставит на новый
- При создании события бот пишет в primary. При просмотре — агрегирует из всех active

### messages
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users ON DELETE CASCADE | Кто написал |
| group_id | BIGINT FK → groups NULL | NULL = личный чат |
| role | TEXT | 'user' или 'assistant' |
| content | TEXT | Текст сообщения |
| parsed | JSONB NULL | Распарсенный AI JSON (intent, title, date...) |
| feedback | TEXT NULL | 'up', 'down', NULL |
| created_at | TIMESTAMPTZ DEFAULT now() | |

### events
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users ON DELETE CASCADE | Создатель |
| group_id | BIGINT FK → groups NULL | NULL = личное |
| assigned_to | UUID FK → users NULL | Кому назначено |
| status_id | INT FK → statuses | Текущий статус |
| category_id | INT FK → categories NULL | Категория |
| calendar_connection_id | INT FK → calendar_connections NULL | Через какое подключение создано |
| external_event_id | TEXT NULL | ID у провайдера (Google event ID, Яндекс...) |
| title | TEXT | Название |
| description | TEXT DEFAULT '' | Описание |
| start_time | TIMESTAMPTZ | Начало |
| end_time | TIMESTAMPTZ | Конец |
| timezone | TEXT | Timezone события |
| recurrence_rule | TEXT NULL | RRULE для повторяющихся |
| color_id | INT NULL | Цвет у провайдера (Google: 1-11) |
| is_deleted | BOOLEAN DEFAULT FALSE | Мягкое удаление |
| deleted_at | TIMESTAMPTZ NULL | Когда удалено |
| created_at | TIMESTAMPTZ DEFAULT now() | |
| updated_at | TIMESTAMPTZ DEFAULT now() | |

**Constraints**:
- UNIQUE (calendar_connection_id, external_event_id) WHERE external_event_id IS NOT NULL — нет дубликатов
- INDEX (user_id, start_time) WHERE is_deleted = FALSE — быстрый поиск предстоящих

### reminders
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users ON DELETE CASCADE | Создатель |
| group_id | BIGINT FK → groups NULL | NULL = личное |
| assigned_to | UUID FK → users | Кому напомнить |
| event_id | INT FK → events NULL | Привязка к событию |
| title | TEXT | Текст напоминания |
| remind_at | TIMESTAMPTZ | Когда напомнить |
| status | TEXT DEFAULT 'pending' | pending / sent / cancelled |
| repeat_rule | TEXT NULL | Cron-выражение для повторов |
| created_at | TIMESTAMPTZ DEFAULT now() | |

### status_models
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| owner_user_id | UUID FK → users NULL | NULL если модель группы |
| owner_group_id | BIGINT FK → groups NULL | NULL если личная |
| name | TEXT | "Простой", "Kanban", кастомное |
| is_default | BOOLEAN | Модель по умолчанию |
| created_at | TIMESTAMPTZ DEFAULT now() | |

### statuses
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| model_id | INT FK → status_models | Какой модели принадлежит |
| name | TEXT | "todo", "done" (ключ если is_system) |
| position | INT | Порядок сортировки |
| color | TEXT NULL | Hex цвет |
| is_system | BOOLEAN DEFAULT FALSE | Системный = переводимый |
| is_terminal | BOOLEAN DEFAULT FALSE | TRUE для Done, Cancelled |

### categories
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| owner_user_id | UUID FK → users NULL | NULL если категория группы |
| owner_group_id | BIGINT FK → groups NULL | NULL если личная |
| name | TEXT | "work", "personal" (ключ если is_system) |
| color | TEXT NULL | Hex цвет |
| icon | TEXT NULL | Эмодзи |
| position | INT | Порядок сортировки |
| is_system | BOOLEAN DEFAULT FALSE | Системная = переводимая |

### attachments
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users ON DELETE CASCADE | Кто загрузил |
| entity_type | TEXT | 'event', 'note', 'message' |
| entity_id | INT | ID сущности |
| file_type | TEXT | 'photo', 'voice', 'document' |
| telegram_file_id | TEXT NULL | Telegram file ID (NULL если не из Telegram) |
| storage_path | TEXT NULL | Путь в S3/storage (для файлов не из Telegram) |
| url | TEXT NULL | URL если есть |
| transcription | TEXT NULL | Расшифровка голосового |
| created_at | TIMESTAMPTZ DEFAULT now() | |

---

## Фаза 2 — Группы + Аналитика

### groups
| Поле | Тип | Описание |
|------|-----|----------|
| group_id | BIGINT PK | Telegram chat_id |
| title | TEXT | Название группы |
| type | TEXT | 'group', 'supergroup' |
| language | TEXT DEFAULT 'ru' | Язык группы |
| status_model_id | INT FK → status_models NULL | Workflow группы |
| created_at | TIMESTAMPTZ DEFAULT now() | |

### group_members
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| group_id | BIGINT FK → groups | |
| user_id | UUID FK → users | |
| role | TEXT DEFAULT 'member' | 'admin', 'member' |
| joined_at | TIMESTAMPTZ DEFAULT now() | |

**Constraint**: UNIQUE (group_id, user_id)

### audit_log
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users | Кто сделал |
| group_id | BIGINT FK → groups NULL | В какой группе |
| action | TEXT | 'created', 'updated', 'deleted' |
| entity_type | TEXT | 'event', 'reminder', 'note' |
| entity_id | INT | ID сущности |
| changes | JSONB | {"old": {...}, "new": {...}} |
| created_at | TIMESTAMPTZ DEFAULT now() | |

### subscriptions
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users | |
| group_id | BIGINT FK → groups NULL | |
| type | TEXT | 'morning_brief', 'evening_brief', 'weekly_review' |
| send_at | TEXT | 'HH:MM' в timezone пользователя |
| is_active | BOOLEAN DEFAULT TRUE | |
| config | JSONB | {"weather": true, "currency": ["USD/RUB"], ...} |
| created_at | TIMESTAMPTZ DEFAULT now() | |

---

## Фаза 3 — Второй мозг

### notes
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | UUID FK → users ON DELETE CASCADE | Создатель |
| group_id | BIGINT FK → groups NULL | NULL = личная |
| category_id | INT FK → categories NULL | |
| title | TEXT | |
| content | TEXT | |
| tags | JSONB | ["идея", "проект_X"] |
| source | TEXT | 'chat', 'manual', 'import', 'voice' |
| created_at | TIMESTAMPTZ DEFAULT now() | |
| updated_at | TIMESTAMPTZ DEFAULT now() | |

### entity_links
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| source_type | TEXT | 'event', 'note', 'reminder' |
| source_id | INT | |
| target_type | TEXT | |
| target_id | INT | |
| link_type | TEXT | 'related', 'blocks', 'depends_on' |
| created_at | TIMESTAMPTZ DEFAULT now() | |

---

## Дефолтные данные при регистрации

### Категории (is_system = true)
| Ключ | RU | EN | Иконка | Цвет |
|------|----|----|--------|------|
| personal | Личное | Personal | 🏠 | #5DCAA5 (teal) |
| work | Работа | Work | 💼 | #85B7EB (blue) |
| shopping | Покупки | Shopping | 🛒 | #F0997B (coral) |
| travel | Путешествия | Travel | ✈️ | #AFA9EC (purple) |
| health | Здоровье | Health | 💪 | #97C459 (green) |
| education | Учёба | Education | 📚 | #FAC775 (amber) |

### Статусная модель "Простой" (для личного чата, is_system = true)
| Статус | RU | EN | position | is_terminal | color |
|--------|----|----|----------|-------------|-------|
| todo | К выполнению | To Do | 1 | false | #B4B2A9 (gray) |
| done | Готово | Done | 2 | true | #1D9E75 (dark teal) |

### Статусная модель "Kanban" (для групп, is_system = true)
| Статус | RU | EN | position | is_terminal | color |
|--------|----|----|----------|-------------|-------|
| todo | К выполнению | To Do | 1 | false | #B4B2A9 (gray) |
| in_progress | В работе | In Progress | 2 | false | #378ADD (strong blue) |
| on_review | На проверке | On Review | 3 | false | #EF9F27 (strong amber) |
| done | Готово | Done | 4 | true | #1D9E75 (dark teal) |

### Логика цветов
- **Категории**: палитра 200 (светлые оттенки) — «что это»
- **Статусы**: прогрессия gray → blue → amber → green (насыщенные 400-600) — «на каком этапе»
- Пересечений нет

---

## Потоки авторизации

### Telegram (MVP)
```
/start → ищем auth_methods(provider='telegram', provider_user_id=tg_id)
  → Найден → возвращаем users.id
  → Не найден → INSERT users + INSERT auth_methods → онбординг
```

### Email + пароль (PWA, deferred)
```
Регистрация → INSERT users(email, password_hash) + INSERT auth_methods(provider='email')
Вход → SELECT users WHERE email = ? → проверяем bcrypt
```

### Связывание аккаунтов
```
Пользователь в Telegram: /link email → вводит email+пароль
  → Если email уже есть в users → добавляем auth_methods(provider='telegram') к этому user_id
  → Если нет → добавляем email + password_hash к текущему users, создаём auth_methods(provider='email')
```

### Подключение календаря
```
/auth (или /connect google) → OAuth flow → callback с токенами
  → INSERT calendar_connections(provider='google', tokens, is_primary=TRUE если первый)
  → sync_token = NULL (полная sync при первом show_events)
  → "Google Календарь подключён ✅"

/connect yandex → OAuth flow → ...
  → INSERT calendar_connections(provider='yandex', is_primary=FALSE)
  → "Яндекс Календарь подключён. Основной: Google. Переключить? /calendar switch yandex"
```

---

## Синхронизация (events mirror)

### Архитектура
- **Ленивая sync**: при каждом `show_events` / `delete_event`
- **Инкрементальная**: через `syncToken` (Google Calendar API)
- **Fallback**: при 410 Gone → сброс syncToken → полный запрос → новый syncToken
- **Направление**: Google → БД (внешние изменения) + Бот → Google + БД (через бота)

### Потоки данных
```
create_event (через бота):
  → Google Calendar API → получить google_event_id
  → upsert в events (external_event_id = google_event_id)

delete_event (через бота):
  → Google Calendar API → удалить
  → events: is_deleted=TRUE, deleted_at=now()

show_events:
  → sync_calendar() [ленивая sync]
    → если syncToken есть → инкрементальный запрос (только изменения)
    → если нет → полный запрос (30 дней назад → 90 дней вперёд)
    → upsert новые/изменённые, soft-delete удалённые
  → читаем из events (БД)

Мягкое удаление:
  → 30 дней хранения → cleanup_deleted_events() физически удаляет
```

---

## Грамматические формы (grammar_form)
| Значение | Описание | Пример |
|----------|----------|--------|
| n | Нейтральный (по умолчанию) | "Ничего не запланировано!" |
| f | Женский | "Свободна как ветер!" |
| m | Мужской | "Свободен как ветер!" |

При онбординге: после выбора timezone — кнопки "👩 Она", "👨 Он", "✨ Нейтрально".

---

## Безопасность
- Токены календарей → TODO: AES-256-Fernet, ENCRYPTION_KEY в env (сейчас plain JSON)
- Пароли → bcrypt с cost factor 12 (для PWA)
- Rate limiting: 50 AI-запросов/час на пользователя
- Валидация AI-ответов: whitelist intents, проверка форматов
- /deletedata, /logout — полное удаление данных (GDPR) + отзыв OAuth-токенов у провайдеров
- /disconnect — удаление calendar_connections (аккаунт остаётся)
- Input sanitization: asyncpg параметризованные запросы
- JWT для PWA-сессий (RS256, срок жизни 7 дней, refresh token 30 дней) — deferred
- region (RU/EU/OTHER) — подготовка к 152-ФЗ (deferred для MVP)

## Локализация
- Подход B: is_system сущности хранят ключи, переводятся при отображении
- Файлы i18n/ru.json, i18n/en.json
- Кастомные категории/статусы — на языке создателя
- grammar_form влияет на строки с грамматическим родом

## ENV переменные

### Текущие (в Koyeb)
- TELEGRAM_TOKEN — токен бота
- DATABASE_URL — Supabase PostgreSQL connection string
- TOGETHER_API_KEY — Together AI (Llama-3.3-70B)
- GOOGLE_CREDENTIALS — OAuth credentials JSON
- WEBHOOK_URL — публичный URL на Koyeb

### Запланированные
- ENCRYPTION_KEY — для Fernet шифрования токенов
- JWT_SECRET — для PWA-сессий
- WEATHER_API_KEY — для погоды в дайджестах
- CURRENCY_API_KEY — для курсов валют
- YANDEX_CLIENT_ID / YANDEX_CLIENT_SECRET — Яндекс OAuth

## Стек
- **Python** + python-telegram-bot + Starlette + uvicorn
- **asyncpg** → Supabase (PostgreSQL, Frankfurt) с ssl="require"
- **Together AI** (Llama-3.3-70B)
- **Google Calendar API** (Web Application OAuth)
- **Koyeb** (Frankfurt, Free tier, webhook mode)