-- Migration v10: add task_destination preference to users
-- Run in Supabase SQL editor

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS task_destination TEXT
    CHECK (task_destination IN ('calendar', 'list'));
