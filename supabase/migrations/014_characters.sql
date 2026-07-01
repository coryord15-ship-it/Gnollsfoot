-- Magelo-style character profiles. Gear comes from /outputfile inventory sync;
-- class/level/race/deity/alias are profile fields the player sets. Public read
-- (profiles are shareable); writes via the service-role /api/character route.
CREATE TABLE IF NOT EXISTS characters (
  id             uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id        uuid        NOT NULL,
  character_name text        NOT NULL,
  alias          text,
  class          text,
  level          integer,
  race           text,
  deity          text,
  is_public      boolean     DEFAULT true,
  synced_at      timestamptz DEFAULT now(),
  created_at     timestamptz DEFAULT now(),
  UNIQUE (user_id, character_name)
);
ALTER TABLE characters ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "public_read_characters" ON characters;
CREATE POLICY "public_read_characters" ON characters FOR SELECT TO public USING (true);
CREATE INDEX IF NOT EXISTS idx_characters_alias ON characters (alias);

CREATE TABLE IF NOT EXISTS character_items (
  character_id uuid    NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
  slot         text    NOT NULL,
  item_name    text    NOT NULL,
  item_id      integer,
  PRIMARY KEY (character_id, slot)
);
ALTER TABLE character_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "public_read_character_items" ON character_items;
CREATE POLICY "public_read_character_items" ON character_items FOR SELECT TO public USING (true);
