ALTER TABLE ai_evaluation_runs
    ADD COLUMN lease_expires_at TIMESTAMPTZ;

UPDATE ai_evaluation_runs
   SET lease_expires_at = created_at + INTERVAL '30 minutes'
 WHERE run_state = 'RUNNING';

WITH duplicate_runs AS (
    SELECT evaluation_run_id,
           ROW_NUMBER() OVER (
               PARTITION BY tenant_id, evaluation_set_id
               ORDER BY created_at DESC, evaluation_run_id DESC
           ) AS run_order
      FROM ai_evaluation_runs
     WHERE run_state = 'RUNNING'
)
UPDATE ai_evaluation_runs run
   SET run_state = 'FAILED',
       completed_at = CURRENT_TIMESTAMP,
       lease_expires_at = NULL
  FROM duplicate_runs duplicate
 WHERE run.evaluation_run_id = duplicate.evaluation_run_id
   AND duplicate.run_order > 1;

CREATE UNIQUE INDEX uk_ai_evaluation_runs_active_set
    ON ai_evaluation_runs (tenant_id, evaluation_set_id)
    WHERE run_state = 'RUNNING';

ALTER TABLE ai_evaluation_runs
    ADD CONSTRAINT ck_ai_evaluation_run_lease
    CHECK (
        (run_state = 'RUNNING' AND lease_expires_at IS NOT NULL)
        OR (run_state <> 'RUNNING' AND lease_expires_at IS NULL)
    );

COMMENT ON COLUMN ai_evaluation_runs.lease_expires_at IS
    'Bounded execution lease used to recover interrupted evaluation runs and reject concurrent runs.';
