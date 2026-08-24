UPDATE ai_operational_gates
   SET status = 'CONFIGURING',
       updated_at = CURRENT_TIMESTAMP
 WHERE status = 'VALIDATING';

ALTER TABLE ai_operational_gates
    DROP CONSTRAINT ck_ai_operational_gate_status;

ALTER TABLE ai_operational_gates
    ADD CONSTRAINT ck_ai_operational_gate_status
        CHECK (status IN (
            'NOT_CONFIGURED', 'CONFIGURING', 'READY_FOR_APPROVAL',
            'APPROVED', 'BLOCKED', 'EXPIRED'));

COMMENT ON COLUMN ai_operational_gates.status IS
    'Synchronous readiness workflow: configure, validate, independently approve, expire.';
