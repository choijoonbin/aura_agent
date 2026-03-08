-- 증빙 재분석 기능용 agent_case.status enum 값 추가
-- 실제 DB에서 status 컬럼이 enum dwp_aura.agent_case_status 를 쓰는 경우 필수.
-- (invalid input value for enum dwp_aura.agent_case_status: "REVIEW_REQUIRED" 방지)
--
-- 실행: psql -f scripts/migrate_agent_case_evidence_status.sql <연결정보>

ALTER TYPE dwp_aura.agent_case_status ADD VALUE IF NOT EXISTS 'REVIEW_REQUIRED';
ALTER TYPE dwp_aura.agent_case_status ADD VALUE IF NOT EXISTS 'COMPLETED_AFTER_EVIDENCE';
ALTER TYPE dwp_aura.agent_case_status ADD VALUE IF NOT EXISTS 'EVIDENCE_PENDING';
ALTER TYPE dwp_aura.agent_case_status ADD VALUE IF NOT EXISTS 'EVIDENCE_REJECTED';
