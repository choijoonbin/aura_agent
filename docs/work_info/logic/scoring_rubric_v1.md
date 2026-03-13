# Scoring Rubric v1

- policy(0~100): 규정 위반 정황 강도(휴일/심야/근태충돌/업종/한도)
- evidence(0~100): 규정 조항/전표 라인/도구 결과의 증거 충실도
- fidelity(grounding, 0~100): 결론이 실제 증거에 의해 직접 지지되는 정도

출력 원칙:
- summary_reason: 사용자에게 이해 가능한 2~3문장 한국어 요약
- internal_reason: 점수 산정 근거를 항목별로 명확히 기술
- 규칙 엔진 탐지 결과(rule_violation_summary)를 반영해 과소평가/과대평가를 피함
