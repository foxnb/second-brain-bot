-- Migration v12: grammar_form для грамматического рода ответов
-- Запустить в Supabase SQL Editor

ALTER TABLE users ADD COLUMN IF NOT EXISTS grammar_form TEXT DEFAULT 'n';
-- Значения: 'm' (мужской), 'f' (женский), 'n' (нейтральный, по умолчанию)
