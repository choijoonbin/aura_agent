ALTER TABLE ai_agent_runs
    ADD COLUMN current_audit_id VARCHAR(128),
    ADD COLUMN audit_record_id UUID,
    ADD COLUMN audit_link_state VARCHAR(24),
    ADD COLUMN data_provenance VARCHAR(24) NOT NULL DEFAULT 'LIVE';

ALTER TABLE ai_agent_runs
    ADD CONSTRAINT ck_ai_agent_runs_audit_link
    CHECK (
        (current_audit_id IS NULL AND audit_record_id IS NULL AND audit_link_state IS NULL)
        OR (
            length(btrim(current_audit_id)) BETWEEN 1 AND 128
            AND audit_record_id IS NOT NULL
            AND audit_link_state IN ('PENDING', 'LINKED')
        )
    ),
    ADD CONSTRAINT ck_ai_agent_runs_data_provenance
    CHECK (data_provenance IN ('LIVE', 'SAMPLE'));

CREATE TABLE ai_agent_run_stages (
    run_id UUID NOT NULL REFERENCES ai_agent_runs(run_id) ON DELETE CASCADE,
    lease_generation BIGINT NOT NULL,
    stage_key VARCHAR(24) NOT NULL,
    stage_state VARCHAR(16) NOT NULL,
    sequence SMALLINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, lease_generation, stage_key),
    CONSTRAINT uk_ai_agent_run_stage_sequence
        UNIQUE (run_id, lease_generation, sequence),
    CONSTRAINT ck_ai_agent_run_stage_generation CHECK (lease_generation > 0),
    CONSTRAINT ck_ai_agent_run_stage_key CHECK (stage_key IN (
        'AUTHORIZING', 'RETRIEVING', 'REASONING', 'VERIFYING',
        'PERSISTING', 'COMPLETED', 'FAILED')),
    CONSTRAINT ck_ai_agent_run_stage_state CHECK (
        stage_state IN ('ACTIVE', 'COMPLETED', 'SKIPPED', 'FAILED')),
    CONSTRAINT ck_ai_agent_run_stage_sequence CHECK (sequence BETWEEN 10 AND 60),
    CONSTRAINT ck_ai_agent_run_stage_completion CHECK (
        (stage_state = 'ACTIVE' AND completed_at IS NULL)
        OR (stage_state <> 'ACTIVE' AND completed_at IS NOT NULL)
    ),
    CONSTRAINT ck_ai_agent_run_stage_time CHECK (
        completed_at IS NULL OR completed_at >= started_at)
);

CREATE INDEX idx_ai_agent_run_stages_attempt
    ON ai_agent_run_stages (run_id, lease_generation, sequence);

CREATE TABLE ai_agent_run_source_health (
    run_id UUID NOT NULL REFERENCES ai_agent_runs(run_id) ON DELETE CASCADE,
    lease_generation BIGINT NOT NULL,
    source_type VARCHAR(40) NOT NULL,
    health_status VARCHAR(24) NOT NULL,
    latency_ms BIGINT,
    last_attempt_at TIMESTAMPTZ NOT NULL,
    last_success_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, lease_generation, source_type),
    CONSTRAINT ck_ai_agent_run_source_generation CHECK (lease_generation > 0),
    CONSTRAINT ck_ai_agent_run_source_type CHECK (
        length(btrim(source_type)) BETWEEN 1 AND 40),
    CONSTRAINT ck_ai_agent_run_source_status CHECK (
        health_status IN ('SUCCESS', 'UNAVAILABLE', 'NOT_CONFIGURED')),
    CONSTRAINT ck_ai_agent_run_source_latency CHECK (
        latency_ms IS NULL OR latency_ms >= 0),
    CONSTRAINT ck_ai_agent_run_source_success CHECK (
        (health_status = 'SUCCESS' AND last_success_at = last_attempt_at)
        OR (health_status <> 'SUCCESS' AND last_success_at IS NULL))
);

CREATE INDEX idx_ai_agent_run_source_health_attempt
    ON ai_agent_run_source_health (run_id, lease_generation, last_attempt_at);

COMMENT ON COLUMN ai_agent_runs.current_audit_id IS
    'Opaque Agent audit identifier; never contains prompt or response content.';
COMMENT ON COLUMN ai_agent_runs.audit_record_id IS
    'Deterministic central Platform audit event identifier; it is a link, not an Agent-side verification claim.';
COMMENT ON TABLE ai_agent_run_stages IS
    'Lease-fenced execution milestones used for measured Activity progress and stage latency.';
COMMENT ON TABLE ai_agent_run_source_health IS
    'Per-attempt source call outcomes and observed latency; no estimated connector health is stored.';
