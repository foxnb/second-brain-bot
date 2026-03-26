# Revory — Схема БД v7 (25.03.2026)

## 14 таблиц, 3 фазы реализации

---

## Фаза 1 — MVP (текущая)

### users (расширение существующей)
| Поле | Тип | Описание |
|------|-----|----------|
| user_id | BIGINT PK | Telegram user ID |
| telegram_username | TEXT | @username |
| display_name | TEXT | Имя для отображения |
| language | TEXT DEFAULT 'ru' | Язык интерфейса (ru, en...) |
| timezone | TEXT | IANA timezone |
| city | TEXT NULL | Город для погоды |
| google_token_encrypted | TEXT | OAuth токен, AES-256-Fernet |
| default_calendar_id | TEXT | Google Calendar ID (default: 'primary') |
| rate_limit_count | INT DEFAULT 0 | Счётчик запросов (сбрасывается раз в час) |
| rate_limit_reset | TIMESTAMPTZ | Когда сбросить счётчик |
| created_at | TIMESTAMPTZ | Дата регистрации |

### messages
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | BIGINT FK → users | Кто написал |
| group_id | BIGINT FK → groups NULL | NULL = личный чат |
| role | TEXT | 'user' или 'assistant' |
| content | TEXT | Текст сообщения |
| parsed | JSONB NULL | Распарсенный AI JSON (intent, title, date...) |
| feedback | TEXT NULL | 'up', 'down', NULL |
| created_at | TIMESTAMPTZ | |

### events
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | BIGINT FK → users | Создатель |
| group_id | BIGINT FK → groups NULL | NULL = личное |
| assigned_to | BIGINT FK → users NULL | Кому назначено |
| status_id | INT FK → statuses | Текущий статус |
| category_id | INT FK → categories NULL | Категория |
| google_event_id | TEXT NULL | ID в Google Calendar |
| title | TEXT | Название |
| description | TEXT | Описание |
| start_time | TIMESTAMPTZ | Начало |
| end_time | TIMESTAMPTZ | Конец |
| timezone | TEXT | Timezone события |
| recurrence_rule | TEXT NULL | RRULE для повторяющихся |
| color_id | INT NULL | Google Calendar цвет (1-11) |
| is_deleted | BOOLEAN DEFAULT FALSE | Мягкое удаление |
| deleted_at | TIMESTAMPTZ NULL | Когда удалено |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

### reminders
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | BIGINT FK → users | Создатель |
| group_id | BIGINT FK → groups NULL | NULL = личное |
| assigned_to | BIGINT FK → users | Кому напомнить |
| event_id | INT FK → events NULL | Привязка к событию |
| title | TEXT | Текст напоминания |
| remind_at | TIMESTAMPTZ | Когда напомнить |
| status | TEXT DEFAULT 'pending' | pending / sent / cancelled |
| repeat_rule | TEXT NULL | Cron-выражение для повторов |
| created_at | TIMESTAMPTZ | |

### status_models
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| owner_user_id | BIGINT FK → users NULL | NULL если модель группы |
| owner_group_id | BIGINT FK → groups NULL | NULL если личная |
| name | TEXT | "Простой", "Kanban", кастомное |
| is_default | BOOLEAN | Модель по умолчанию |
| created_at | TIMESTAMPTZ | |

### statuses
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| model_id | INT FK → status_models | Какой модели принадлежит |
| name | TEXT | "To Do", "In Progress", "Done" |
| position | INT | Порядок сортировки |
| color | TEXT NULL | Hex или название цвета |
| is_system | BOOLEAN DEFAULT FALSE | Системный = переводимый |
| is_terminal | BOOLEAN DEFAULT FALSE | TRUE для Done, Cancelled |

### categories
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| owner_user_id | BIGINT FK → users NULL | NULL если категория группы |
| owner_group_id | BIGINT FK → groups NULL | NULL если личная |
| name | TEXT | "work", "personal" (ключ если is_system) |
| color | TEXT NULL | Hex или цвет |
| icon | TEXT NULL | Эмодзи |
| position | INT | Порядок сортировки |
| is_system | BOOLEAN DEFAULT FALSE | Системная = переводимая |

### attachments
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | BIGINT FK → users | Кто загрузил |
| entity_type | TEXT | 'event', 'note', 'message' |
| entity_id | INT | ID сущности |
| file_type | TEXT | 'photo', 'voice', 'document' |
| telegram_file_id | TEXT | Telegram file ID |
| url | TEXT NULL | URL если есть |
| transcription | TEXT NULL | Расшифровка голосового |
| created_at | TIMESTAMPTZ | |

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
| created_at | TIMESTAMPTZ | |

### group_members
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| group_id | BIGINT FK → groups | |
| user_id | BIGINT FK → users | |
| role | TEXT DEFAULT 'member' | 'admin', 'member' |
| joined_at | TIMESTAMPTZ | |

### audit_log
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | BIGINT FK → users | Кто сделал |
| group_id | BIGINT FK → groups NULL | В какой группе |
| action | TEXT | 'created', 'updated', 'deleted' |
| entity_type | TEXT | 'event', 'reminder', 'note' |
| entity_id | INT | ID сущности |
| changes | JSONB | {"old": {...}, "new": {...}} |
| created_at | TIMESTAMPTZ | |

### subscriptions
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | BIGINT FK → users | |
| group_id | BIGINT FK → groups NULL | |
| type | TEXT | 'morning_brief', 'evening_brief', 'weekly_review' |
| send_at | TEXT | 'HH:MM' в timezone пользователя |
| is_active | BOOLEAN DEFAULT TRUE | |
| config | JSONB | {"weather": true, "currency": ["USD/RUB"], ...} |
| created_at | TIMESTAMPTZ | |

---

## Фаза 3 — Второй мозг

### notes
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| user_id | BIGINT FK → users | Создатель |
| group_id | BIGINT FK → groups NULL | NULL = личная |
| category_id | INT FK → categories NULL | |
| title | TEXT | |
| content | TEXT | |
| tags | JSONB | ["идея", "проект_X"] |
| source | TEXT | 'chat', 'manual', 'import', 'voice' |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

### entity_links
| Поле | Тип | Описание |
|------|-----|----------|
| id | SERIAL PK | |
| source_type | TEXT | 'event', 'note', 'reminder' |
| source_id | INT | |
| target_type | TEXT | |
| target_id | INT | |
| link_type | TEXT | 'related', 'blocks', 'depends_on' |
| created_at | TIMESTAMPTZ | |

---

## Дефолтные данные при регистрации

### Категории (is_system = true)
| Ключ | RU | EN | Иконка | Цвет |
|------|----|----|--------|------|
| personal | Личное | Personal | 🏠 | #5DCAA5 |
| work | Работа | Work | 💼 | #85B7EB |
| shopping | Покупки | Shopping | 🛒 | #F0997B |
| travel | Путешествия | Travel | ✈️ | #AFA9EC |
| health | Здоровье | Health | 💪 | #97C459 |
| education | Учёба | Education | 📚 | #FAC775 |

### Статусная модель "Простой" (для личного чата)
| Статус | position | is_terminal | color |
|--------|----------|-------------|-------|
| To Do | 1 | false | #85B7EB |
| Done | 2 | true | #5DCAA5 |

### Статусная модель "Kanban" (для групп)
| Статус | position | is_terminal | color |
|--------|----------|-------------|-------|
| To Do | 1 | false | #85B7EB |
| In Progress | 2 | false | #FAC775 |
| On Review | 3 | false | #AFA9EC |
| Done | 4 | true | #5DCAA5 |

---

## Безопасность
- google_token → AES-256-Fernet, ENCRYPTION_KEY в env
- Rate limiting: 50 AI-запросов/час на пользователя
- Валидация AI-ответов: whitelist intents, проверка форматов
- /deletedata — полное удаление данных пользователя (GDPR)
- Input sanitization перед SQL

## Локализация
- Подход B: is_system сущности хранят ключи, переводятся при отображении
- Файлы i18n/ru.json, i18n/en.json
- Кастомные категории/статусы — на языке создателя

## ENV переменные (добавить)
- ENCRYPTION_KEY — для шифрования токенов
- WEATHER_API_KEY — для погоды в дайджестах
- CURRENCY_API_KEY — для курсов валют
