# Screening 고도화 정의서 (Hybrid: LLM + 결정론 보정)

작성일: 2026-03-09
대상 프로젝트: AuraAgent

## 1) 배경
- 현재 AuraAgent 스크리닝은 `agent/screener.py`의 결정론(rule) 기반으로 즉시 분류된다.
- 기존 레퍼런스 조합(`dwp-backend + aura-platform`)은 `screen-batch`에서 LLM을 호출하고, 이후 결정론 신호로 점수/정합성을 보정하는 하이브리드 구조다.
- 목표는 AuraAgent에서도 동일한 운영 철학(LLM 유연성 + 결정론 재현성)을 확보하는 것이다.

## 2) 목표
- 목표 1: `SCREENING_MODE=hybrid`에서 LLM 초기 분류를 수행한다.
- 목표 2: 최종 `score/severity`는 결정론 규칙으로 계산해 재현성을 보장한다.
- 목표 3: LLM 오류/지연 시 자동으로 rule-only fallback 하여 서비스 연속성을 보장한다.
- 목표 4: 기본 동작을 `hybrid`로 전환하되, LLM 실패 시 rule fallback으로 회귀 리스크를 최소화한다.

## 3) 최종 결정사항 (고정 정의)
### 3.1 case_type 표준
- AuraAgent 운영 표준 case_type은 아래 5종으로 고정한다.
- `HOLIDAY_USAGE`, `LIMIT_EXCEED`, `PRIVATE_USE_RISK`, `UNUSUAL_PATTERN`, `NORMAL_BASELINE`
- 내부 소스(시나리오 생성/스크리닝/저장)는 위 5종만 사용한다.
- 레거시 값(`DEFAULT`, `NORMAL`, `DUPLICATE_SUSPECT`, `SPLIT_PAYMENT`)은 신규 생성 경로에서 사용하지 않는다.
- 런타임에서 5종 외 값은 `pre-screened`로 간주하지 않고 스크리닝을 재실행한다.

### 3.2 권한 분리
- LLM 권한: `초기 case_type 제안`, `사유 문장 생성`
- 결정론 권한: `최종 score`, `최종 severity`, `신호-분류 정합성 보정`
- 즉, "판단 유연성은 LLM", "재현성/안정성은 규칙"으로 분리한다.

### 3.3 입력 정규화 기준
- 입력 핵심 필드: `occurredAt`, `isHoliday`, `hrStatus`, `hrStatusRaw`, `mccCode`, `budgetExceeded`, `amount`
- 운영 기본 경로에서는 필드 누락 시에도 스크리닝은 실패하지 않고 보수적으로 분류한다.
- 단, 테스트 데이터 생성 경로(`seed_demo_scenarios`)에서는 `strict_required_fields=true`로 핵심 필드 누락 시 즉시 실패시켜 데이터 품질을 강제한다.

### 3.4 fallback 정책
- LLM 미설정/실패/파싱오류/타임아웃 시 `rule` 결과로 즉시 대체한다.
- fallback 시에도 API 성공 응답을 유지한다(사용자 체감 장애 방지).

## 4) 하이브리드 결정 로직
1. 결정론 신호 추출 및 점수 계산
2. LLM이 case_type과 reason 제안
3. 정합성 보정 규칙 적용
- 예: `HOLIDAY_USAGE`를 LLM이 제안했지만 휴일/휴무 신호가 없으면 rule 결과를 우선
- 예: `LIMIT_EXCEED`를 LLM이 제안했지만 예산초과 신호가 없으면 rule 결과를 우선
- 예: LLM이 `NORMAL_BASELINE` 제안해도 rule score가 충분히 높으면 rule 결과 우선
4. 최종 저장
- `case_type`: 보정 후 최종값
- `score/severity`: 결정론 계산값
- `reason_text`: LLM 사유(정합성 충족 시) 또는 결정론 사유

## 5) 운영 설정값
- `SCREENING_MODE=rule|hybrid` (기본 `hybrid`)
- `SCREENING_LLM_MODEL` (기본 `gpt-5`)
- `SCREENING_LLM_FALLBACK_MODEL` (기본 `gpt-5`)
- `SCREENING_LLM_TEMPERATURE` (기본 `0`)
- `SCREENING_LLM_MAX_TOKENS` (기본 `220`)
- `SCREENING_LLM_TIMEOUT_SECONDS` (기본 `8`)
- `SCREENING_LLM_OVERRIDE_MIN_CONFIDENCE` (기본 `0.75`)

## 6) 성능/안정성 기준 (진행 전 합의)
- SLO(권장): 단건 스크리닝 p95 2초 이내 (LLM 실패 fallback 포함)
- LLM 실패 허용: 실패 시 즉시 rule fallback, 전체 요청 실패로 전파 금지
- 비용 제어: `max_tokens` 제한 + 필요 시 특정 테넌트만 hybrid 활성화

## 7) 관측성/로그 기준
- 아래 항목을 로그에 남긴다.
- `screening_mode`, `screening_source`, `llm_case_type`, `final_case_type`, `hybrid_case_align_reason`
- 운영 대시보드에서는 fallback 비율과 정합성 보정 비율을 추적한다.

## 8) 롤아웃 계획
### Phase 1 (완료 목표: 코드 반영)
- 플래그 기반 하이브리드 엔진 추가
- 기본값 hybrid 전환
- LangGraph screener 노드에서 스크리닝 호출을 thread 오프로딩

### Phase 2 (운영 검증)
- 운영 시나리오에서 fallback 비율/분류 안정성 모니터링
- rule vs hybrid 결과 비교(분류 차이율, fallback 비율, 응답시간)

### Phase 3 (점진 배포)
- 시연 테넌트/시나리오별 임계값(`SCREENING_LLM_OVERRIDE_MIN_CONFIDENCE`) 튜닝
- KPI 안정 시 rule 모드 사용 범위 축소 여부 판단

## 9) 진행 가능 여부 판단
결론: **진행 가능(Go)**

근거:
- 기존 코드 구조에서 플래그 기반 분기 적용이 용이함
- 실패 시 rule fallback으로 안정성 확보 가능
- LLM 실패 시 rule fallback으로 기존 시연/운영 영향 최소화 가능

진행 조건:
- OpenAI/Azure 키 미설정 환경에서도 정상 fallback이 동작해야 함
- 기존 API 응답 스키마(`case_type`, `severity`, `score`, `reason_text`)는 유지해야 함
- `AgentCase` 저장 필드 호환성(문자열 case_type, 수치 score)은 기존과 동일해야 함

## 10) 리스크 및 대응
- 리스크: LLM 응답 형식 이탈(JSON 불일치)
- 대응: 파서 실패 시 rule fallback

- 리스크: LLM 지연으로 워크플로우 체감 지연
- 대응: timeout + fallback + thread 오프로딩

- 리스크: 유형 체계 불일치(DEFAULT/기타)
- 대응: 레거시 값은 pre-screened로 인정하지 않고 재스크리닝하며, LLM 출력은 5종 외 값을 `UNUSUAL_PATTERN`으로 정규화


#스크리닝 고도화

이슈 : 스크리닝도 단순 LLM 호출이 아니라 현재 이 프로세스 + 더 고도화된 프로세스를 넣어서 Langgraph로 구현 할수 있나요 고도화 관련하여 질문하는겁니다. 우리 프로젝트는 agentic ai 의 표본이 되기 위해 만들고 있으므로 이 취지에 맞췄으면 합니다.좋은 아이디어를 먼저 작성해주세요 작업은 이후 결정되면 하는게 좋을거같아요

아이디어 :
Dual-Lane Screening Graph로 분리
Fast lane: 현재 규칙/신호 기반 + 기본 LLM 분류 (저지연)
Deep lane: 불확실/충돌/고위험 건만 LangGraph 다단계 추론으로 승격
장점: 데모에서는 “에이전트성”을 보여주고, 운영에서는 속도/비용 통제 가능
스크리닝을 “다중 가설” 구조로 변경
단일 case_type 1개만 뽑지 말고 Top-3 가설 + 확률/근거를 생성
이후 노드에서 반례 검토 후 최종 1개 확정
장점: 왜 그렇게 분류됐는지 설명력이 크게 올라감
권장 LangGraph 노드 구성
intake_normalize (입력 정규화/품질체크)
policy_retrieve (관련 규정 조항 회수)
hypothesis_generate (LLM 다중 가설 생성)
evidence_probe (필요 도구 호출)
counter_example_check (반례/정상 시나리오 검증)
rule_guardrail (결정론 보정)
confidence_calibrate (신뢰도 보정)
finalize_screening (최종 타입/점수/설명/다음액션)
결과 스키마를 강화
final_case_type, alt_hypotheses, confidence, uncertainty_reason
decision_path(어느 노드에서 어떤 이유로 바뀌었는지)
evidence_refs, guardrail_actions, needs_hitl
운영/품질 루프 추가
사람 검토 결과를 screening_feedback으로 저장
오분류 패턴 자동 리포트(정상군 과탐/누락탐지)
프롬프트·가드레일 버전별 A/B 비교
추천 방향은 1번+3번 조합입니다.
즉, “대부분은 빠르게, 애매한 건은 진짜 에이전트처럼 깊게”가 가장 설득력 있습니다.




# 아이디어별 의견
1) Dual-Lane (Fast / Deep) — 강하게 추천
Fast lane: 현재 hybrid(규칙+LLM) 그대로 두고, 저지연·저비용 유지.
Deep lane: “애매한 건만” LangGraph로 올려서 다단계 추론(가설 → 증거/반례 → 보정 → 확정) 수행.
의견:
PoC/데모에서는 “스크리닝도 에이전트처럼 판단·검증한다”는 메시지를 보여주기 좋고,
운영에서는 대량 건은 Fast로 처리해 비용·지연을 통제할 수 있어서, 설계 목표(에이전트성 + 운영 가능성) 와 잘 맞습니다.
구현 시 라우터 노드 하나만 잘 정의하면 됩니다.
예: rule 점수 + LLM confidence(또는 불일치 여부)로 “Deep 승격” 조건을 문서에 명시해 두면, 나중에 튜닝·모니터링하기 좋습니다.
2) 다중 가설(Top-3 + 반례 검토) — 방향 좋음, 범위는 단계적으로
단일 case_type 1개가 아니라 가설 여러 개 + 확률/근거 → 반례 검토 후 1개 확정은, “왜 이 유형인지” 설명력을 크게 올려줍니다.
의견:
개념적으로 문서와 잘 맞고, Deep lane 안에서만 먼저 적용하는 게 부담이 적습니다.
Fast lane은 기존처럼 단일 분류 유지하고, Deep에서만 alt_hypotheses + decision_path를 채우는 식으로 가면, 스키마·API 변경을 최소화할 수 있습니다.
3) 권장 LangGraph 노드 구성 — 그대로 채택해도 무방
intake_normalize → policy_retrieve → hypothesis_generate → evidence_probe → counter_example_check → rule_guardrail → confidence_calibrate → finalize_screening 흐름은:
입력 정규화 → 규정/증거 수집 → 가설 생성 → 반례/검증 → 규칙 보정 → 신뢰도 보정 → 최종 출력으로, 에이전트성(도구 호출, 검증, 보정)을 잘 드러냅니다.
의견:
지금 메인 오케스트레이션(screener → intake → planner → …)과 역할이 겹치지 않도록,
“스크리닝 전용 서브그래프” 로 두고, 메인에서는 “screener 노드 = 이 그래프 invoke” 수준으로 두는 게 정리하기 좋습니다.
policy_retrieve는 기존 policy_rulebook_probe/규정 검색을 재사용하면 중복을 줄일 수 있습니다.
4) 결과 스키마 강화 — 단계적 확장 권장
final_case_type, alt_hypotheses, confidence, uncertainty_reason, decision_path, evidence_refs, guardrail_actions, needs_hitl 등은 감사·설명용으로 매우 유용합니다.
의견:
Phase 1에서는 기존 API/DB 스키마(case_type, severity, score, reason_text)를 유지하고,
추가 필드는 optional로 두어, Deep lane 또는 “상세 모드”에서만 채우는 식이면, 기존 호환성과 점진적 고도화를 동시에 만족시킬 수 있습니다.
5) 운영/품질 루프 — 롤아웃 후 Phase로 두기 좋음
screening_feedback, 오분류 패턴 리포트, 프롬프트·가드레일 A/B는 운영 단계에서 가치가 큽니다.
의견:
문서의 Phase 2(운영 검증) / Phase 3(점진 배포) 이후에, “스크리닝 품질 루프”로 넣는 게 부담이 적습니다.
먼저 Dual-Lane + 노드 구성으로 “에이전트성”을 보여주고, 로그/관측성(9)만 잘 쌓아두면, 이후 피드백 스키마·대시보드는 그때 설계해도 늦지 않습니다.
정리 및 추천 순서
추천 방향: 문서대로 1번(Dual-Lane) + 3번(권장 노드) 를 골간으로 가져가고,
2번(다중 가설) 은 Deep lane 안에서만,
4번(스키마 강화) 는 optional 확장으로,
5번(운영 루프) 는 Phase 2/3 이후로 두는 구성이 무난해 보입니다.
구현 순서 제안:
1) Fast/Deep 라우터 + Deep lane용 스크리닝 서브그래프 (권장 노드로 구성)
2) Deep 결과에 대한 스키마 확장(optional) 및 decision_path/alt_hypotheses 채우기
3) 운영/품질 루프는 로그·관측성 확보 후 설계


# 아이디어별 의견에 대한 답변
다음은 위 아이디어를 **스크리닝 업무 난이도에 맞게 경량화**하여 머지한 최종 초안이다.
목표는 "단순 분류 성능 + 설명 가능성 + 운영 가능성"이며, 과도한 복잡도는 제외한다.

## A) 최종 방향 (경량 Agentic Screening)
- 기본은 Fast lane(현재 hybrid)로 처리하고, Deep lane은 "애매한 건"에만 제한적으로 적용한다.
- 스크리닝은 분류 단계이므로 Deep 비율 상한을 둔다(권장: 전체의 20% 이내).
- 메인 분석 그래프와 역할 중복을 피하기 위해, Deep lane은 "스크리닝 전용 서브그래프"로 분리한다.
- 실패/지연 시 즉시 Fast 결과로 fallback하여 사용자 체감 장애를 방지한다.

## B) Deep 승격 기준 (초기 고정안)
- 아래 조건 중 하나라도 만족하면 Deep lane으로 승격:
- `rule_case_type != llm_case_type`
- `llm_confidence < 0.75`
- `final_score` 경계구간(권장: 45~65)
- `NORMAL_BASELINE`인데 위험 신호 2개 이상 동시 충족(과탐/누락 방어 목적)
- 위 조건 외에는 Fast 결과를 최종 확정한다.

## C) 노드 구성 (경량 버전)
- Fast lane: 현재 `run_screening` 유지 (LLM 제안 + 규칙 가드레일)
- Deep lane(4노드 우선):
- `intake_normalize`: 입력 정규화/필수 필드 체크
- `hypothesis_generate`: LLM 가설 생성(Top-2)
- `rule_guardrail`: 결정론 보정/모순 차단
- `finalize_screening`: 최종 case_type/score/reason 확정
- 확장 노드(`policy_retrieve`, `evidence_probe`, `counter_example_check`)는 Phase 2에서 조건부 추가한다.

## D) 출력 스키마 (호환 우선)
- 필수 응답은 기존 유지:
- `case_type`, `severity`, `score`, `reason_text`
- 확장 정보는 optional JSON으로만 추가:
- `screening_meta.alt_hypotheses` (Deep만)
- `screening_meta.decision_path` (Deep만)
- `screening_meta.uncertainty_reason` (필요 시)
- 기존 DB/API 호환성을 깨지 않는다.

## E) 성능/운영 기준 (스크리닝 난이도 기준)
- p95 지연 목표:
- Fast 1.5초 이내
- Deep 3.0초 이내
- Deep 호출 비율:
- 20% 이내 유지(초기 목표)
- fallback 허용 정책:
- LLM 실패/타임아웃 시 100% Fast 확정
- 비용 정책:
- Deep lane max tokens/timeout 별도 제한

## F) UI/설명 기준
- 기본 화면(판단 요약)은 단순 유지:
- 최종 케이스, 점수, 간단 사유만 표시
- 상세 설명은 expander에서만 표시:
- 가드레일 적용 이유(`align_reason`)
- Deep 승격 이유(왜 Deep로 갔는지)
- Top-2 가설(Deep인 경우만)

## G) 구현 단계 (확정안)
1. Phase 1 (바로 개발 가능)
- Deep 승격 라우터 추가
- Deep 경량 4노드 서브그래프 추가
- optional `screening_meta` 저장/응답
- 로그 지표 추가(승격 이유, fallback 이유, 최종 align_reason)

2. Phase 2 (운영 검증)
- Deep 비율/정확도/과탐률/지연 모니터링
- 임계값(`confidence`, 경계 점수 구간) 튜닝
- 필요 시 `policy_retrieve`만 우선 추가

3. Phase 3 (확장)
- 반례 검토 노드 추가
- screening_feedback 및 주간 오분류 리포트
- 프롬프트/가드레일 버전 비교 실험

## H) 착수 전 체크리스트 (Yes면 개발 시작)
- Deep 승격 임계값 확정 (`0.75`, `45~65`, 비율 상한 `20%`)
- optional 필드 저장 위치 확정 (`screening_meta` JSON)
- UI 표시 범위 확정 (기본/상세 분리)
- 성능 상한 확정 (Fast 1.5s, Deep 3.0s)
- fallback 정책 확정 (실패 시 Fast 고정)

위 체크리스트 합의가 끝나면, 구현 착수 가능한 수준이다.

**PoC 전제 시 Phase 1 범위 정리**  
스크리닝을 PoC에서 끝낸다는 전제라면 Phase 1만 구현하면 충분하다. 이때 다음만 정해 두면 과잉/누락이 없다.
- **20% 비율**: Phase 1에서는 **로깅만** 하고, 상한 강제(초과 시 강제 Fast)는 구현하지 않아도 됨. PoC 데모·검증에 로그로 확인하면 충분.
- **screening_meta 저장**: PoC에서는 **응답에만 포함**하고 DB 컬럼 추가 없이 가도 됨. 상세 설명(expander)은 당 요청 응답 기준으로 표시.
- **UI(F)**: 기본 요약 + expander(승격 이유, Top-2 가설, align_reason)는 Phase 1에서 구현. API가 `screening_meta`를 내려주면 됨.
- **테스트**: 라우터(승격 조건)와 fallback(Deep 실패 시 Fast 반환) 시나리오만 최소 테스트로 포함하면 PoC 완결에 유리함.

---

## 검토 의견 (구현 전 리뷰)

전체적으로 **경량 Agentic Screening** 방향이 문서·코드와 잘 맞고, Phase 1 착수 가능한 수준으로 정리되어 있다. 아래는 보완 시 구현 시 혼선을 줄이기 위한 명확화 제안이다.

### 잘 맞는 부분
- **A)** Fast 우선 + Deep 제한적 + 서브그래프 분리 + fallback 정책이 한 세트로 정리되어 있음.
- **C)** 4노드( intake → hypothesis → rule_guardrail → finalize )로 Deep 범위가 스크리닝에 맞게 제한됨.
- **D)** 필수 응답은 기존 `case_type`/`severity`/`score`/`reason_text` 유지, 확장은 optional로 호환성 유지.
- **E)** p95 목표(Fast 1.5s, Deep 3.0s), fallback(실패 시 Fast 확정)이 현재 플로우(먼저 Fast 실행 후 Deep 여부 결정)와 부합함.
- **G)** Phase 1→2→3 단계가 명확하고, 확장 노드는 Phase 2 이후로 미루는 것이 합리적임.

### 보완 권장 (명확화만, 설계 변경 아님)

| 구간 | 내용 | 제안 |
|------|------|------|
| **B) Deep 승격 기준** | "위험 신호 2개 이상" | **위험 신호 정의**를 문서에 한 줄로 고정할 것. 예: `is_holiday`, `is_leave`, `is_night`, `budget_exceeded`, `mcc_*` 등 규칙에서 쓰는 신호 중 True인 개수 ≥2. (현재 `agent/screener.py`의 `_derive_case_type`/신호 집합과 동일하게 맞추면 됨.) |
| **B)** | "경계구간(권장: 45~65)" | **구간 포함 여부** 명시 권장: 예) `45 ≤ final_score ≤ 65` (이미 0~100 정수 스케일이라고 가정). |
| **D) / H)** | `screening_meta` 저장 위치 | 현재 **AgentCase**에는 `case_type`, `severity`, `score`, `reason_text`만 있고 `screening_meta` 컬럼 없음. 따라서 **저장 위치 확정** 시 다음 중 하나를 선택해 문서에 적어 두는 것이 좋음: (1) AgentCase에 optional `screening_meta` JSON/JSONB 컬럼 추가, (2) 기존 `agent_activity_log` 등에 메타데이터로 저장, (3) 당 요청 응답에만 포함하고 DB에는 미저장(재조회 시 상세 비표시). |
| **E) 20% 비율** | "Deep 호출 비율 20% 이내" | Phase 1에서 **측정만** 할지, **상한 강제(예: 초과 시 강제 Fast)** 까지 할지 정해 두면 좋음. 강제 시 "최근 N건 중 Deep 비율" 정의와 윈도우 크기(N) 필요. |

### 구현 시 확인할 점
- **라우터 입력**: Deep 승격 판단을 위해 Fast(현재 hybrid) 1회 실행 결과(`rule_case_type`, `llm_case_type`, `llm_confidence`, `final_score`, baseline 여부·위험 신호 개수)가 필요함. 현재 `run_screening` 내부에서 이미 산출 가능하므로, 라우터는 그 결과를 인자로 받으면 됨.
- **Fallback**: Deep 타임아웃/실패 시 이미 확보한 Fast 결과를 그대로 반환하면 되므로, 정책(E)과 일치함.

**결론**: 위 보완만 반영하면 **현재 초안 기준으로 구현 착수해도 무방**하다. H 체크리스트의 "optional 필드 저장 위치 확정"에 `screening_meta`의 **저장소(테이블·컬럼 또는 비저장)** 를 명시하면 Phase 1 개발 시 결정이 일관되게 유지된다.
