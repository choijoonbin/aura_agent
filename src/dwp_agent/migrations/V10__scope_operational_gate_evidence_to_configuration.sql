ALTER TABLE ai_operational_gates
    ADD COLUMN configuration_revision INTEGER NOT NULL DEFAULT 0;

UPDATE ai_operational_gates
   SET configuration_revision = CASE WHEN selected_option IS NULL THEN 0 ELSE 1 END;

ALTER TABLE ai_operational_gates
    ADD CONSTRAINT ck_ai_operational_gate_configuration_revision
        CHECK (configuration_revision >= 0);

ALTER TABLE ai_operational_gate_evidence
    ADD COLUMN configuration_revision INTEGER NOT NULL DEFAULT 0;

UPDATE ai_operational_gate_evidence evidence
   SET configuration_revision = gate.configuration_revision
  FROM ai_operational_gates gate
 WHERE gate.tenant_id = evidence.tenant_id
   AND gate.environment = evidence.environment
   AND gate.gate_key = evidence.gate_key;

DROP INDEX idx_ai_operational_gate_evidence_lookup;

CREATE INDEX idx_ai_operational_gate_evidence_lookup
    ON ai_operational_gate_evidence (
        tenant_id, environment, gate_key, configuration_revision,
        created_at DESC, evidence_id DESC);

COMMENT ON COLUMN ai_operational_gate_evidence.configuration_revision IS
    'Evidence applies only to the matching gate configuration revision; historical evidence remains immutable.';
