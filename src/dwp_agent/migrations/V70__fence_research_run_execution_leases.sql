ALTER TABLE ai_research_runs
    ADD CONSTRAINT ck_ai_research_run_execution_lease CHECK (
        execution_requested_at IS NULL
        OR (
            run_state = 'RUNNING'
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND lease_owner IS NOT NULL
        )
        OR (
            run_state <> 'RUNNING'
            AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND lease_owner IS NULL
        )
    );
