-- Payment fields on tenants
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS payment_enabled       BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS payment_amount_paise  INTEGER,
  ADD COLUMN IF NOT EXISTS payment_expiry_hours  INTEGER NOT NULL DEFAULT 24,
  ADD COLUMN IF NOT EXISTS razorpay_key_id       TEXT,
  ADD COLUMN IF NOT EXISTS razorpay_key_secret   TEXT;

-- Payment fields on appointments
ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS payment_status        TEXT NOT NULL DEFAULT 'not_required',
  ADD COLUMN IF NOT EXISTS payment_link_url      TEXT,
  ADD COLUMN IF NOT EXISTS payment_expires_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS payment_completed_at  TIMESTAMPTZ;

-- Trusted callers per tenant
CREATE TABLE IF NOT EXISTS trusted_callers (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    phone       TEXT NOT NULL,
    name        TEXT,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone)
);

CREATE INDEX IF NOT EXISTS idx_trusted_callers_lookup
  ON trusted_callers (tenant_id, phone);

-- Handoff requests (human intervention needed)
CREATE TABLE IF NOT EXISTS handoff_requests (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    call_id         TEXT,
    appointment_id  BIGINT REFERENCES appointments(id),
    reason          TEXT NOT NULL,
    urgency         TEXT NOT NULL DEFAULT 'normal',
    caller_phone    TEXT,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
