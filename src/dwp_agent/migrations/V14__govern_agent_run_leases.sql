ALTER TABLE ai_agent_runs
    ADD COLUMN lease_expires_at TIMESTAMPTZ;

UPDATE ai_agent_runs
   SET lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '2 minutes'
 WHERE run_state = 'RUNNING';

ALTER TABLE ai_agent_runs
    ADD CONSTRAINT ck_ai_agent_run_lease
    CHECK (
        (run_state = 'RUNNING' AND lease_expires_at IS NOT NULL)
        OR (run_state <> 'RUNNING' AND lease_expires_at IS NULL)
    );

CREATE INDEX idx_ai_agent_runs_active_lease
    ON ai_agent_runs (lease_expires_at)
    WHERE run_state = 'RUNNING';

COMMENT ON COLUMN ai_agent_runs.lease_expires_at IS
    'Bounded Ask execution lease; expired runs may be reclaimed only by the same request payload.';
