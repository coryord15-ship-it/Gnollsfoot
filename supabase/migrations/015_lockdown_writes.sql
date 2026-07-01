-- 015_lockdown_writes.sql
-- SECURITY HARDENING (applied live 2026-07-01)
--
-- The live DB had two leftover wide-open policies on community_items that let
-- ANYONE holding the public anon key insert/update rows directly, bypassing the
-- authenticated /api/submit route (auth + validation + rate limiting). All real
-- writes use the service_role key, which bypasses RLS, so removing these closes
-- the hole without breaking any upload path.
DROP POLICY IF EXISTS "Public insert" ON public.community_items;
DROP POLICY IF EXISTS "Public update" ON public.community_items;

-- community_items is now anon READ-ONLY (policy "anon_read"); writes = service_role only.

-- Re-tighten anon image uploads to the documented shape (the live policy had lost
-- its path-format + size checks). Bucket-scoped, name < 200 chars, path must be
-- slug/timestamp.ext. The web uploader already produces conforming paths.
DROP POLICY IF EXISTS "storage_anon_insert" ON storage.objects;
CREATE POLICY "storage_anon_insert"
    ON storage.objects FOR INSERT
    TO anon
    WITH CHECK (
        bucket_id = 'item-images'
        AND octet_length(name) < 200
        AND name ~ '^[a-z0-9_\-]+/[0-9]+\.(jpg|jpeg|png|webp|gif)$'
    );
