-- Partial unique index: at most one 'booked' appointment per (tenant, slot).
-- Cancelled/completed slots stay, so the slot can be re-booked after cancellation.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_appointments_active_slot
    ON appointments (tenant_id, slot_at)
    WHERE status = 'booked';
