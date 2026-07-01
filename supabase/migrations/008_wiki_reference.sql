-- Canonical EQL reference data harvested from eqlwiki.com (items, NPCs, loot,
-- merchant inventories). Public read-only; writes via service-role import
-- (tools/import_eqlwiki.py). Powers item/zone/NPC pages + the EQLootWindow.

-- ── Items ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wiki_items (
  name           text PRIMARY KEY,
  slot           text,
  flags          text,                       -- "MAGIC LORE NO DROP" etc.
  classes        text,
  races          text,
  weight         numeric,
  size           text,
  effect         text,                        -- clicky/proc effect line
  icon_id        integer,                     -- lucy_img_ID (EQ item-icon number)
  era            text,
  stats          jsonb DEFAULT '{}'::jsonb,   -- {ac,hp,mana,endurance,str,sta,agi,dex,wis,int,cha,damage,delay}
  recipes        text,
  raw_statsblock text,                        -- original wiki statsblock (fallback)
  url            text,
  updated_at     timestamptz DEFAULT now()
);
ALTER TABLE wiki_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "public_read_wiki_items" ON wiki_items;
CREATE POLICY "public_read_wiki_items" ON wiki_items FOR SELECT TO public USING (true);
CREATE INDEX IF NOT EXISTS idx_wiki_items_slot    ON wiki_items (slot);

-- ── NPCs / mobs ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wiki_npcs (
  name        text PRIMARY KEY,
  zone        text,
  level       text,
  race        text,
  class       text,
  description text,
  is_merchant boolean DEFAULT false,
  url         text
);
ALTER TABLE wiki_npcs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "public_read_wiki_npcs" ON wiki_npcs;
CREATE POLICY "public_read_wiki_npcs" ON wiki_npcs FOR SELECT TO public USING (true);
CREATE INDEX IF NOT EXISTS idx_wiki_npcs_zone ON wiki_npcs (zone);

-- ── Loot drops (mob → item) ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wiki_npc_loot (
  npc_name  text NOT NULL,
  item_name text NOT NULL,
  rarity    text,
  PRIMARY KEY (npc_name, item_name)
);
ALTER TABLE wiki_npc_loot ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "public_read_wiki_npc_loot" ON wiki_npc_loot;
CREATE POLICY "public_read_wiki_npc_loot" ON wiki_npc_loot FOR SELECT TO public USING (true);
CREATE INDEX IF NOT EXISTS idx_wiki_loot_npc  ON wiki_npc_loot (npc_name);
CREATE INDEX IF NOT EXISTS idx_wiki_loot_item ON wiki_npc_loot (item_name);  -- "what drops X"

-- ── Merchant inventories (merchant → item) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS wiki_merchant_items (
  npc_name  text NOT NULL,
  item_name text NOT NULL,
  price     text,
  PRIMARY KEY (npc_name, item_name)
);
ALTER TABLE wiki_merchant_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "public_read_wiki_merchant_items" ON wiki_merchant_items;
CREATE POLICY "public_read_wiki_merchant_items" ON wiki_merchant_items FOR SELECT TO public USING (true);
CREATE INDEX IF NOT EXISTS idx_wiki_merch_npc  ON wiki_merchant_items (npc_name);
CREATE INDEX IF NOT EXISTS idx_wiki_merch_item ON wiki_merchant_items (item_name);
