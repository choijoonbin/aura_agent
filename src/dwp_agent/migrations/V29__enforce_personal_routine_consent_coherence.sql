ALTER TABLE ai_personal_routines
    DROP CONSTRAINT ck_ai_personal_routine_consent,
    ADD CONSTRAINT ck_ai_personal_routine_consent CHECK (
        source_access_consent_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
        AND analysis_consent_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
        AND proposal_delivery_consent_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
        AND consent_state = CASE
            WHEN 'RECONSENT_REQUIRED' IN (
                source_access_consent_state,
                analysis_consent_state,
                proposal_delivery_consent_state
            ) THEN 'RECONSENT_REQUIRED'
            WHEN source_access_consent_state = 'ENABLED'
             AND analysis_consent_state = 'ENABLED'
             AND proposal_delivery_consent_state = 'ENABLED' THEN 'ENABLED'
            WHEN 'DISABLED' IN (
                source_access_consent_state,
                analysis_consent_state,
                proposal_delivery_consent_state
            ) THEN 'DISABLED'
            ELSE 'UNSET'
        END
    );

COMMENT ON CONSTRAINT ck_ai_personal_routine_consent ON ai_personal_routines IS
    'The aggregate consent state is a fail-closed projection of the three explicit scopes.';
