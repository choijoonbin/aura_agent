DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM ai_artifact_versions AS version
          LEFT JOIN (
              SELECT artifact_id, version_number, COUNT(*) AS actual_count
                FROM ai_artifact_version_sources
               GROUP BY artifact_id, version_number
          ) AS sources USING (artifact_id, version_number)
         WHERE version.source_count <> COALESCE(sources.actual_count, 0)
    ) THEN
        RAISE EXCEPTION 'Artifact version source evidence is inconsistent';
    END IF;
END $$;

CREATE TRIGGER trg_ai_artifact_version_sources_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_version_sources
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE FUNCTION enforce_ai_artifact_version_source_count()
RETURNS TRIGGER AS $$
DECLARE
    expected_count INTEGER;
    actual_count BIGINT;
BEGIN
    SELECT source_count
      INTO expected_count
      FROM ai_artifact_versions
     WHERE artifact_id = NEW.artifact_id
       AND version_number = NEW.version_number;

    SELECT COUNT(*)
      INTO actual_count
      FROM ai_artifact_version_sources
     WHERE artifact_id = NEW.artifact_id
       AND version_number = NEW.version_number;

    IF expected_count IS NULL OR actual_count <> expected_count THEN
        RAISE EXCEPTION 'Artifact version source evidence count is inconsistent'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_ai_artifact_version_source_count_from_version
AFTER INSERT ON ai_artifact_versions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_ai_artifact_version_source_count();

CREATE CONSTRAINT TRIGGER trg_ai_artifact_version_source_count_from_source
AFTER INSERT ON ai_artifact_version_sources
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_ai_artifact_version_source_count();

COMMENT ON TABLE ai_artifact_version_sources IS
    'Immutable encrypted source evidence sealed to the version source_count at commit.';
