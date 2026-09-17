CREATE INDEX idx_ai_agent_runs_owner_created
    ON ai_agent_runs (tenant_id, user_id, created_at DESC, run_id DESC);

COMMENT ON INDEX idx_ai_agent_runs_owner_created IS
    'Supports owner-scoped, time-bounded keyset pagination for DWAI-ON run history.';
