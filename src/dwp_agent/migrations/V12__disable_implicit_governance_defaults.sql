UPDATE ai_data_source_policies
   SET access_mode = 'BLOCKED',
       enabled = FALSE,
       connection_state = 'BLOCKED',
       policy_version = policy_version + 1,
       updated_by = 'SYSTEM_SECURITY_HARDENING',
       updated_at = CURRENT_TIMESTAMP
 WHERE policy_version = 1
   AND enabled = TRUE;

UPDATE ai_action_policies
   SET enabled = FALSE,
       confirmation_required = TRUE,
       execution_policy = 'BLOCKED',
       policy_version = policy_version + 1,
       updated_by = 'SYSTEM_SECURITY_HARDENING',
       updated_at = CURRENT_TIMESTAMP
 WHERE policy_version = 1
   AND enabled = TRUE;
