-- Sort items by any numeric stat in the jsonb, best-first (true numeric order).
-- Respects optional slot/class filters. SECURITY INVOKER → honors wiki_items RLS.
CREATE OR REPLACE FUNCTION items_by_stat(
  stat_key text,
  slot_filter text DEFAULT NULL,
  class_filter text DEFAULT NULL,
  lim int DEFAULT 200
)
RETURNS TABLE(name text, slot text, classes text, stats jsonb)
LANGUAGE sql STABLE AS $$
  SELECT w.name, w.slot, w.classes, w.stats
  FROM wiki_items w
  WHERE w.stats ? stat_key
    AND (w.stats->>stat_key) ~ '^-?\d+(\.\d+)?$'
    AND (slot_filter IS NULL OR w.slot ILIKE '%'||slot_filter||'%')
    AND (class_filter IS NULL OR w.classes ILIKE '%'||class_filter||'%' OR w.classes ILIKE '%ALL%')
  ORDER BY (w.stats->>stat_key)::numeric DESC
  LIMIT lim;
$$;
GRANT EXECUTE ON FUNCTION items_by_stat(text, text, text, int) TO anon, authenticated;
