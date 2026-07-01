-- NPC comments. Anyone can READ; only Ko-fi supporters can POST (enforced in the
-- /api/npc-comment route via app_metadata.is_supporter, written with the service role).

CREATE TABLE IF NOT EXISTS npc_comments (
  id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  npc_name    text        NOT NULL,
  user_id     uuid        NOT NULL,
  author_name text,
  body        text        NOT NULL,
  created_at  timestamptz DEFAULT now()
);

ALTER TABLE npc_comments ENABLE ROW LEVEL SECURITY;

-- Public read; writes go through the service-role route (supporter-gated).
DROP POLICY IF EXISTS "public_read_npc_comments" ON npc_comments;
CREATE POLICY "public_read_npc_comments" ON npc_comments FOR SELECT TO public USING (true);

CREATE INDEX IF NOT EXISTS idx_npc_comments_npc ON npc_comments (npc_name, created_at DESC);
