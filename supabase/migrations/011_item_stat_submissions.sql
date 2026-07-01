-- Community screenshot consensus. Players upload an item screenshot → /api/ocr
-- extracts stats → a PENDING submission lands here. When N distinct users submit
-- MATCHING stats (same stats_hash) for an item, the stats auto-publish to
-- wiki_items (source='community-consensus'). One bad OCR never publishes alone.

CREATE TABLE IF NOT EXISTS item_stat_submissions (
  id         uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  item_name  text        NOT NULL,
  user_id    uuid        NOT NULL,
  stats      jsonb       NOT NULL,
  stats_hash text        NOT NULL,           -- hash of the normalized stat set
  created_at timestamptz DEFAULT now(),
  UNIQUE (item_name, user_id)                -- one vote per user per item (re-submit overwrites)
);

ALTER TABLE item_stat_submissions ENABLE ROW LEVEL SECURITY;
-- Public read (so the UI can show "2 of 2 confirmations"); writes via service-role route.
DROP POLICY IF EXISTS "public_read_item_stat_submissions" ON item_stat_submissions;
CREATE POLICY "public_read_item_stat_submissions" ON item_stat_submissions FOR SELECT TO public USING (true);

CREATE INDEX IF NOT EXISTS idx_iss_item_hash ON item_stat_submissions (item_name, stats_hash);
