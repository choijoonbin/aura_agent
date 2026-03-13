제목: [에이전틱 검토] 검증 노드 내 LLM Critic 도입 및 Self-Correction 루프 구현

내용:

현재 aura_agent 프로젝트의 agent/langgraph_verification_logic.py는 단순 수치와 규칙에 의존하여 통과 여부를 결정하고 있습니다. 이를 LLM의 비판적 사고(Critical Thinking)를 활용한 '에이전틱 검사(Audit)' 노드로 고도화하고자 합니다. 아래 소스 분석 결과와 지시사항에 따라 코드를 개편하세요.

1. [상태 확장] agent/langgraph_nodes.py 내 AgentState 업데이트

지시: 아래 필드들을 AgentState에 추가하여 검증 과정의 모든 추론 데이터를 추적 가능하게 하세요.

rule_score, llm_score, final_score: 하이브리드 점수 체계.

fidelity: min(evidence_completeness * 100, llm_grounding_score) 로직으로 계산된 충실도.

verification_gate: pass, hold, caution, regenerate 상태값.

diagnostic_log: LLM Critic이 작성한 상세 비판 사유 (내부용).

summary_reason: 사용자에게 보여줄 친절한 요약 설명.

2. [로직 구현] agent/langgraph_verification_logic.py 고도화

하이브리드 검증 노드: 1.  먼저 규칙 기반 엔진(rule-based)을 실행하여 HOLD 또는 REGENERATE 사유가 있는지 체크하세요. (규칙 우선순위 준수)
2.  규칙 위반이 없을 경우, LLM에게 '감사관(Auditor)' 페르소나를 부여하여 추출 결과와 증거 자료(Evidence) 간의 모순을 찾게 하세요.

Fidelity 산정: rule_fidelity (증거 완결성)와 LLM이 직접 매긴 grounding_score 중 최솟값을 fidelity 필드에 할당하세요.

Critic 피드백 생성: 검증 실패(regenerate) 시, 단순 에러 메시지가 아니라 **"어떤 증거 자료의 어떤 수치가 실제 추출값과 다르며, 어떻게 수정해야 하는지"**에 대한 구체적인 피드백을 diagnostic_log에 생성하세요.

3. [루프 제어] Self-Correction 트리거 및 우선순위

우선순위 엔진: resolve_final_decision 함수를 구현하여 gate > rule_score > llm_score 순으로 최종 의사결정을 내리세요.

should_re_evaluate 로직: * abs(rule_score - llm_score) >= 20이거나 fidelity < 40인 경우 자율적으로 regenerate 노드로 분기하게 하세요.

이때 max_retries는 2회로 제한하며, 재시도 시 이전 Critic의 피드백을 프롬프트에 동적으로 주입하세요.

4. [참조 및 안정성]

OpenAI SDK 활용: agent/output_models.py에 정의된 Pydantic 모델을 사용하여 LLM의 비판 결과를 구조화된 데이터(JSON)로 받으세요.

Fallback: LLM 비판 로직에서 오류 발생 시 fallback_used: true를 기록하고 기존의 규칙 기반 판정으로 즉시 복구(Fallback)하세요.

작업 순서 제안:

agent/langgraph_nodes.py의 AgentState 필드 확장.

agent/langgraph_verification_logic.py 내 LLM Critic용 시스템 프롬프트 작성 및 노드 로직 수정.

agent/langgraph_scoring.py의 하이브리드 점수 합산 기능 연결.


5.참고용
[agent/langgraph_nodes.py]: 모든 노드가 공유하는 '기억(State)'의 저장소입니다. 여기에 비판 사유와 상세 점수가 남아야 다음 루프에서 에이전트가 "아, 내가 이 부분을 틀렸구나"라고 인지할 수 있습니다.

[agent/langgraph_verification_logic.py]: 기존의 verify_extraction_results 함수가 단순히 문자열을 반환했다면, 이제는 LLM의 추론 결과물인 ScoringResult 객체를 처리하고 게이트를 결정하는 컨트롤 타워가 되어야 합니다.

[docs/work_info/logic/scorelogic.md]: 이 문서에 정의된 엄격한 규칙(예: 휴일 사용, 한도 초과)은 LLM이 우회할 수 없는 '하드 필터'로 작동하도록 프롬프트에 명시했습니다.
