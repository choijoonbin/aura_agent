제목: [고도화] 스코어링 로직의 하드코딩 제거 및 LLM-as-a-Judge 기반 에이전틱 평가 시스템 도입

내용:

현재 aura_agent 프로젝트의 agent/langgraph_scoring.py와 agent/langgraph_verification_logic.py에 구현된 스코어링 방식은 정적인 규칙이나 단순 수치 비교에 의존하는 경향이 있습니다. 이를 자율형 에이전트의 판단력을 극대화할 수 있는 **'LLM 기반의 다차원 평가(Rubric) 시스템'**으로 개편하고자 합니다. 다음 가이드라인에 따라 코드를 수정해주세요.

1. 분석 대상 및 목표

대상 파일: agent/langgraph_scoring.py, agent/langgraph_verification_logic.py, agent/output_models.py

목표: 하드코딩된 점수 계산 로직을 제거하고, 에이전트가 생성한 결과물의 '충실도(Faithfulness)', '규정 준수성(Policy Alignment)', '증거 근거성(Evidence Grounding)'을 LLM이 직접 검토하여 점수를 매기도록 변경.

2. 상세 요구사항

평가 루브릭(Rubric) 도입: * 단순 점수가 아닌, 구체적인 평가 기준(예: 규정 참조의 정확성, 영수증 데이터와 대조 결과의 일치도 등)을 담은 ScoringCriteria 구조체를 정의하세요.

LLM이 각 기준별로 1~5점을 부여하고, 그 이유(Reasoning)를 반드시 포함하도록 ScoringResult Pydantic 모델을 업데이트하세요.

비판적 사고(Critic) 노드 강화:

검증(Verification) 노드에서 통과/실패(Pass/Fail)만 결정하는 것이 아니라, "무엇이 부족하여 이 점수가 나왔는지"에 대한 피드백을 생성하게 하세요.

이 피드백은 다시 Reasoning 노드로 전달되어 에이전트가 스스로 수정(Self-correction)할 수 있는 루프의 기반이 되어야 합니다.

동적 임계값(Threshold) 처리:

고정된 score > 70 방식이 아니라, 안건의 중요도(예: 금액의 크기나 규정의 엄격함)에 따라 LLM이 '추가 검토 필요' 여부를 자율적으로 결정하는 should_revaluate 로직을 구현하세요.

3. 코드 스타일 및 구현 가이드

langchain의 StructuredOutput 기능을 활용하여 평가 결과를 JSON 형태로 정형화하여 받으세요.

langgraph의 State 객체에 evaluation_history를 추가하여, 몇 번의 수정을 거쳤고 점수가 어떻게 변했는지 추적할 수 있도록 하세요.

기존의 score_logic.md에 기술된 비즈니스 로직을 LLM의 시스템 프롬프트(System Prompt)로 자연스럽게 녹여내어, 코드 수정 없이 프롬프트 수정만으로 평가 기준을 바꿀 수 있게 설계하세요.

4. 결과 확인

수정된 로직이 적용된 후, 에이전트가 잘못된 영수증 처리에 대해 왜 낮은 점수를 주었는지 '이유'를 논리적으로 설명하는지 확인하는 테스트 코드를 제안해주세요.


참고
-@agent/langgraph_scoring.py, @agent/langgraph_verification_logic.py, @agent/output_models.py 파일을 참조
-이 로직을 위해 새로운 ScoringAgent 클래스를 별도 파일로 분리하는 게 좋다
-한꺼번에 모든 것을 바꾸기보다, "먼저 output_models.py에 점수 모델부터 정의하고, 그 다음 스코어링 로직을 수정

---

## AuraAgent 적용성 검토 의견 (시너지/보완/주의)

### 1) 시너지 효과가 큰 부분
- **평가 루브릭 구조화**: `policy/evidence/fidelity` 축을 분리하면 현재 점수 산정 근거 UI와 자연스럽게 연결된다.
- **점수 + 이유 동시 저장**: `왜 이 점수인지`를 run 결과에 남기면 HITL/감사 추적 품질이 올라간다.
- **critic 피드백 강화**: 현재 unsupported taxonomy와 결합하면 재검토 사유를 더 일관되게 만들 수 있다.
- **evaluation_history 추적**: 재시도/보정 루프의 개선 효과를 정량적으로 보여줄 수 있어 PoC 설명력에 유리하다.

### 2) 그대로 적용 시 위험한/잘못된 부분
- **"하드코딩 제거"를 전면 목표로 잡는 것은 위험**:
  - 금융/감사 도메인은 재현성과 통제가 중요하므로, 결정론(룰/게이트)을 전부 LLM으로 치환하면 운영 리스크가 커진다.
  - 권장: "하드코딩 제거"가 아니라 "결정론 + LLM Judge 하이브리드"로 정의 변경.
- **검증 노드를 Pass/Fail에서 점수 중심으로만 전환**은 부적절:
  - 현재 verify의 게이트(`hold/caution/regenerate`)는 운영 안정장치다.
  - 점수는 보조 신호로 두고 게이트는 유지하는 것이 맞다.
- **`should_revaluate` 오탈자**:
  - 의도는 `should_re_evaluate` 또는 `should_reassess`가 명확하다.
- **LangChain StructuredOutput 강제**는 현재 스택과 충돌 가능:
  - 현 프로젝트는 OpenAI SDK + Pydantic 경로가 이미 안정화됨.
  - LangChain 강제 도입은 의존성 리스크만 키울 수 있음(필수 조건 아님).

### 3) 보완해서 반영하면 좋은 방향 (권장안)
- **단계적 도입(무중단)**:
  1. `output_models.py`에 `ScoringCriteria`, `ScoringResult`, `EvaluationHistoryEntry` 추가
  2. `langgraph_scoring.py`에 `llm_judge_enabled` 플래그 기반 병행 계산(기존 점수 유지 + LLM 점수 병렬 저장)
  3. `verifier`는 기존 게이트 유지, 단 `caution` 판정 시 LLM Judge reason을 추가 근거로만 사용
  4. UI에는 "기존 점수 / LLM 보조점수 / 최종 결정 근거"를 구분 노출
- **Fail-safe 원칙 명시**:
  - LLM 실패/타임아웃/파싱 오류 시 기존 결정론 점수로 자동 fallback
  - 운영 기본값은 기존 경로 유지, feature flag로 점진 활성화
- **프롬프트 외부화**:
  - `score_logic.md` 내용을 시스템 프롬프트로 이관하되, 버전/배포 이력 관리 필수

### 4) 테스트/검증 보완 포인트
- 회귀 테스트:
  - 동일 입력에서 기존 점수/게이트가 깨지지 않는지
  - LLM 실패 시 fallback이 항상 동작하는지
- 품질 테스트:
  - 정상 비교군/휴일의심/한도초과/사적사용 케이스별 reason 일관성
  - HITL 재개 후 점수/사유가 비정상적으로 흔들리지 않는지
- 관측성:
  - `judge_model`, `judge_score`, `judge_reason`, `fallback_used`, `latency_ms` 로그/진단 노출

### 5) 결론
- 본 지시서는 **방향성은 매우 좋음**(설명가능성/자기수정 강화).
- 다만 AuraAgent에는 **전면 치환형이 아닌 하이브리드형**으로 적용해야 시너지와 안정성을 동시에 얻을 수 있다.





#제목: [하이브리드 고도화] 규칙 기반 게이트웨이와 LLM Judge를 결합한 신뢰형 스코어링 시스템 구축

내용:

현재 aura_agent의 스코어링 및 검증 로직을 '결정론적 규칙(Deterministic Rules) + LLM 비판(Critic) 하이브리드' 구조로 고도화하고자 합니다. 단순한 하드코딩 제거가 아니라, 운영 안정성을 유지하면서도 AI의 추론 능력을 보조 지표로 활용하는 것이 목적입니다. 다음 지시사항에 따라 코드를 수정하세요.

1. [모델 정의] agent/output_models.py 수정

다차원 평가 모델 도입: ScoringCriteria(항목별 기준), ScoringResult(점수, 이유, 근거 포함), EvaluationHistoryEntry(이력 추적) Pydantic 모델을 정의하세요.

하이브리드 결과 구조: 기존의 규칙 기반 점수(rule_score)와 LLM Judge 점수(llm_score)를 각각 저장하고, 최종 판정 사유(final_reasoning)를 포함하도록 VerificationState를 확장하세요.

OpenAI SDK & Pydantic 준수: LangChain StructuredOutput 대신, 현재 프로젝트 스택인 OpenAI Function Calling/Structured Outputs와 Pydantic 경로를 그대로 유지하세요.

2. [로직 개편] agent/langgraph_scoring.py 수정

하이브리드 계산 엔진:

먼저 기존의 결정론적 규칙(금액 한도, 날짜 오류 등)에 의한 점수를 계산합니다.

llm_judge_enabled 플래그를 확인하여, 활성화 시 LLM이 policy/evidence/fidelity 관점에서 심층 평가를 수행하도록 합니다.

Fail-safe 원칙 적용: LLM 호출 실패, 타임아웃, 파싱 오류 발생 시 즉시 에러를 내뱉지 말고, 기존 규칙 기반 점수로 자동 Fallback 하도록 try-except 로직을 구현하세요. fallback_used: bool 플래그를 남겨야 합니다.

3. [검증 강화] agent/langgraph_verification_logic.py 및 verifier 노드

안전 게이트 유지: 현재의 게이트(hold/caution/regenerate)는 운영 안전장치이므로 제거하지 마세요.

Caution 사유 고도화: 상태가 caution일 때, LLM Judge가 생성한 reasoning을 추가 근거로 결합하여 사용자(HITL)에게 노출하세요.

재검토 로직: should_re_evaluate(오타 수정) 메서드를 추가하여, 점수 차이가 크거나 특정 항목 점수가 낮을 경우 자율적으로 재수행 여부를 결정하는 로직을 보조적으로 넣으세요.

4. [프롬프트 관리] 외부 지식 반영

docs/work_info/logic/scorelogic.md 등에 정의된 비즈니스 로직을 시스템 프롬프트의 기본 루브릭으로 주입하세요. 코드와 정책이 분리되도록 설계해야 합니다.

5. [검증 및 관측성]

수정 후, 다음 항목을 로그 또는 진단 노드에 노출하는 코드를 포함하세요: rule_score, llm_score, final_decision, fallback_used, latency_ms.

회귀 테스트 가이드: 기존의 정상/오류 케이스 입력 시, 규칙 기반 게이트웨이가 깨지지 않고 정상 작동하는지 확인하는 테스트 구조를 제안하세요.
작업 완료 후 커서에게 "LLM 실패 시 Fallback 로직이 제대로 구현되었는지 다시 한번 검토

---

## [재검토 피드백] 하이브리드 고도화 지시서 검토 의견

### 총평
- 이번 개정안은 이전 버전 대비 **현 프로젝트에 더 적합**합니다.
- 특히 `결정론 게이트 유지 + LLM Judge 보조` 원칙, `Fallback 강제`, `OpenAI SDK/Pydantic 유지`는 운영 안정성과 설명가능성을 동시에 확보하는 방향으로 타당합니다.

### 잘 정리된 부분 (그대로 진행 권장)
- `rule_score`와 `llm_score`를 분리 저장하는 설계는 추적성과 디버깅에 매우 유리합니다.
- `caution`에서 LLM reasoning을 보조 근거로 결합하는 방식은 HITL 사용자 경험 개선에 직접 효과가 있습니다.
- `fallback_used`, `latency_ms`를 필수 관측 항목으로 둔 점은 운영 관점에서 적절합니다.

### 보완 필요 포인트 (작업 전 명시 권장)
- `final_decision` 우선순위를 문서에 명시하세요.
  - 권장: **게이트(hold/caution/regenerate) > rule_score > llm_score** 순.
  - 이유: LLM 점수가 높아도 규칙상 차단 사유가 있으면 우회되면 안 됩니다.
- `llm_judge_enabled` 플래그 기본값을 명시하세요.
  - 권장: 기본 `false`(점진 활성화), 환경별 on/off.
- `should_re_evaluate` 발동 조건을 정량화하세요.
  - 예: `abs(rule_score - llm_score) >= X`, `fidelity <= Y`, `unsupported taxonomy high-severity`.
- `final_reasoning` 구성 규칙을 정하세요.
  - 권장: 사용자 노출용은 2~3문장 요약 + 내부 진단용 상세 reason 분리.

### 잠재 리스크 / 오해 가능 구간
- "자율적으로 재수행" 문구는 무한 재시도를 유발할 수 있습니다.
  - 권장: `max_retries`(예: 1~2회), 재시도 사유 코드 저장 필수.
- `scorelogic.md`를 프롬프트로 주입할 때, 정책문구 과다 주입으로 토큰 비용/응답 변동이 커질 수 있습니다.
  - 권장: 핵심 규칙만 요약한 `scoring_rubric_v*.md` 별도 분리.
- `fallback_used`가 true인 케이스가 많아질 경우 LLM Judge 품질이 아닌 인프라 문제일 수 있으므로 원인코드(`timeout`, `parse_error`, `schema_error`)를 함께 저장하세요.

### 최소 구현 단위(추천 순서)
1. `output_models.py`에 스키마 추가 (`ScoringCriteria`, `ScoringResult`, `EvaluationHistoryEntry`).
2. `langgraph_scoring.py`에 병렬 계산 + Fallback + 진단 필드 저장.
3. `langgraph_verification_logic.py`에 `should_re_evaluate`와 caution 노출 문구 결합.
4. 로그/진단 API/UI에 `rule_score`, `llm_score`, `final_decision`, `fallback_used`, `latency_ms`, `fallback_reason` 노출.
5. 회귀 테스트(게이트 불변성, fallback 강제 동작, 정상/의심 케이스 일관성) 추가.

### 결론
- 현재 작성하신 **[하이브리드 고도화] 지시서는 실무 적용 가능성이 높고 방향이 올바릅니다.**
- 위 보완 포인트(의사결정 우선순위/재시도 제한/fallback 원인코드)만 반영하면 바로 구현 단계로 넘어가도 무방합니다.




#제목: [최종 구현] 규칙 우선순위와 LLM Judge가 결합된 하이브리드 검증 시스템 구축

내용:

aura_agent 프로젝트의 검증 정확도를 높이기 위해, 기존의 규칙 기반 로직과 LLM의 추론 능력을 결합한 하이브리드 스코어링 및 검증 시스템을 구현하세요. 이 시스템은 **"규칙에 의한 통제(Determinism)가 LLM의 판단보다 우선한다"**는 원칙을 준수해야 합니다.

1. [모델 고도화] agent/output_models.py 수정

평가 스키마: ScoringCriteria(항목별 루브릭), ScoringResult(rule/llm 점수 분리), EvaluationHistoryEntry를 추가하세요.

상세 진단 필드: VerificationState에 다음 필드를 추가하여 관측성을 확보하세요.

fallback_used (bool), fallback_reason (code: timeout, parse_error, schema_error 등)

llm_judge_enabled (bool, default=False)

retry_count (int, default=0), max_retries (int, default=2)

2. [병렬 스코어링 로직] agent/langgraph_scoring.py 수정

의사결정 우선순위 준수: 최종 판정은 게이트(hold/caution/regen) > rule_score > llm_score 순으로 결정되어야 합니다. LLM 점수가 높더라도 규칙 위반(Gate 차단)이 있다면 우회할 수 없습니다.

하이브리드 엔진 구현:

고정된 비즈니스 룰을 계산하여 rule_score 생성.

llm_judge_enabled=True인 경우에만 LLM 평가 수행.

Fallback 강제: LLM 호출 중 에러 발생 시, 원인 코드를 fallback_reason에 기록하고 즉시 rule_score를 최종 점수로 사용하세요.

이유(Reasoning) 분리:

user_reason: 사용자 노출용 (2~3문장 요약)

internal_reason: 내부 진단 및 디버깅용 상세 근거

3. [흐름 제어 로직] agent/langgraph_verification_logic.py 수정

should_re_evaluate 정량화 구현: 다음 조건 중 하나라도 충족 시 retry_count가 max_retries 미만인 경우에만 재수행합니다.

abs(rule_score - llm_score) >= 20 (규칙과 AI의 판단 차이가 클 때)

fidelity <= 40 (생성 결과의 근거가 매우 희박할 때)

특정 고위험(high-severity) 항목의 점수가 낮을 때

재검토 피드백 생성: 재수행 시, 이전 실패의 internal_reason을 LLM에게 전달하여 스스로 수정(Self-correction)하게 유도하세요.

4. [프롬프트 및 외부 로직]

docs/work_info/logic/scorelogic.md를 참조하되, 핵심 규칙만 추출한 요약본을 시스템 프롬프트의 루브릭으로 활용하여 토큰 비용을 최적화하세요.

5. [관측성 및 테스트]

모든 결과에 latency_ms를 측정하여 저장하세요.

회귀 테스트 구현:

기존 규칙(휴일 사용 등)이 LLM 점수에 의해 무시되지 않는지 확인하는 테스트 케이스.

강제 타임아웃 발생 시 Fallback이 정상 작동하는지 확인하는 케이스.


#제목: [완결본] 8대 거버넌스 규칙 기반 하이브리드 스코어링 및 검증 시스템 구현

내용:

aura_agent의 검증 로직을 금융권 수준의 안정성을 갖춘 하이브리드 구조로 개편합니다. 다음 8가지 보완 포인트를 엄격히 준수하여 코드를 수정하세요.

1. [모델 및 엔지니어링] agent/output_models.py

FallbackReason Enum을 추가하세요: TIMEOUT, PARSE_ERROR, SCHEMA_ERROR, PROVIDER_ERROR.

모든 검증 결과에 scoring_version: "1.0.0", rubric_version: "1.2.0", prompt_version: "2.0.1"과 같은 버전 정보를 포함하세요.

VerificationResult를 수정하여 summary_reason(사용자용)과 diagnostic_log(내부 상세)를 분리하세요.

2. [하이브리드 엔진] agent/langgraph_scoring.py

우선순위 강제: 최종 판정 시 gate(hold/caution/regenerate) > rule_score > llm_score 순으로 적용하는 resolve_final_decision 메서드를 구현하세요. 규칙 게이트가 최우선입니다.

기본값 및 SLO: llm_judge_enabled의 기본값은 False로 설정하세요. LLM Judge 실행 시 time.perf_counter()로 지연 시간을 측정하고, p95 SLO인 3,000ms를 초과하거나 실패 시 즉시 Fallback을 실행하세요.

Fallback 처리: 실패 시 fallback_used=True와 함께 발생 원인(예: TIMEOUT)을 기록하고, 기존 rule_score를 최종 결과로 반환하세요.

3. [재평가 제어] agent/langgraph_verification_logic.py

should_re_evaluate 메서드를 구현하세요. 발동 기준은 점수 차이 >= 20 또는 신뢰도(fidelity) < 40입니다.

max_retries를 2회로 엄격히 제한하고, 재시도 시마다 retry_count를 증가시키며 이전 실패 사유를 컨텍스트에 포함하세요.

4. [회귀 테스트] tests/test_autonomy_regressions.py

다음 시나리오를 포함하는 테스트 세트를 작성하세요:

우선순위 테스트: LLM 점수는 높으나 규칙 게이트가 HOLD인 경우 최종 결과가 HOLD인지 확인.

Fallback 테스트: LLM API 강제 지연/에러 발생 시 rule_score로 정상 복구되는지 확인.

정상/의심 케이스: 휴일 결제 및 한도 초과 케이스에서 rule_score와 llm_score의 상관관계 확인.

5. [프롬프트 최적화]

docs/work_info/logic/scorelogic.md의 정책 중 핵심 루브릭만 요약하여 scoring_rubric_v1.md로 분리하고, 이를 시스템 프롬프트로 주입하여 토큰 효율을 높이세요.

---

## [착수 전 실행계획] 하이브리드 스코어링/검증 구현 상세 절차

아래 순서로 진행하면 기존 동작을 깨지 않고 단계적으로 반영할 수 있습니다.

### 0) 작업 원칙 (고정)
- 규칙 우선순위 고정: `gate(hold/caution/regenerate) > rule_score > llm_score`
- 기본 동작 보수 유지: `llm_judge_enabled=false`를 기본값으로 시작
- 실패 시 즉시 복귀: LLM 실패/지연/파싱 오류는 항상 `rule_score` fallback
- 사용자/내부 이유 분리: `summary_reason`(사용자) vs `diagnostic_log`(운영/개발)

### 1) 1차: 데이터 모델/설정 확장 (무중단)
- 대상 파일:
  - `agent/output_models.py`
  - `utils/config.py`
  - `.env.example`
- 작업 내용:
  - `FallbackReason` Enum 추가: `TIMEOUT`, `PARSE_ERROR`, `SCHEMA_ERROR`, `PROVIDER_ERROR`
  - `ScoringCriteria`, `ScoringResult`, `EvaluationHistoryEntry` 모델 추가
  - 검증 결과 모델에 아래 필드 추가
    - `rule_score`, `llm_score`, `final_score`
    - `fallback_used`, `fallback_reason`
    - `llm_judge_enabled`, `retry_count`, `max_retries`
    - `scoring_version`, `rubric_version`, `prompt_version`
    - `summary_reason`, `diagnostic_log`
  - 설정값 추가
    - `LLM_JUDGE_ENABLED=false`
    - `LLM_JUDGE_TIMEOUT_MS`
    - `LLM_JUDGE_MAX_RETRIES=2`
    - `LLM_JUDGE_SLO_P95_MS=3000`
- 산출물:
  - 기존 코드 경로와 호환되는 기본값 포함 스키마
  - 구버전 run 결과와 함께 읽혀도 오류 없는 backward-compatible 구조

### 2) 2차: 스코어링 엔진 병행 계산 도입
- 대상 파일:
  - `agent/langgraph_scoring.py`
  - (필요 시) `agent/langgraph_decisions.py`
- 작업 내용:
  - `resolve_final_decision()` 구현
    - 게이트 상태 우선 적용 후 점수 반영
  - 규칙 점수(`rule_score`) 기존 계산 로직 유지
  - **Short-circuit 최적화**:
    - 규칙 엔진 결과가 `HOLD` 또는 `REGENERATE`로 확정되면 LLM Judge 호출 생략(`judge_skipped=true`, `skip_reason=rule_gate_blocked`)
    - 비용/지연 절감을 위해 기본 경로로 적용
  - `llm_judge_enabled=true`일 때만 LLM Judge 실행
    - rubric 입력: policy/evidence/fidelity
    - **Context Injection**: `rule_violation_summary`(규칙 엔진 탐지 요약)를 프롬프트에 함께 주입
    - `time.perf_counter()`로 `latency_ms` 측정
  - 예외 처리 및 fallback 강제
    - timeout/파싱/스키마/프로바이더 오류를 `fallback_reason` 코드화
    - `fallback_used=true`, `final_score=rule_score`
  - reason 분리 저장
    - `summary_reason`: 사용자 2~3문장
    - `diagnostic_log`: 상세 계산/예외/리트라이 정보
- 산출물:
  - 기존 점수/판정과 동치(LLM 비활성 상태)
  - LLM 활성 시에도 규칙 게이트 우회 불가

### 3) 3차: 검증 로직 재평가 제어 추가
- 대상 파일:
  - `agent/langgraph_verification_logic.py`
- 작업 내용:
  - `should_re_evaluate()` 구현 (정량 조건)
    - `abs(rule_score - llm_score) >= 20`
    - `fidelity < 40`
    - 필요 시 high-severity 저점 조건
  - 재시도 제어
    - `max_retries=2` 엄격 적용
    - 재시도 시 이전 `diagnostic_log`를 컨텍스트로 전달
  - `caution` 상태 메시지 고도화
    - 규칙 사유 + LLM 보조 reasoning 결합
    - 사용자는 쉽게 이해, 내부는 원인 코드 추적 가능
- 산출물:
  - 과도한 재시도/루프 방지
  - HITL 안내 문구 품질 개선

### 4) 4차: 프롬프트/루브릭 외부화
- 대상 파일:
  - `docs/work_info/logic/scoring_rubric_v1.md` (신규)
  - `agent/langgraph_scoring.py` (로딩/주입부)
- 작업 내용:
  - `scorelogic.md` 핵심만 요약한 rubric 문서 생성
  - 시스템 프롬프트는 해당 요약본 참조 방식으로 주입
  - 버전 필드(`rubric_version`, `prompt_version`) 고정
- 산출물:
  - 정책 변경 시 코드 수정 최소화
  - 토큰 비용 안정화

### 5) 5차: 관측성/진단/API 반영
- 대상 파일:
  - `main.py` (diagnostics payload)
  - `services/*diagnostics*.py` 또는 기존 진단 경로
  - `ui/workspace.py` (필요 시 표시)
- 작업 내용:
  - 진단에 최소 필드 노출
    - `rule_score`, `llm_score`, `final_decision`, `fallback_used`, `fallback_reason`, `latency_ms`
    - `judge_skipped`, `skip_reason`, `rule_violation_summary`
  - 로그 키 표준화
    - `judge_model`, `judge_latency_ms`, `judge_retry_count`
  - 사용자 화면에는 요약만, 상세 진단은 개발자용 영역 분리
  - **Conflict UX 규칙**:
    - `abs(rule_score - llm_score) >= 20`이면 `판단 불일치 주의` 배지 표시
    - `summary_reason`에 `diagnostic_log` 핵심 1줄을 병합 노출
- 산출물:
  - 운영 중 원인 추적 가능
  - "왜 이 판정인지" 설명 가능

### 6) 6차: 테스트/검증 패키지 추가
- 대상 파일:
  - `tests/test_autonomy_regressions.py` (신규)
  - 기존 `tests/test_graph.py` 보강
- 필수 테스트 케이스:
  - 우선순위 테스트
    - LLM 점수 높아도 gate=HOLD면 최종 HOLD
  - short-circuit 테스트
    - gate=HOLD/REGENERATE일 때 LLM 호출이 실제로 생략되는지 확인
  - fallback 테스트
    - LLM timeout/parse error 강제 시 rule_score로 복구
  - conflict UX 조건 테스트
    - 점수 편차 20점 이상 시 경고 플래그/요약문 병합 여부 확인
  - 정상/의심 케이스 회귀
    - NORMAL/HOLIDAY/LIMIT/PRIVATE_USE 시 기존 판정 안정성 유지
  - 재평가 제한 테스트
    - 조건 충족 시 재시도되되 max_retries 초과 없음
- 산출물:
  - 기능 안정성 확인
  - 회귀 위험 최소화

### 7) 배포/적용 전략
- Step A: 코드 반영 후 `llm_judge_enabled=false`로 배포 (관측만)
- Step B: 내부 테스트 케이스에서만 `true` 활성(canary)
- Step C: fallback 비율/지연/판정 변동성 확인 후 점진 확대
- 모니터링 기준:
  - fallback 비율
  - 평균/95p 지연
  - 기존 대비 판정 편차
  - **Circuit Breaker**:
    - 최근 10회 중 fallback 5회(50%) 이상이면 `llm_judge_enabled`를 자동 `false` 전환
    - 운영 로그/알림에 차단 사유 기록 (`breaker_open=true`)

### 8) 롤백/복구 계획
- 즉시 롤백 스위치: `LLM_JUDGE_ENABLED=false`
- 심각 이슈 시:
  - LLM Judge 호출 경로 비활성화
  - 진단 필드는 유지하여 사후 분석
- 데이터 호환성:
  - 새 필드는 optional/기본값으로 설계하여 구버전 데이터 파손 방지

### 9) 완료 기준 (Definition of Done)
- 규칙 게이트 우선순위가 테스트로 증명됨
- LLM 실패 시 fallback 100% 동작
- `rule_score/llm_score/final_decision/fallback_reason/latency_ms` 진단 조회 가능
- 사용자 메시지는 이해 가능한 요약문으로 노출
- 기본 운영 모드에서 기존 결과와 호환(회귀 없음)

### [구현 시 주의사항]
- Short-circuit: 규칙 엔진에서 `HOLD` 또는 `REGENERATE` 판정이 확정된 경우 LLM Judge 호출을 생략하여 비용을 최적화할 것.
- Context Injection: LLM Judge 프롬프트에 규칙 엔진의 발견 사항(`Rule Violation Summary`)을 컨텍스트로 포함하여 검증력을 높일 것.
- Circuit Breaker: LLM 호출 실패율이 50%를 넘어서면 자동으로 `llm_judge_enabled=false`로 전환하는 안전장치를 고려할 것.
- Conflict UX: Rule 점수와 LLM 점수의 편차가 클 때, `diagnostic_log` 핵심 내용을 `summary_reason`에 포함시켜 사용자 경고를 줄 것.
