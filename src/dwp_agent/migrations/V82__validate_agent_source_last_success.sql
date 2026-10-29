SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30min';

ALTER TABLE ai_agent_run_source_health
    VALIDATE CONSTRAINT ck_ai_agent_run_source_success;
