-- dwp:execute-in-transaction=false
CREATE OR REPLACE PROCEDURE backfill_ai_agent_run_state_changes()
LANGUAGE plpgsql
AS $$
DECLARE
    affected INTEGER;
BEGIN
    LOOP
        WITH batch AS (
            SELECT run_id
              FROM ai_agent_runs
             WHERE state_changed_at IS NULL
             ORDER BY created_at, run_id
             LIMIT 5000
             FOR UPDATE SKIP LOCKED
        )
        UPDATE ai_agent_runs run
           SET state_changed_at = GREATEST(
               COALESCE(run.completed_at, run.created_at), run.created_at)
          FROM batch
         WHERE run.run_id = batch.run_id;
        GET DIAGNOSTICS affected = ROW_COUNT;
        COMMIT;
        EXIT WHEN affected = 0;
    END LOOP;
END;
$$;
-- dwp:next-statement
CALL backfill_ai_agent_run_state_changes();
-- dwp:next-statement
DROP PROCEDURE backfill_ai_agent_run_state_changes();
