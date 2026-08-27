ALTER TABLE ai_agent_runs
    ADD COLUMN lease_generation BIGINT NOT NULL DEFAULT 0;

UPDATE ai_agent_runs
   SET lease_generation = 1
 WHERE run_state = 'RUNNING';

ALTER TABLE ai_agent_runs
    ADD CONSTRAINT ck_ai_agent_run_lease_generation
    CHECK (
        (run_state = 'RUNNING' AND lease_generation > 0)
        OR (run_state <> 'RUNNING' AND lease_generation >= 0)
    );

COMMENT ON COLUMN ai_agent_runs.lease_generation IS
    'Monotonic fencing generation; completion and failure writes must match the active lease owner.';
