-- Run this in your Supabase SQL Editor (Project → SQL Editor → New query)
--
-- Fix: the desktop Quest Journal was always empty even when the quest was in
-- the user's journal. Root cause was an RLS role mismatch from migration 005:
--   * quests / quest_steps read policies were scoped TO anon ONLY.
--   * user_quests owner policy requires auth.uid() = user_id, which only the
--     AUTHENTICATED role can satisfy.
-- So the app had to query as the authenticated user (to see user_quests), but
-- then could NOT read quests/quest_steps (anon-only) — no single role worked.
-- (The website was unaffected: its API routes use the service_role key.)
--
-- Broaden the public quest data to the `public` role (covers anon AND
-- authenticated). Quest data is non-sensitive and meant to be world-readable.

ALTER TABLE quests ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_read_quests" ON quests;
DROP POLICY IF EXISTS "public_read_quests" ON quests;
CREATE POLICY "public_read_quests"
  ON quests FOR SELECT TO public USING (true);

ALTER TABLE quest_steps ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_read_quest_steps" ON quest_steps;
DROP POLICY IF EXISTS "public_read_quest_steps" ON quest_steps;
CREATE POLICY "public_read_quest_steps"
  ON quest_steps FOR SELECT TO public USING (true);
