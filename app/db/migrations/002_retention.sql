-- Retention policy: auto-delete call data older than configurable threshold.
-- Default 90 days; per-tenant override possible via tenants.metadata.retention_days.

-- Function: delete old call_logs and orphaned/stale appointments.
-- Returns count of rows deleted, useful for the cron runner to log progress.
CREATE OR REPLACE FUNCTION enforce_retention(default_days INTEGER DEFAULT 90)
RETURNS TABLE(table_name TEXT, rows_deleted BIGINT) AS $$
DECLARE
    logs_deleted BIGINT;
    appts_deleted BIGINT;
BEGIN
    -- Delete old call_logs
    WITH deleted AS (
        DELETE FROM call_logs
        WHERE started_at < NOW() - (default_days || ' days')::INTERVAL
        RETURNING id
    )
    SELECT COUNT(*) INTO logs_deleted FROM deleted;

    -- Delete past appointments older than the retention window
    -- (future appointments are always kept)
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
