-- Migration v16: Notes table
-- Apply in Supabase SQL Editor

CREATE TABLE IF NOT EXISTS notes (
    id                   SERIAL PRIMARY KEY,
    user_id              UUID REFERENCES users(id) ON DELETE CASCADE NOT NULL,
    title                TEXT NOT NULL,
    content              TEXT,
    url                  TEXT,
    attachment_file_id   TEXT,
    attachment_file_type TEXT,        -- 'photo', 'document'
    tags                 TEXT[] DEFAULT '{}',
    is_deleted           BOOLEAN DEFAULT FALSE,
    deleted_at           TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT now(),
    updated_at           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notes_user_active ON notes(user_id) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_notes_tags ON notes USING GIN(tags) WHERE is_deleted = FALSE;

-- Trigram-индекс для полнотекстового поиска по title и content (find_notes_by_query)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_notes_title_trgm ON notes USING GIN(LOWER(title) gin_trgm_ops) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_notes_content_trgm ON notes USING GIN(LOWER(content) gin_trgm_ops) WHERE is_deleted = FALSE;
