-- Per-tier (leveled) item stats. EQL items level by combining equal tiers
-- (1+1→2 … 9+9→10). Base stats live in wiki_items.stats (tier 0/1); this table
-- holds the stat values at each higher tier. Populated by the private owner dev
-- tool from EQL captures (July 1). Public read; website tier selector reads it.

CREATE TABLE IF NOT EXISTS wiki_item_tiers (
  item_name text  NOT NULL,
  tier      int   NOT NULL,                -- 1..10
  stats     jsonb NOT NULL DEFAULT '{}'::jsonb,
  source    text,                          -- 'capture' | 'derived' | 'manual'
  PRIMARY KEY (item_name, tier)
);

ALTER TABLE wiki_item_tiers ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "public_read_wiki_item_tiers" ON wiki_item_tiers;
CREATE POLICY "public_read_wiki_item_tiers" ON wiki_item_tiers FOR SELECT TO public USING (true);

CREATE INDEX IF NOT EXISTS idx_wiki_item_tiers_item ON wiki_item_tiers (item_name);
