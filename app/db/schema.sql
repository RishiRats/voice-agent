-- =============================================================================
-- TENANTS — one row per business using the platform.
-- =============================================================================
-- Everything that makes one AI agent different from another lives in this row.
-- To onboard a new customer, you INSERT a row. No code change needed.
--
-- The `system_prompt` is the most important field: it contains all the business
-- knowledge, personality, rules, and tool-use instructions. It gets sent to
-- Sarvam-30B as the first message of every conversation.

CREATE TABLE IF NOT EXISTS tenants (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,                    -- "Sharma Dental Clinic"
    inbound_did     TEXT UNIQUE,                      -- "+912240001234" — phone number that routes to this tenant. Nullable until they get a number.
    system_prompt   TEXT NOT NULL,                    -- The whole persona + business context. Can be huge (10k+ chars), Sarvam-30B has 64K token context.
    greeting        TEXT NOT NULL,                    -- First sentence the agent speaks when answering.
    voice           TEXT NOT NULL DEFAULT 'anushka',  -- Bulbul speaker. v3 options: shubh, anushka, meera, pavithra, maitreyi, etc.
    default_language TEXT NOT NULL DEFAULT 'hi-IN',   -- Hint for STT/TTS. The agent can still switch mid-call.
    llm_model       TEXT NOT NULL DEFAULT 'sarvam-30b',  -- 'sarvam-30b' for normal, 'sarvam-105b' for complex reasoning
    temperature     REAL NOT NULL DEFAULT 0.5,        -- LLM sampling temp. Lower = more consistent. Phone agents want low temp.
    tools_enabled   JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ["check_availability", "book_appointment", "handoff_to_human"]
    business_hours  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"mon-sat": "10:00-20:00", "sun": "closed"}
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- catch-all: doctor names, prices, address, etc. Used by tool endpoints.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fast lookup by inbound DID — this is the hot path on every incoming call.
CREATE INDEX IF NOT EXISTS idx_tenants_inbound_did ON tenants(inbound_did);


-- =============================================================================
-- CALL LOGS — one row per call, persisted after the call ends.
-- =============================================================================
-- During the call, conversation history lives in Redis for speed.
-- When the call ends (or every N turns as a backup), the full transcript
-- gets written here for analytics, billing, and CRM downstream.

CREATE TABLE IF NOT EXISTS call_logs (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    call_id         TEXT NOT NULL UNIQUE,             -- our internal ID; also used as Redis key
    caller_number   TEXT,                             -- E.164 format, NULL for browser-mic test calls
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    duration_secs   INTEGER,
    transcript      JSONB,                            -- full messages array: [{role, content}, ...]
    summary         TEXT,                             -- LLM-generated 1-line summary, written on hangup
    outcome         TEXT,                             -- 'appointment_booked' | 'lead_captured' | 'transferred' | 'abandoned' | 'unknown'
    tool_calls      JSONB NOT NULL DEFAULT '[]'::jsonb,  -- audit log of every tool the LLM called
    cost_paise      INTEGER,                          -- Sarvam costs + telephony, in paise. For billing.
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_call_logs_tenant_id ON call_logs(tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_logs_caller ON call_logs(caller_number, started_at DESC);


-- =============================================================================
-- APPOINTMENTS — example domain table, used by Stage 2's book_appointment tool.
-- =============================================================================
-- In a real deployment, different tenants would have different domain tables
-- (a dental clinic needs appointments, a real estate office needs leads, etc.).
-- For Stage 1 we just stub one example so you can see the shape.

CREATE TABLE IF NOT EXISTS appointments (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    call_id         TEXT REFERENCES call_logs(call_id),
    caller_name     TEXT,
    caller_phone    TEXT,
    slot_at         TIMESTAMPTZ NOT NULL,
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'booked',   -- 'booked' | 'cancelled' | 'completed' | 'no_show'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_appointments_tenant_slot ON appointments(tenant_id, slot_at);

-- Partial unique index: at most one 'booked' appointment per (tenant, slot).
-- Cancelled/completed rows are excluded so the slot can be re-booked after cancellation.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_appointments_active_slot
    ON appointments (tenant_id, slot_at)
    WHERE status = 'booked';


-- =============================================================================
-- TRIGGER: keep tenants.updated_at fresh
-- =============================================================================
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tenants_updated_at ON tenants;
CREATE TRIGGER trg_tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW
    EXECUTE FUNCTION touch_updated_at();


-- =============================================================================
-- PAYMENT (migration 003) — payment flow fields, trusted callers, handoffs
-- =============================================================================

ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS payment_enabled       BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS payment_amount_paise  INTEGER,
  ADD COLUMN IF NOT EXISTS payment_expiry_hours  INTEGER NOT NULL DEFAULT 24,
  ADD COLUMN IF NOT EXISTS razorpay_key_id       TEXT,
  ADD COLUMN IF NOT EXISTS razorpay_key_secret   TEXT;

ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS payment_status        TEXT NOT NULL DEFAULT 'not_required',
  ADD COLUMN IF NOT EXISTS payment_link_url      TEXT,
  ADD COLUMN IF NOT EXISTS payment_expires_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS payment_completed_at  TIMESTAMPTZ;

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


-- =============================================================================
-- RETENTION POLICY — delete call data older than default_days (default 90).
-- Run via: python -m app.db.retention_job
-- =============================================================================
CREATE OR REPLACE FUNCTION enforce_retention(default_days INTEGER DEFAULT 90)
RETURNS TABLE(table_name TEXT, rows_deleted BIGINT) AS $$
DECLARE
    logs_deleted BIGINT;
    appts_deleted BIGINT;
BEGIN
    WITH deleted AS (
        DELETE FROM call_logs
        WHERE started_at < NOW() - (default_days || ' days')::INTERVAL
        RETURNING id
    )
    SELECT COUNT(*) INTO logs_deleted FROM deleted;

    WITH deleted AS (
        DELETE FROM appointments
        WHERE slot_at < NOW() - (default_days || ' days')::INTERVAL
          AND status IN ('completed', 'cancelled', 'no_show')
        RETURNING id
    )
    SELECT COUNT(*) INTO appts_deleted FROM deleted;

    RETURN QUERY VALUES
        ('call_logs', logs_deleted),
        ('appointments', appts_deleted);
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- CATALOG (migration 004) — service catalog per tenant
-- =============================================================================

CREATE TABLE IF NOT EXISTS catalog_items (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    category        TEXT NOT NULL DEFAULT 'General',
    price_min_paise INTEGER CHECK (price_min_paise >= 0),
    price_max_paise INTEGER CHECK (price_max_paise >= 0),
    duration_mins   INTEGER NOT NULL DEFAULT 30 CHECK (duration_mins > 0),
    available       BOOLEAN NOT NULL DEFAULT true,
    display_order   INTEGER NOT NULL DEFAULT 0,
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_tenant_name ON catalog_items (tenant_id, name);

CREATE OR REPLACE FUNCTION touch_catalog_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_catalog_updated_at ON catalog_items;
CREATE TRIGGER trg_catalog_updated_at
  BEFORE UPDATE ON catalog_items
  FOR EACH ROW EXECUTE FUNCTION touch_catalog_updated_at();


-- =============================================================================
-- APPOINTMENT SERVICE FIELDS (migration 005)
-- =============================================================================

ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS service_name          TEXT,
  ADD COLUMN IF NOT EXISTS service_duration_mins INTEGER;
