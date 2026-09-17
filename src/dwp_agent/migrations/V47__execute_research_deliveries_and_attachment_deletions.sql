ALTER TABLE ai_secure_attachments
    ADD COLUMN deletion_attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN deletion_last_attempt_at TIMESTAMPTZ,
    ADD COLUMN deletion_last_error_code VARCHAR(128),
    ADD COLUMN deletion_receipt_envelope TEXT;

ALTER TABLE ai_secure_attachments
    ADD CONSTRAINT ck_ai_secure_attachment_deletion_attempts
        CHECK (deletion_attempt_count >= 0),
    ADD CONSTRAINT ck_ai_secure_attachment_deletion_error
        CHECK (
            deletion_last_error_code IS NULL
            OR deletion_last_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
        ),
    ADD CONSTRAINT ck_ai_secure_attachment_deletion_receipt
        CHECK (
            deletion_receipt_envelope IS NULL
            OR deletion_receipt_envelope LIKE 'dwp2.%'
        );

ALTER TABLE ai_research_deliveries
    ADD COLUMN safe_error_code VARCHAR(128),
    ADD COLUMN recovery_hint VARCHAR(500);

ALTER TABLE ai_research_deliveries
    ADD CONSTRAINT ck_ai_research_delivery_error
        CHECK (
            safe_error_code IS NULL
            OR safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
        ),
    ADD CONSTRAINT ck_ai_research_delivery_completion_proof
        CHECK (
            (delivery_state = 'COMPLETED'
                AND receipt_id IS NOT NULL
                AND receipt_envelope IS NOT NULL
                AND completed_at IS NOT NULL
                AND safe_error_code IS NULL
                AND recovery_hint IS NULL)
            OR delivery_state <> 'COMPLETED'
        );

CREATE INDEX idx_ai_research_deliveries_worker
    ON ai_research_deliveries (delivery_state, created_at, delivery_id)
    WHERE delivery_state IN ('QUEUED', 'RUNNING');

COMMENT ON COLUMN ai_secure_attachments.deletion_receipt_envelope IS
    'Encrypted provider deletion confirmation. DELETION_PENDING alone is never deletion proof.';
COMMENT ON COLUMN ai_research_deliveries.safe_error_code IS
    'Safe machine-readable evidence for a truthful PARTIAL or FAILED delivery.';
COMMENT ON COLUMN ai_research_deliveries.recovery_hint IS
    'User-safe recovery guidance when downstream delivery did not complete.';
