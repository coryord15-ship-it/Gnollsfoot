-- Run this in your Supabase SQL Editor (Project → SQL Editor → New query)
-- Adds the drop_reports and item_votes tables, plus drop columns on community_items.

-- 1. Add drop location columns to community_items (safe to run even if they exist)
ALTER TABLE community_items
  ADD COLUMN IF NOT EXISTS drop_zone text,
  ADD COLUMN IF NOT EXISTS drop_npc  text;

-- 2. Drop reports table (one row per player report, used for the 24h feed)
CREATE TABLE IF NOT EXISTS drop_reports (
  id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  item_name   text        NOT NULL,
  item_level  integer     NOT NULL DEFAULT 0,
  drop_zone   text,
  drop_npc    text,
  reported_at timestamptz DEFAULT now()
);

ALTER TABLE drop_reports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_read_drop_reports" ON drop_reports;
CREATE POLICY "anon_read_drop_reports"
  ON drop_reports FOR SELECT TO anon USING (true);

-- Writes go through /api/drop-report (service_role key) — no anon insert needed.

-- 3. Item votes table
CREATE TABLE IF NOT EXISTS item_votes (
  id         uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  item_name  text        NOT NULL,
  item_level integer     NOT NULL DEFAULT 0,
  vote_type  text        NOT NULL CHECK (vote_type IN ('keep', 'sell', 'tribute')),
  reason     text        NOT NULL,
  voted_at   timestamptz DEFAULT now()
);

ALTER TABLE item_votes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_read_item_votes" ON item_votes;
CREATE POLICY "anon_read_item_votes"
  ON item_votes FOR SELECT TO anon USING (true);

-- Writes go through /api/vote (service_role key) — no anon insert needed.

-- 4. Optional: index for fast feed queries
CREATE INDEX IF NOT EXISTS idx_drop_reports_recent
  ON drop_reports (reported_at DESC);

CREATE INDEX IF NOT EXISTS idx_item_votes_item
  ON item_votes (item_name, item_level);
