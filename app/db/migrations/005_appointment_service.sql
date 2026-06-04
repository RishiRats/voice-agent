ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS service_name          TEXT,
  ADD COLUMN IF NOT EXISTS service_duration_mins INTEGER;
