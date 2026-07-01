-- Migration 004 — Vendor Prices
-- Run in Supabase Dashboard → SQL Editor → New query

CREATE TABLE IF NOT EXISTS vendor_prices (
  id               uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  item_name        text        NOT NULL,
  merchant_name    text        NOT NULL,
  -- 'sell' = player sold TO vendor; 'buy' = player bought FROM vendor
  transaction_type text        NOT NULL CHECK (transaction_type IN ('sell', 'buy')),
  price_copper     integer     NOT NULL CHECK (price_copper >= 0),
  price_raw        text,
  quantity         integer     NOT NULL DEFAULT 1 CHECK (quantity > 0),
  created_at       timestamptz DEFAULT now()
);

ALTER TABLE vendor_prices ENABLE ROW LEVEL SECURITY;

-- Anon can read (for min/max price display on item pages)
DROP POLICY IF EXISTS "anon_read_vendor_prices" ON vendor_prices;
CREATE POLICY "anon_read_vendor_prices"
  ON vendor_prices FOR SELECT TO anon USING (true);

-- Indexes for item page lookups
CREATE INDEX IF NOT EXISTS idx_vendor_prices_item ON vendor_prices (item_name);
CREATE INDEX IF NOT EXISTS idx_vendor_prices_merchant ON vendor_prices (merchant_name);
CREATE INDEX IF NOT EXISTS idx_vendor_prices_type ON vendor_prices (transaction_type);

-- Field length constraints
DO $$
BEGIN
  BEGIN ALTER TABLE vendor_prices ADD CONSTRAINT chk_vp_item_len     CHECK (char_length(item_name)     <= 200); EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE vendor_prices ADD CONSTRAINT chk_vp_merchant_len CHECK (char_length(merchant_name) <= 200); EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE vendor_prices ADD CONSTRAINT chk_vp_qty          CHECK (quantity BETWEEN 1 AND 9999);       EXCEPTION WHEN duplicate_object THEN NULL; END;
  BEGIN ALTER TABLE vendor_prices ADD CONSTRAINT chk_vp_copper       CHECK (price_copper <= 10000000);          EXCEPTION WHEN duplicate_object THEN NULL; END;
END;
$$;
