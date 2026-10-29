SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE ai_agent_run_source_health
    DROP CONSTRAINT ck_ai_agent_run_source_success;

ALTER TABLE ai_agent_run_source_health
    ADD CONSTRAINT ck_ai_agent_run_source_success
    CHECK (
        (last_success_at IS NULL OR last_success_at <= last_attempt_at)
        AND (health_status <> 'SUCCESS' OR last_success_at = last_attempt_at)
    ) NOT VALID;

COMMENT ON COLUMN ai_agent_run_source_health.last_success_at IS
    'Most recent successful observation in this attempt; later failures advance last_attempt_at without erasing it.';
