ALTER TABLE ai_execution_policies
    DROP CONSTRAINT ck_ai_execution_policy_budget_mode;

ALTER TABLE ai_execution_policies
    ADD CONSTRAINT ck_ai_execution_policy_budget_mode
        CHECK (budget_enforcement_mode IN ('ALERT_ONLY', 'THROTTLED', 'ENFORCED'));

ALTER TABLE ai_execution_policies
    DROP CONSTRAINT ck_ai_execution_policy_enforced_limit;

ALTER TABLE ai_execution_policies
    ADD CONSTRAINT ck_ai_execution_policy_enforced_limit
        CHECK (
            budget_enforcement_mode = 'ALERT_ONLY'
            OR period_token_limit IS NOT NULL
        );

COMMENT ON COLUMN ai_execution_policies.budget_enforcement_mode IS
    'ALERT_ONLY warns, THROTTLED rejects over-budget admission with a retryable throttle code, and ENFORCED rejects with a hard-limit code.';
