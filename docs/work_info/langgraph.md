# LangGraph 오케스트레이션 구조

> 이 문서는 [`agent/langgraph_agent.py`](../agent/langgraph_agent.py)와 [`agent/skills.py`](../agent/skills.py) 코드 기준으로 자동 현행화됩니다.
> UI에 표시되는 그래프(`ui/studio.py`)는 `draw_mermaid()` 출력을 파싱해 자동 생성됩니다.

---

## 1. 메인 오케스트레이션 흐름

실제 [`build_agent_graph()`](../agent/langgraph_agent.py#L733)가 등록하는 노드와 엣지입니다.

```
START
  │
  ▼
screener          Phase 0 — 전표 원시 데이터에서 케이스 유형 결정론적 분류
  │                         (HOLIDAY_USAGE / LIMIT_EXCEED / PRIVATE_USE_RISK / UNUSUAL_PATTERN / NORMAL_BASELINE)
  │                         body_evidence.case_type / intended_risk_type 전파
  ▼
intake            전표 입력 파싱 + 위험 지표 정규화 (flags 파생)
  │               LLM 생성 노트: NODE_START / NODE_END 이벤트
  ▼
planner           flags 기반 조사 계획 수립 (skill 호출 순서와 이유 결정)
  │               LLM 생성 노트: PLAN_READY 이벤트
  ▼
execute           계획된 skill을 순서대로 호출
  │               ├─ TOOL_CALL 이벤트 (호출 전)
  │               ├─ skill 실행 (아래 스킬 실행 흐름 참조)
  │               ├─ TOOL_RESULT 이벤트 (호출 후)
  │               ├─ TOOL_SKIPPED 이벤트 (조건부 생략 시)
  │               └─ SCORE_BREAKDOWN 이벤트 (전체 점수 집계)
  ▼
critic            tool_results 기반 과잉 주장·반례 검토
  │               legacy 결과 존재 여부, missing_fields, score 품질 지표 평가
  │               → recommend_hold 판정
  ▼
verify            자동 확정 가능 여부 게이트
  │               ├─ GATE_APPLIED 이벤트 (READY / HITL_REQUIRED)
  │               └─ HITL_REQUESTED 이벤트 (사람 검토 필요 시)
  │               build_hitl_request() 호출 → hitl_request 생성
  ├─ HITL 필요 시 ──▶ hitl_pause   interrupt()로 일시정지, 사용자 응답 후 같은 run으로 resume
  │                         │
  └─ 자동 확정 시 ──────────┼─────────────────────────────────────────────┐
                            ▼                                             ▼
reporter          최종 설명 문장·요약 생성
  │               LLM 생성 노트: NODE_START / NODE_END / REASONING_COMPLETE 이벤트
  ▼
finalizer         상태·점수·이력 최종 확정
  │               final_result 생성 → completed 이벤트 yield
  ▼
END
```

### 노드별 역할 요약

| 노드 | 단계 | 주요 출력 | 이벤트 | 소스 |
|---|---|---|---|---|
| `screener` | Phase 0 | `screening_result`, `intended_risk_type` | `NODE_START`, `SCREENING_RESULT` | [L182](../agent/langgraph_agent.py#L182) |
| `intake` | analyze | `flags` | `NODE_START`, `NODE_END` | [L254](../agent/langgraph_agent.py#L254) |
| `planner` | plan | `plan` (skill 호출 목록) | `PLAN_READY` | [L310](../agent/langgraph_agent.py#L310) |
| `execute` | execute | `tool_results`, `score_breakdown` | `TOOL_CALL`, `TOOL_RESULT`, `TOOL_SKIPPED`, `SCORE_BREAKDOWN` | [L364](../agent/langgraph_agent.py#L364) |
| `critic` | critique | `critique` (recommend_hold 포함) | `NODE_START`, `NODE_END` | [L493](../agent/langgraph_agent.py#L493) |
| `verify` | verify | `verification`, `hitl_request` | `NODE_START`, `GATE_APPLIED`, `HITL_REQUESTED` | [L554](../agent/langgraph_agent.py#L554) |
| `hitl_pause` | HITL | (interrupt 후 resume 시 `body_evidence.hitlResponse` 반영) | `interrupt()`로 일시정지, `Command(resume=...)`로 같은 run 재개 | [L722](../agent/langgraph_agent.py#L722) |
| `reporter` | report | `final_result` (설명 포함) | `NODE_START`, `NODE_END`, `REASONING_COMPLETE` | [L641](../agent/langgraph_agent.py#L641) |
| `finalizer` | finalize | `final_result` (완성) | `NODE_START`, `NODE_END` → `completed` yield | [L706](../agent/langgraph_agent.py#L706) |

---

## 2. 스킬 실행 흐름 (execute 노드 내부)

[`execute_node`](../agent/langgraph_agent.py#L364)는 [`planner`](../agent/langgraph_agent.py#L310)가 수립한 plan을 순회하며 [`SKILL_REGISTRY`](../agent/skills.py#L141)에서 스킬을 조회해 순서대로 호출합니다.

```
execute
  │
  ├──▶ holiday_compliance_probe     휴일/휴무/연차 사용 정황 검증
  │                                 (hr_status, occurredAt, budat, cputm 분석)
  │
  ├──▶ budget_risk_probe            예산 초과 여부 및 금액 지표 검증
  │                                 (amount vs threshold 비교)
  │
  ├──▶ merchant_risk_probe          거래처·MCC 기반 업종 위험도 검증
  │                                 (mccCode 기반 고위험 업종 분류)
  │
  ├──▶ document_evidence_probe      전표 라인아이템·문서 증거 수집
  │                                 (document.items 파싱, 첨부 여부)
  │
  ├──▶ policy_rulebook_probe        내부 규정집 관련 조항 RAG 조회
  │                                 (policy_refs, ref_count 반환)
  │
  └──▶ legacy_aura_deep_audit       [조건부] 기존 Aura 심층 분석 호출
                                    생략 조건: policy_ref_count ≥ 2
                                              AND lineItemCount > 0
                                              AND missingFields 없음
                                              AND budgetExceeded 아님
  │
  ▼
score_breakdown                     전체 결과 집계
                                    policy_score + evidence_score → final_score
```

### 스킬별 소스 링크

| 스킬 | 역할 | 생략 가능 | 소스 |
|---|---|---|---|
| `holiday_compliance_probe` | 휴일/휴무/연차 사용 정황 검증 | 아니오 | [L25](../agent/skills.py#L25) |
| `budget_risk_probe` | 예산 초과 여부 및 금액 지표 검증 | 아니오 | [L43](../agent/skills.py#L43) |
| `merchant_risk_probe` | 거래처·MCC 기반 업종 위험도 검증 | 아니오 | [L58](../agent/skills.py#L58) |
| `document_evidence_probe` | 전표 라인아이템·문서 증거 수집 | 아니오 | [L77](../agent/skills.py#L77) |
| `policy_rulebook_probe` | 내부 규정집 관련 조항 RAG 조회 | 아니오 | [L91](../agent/skills.py#L91) |
| `legacy_aura_deep_audit` | 기존 Aura 심층 분석 (specialist) | **조건부** ([`_should_skip_skill`](../agent/langgraph_agent.py#L55)) | [L106](../agent/skills.py#L106) |

---

## 3. 그래프 자동화 현황

에이전트 스튜디오에서는 **상위 오케스트레이션 그래프**(메인 노드 흐름)와 **하위 실행 스킬 그래프**(execute 노드 내부 도구 순서)를 별도 탭으로 구분해 표시합니다.

| 항목 | 방식 | 파일 |
|---|---|---|
| 메인 오케스트레이션 그래프 | `draw_mermaid()` → regex 파싱 → matplotlib PNG | [`ui/studio.py`](../ui/studio.py) |
| 스킬 실행 흐름 그래프 | 직접 networkx 구성 → matplotlib PNG | [`ui/shared.py`](../ui/shared.py) |
| 그래프 표시 | `st.image()` (브라우저 JS 불필요) | [`ui/shared.py`](../ui/shared.py) |

`langgraph_agent.py`의 노드·엣지를 수정하면 메인 오케스트레이션 그래프는 **재시작 없이 자동 반영**됩니다 (`@st.cache_resource` 캐시 초기화 시).
