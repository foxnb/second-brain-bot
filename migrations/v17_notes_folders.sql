-- Migration v17: folders for notes and lists
-- Add folder column to notes
ALTER TABLE notes ADD COLUMN IF NOT EXISTS folder TEXT;
CREATE INDEX IF NOT EXISTS idx_notes_user_folder ON notes(user_id, folder) WHERE folder IS NOT NULL;

-- Add folder column to lists
ALTER TABLE lists ADD COLUMN IF NOT EXISTS folder TEXT;
CREATE INDEX IF NOT EXISTS idx_lists_user_folder ON lists(user_id, folder) WHERE folder IS NOT NULL;
