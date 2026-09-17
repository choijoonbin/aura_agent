CREATE TABLE ai_admin_control_resource_versions (
    tenant_id BIGINT NOT NULL,
    resource_type VARCHAR(80) NOT NULL,
    resource_id VARCHAR(160) NOT NULL,
    resource_version INTEGER NOT NULL,
    snapshot_hash CHAR(64) NOT NULL,
    snapshot_envelope TEXT NOT NULL,
    source_command_id UUID NOT NULL
        REFERENCES ai_admin_control_commands(command_id) ON DELETE RESTRICT,
    execution_state VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, resource_type, resource_id, resource_version),
    CONSTRAINT ck_ai_admin_control_resource_version_history_version
        CHECK (resource_version >= 1),
    CONSTRAINT ck_ai_admin_control_resource_version_history_hash
        CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_admin_control_resource_version_history_envelope
        CHECK (snapshot_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_admin_control_resource_version_history_state
        CHECK (execution_state IN ('SUCCEEDED', 'ROLLED_BACK'))
);

INSERT INTO ai_admin_control_resource_versions (
    tenant_id, resource_type, resource_id, resource_version,
    snapshot_hash, snapshot_envelope, source_command_id, execution_state, created_at)
SELECT tenant_id, resource_type, resource_id, resource_version,
       snapshot_hash, snapshot_envelope, updated_by_command_id, 'SUCCEEDED', updated_at
  FROM ai_admin_control_resources
ON CONFLICT DO NOTHING;

CREATE INDEX idx_ai_admin_control_resource_versions_command
    ON ai_admin_control_resource_versions (tenant_id, source_command_id);

CREATE TRIGGER trg_ai_admin_control_resource_versions_append_only
BEFORE UPDATE OR DELETE ON ai_admin_control_resource_versions
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_admin_control_resource_versions IS
    'Append-only version history written atomically with verified DWAI-ON admin command receipts; supports monotonic rollback as a new resource version.';
