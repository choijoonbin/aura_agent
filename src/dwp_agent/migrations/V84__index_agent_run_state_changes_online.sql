-- dwp:execute-in-transaction=false
SET lock_timeout = '5s';
SET statement_timeout = '30min';
-- dwp:next-statement
DROP INDEX CONCURRENTLY IF EXISTS idx_ai_agent_runs_owner_state_changed;
-- dwp:next-statement
CREATE INDEX CONCURRENTLY idx_ai_agent_runs_owner_state_changed
    ON ai_agent_runs (tenant_id, user_id, state_changed_at DESC, run_id DESC)
    WHERE data_provenance = 'LIVE';
-- dwp:next-statement
COMMENT ON INDEX idx_ai_agent_runs_owner_state_changed IS
    'Supports owner-scoped attention ordering by the latest persisted execution state transition.';
-- dwp:next-statement
RESET lock_timeout;
-- dwp:next-statement
RESET statement_timeout;
