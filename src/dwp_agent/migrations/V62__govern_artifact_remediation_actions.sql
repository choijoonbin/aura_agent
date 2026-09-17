ALTER TABLE ai_artifact_collaboration_commands
    DROP CONSTRAINT ck_ai_artifact_collaboration_command_type;

ALTER TABLE ai_artifact_collaboration_commands
    ADD CONSTRAINT ck_ai_artifact_collaboration_command_type CHECK (
        command_type IN (
            'PREFLIGHT', 'CREATE_WORKSPACE', 'UPDATE_MEMBERS', 'EDIT',
            'RESOLVE_CONFLICT', 'CREATE_SHARE', 'REVOKE_SHARE',
            'REQUEST_ACCESS', 'CREATE_COMMENT', 'REPLY_COMMENT',
            'RESOLVE_COMMENT', 'REVIEW_DECISION', 'REMEDIATE_MASK',
            'REMEDIATE_SYNTHETIC', 'REVIEW_NOTIFICATION'
        )
    );

ALTER TABLE ai_artifact_collaboration_events
    DROP CONSTRAINT ck_ai_artifact_collaboration_event_type;

ALTER TABLE ai_artifact_collaboration_events
    ADD CONSTRAINT ck_ai_artifact_collaboration_event_type CHECK (
        event_type IN (
            'PREFLIGHT_COMPLETED', 'WORKSPACE_CREATED', 'MEMBERS_UPDATED',
            'EDIT_APPLIED', 'CONFLICT_DETECTED', 'CONFLICT_RESOLVED',
            'SHARE_CREATED', 'SHARE_REVOKED', 'ACCESS_REQUESTED',
            'COMMENT_CREATED', 'COMMENT_REPLIED', 'COMMENT_RESOLVED',
            'REVIEW_APPROVED', 'REVIEW_REJECTED', 'REMEDIATION_APPLIED',
            'REVIEW_NOTIFICATION_SENT'
        )
    );

COMMENT ON CONSTRAINT ck_ai_artifact_collaboration_command_type
    ON ai_artifact_collaboration_commands IS
    'Includes deterministic masking, synthetic replacement, and attested review notification commands.';
