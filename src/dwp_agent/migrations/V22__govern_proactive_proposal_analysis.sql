ALTER TABLE ai_agent_proposals
    ADD COLUMN hidden_at TIMESTAMPTZ;

CREATE INDEX idx_ai_agent_proposal_visible_inbox
    ON ai_agent_proposals (
        tenant_id, target_user_id, state, available_at,
        proposed_at DESC, proposal_id DESC
    )
    WHERE hidden_at IS NULL;

CREATE TABLE ai_agent_proposal_preferences (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    proactive_analysis_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    revision INTEGER NOT NULL,
    updated_by_user_id VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id),
    CONSTRAINT ck_ai_agent_proposal_preference_revision CHECK (revision > 0)
);

CREATE TABLE ai_agent_proposal_preference_events (
    event_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    previous_enabled BOOLEAN NOT NULL,
    current_enabled BOOLEAN NOT NULL,
    revision INTEGER NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_agent_proposal_preference_command
        UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_agent_proposal_preference_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_agent_proposal_preference_fingerprint
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TRIGGER trg_ai_agent_proposal_preference_events_append_only
BEFORE UPDATE OR DELETE ON ai_agent_proposal_preference_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_agent_proposal_analysis_commands (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    purpose VARCHAR(64) NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    generation INTEGER NOT NULL,
    lease_token UUID,
    attempt_count INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    result_envelope TEXT,
    PRIMARY KEY (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_agent_proposal_analysis_purpose
        CHECK (purpose = 'PROACTIVE_WORK_ANALYSIS_V1'),
    CONSTRAINT ck_ai_agent_proposal_analysis_session
        CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_agent_proposal_analysis_request
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_agent_proposal_analysis_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    CONSTRAINT ck_ai_agent_proposal_analysis_generation
        CHECK (generation > 0 AND attempt_count > 0),
    CONSTRAINT ck_ai_agent_proposal_analysis_lifecycle CHECK (
        (status = 'RUNNING'
            AND lease_token IS NOT NULL
            AND completed_at IS NULL
            AND result_envelope IS NULL)
        OR (status = 'FAILED'
            AND lease_token IS NULL
            AND completed_at IS NULL
            AND result_envelope IS NULL)
        OR (status = 'COMPLETED'
            AND lease_token IS NULL
            AND completed_at IS NOT NULL
            AND result_envelope IS NOT NULL
            AND completed_at >= created_at)
    )
);

CREATE INDEX idx_ai_agent_proposal_analysis_rate
    ON ai_agent_proposal_analysis_commands (tenant_id, user_id, created_at DESC);

CREATE OR REPLACE FUNCTION reject_agent_proposal_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.event_id = OLD.event_id
       AND NEW.proposal_id = OLD.proposal_id
       AND NEW.tenant_id = OLD.tenant_id
       AND NEW.target_user_id = OLD.target_user_id
       AND NEW.actor_user_id = OLD.actor_user_id
       AND NEW.correlation_id = OLD.correlation_id
       AND NEW.command_id = OLD.command_id
       AND NEW.event_type = OLD.event_type
       AND NEW.previous_state IS NOT DISTINCT FROM OLD.previous_state
       AND NEW.current_state = OLD.current_state
       AND NEW.revision = OLD.revision
       AND NEW.occurred_at = OLD.occurred_at
       AND NEW.request_fingerprint = OLD.request_fingerprint
       AND OLD.note_envelope IS NOT NULL
       AND NEW.note_envelope IS NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Agent proposal events are append-only';
END;
$$ LANGUAGE plpgsql;

COMMENT ON COLUMN ai_agent_proposals.hidden_at IS
    'User privacy tombstone; hidden rows retain only redacted content and audit metadata.';
COMMENT ON TABLE ai_agent_proposal_preferences IS
    'User-controlled opt-out for explicit and future proactive proposal analysis.';
COMMENT ON TABLE ai_agent_proposal_analysis_commands IS
    'Generation-fenced analysis commands with encrypted canonical receipts; raw sessions and analysis results are never stored.';
