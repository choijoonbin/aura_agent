-- Agentic Screening Deep Lane: screening_meta JSONB 컬럼 추가
-- dwp_aura.agent_case 테이블에 Deep lane 스크리닝 메타 저장용 컬럼 추가
--
-- 저장 내용 (D안 optional 필드):
--   lane             : "fast" | "deep"
--   promotion_reason : Deep 승격 이유 (rule_llm_mismatch / llm_low_confidence / boundary_score / normal_baseline_with_risk_signals)
--   alt_hypotheses   : Top-2 가설 목록 [{case_type, confidence, reason}, ...]
--   decision_path    : 노드별 결정 경로 로그 [string, ...]
--   align_reason     : 가드레일 정합성 보정 결과 코드
--   uncertainty_reason: Top-2 신뢰도 차이가 작을 때 불확실성 사유 (nullable)
--   fast_case_type   : Fast lane 최종 case_type
--   fast_llm_case_type: Fast lane LLM 제안 case_type
--   fast_llm_confidence: Fast lane LLM 신뢰도
--   fast_score       : Fast lane 점수
--
-- 실행: psql -f scripts/migrate_agent_case_screening_meta.sql <연결정보>

ALTER TABLE dwp_aura.agent_case
    ADD COLUMN IF NOT EXISTS screening_meta JSONB;

COMMENT ON COLUMN dwp_aura.agent_case.screening_meta IS
    'Deep lane screening metadata: lane, promotion_reason, alt_hypotheses, decision_path, align_reason, uncertainty_reason';
