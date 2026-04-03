-- Migration v11: статусная модель для элементов списков
-- Запустить в Supabase SQL Editor

-- Добавляем статус к элементам списка
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'todo';

-- Добавляем кастомные статусы в настройки списка (если settings ещё нет колонки — она уже есть как JSONB)
-- Пример settings: {"statuses": ["нужно сделать", "в работе", "сделано"]}
-- Индекс для быстрого поиска по статусу
CREATE INDEX IF NOT EXISTS idx_list_items_status ON list_items(list_id, status) WHERE is_deleted = FALSE;
