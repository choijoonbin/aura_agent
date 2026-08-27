CREATE TABLE ai_agent_proposals (
    proposal_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    target_user_id VARCHAR(160) NOT NULL,
    source_event_id VARCHAR(160) NOT NULL,
    creation_command_id UUID NOT NULL,
    created_by_user_id VARCHAR(160) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    priority VARCHAR(16) NOT NULL,
    state VARCHAR(16) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    agent_key VARCHAR(100) NOT NULL,
    action_key VARCHAR(128),
    payload_envelope TEXT NOT NULL,
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    snoozed_until TIMESTAMPTZ,
    decided_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_agent_proposal_subject
        UNIQUE (proposal_id, tenant_id, target_user_id),
    CONSTRAINT uq_ai_agent_proposal_source
        UNIQUE (tenant_id, target_user_id, source_event_id),
    CONSTRAINT uq_ai_agent_proposal_command
        UNIQUE (tenant_id, created_by_user_id, creation_command_id),
    CONSTRAINT ck_ai_agent_proposal_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_agent_proposal_kind
        CHECK (kind IN ('WORK_SIGNAL', 'RISK', 'SCHEDULE', 'APPROVAL', 'INSIGHT')),
    CONSTRAINT ck_ai_agent_proposal_priority
        CHECK (priority IN ('LOW', 'MEDIUM', 'HIGH', 'URGENT')),
    CONSTRAINT ck_ai_agent_proposal_state
        CHECK (state IN ('PENDING', 'SNOOZED', 'ACCEPTED', 'DISMISSED')),
    CONSTRAINT ck_ai_agent_proposal_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_agent_proposal_agent_key
        CHECK (agent_key ~ '^[A-Z][A-Z0-9_.-]{0,99}$'),
    CONSTRAINT ck_ai_agent_proposal_action_key
        CHECK (action_key IS NULL OR action_key ~ '^[A-Z][A-Z0-9_.-]{0,127}$'),
    CONSTRAINT ck_ai_agent_proposal_envelope
        CHECK (payload_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_agent_proposal_window CHECK (
        expires_at > available_at
        AND expires_at <= proposed_at + INTERVAL '90 days'
    ),
    CONSTRAINT ck_ai_agent_proposal_snooze CHECK (
        (state = 'SNOOZED' AND snoozed_until IS NOT NULL
            AND snoozed_until < expires_at)
        OR (state <> 'SNOOZED' AND snoozed_until IS NULL)
    ),
    CONSTRAINT ck_ai_agent_proposal_decision CHECK (
        (state IN ('ACCEPTED', 'DISMISSED') AND decided_at IS NOT NULL)
        OR (state NOT IN ('ACCEPTED', 'DISMISSED') AND decided_at IS NULL)
    ),
    CONSTRAINT ck_ai_agent_proposal_updated CHECK (updated_at >= proposed_at)
);

CREATE INDEX idx_ai_agent_proposal_inbox
    ON ai_agent_proposals (
        tenant_id, target_user_id, state, available_at, proposed_at DESC, proposal_id DESC
    );
CREATE INDEX idx_ai_agent_proposal_expiry
    ON ai_agent_proposals (expires_at);

CREATE TABLE ai_agent_proposal_events (
    event_id UUID PRIMARY KEY,
    proposal_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    target_user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(16) NOT NULL,
    previous_state VARCHAR(16),
    current_state VARCHAR(16) NOT NULL,
    revision INTEGER NOT NULL,
    note_envelope TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_agent_proposal_event_subject
        FOREIGN KEY (proposal_id, tenant_id, target_user_id)
        REFERENCES ai_agent_proposals (proposal_id, tenant_id, target_user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_agent_proposal_event_command
        UNIQUE (tenant_id, actor_user_id, command_id),
    CONSTRAINT ck_ai_agent_proposal_event_type
        CHECK (event_type IN ('CREATED', 'ACCEPT', 'SNOOZE', 'DISMISS')),
    CONSTRAINT ck_ai_agent_proposal_event_previous CHECK (
        previous_state IS NULL OR previous_state IN ('PENDING', 'SNOOZED')
    ),
    CONSTRAINT ck_ai_agent_proposal_event_current CHECK (
        current_state IN ('PENDING', 'SNOOZED', 'ACCEPTED', 'DISMISSED')
    ),
    CONSTRAINT ck_ai_agent_proposal_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_agent_proposal_event_note CHECK (
        note_envelope IS NULL OR note_envelope LIKE 'dwp2.%'
    )
);

CREATE INDEX idx_ai_agent_proposal_event_timeline
    ON ai_agent_proposal_events (
        tenant_id, target_user_id, occurred_at DESC, event_id DESC
    );

CREATE OR REPLACE FUNCTION reject_agent_proposal_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Agent proposal events are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ai_agent_proposal_events_append_only
BEFORE UPDATE OR DELETE ON ai_agent_proposal_events
FOR EACH ROW EXECUTE FUNCTION reject_agent_proposal_event_mutation();

COMMENT ON TABLE ai_agent_proposals IS
    'Tenant and user scoped proactive Agent proposals with encrypted business content.';
COMMENT ON COLUMN ai_agent_proposals.payload_envelope IS
    'DWP2 envelope ciphertext; title, rationale, inputs, and evidence are never stored in plaintext.';
COMMENT ON TABLE ai_agent_proposal_events IS
    'Append-only decision evidence for proposal creation, acceptance, snooze, and dismissal.';
