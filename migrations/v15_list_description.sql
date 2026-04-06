-- Migration v15: List description
-- Apply in Supabase SQL Editor

ALTER TABLE lists ADD COLUMN IF NOT EXISTS description TEXT;
