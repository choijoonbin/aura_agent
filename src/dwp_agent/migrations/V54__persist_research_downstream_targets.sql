CREATE TABLE ai_research_downstream_targets (
    target_id UUID PRIMARY KEY,
    delivery_id UUID NOT NULL
        REFERENCES ai_research_deliveries(delivery_id) ON DELETE RESTRICT,
    run_id UUID NOT NULL REFERENCES ai_research_runs(run_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    target_type VARCHAR(24) NOT NULL,
    result_sha256 CHAR(64) NOT NULL,
    receipt_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_research_downstream_delivery UNIQUE (delivery_id),
    CONSTRAINT uq_ai_research_downstream_owner
        UNIQUE (target_id, tenant_id, user_id),
    CONSTRAINT ck_ai_research_downstream_type
        CHECK (target_type IN ('HANDOFF', 'SHARE')),
    CONSTRAINT ck_ai_research_downstream_result
        CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_research_downstream_receipt
        CHECK (receipt_envelope LIKE 'dwp2.%')
);

CREATE INDEX idx_ai_research_downstream_owner
    ON ai_research_downstream_targets (tenant_id, user_id, target_type, created_at DESC);

CREATE TRIGGER trg_ai_research_downstream_targets_append_only
BEFORE UPDATE OR DELETE ON ai_research_downstream_targets
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_research_downstream_targets IS
    'Immutable governed internal handoff and share targets produced from completed research receipts.';
