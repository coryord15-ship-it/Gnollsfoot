-- The EQ item_id (from /outputfile inventory) for catalog items. Lets the
-- inventory sync create unknown items in wiki_items carrying their game ID.
ALTER TABLE wiki_items ADD COLUMN IF NOT EXISTS item_id integer;
CREATE INDEX IF NOT EXISTS idx_wiki_items_item_id ON wiki_items (item_id);
