-- dwp:execute-in-transaction=false
SET lock_timeout = '5s';
SET statement_timeout = '30min';
-- dwp:next-statement
DROP INDEX CONCURRENTLY IF EXISTS idx_ai_agent_run_audit_pending;
-- dwp:next-statement
CREATE INDEX CONCURRENTLY idx_ai_agent_run_audit_pending
    ON ai_agent_run_audit_links (created_at, run_id, lease_generation)
    WHERE link_state = 'PENDING';
-- dwp:next-statement
RESET lock_timeout;
-- dwp:next-statement
RESET statement_timeout;
