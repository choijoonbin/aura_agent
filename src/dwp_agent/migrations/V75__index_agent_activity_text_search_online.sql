-- dwp:execute-in-transaction=false
SET lock_timeout = '5s';
SET statement_timeout = '30min';
-- dwp:next-statement
DROP INDEX CONCURRENTLY IF EXISTS idx_ai_agent_runs_activity_search_trgm;
-- dwp:next-statement
CREATE INDEX CONCURRENTLY idx_ai_agent_runs_activity_search_trgm
    ON ai_agent_runs USING GIN ((
        lower(
            'DWAI_ON DWAI·ON Agent execution 에이전트 실행 ' ||
            agent_key || ' ' || COALESCE(status_code, '') || ' ' || run_id::text
        )
    ) gin_trgm_ops)
    WHERE data_provenance = 'LIVE';
-- dwp:next-statement
COMMENT ON INDEX idx_ai_agent_runs_activity_search_trgm IS
    'Online trigram index for privacy-minimized Agent Activity title, agent key, status and run identifier search.';
-- dwp:next-statement
RESET lock_timeout;
-- dwp:next-statement
RESET statement_timeout;
