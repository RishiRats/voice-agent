CREATE TABLE IF NOT EXISTS catalog_items (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Core fields
    name            TEXT NOT NULL,
    description     TEXT,
    category        TEXT NOT NULL DEFAULT 'General',

    -- Pricing in paise (₹1 = 100 paise). NULL = price on request.
    -- price_min = price_max = fixed price.
    -- price_min < price_max = range ("₹4,000 to ₹8,000").
    price_min_paise INTEGER CHECK (price_min_paise >= 0),
    price_max_paise INTEGER CHECK (price_max_paise >= 0),

    -- Appointment duration — used by check_availability for slot blocking
    duration_mins   INTEGER NOT NULL DEFAULT 30 CHECK (duration_mins > 0),

    -- Visibility
    available       BOOLEAN NOT NULL DEFAULT true,
    display_order   INTEGER NOT NULL DEFAULT 0,

    -- Audit
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT name_not_empty CHECK (char_length(trim(name)) > 0),
    CONSTRAINT price_range_valid CHECK (
        price_max_paise IS NULL OR
        price_min_paise IS NULL OR
        price_max_paise >= price_min_paise
    )
);

CREATE INDEX IF NOT EXISTS idx_catalog_tenant_available
  ON catalog_items (tenant_id, available, category, display_order);

CREATE OR REPLACE FUNCTION touch_catalog_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_catalog_updated_at ON catalog_items;
CREATE TRIGGER trg_catalog_updated_at
  BEFORE UPDATE ON catalog_items
  FOR EACH ROW EXECUTE FUNCTION touch_catalog_updated_at();
