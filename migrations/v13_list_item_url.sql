-- v13: URL field for list items
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS url TEXT;
