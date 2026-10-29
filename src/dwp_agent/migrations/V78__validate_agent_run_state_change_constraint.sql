SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30min';

ALTER TABLE ai_agent_runs
    VALIDATE CONSTRAINT ck_ai_agent_runs_state_changed_at;
