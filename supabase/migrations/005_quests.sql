-- Run this in your Supabase SQL Editor (Project → SQL Editor → New query)
-- Adds quests and quest_steps tables for quest chain tracking.

-- 1. Quests table
CREATE TABLE IF NOT EXISTS quests (
  id              uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  quest_name      text        NOT NULL UNIQUE,
  quest_giver_npc text,
  zone            text,
  reward_item     text,
  reward_copper   integer     DEFAULT 0,
  description     text,
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now()
);

ALTER TABLE quests ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_read_quests" ON quests;
CREATE POLICY "anon_read_quests"
  ON quests FOR SELECT TO anon USING (true);

-- 2. Quest steps table (ordered steps in a chain)
CREATE TABLE IF NOT EXISTS quest_steps (
  id              uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  quest_id        uuid        NOT NULL REFERENCES quests(id) ON DELETE CASCADE,
  step_order      integer     NOT NULL,
  instruction     text        NOT NULL,
  required_item   text,
  deliver_to_npc  text,
  created_at      timestamptz DEFAULT now()
);

ALTER TABLE quest_steps ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_read_quest_steps" ON quest_steps;
CREATE POLICY "anon_read_quest_steps"
  ON quest_steps FOR SELECT TO anon USING (true);

-- 3. Indexes
CREATE INDEX IF NOT EXISTS idx_quests_name ON quests (quest_name);
CREATE INDEX IF NOT EXISTS idx_quests_zone ON quests (zone);
CREATE INDEX IF NOT EXISTS idx_quest_steps_quest ON quest_steps (quest_id, step_order);

-- 4. Auto-update updated_at trigger
CREATE OR REPLACE FUNCTION update_quests_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS quests_updated_at ON quests;
CREATE TRIGGER quests_updated_at
  BEFORE UPDATE ON quests
  FOR EACH ROW EXECUTE FUNCTION update_quests_updated_at();
