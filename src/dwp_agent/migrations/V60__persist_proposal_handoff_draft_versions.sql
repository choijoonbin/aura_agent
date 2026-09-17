CREATE TABLE ai_proposal_handoff_draft_versions (
    draft_id UUID PRIMARY KEY,
    handoff_id UUID NOT NULL REFERENCES ai_proposal_handoffs(handoff_id) ON DELETE RESTRICT,
    proposal_id UUID NOT NULL REFERENCES ai_agent_proposals(proposal_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    handoff_version INTEGER NOT NULL,
    draft_revision INTEGER NOT NULL,
    reviewed_inputs_envelope TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_proposal_handoff_draft_command
        UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_proposal_handoff_draft_revision
        UNIQUE (handoff_id, tenant_id, user_id, draft_revision),
    CONSTRAINT ck_ai_proposal_handoff_draft_handoff_version
        CHECK (handoff_version > 0),
    CONSTRAINT ck_ai_proposal_handoff_draft_revision
        CHECK (draft_revision > 0),
    CONSTRAINT ck_ai_proposal_handoff_draft_envelope
        CHECK (reviewed_inputs_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_proposal_handoff_draft_content
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_proposal_handoff_draft_request
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX idx_ai_proposal_handoff_draft_current
    ON ai_proposal_handoff_draft_versions (
        tenant_id, user_id, handoff_id, draft_revision DESC
    );

CREATE TRIGGER trg_ai_proposal_handoff_draft_versions_append_only
BEFORE UPDATE OR DELETE ON ai_proposal_handoff_draft_versions
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_proposal_handoff_draft_versions IS
    'Append-only, encrypted server drafts and audit evidence for proposal handoff review.';
