ALTER TABLE ai_governance_events
    DROP CONSTRAINT ck_ai_governance_event_category;

ALTER TABLE ai_governance_events
    ADD CONSTRAINT ck_ai_governance_event_category
        CHECK (category IN ('SOURCE', 'ACTION', 'SAFETY', 'EVALUATION', 'RETENTION', 'GATE'));

CREATE TABLE ai_operational_gates (
    tenant_id BIGINT NOT NULL,
    environment VARCHAR(24) NOT NULL,
    gate_key VARCHAR(80) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'NOT_CONFIGURED',
    selected_option VARCHAR(80),
    owner_user_id VARCHAR(160),
    configuration_ref VARCHAR(500),
    notes VARCHAR(2000),
    validation_summary VARCHAR(1000),
    last_configured_by VARCHAR(160),
    last_validated_by VARCHAR(160),
    approved_by VARCHAR(160),
    policy_version INTEGER NOT NULL DEFAULT 1,
    effective_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    updated_by VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, environment, gate_key),
    CONSTRAINT ck_ai_operational_gate_environment
        CHECK (environment IN ('DEVELOPMENT', 'STAGING', 'PRODUCTION')),
    CONSTRAINT ck_ai_operational_gate_status
        CHECK (status IN (
            'NOT_CONFIGURED', 'CONFIGURING', 'VALIDATING', 'READY_FOR_APPROVAL',
            'APPROVED', 'BLOCKED', 'EXPIRED')),
    CONSTRAINT ck_ai_operational_gate_version CHECK (policy_version >= 1),
    CONSTRAINT ck_ai_operational_gate_expiry
        CHECK (expires_at IS NULL OR effective_at IS NULL OR expires_at > effective_at),
    CONSTRAINT ck_ai_operational_gate_approval
        CHECK (status <> 'APPROVED' OR
               (approved_by IS NOT NULL AND effective_at IS NOT NULL AND expires_at IS NOT NULL))
);

CREATE INDEX idx_ai_operational_gates_tenant_status
    ON ai_operational_gates (tenant_id, environment, status, gate_key);

CREATE TABLE ai_operational_gate_evidence (
    evidence_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    environment VARCHAR(24) NOT NULL,
    gate_key VARCHAR(80) NOT NULL,
    evidence_type VARCHAR(40) NOT NULL,
    title VARCHAR(200) NOT NULL,
    reference VARCHAR(500) NOT NULL,
    checksum_sha256 VARCHAR(64),
    notes VARCHAR(1000),
    created_by VARCHAR(160) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_operational_gate_evidence
        FOREIGN KEY (tenant_id, environment, gate_key)
        REFERENCES ai_operational_gates (tenant_id, environment, gate_key)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_operational_gate_evidence_type
        CHECK (evidence_type IN (
            'CONFIGURATION_REFERENCE', 'TEST_RESULT', 'SECURITY_REVIEW',
            'LEGAL_APPROVAL', 'BUSINESS_APPROVAL', 'RUNBOOK', 'OTHER')),
    CONSTRAINT ck_ai_operational_gate_evidence_checksum
        CHECK (checksum_sha256 IS NULL OR checksum_sha256 ~ '^[a-f0-9]{64}$')
);

CREATE INDEX idx_ai_operational_gate_evidence_lookup
    ON ai_operational_gate_evidence (
        tenant_id, environment, gate_key, created_at DESC, evidence_id DESC);

COMMENT ON TABLE ai_operational_gates IS
    'Environment-specific customer delivery decisions; stores references and approvals, never secrets.';
COMMENT ON TABLE ai_operational_gate_evidence IS
    'Versioned evidence references supporting DWAI-ON delivery readiness and independent approval.';
