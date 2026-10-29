-- dwp:execute-in-transaction=false
CREATE OR REPLACE PROCEDURE backfill_ai_agent_run_audit_links()
LANGUAGE plpgsql
AS $$
DECLARE
    affected INTEGER;
BEGIN
    LOOP
        WITH batch AS (
            SELECT run_id, lease_generation, current_audit_id, audit_record_id,
                   audit_link_state, created_at
              FROM ai_agent_runs run
             WHERE current_audit_id IS NOT NULL
               AND audit_record_id IS NOT NULL
               AND audit_link_state IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                     FROM ai_agent_run_audit_links link
                    WHERE link.run_id = run.run_id
                      AND link.lease_generation = run.lease_generation)
             ORDER BY created_at, run_id
             LIMIT 5000
             FOR UPDATE SKIP LOCKED
        )
        INSERT INTO ai_agent_run_audit_links (
            run_id, lease_generation, audit_id, event_id, link_state,
            created_at, linked_at)
        SELECT run_id, lease_generation, current_audit_id, audit_record_id,
               audit_link_state, created_at,
               CASE WHEN audit_link_state = 'LINKED' THEN CURRENT_TIMESTAMP ELSE NULL END
          FROM batch
        ON CONFLICT (run_id, lease_generation) DO NOTHING;
        GET DIAGNOSTICS affected = ROW_COUNT;
        COMMIT;
        EXIT WHEN affected = 0;
    END LOOP;
END;
$$;
-- dwp:next-statement
CALL backfill_ai_agent_run_audit_links();
-- dwp:next-statement
DROP PROCEDURE backfill_ai_agent_run_audit_links();
