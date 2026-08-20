-- Preserve configured tenant policy values while allowing newly provisioned
-- safety policies to use every governed source type by default.
ALTER TABLE ai_safety_policies
    ALTER COLUMN max_source_scopes SET DEFAULT 7;
