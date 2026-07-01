-- Run this in your Supabase SQL Editor (Project → SQL Editor → New query)
-- Tradeskill recipe database powering the website's /tradeskills section.

CREATE TABLE IF NOT EXISTS tradeskill_recipes (
  id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  name        text        NOT NULL,
  tradeskill  text        NOT NULL,
  trivial     integer,
  yield       integer     DEFAULT 1,
  container   text,
  components  jsonb       DEFAULT '[]'::jsonb,   -- [{ "name": "...", "count": 1 }]
  result_item text,
  notes       text,
  source      text,
  status      text        DEFAULT 'verified',
  created_at  timestamptz DEFAULT now(),
  UNIQUE (name, tradeskill)
);

ALTER TABLE tradeskill_recipes ENABLE ROW LEVEL SECURITY;

-- Public, read-only data (anon + authenticated). Writes go through the
-- service-role key in the scraper / admin path.
DROP POLICY IF EXISTS "public_read_tradeskills" ON tradeskill_recipes;
CREATE POLICY "public_read_tradeskills"
  ON tradeskill_recipes FOR SELECT TO public USING (true);

CREATE INDEX IF NOT EXISTS idx_tradeskills_skill   ON tradeskill_recipes (tradeskill);
CREATE INDEX IF NOT EXISTS idx_tradeskills_name    ON tradeskill_recipes (name);
CREATE INDEX IF NOT EXISTS idx_tradeskills_trivial ON tradeskill_recipes (trivial);
