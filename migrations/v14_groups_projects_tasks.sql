-- Migration v14: Groups, Projects, Tasks
-- Apply in Supabase SQL Editor

-- Группы (Telegram-чаты)
CREATE TABLE IF NOT EXISTS groups (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_chat_id BIGINT UNIQUE NOT NULL,
    title           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Участники группы (связь группа ↔ пользователь)
CREATE TABLE IF NOT EXISTS group_members (
    group_id    UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    joined_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);

-- Проекты (папки задач внутри группы)
CREATE TABLE IF NOT EXISTS projects (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id   UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (group_id, name)
);

-- Задачи проекта
CREATE TABLE IF NOT EXISTS tasks (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id               UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title                    TEXT NOT NULL,
    deadline                 DATE,
    assignee_user_id         UUID REFERENCES users(id),
    status                   TEXT NOT NULL DEFAULT 'open',  -- open | done
    reminder_day_before_sent BOOLEAN NOT NULL DEFAULT false,
    reminder_day_of_sent     BOOLEAN NOT NULL DEFAULT false,
    created_at               TIMESTAMPTZ DEFAULT now()
);

-- Индексы для reminder worker
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_tasks_project  ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_group_members_telegram ON group_members(telegram_id);
