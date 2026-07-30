UPDATE integration_configs
SET username = '',
    password = '',
    enabled = 0,
    auto_sync_enabled = 0,
    production_sync_enabled = 0,
    diagnostics_sync_enabled = 0,
    last_error = 'Disabled in preview copy';

INSERT INTO alert_settings (key, value)
VALUES ('TELEGRAM_ALERTS_ENABLED', 'false')
ON CONFLICT(key) DO UPDATE SET value = 'false';

UPDATE report_automations
SET active = 0,
    last_blocked_reason = 'Disabled in preview copy';

UPDATE background_jobs
SET status = 'failed',
    error_message = 'Disabled in preview copy',
    finished_at = datetime('now')
WHERE status IN ('pending', 'running', 'retry_wait');

UPDATE report_distributions
SET status = 'cancelled',
    error_message = 'Disabled in preview copy',
    updated_at = datetime('now')
WHERE status IN (
    'ready_to_send',
    'approved_to_send',
    'queued',
    'approved',
    'sending'
);
