SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE ai_agent_runs
    ALTER COLUMN state_changed_at SET NOT NULL;
