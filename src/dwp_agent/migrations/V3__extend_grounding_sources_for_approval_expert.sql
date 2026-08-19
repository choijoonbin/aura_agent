ALTER TABLE ai_agent_citations
    DROP CONSTRAINT IF EXISTS ck_ai_agent_citations_type;

ALTER TABLE ai_agent_citations
    ADD CONSTRAINT ck_ai_agent_citations_type CHECK (
        source_type IN (
            'WORK_ITEM',
            'MAIL',
            'CALENDAR',
            'APPROVAL_TASK',
            'APPROVAL_REQUEST',
            'APPROVAL_FORM',
            'APPROVAL_OPERATION'));
