SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE ai_agent_runs
    ADD CONSTRAINT ck_ai_agent_runs_state_changed_at
    CHECK (state_changed_at IS NOT NULL AND state_changed_at >= created_at)
    NOT VALID;
