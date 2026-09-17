ALTER TABLE ai_research_runs
    ADD COLUMN runtime_controls_envelope TEXT,
    ADD COLUMN execution_authorization_envelope TEXT,
    ADD COLUMN execution_command_id UUID,
    ADD COLUMN execution_requested_at TIMESTAMPTZ,
    ADD COLUMN runtime_deadline_at TIMESTAMPTZ,
    ADD COLUMN checkpoint_sequence BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN last_checkpoint_at TIMESTAMPTZ,
    ADD COLUMN lease_owner VARCHAR(160),
    ADD COLUMN execution_attempt_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE ai_research_runs
    ADD CONSTRAINT ck_ai_research_run_runtime_controls CHECK (
        runtime_controls_envelope IS NULL OR runtime_controls_envelope LIKE 'dwp2.%'
    ),
    ADD CONSTRAINT ck_ai_research_run_execution_authorization CHECK (
        execution_authorization_envelope IS NULL
        OR execution_authorization_envelope LIKE 'dwp2.%'
    ),
    ADD CONSTRAINT ck_ai_research_run_checkpoint CHECK (
        checkpoint_sequence >= 0 AND execution_attempt_count >= 0
    ),
    ADD CONSTRAINT ck_ai_research_run_lease_owner CHECK (
        lease_owner IS NULL OR (
            length(lease_owner) BETWEEN 1 AND 160
            AND lease_owner = btrim(lease_owner)
        )
    );

CREATE INDEX idx_ai_research_runs_execution_claim
    ON ai_research_runs (execution_requested_at, created_at, run_id)
    WHERE execution_authorization_envelope IS NOT NULL
      AND run_state IN ('QUEUED', 'RUNNING', 'CANCELLING');
