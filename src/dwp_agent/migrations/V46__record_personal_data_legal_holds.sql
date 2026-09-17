CREATE TABLE ai_personal_data_legal_holds (
    hold_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    domain_key VARCHAR(32) NOT NULL,
    hold_state VARCHAR(24) NOT NULL DEFAULT 'ACTIVE',
    authority_reference VARCHAR(160) NOT NULL,
    dpo_subject_id VARCHAR(160) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    created_by_user_id VARCHAR(160) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_personal_data_active_legal_hold
        UNIQUE NULLS NOT DISTINCT (tenant_id, domain_key, released_at),
    CONSTRAINT fk_ai_personal_data_legal_hold_policy
        FOREIGN KEY (tenant_id, domain_key)
        REFERENCES ai_domain_retention_policies(tenant_id, domain_key)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_personal_data_legal_hold_domain CHECK (
        domain_key IN ('ROUTINE', 'MEMORY', 'ARTIFACT', 'ARTIFACT_EXPORT')
    ),
    CONSTRAINT ck_ai_personal_data_legal_hold_state CHECK (
        hold_state IN ('ACTIVE', 'RELEASED')
    ),
    CONSTRAINT ck_ai_personal_data_legal_hold_reason CHECK (
        reason_code ~ '^[A-Z][A-Z0-9_.-]{1,63}$'
    ),
    CONSTRAINT ck_ai_personal_data_legal_hold_lifecycle CHECK (
        (hold_state = 'ACTIVE' AND released_at IS NULL
            AND (expires_at IS NULL OR expires_at > effective_at))
        OR (hold_state = 'RELEASED' AND released_at IS NOT NULL
            AND released_at >= effective_at)
    )
);

CREATE TABLE ai_personal_data_legal_hold_events (
    event_id UUID PRIMARY KEY,
    hold_id UUID NOT NULL
        REFERENCES ai_personal_data_legal_holds(hold_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    domain_key VARCHAR(32) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(24) NOT NULL,
    current_state VARCHAR(24) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_personal_data_legal_hold_command
        UNIQUE (tenant_id, actor_user_id, command_id),
    CONSTRAINT ck_ai_personal_data_legal_hold_event_type CHECK (
        event_type IN ('CREATED', 'UPDATED', 'RELEASED')
    )
);

CREATE TRIGGER trg_ai_personal_data_legal_hold_events_append_only
BEFORE UPDATE OR DELETE ON ai_personal_data_legal_hold_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE INDEX idx_ai_personal_data_legal_holds_active
    ON ai_personal_data_legal_holds (tenant_id, domain_key, effective_at DESC)
    WHERE hold_state = 'ACTIVE';

COMMENT ON TABLE ai_personal_data_legal_holds IS
    'Actual DPO/compliance legal-hold authority metadata; absence is represented as unavailable evidence.';
