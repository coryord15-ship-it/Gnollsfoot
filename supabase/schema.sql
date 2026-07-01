-- Gnoll Guard — Supabase Community Database Schema
-- Run this once in your Supabase project:
--   Dashboard → SQL Editor → New Query → paste → Run

-- Community-verified item table.
-- Each row is one unique (item_name, item_level) pair. item_level 0 = base item,
-- 1-5 = enhancement tiers (+1 … +5). submission_count tracks independent confirmations.
CREATE TABLE IF NOT EXISTS community_items (
    id                  uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    item_name           text        NOT NULL,
    item_level          integer     NOT NULL DEFAULT 0,
    description         text,
    drop_mob            text,
    drop_zone           text,
    drop_time_of_day    text        DEFAULT 'unknown',
    quest_linked        boolean     DEFAULT false,
    quest_npc           text,
    quest_reward        text,
    source_url          text,
    submission_count    integer     DEFAULT 1,
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now(),

    CONSTRAINT community_items_name_level_unique UNIQUE (item_name, item_level)
);

-- Image URLs stored as a text array so items can have multiple screenshots.
-- Added separately so existing installs can run ALTER TABLE safely.
ALTER TABLE community_items ADD COLUMN IF NOT EXISTS images text[] DEFAULT '{}';

-- ── item_level migration (run once on existing databases) ─────────────────────
-- Adds item_level column and migrates from the old single-column unique constraint
-- to the composite (item_name, item_level) key. Safe to re-run.
ALTER TABLE community_items ADD COLUMN IF NOT EXISTS item_level integer NOT NULL DEFAULT 0;
UPDATE community_items SET item_level = 0 WHERE item_level IS NULL;
ALTER TABLE community_items DROP CONSTRAINT IF EXISTS community_items_name_unique;
DO $$
BEGIN
  ALTER TABLE community_items ADD CONSTRAINT community_items_name_level_unique
    UNIQUE (item_name, item_level);
  EXCEPTION WHEN duplicate_object THEN NULL;
END;
$$;

-- ── Supabase Storage: item screenshots ────────────────────────────────────────
-- Run this ONCE in Supabase Dashboard → Storage → New Bucket:
--   Name: item-images
--   Public bucket: YES  (so image URLs work without auth)
--   File size limit: 10 MB
--   Allowed MIME types: image/jpeg, image/png, image/webp, image/gif
--
-- Then run these policies in SQL Editor:

-- Anyone can read/download images (bucket is public, but belt-and-suspenders)
DROP POLICY IF EXISTS "storage_anon_read" ON storage.objects;
CREATE POLICY "storage_anon_read"
    ON storage.objects FOR SELECT
    TO anon
    USING (bucket_id = 'item-images');

-- Anon can upload images; enforce path format (item_slug/timestamp.ext only)
DROP POLICY IF EXISTS "storage_anon_insert" ON storage.objects;
CREATE POLICY "storage_anon_insert"
    ON storage.objects FOR INSERT
    TO anon
    WITH CHECK (
        bucket_id = 'item-images'
        AND octet_length(name) < 200
        -- Path must look like: word_chars/digits.ext  (no path traversal)
        AND name ~ '^[a-z0-9_\-]+/[0-9]+\.(jpg|jpeg|png|webp|gif)$'
    );

-- No anon deletes or updates on storage objects
DROP POLICY IF EXISTS "storage_anon_delete" ON storage.objects;
DROP POLICY IF EXISTS "storage_anon_update" ON storage.objects;

-- Auto-update updated_at on every write
CREATE OR REPLACE FUNCTION _set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS community_items_updated_at ON community_items;
CREATE TRIGGER community_items_updated_at
    BEFORE UPDATE ON community_items
    FOR EACH ROW EXECUTE FUNCTION _set_updated_at();

-- ── Field-length constraints (last line of defence regardless of who writes) ──
DO $$
BEGIN
  BEGIN ALTER TABLE community_items ADD CONSTRAINT chk_item_name_len    CHECK (char_length(item_name)    <= 200);  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE community_items ADD CONSTRAINT chk_description_len  CHECK (char_length(description)  <= 2000); EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE community_items ADD CONSTRAINT chk_drop_mob_len     CHECK (char_length(drop_mob)     <= 200);  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE community_items ADD CONSTRAINT chk_drop_zone_len    CHECK (char_length(drop_zone)    <= 200);  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE community_items ADD CONSTRAINT chk_quest_npc_len    CHECK (char_length(quest_npc)    <= 200);  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE community_items ADD CONSTRAINT chk_quest_reward_len CHECK (char_length(quest_reward) <= 200);  EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE community_items ADD CONSTRAINT chk_submission_cap   CHECK (submission_count <= 500);           EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE community_items ADD CONSTRAINT chk_item_level_range CHECK (item_level BETWEEN 0 AND 10);        EXCEPTION WHEN duplicate_object THEN NULL; END;
END;
$$;

-- Prevent item_name from ever being changed once written
CREATE OR REPLACE FUNCTION _prevent_item_name_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.item_name IS DISTINCT FROM OLD.item_name THEN
        RAISE EXCEPTION 'item_name is immutable after creation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS no_item_name_change ON community_items;
CREATE TRIGGER no_item_name_change
    BEFORE UPDATE ON community_items
    FOR EACH ROW EXECUTE FUNCTION _prevent_item_name_change();

-- ── Row-level security ────────────────────────────────────────────────────────
-- Anon users: READ only.
-- All writes (INSERT / UPDATE) go through our /api/submit server route which
-- uses the service_role key and handles validation + rate limiting.
-- This means even if someone finds the anon key they cannot write to the DB
-- directly — they hit a permission denied from RLS.
ALTER TABLE community_items ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_read"   ON community_items;
DROP POLICY IF EXISTS "anon_insert" ON community_items;
DROP POLICY IF EXISTS "anon_update" ON community_items;

-- Anon users: READ ONLY.
-- All writes (INSERT / UPDATE) go exclusively through /api/submit which uses
-- the service_role key — the anon key cannot write to this table at all.
CREATE POLICY "anon_read" ON community_items FOR SELECT TO anon USING (true);

-- ── JSONL export view ──────────────────────────────────────────────────────
-- Use this to download training data:
--   SELECT jsonl FROM community_jsonl_export;
-- Or: Dashboard → Table Editor → community_jsonl_export → Export CSV,
-- then strip the outer quotes.
CREATE OR REPLACE VIEW community_jsonl_export AS
SELECT
    json_build_object(
        'instruction', 'What is the ' || item_name || ' in EverQuest Legends?',
        'response', TRIM(
            COALESCE(description, '') ||
            CASE WHEN drop_mob IS NOT NULL AND drop_zone IS NOT NULL
                 THEN ' It drops from ' || drop_mob || ' in ' || drop_zone || '.'
                 WHEN drop_mob IS NOT NULL
                 THEN ' It drops from ' || drop_mob || '.'
                 ELSE '' END ||
            CASE WHEN drop_time_of_day NOT IN ('unknown', '') AND drop_time_of_day IS NOT NULL
                 THEN ' Only drops during ' || drop_time_of_day || 'time.'
                 ELSE '' END ||
            CASE WHEN quest_linked AND quest_npc IS NOT NULL AND quest_reward IS NOT NULL
                 THEN ' Quest item for ' || quest_npc || '. Reward: ' || quest_reward || '.'
                 ELSE '' END
        )
    )::text AS jsonl
FROM community_items
WHERE description IS NOT NULL
ORDER BY item_name;
