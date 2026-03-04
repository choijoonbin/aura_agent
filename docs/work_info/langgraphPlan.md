# AuraAgent LangGraph / LangChain 구현 로드맵

> 기준 문서: `docs/langgraph-langchain-comparison.md`
> 목적: 기준 문서를 실제 구현 순서로 번역한 작업계획서
> 원칙: 한 번에 전면 교체하지 않고, PoC 안정성을 유지하면서 단계적으로 리팩토링한다.

### 공식 문서 대조 검토 요약

- **방향·목표·금지 사항**: 공식 문서와 일치함.
- **Phase 구성**: 공식은 A～E 5단계, 본 로드맵은 Phase 0(준비) + A～H로 세분화. 공식의 B를 “structured output”과 “ToolNode 전환”으로 나누고, Persistence·RAG·UI·관찰지표를 각각 E～H로 둔 구조이며, **공식 문서 4.2·8.10이 최종 기준**이다.
- **반영한 수정**: 참고 원본 소스 경로를 공식 11과 동일하게 수정, Phase D/F 완료 기준에 수치형 목표(95%, 90%) 추가, 테스트 전략·안티패턴은 공식 문서 참조로 명시.

---

## 1. 작업 원칙

- 이 문서는 구현 순서와 범위를 고정하는 실행 로드맵이다.
- 세부 기술 기준은 `docs/langgraph-langchain-comparison.md`를 따른다.
- 구현 중 판단 충돌 시:
  1. `langgraph-langchain-comparison.md`
  2. `langgraphPlan.md`
  순서로 우선 적용한다.
- 로직 변경 시 Streamlit UI 대응은 필수다.
- 신규 기능은 registry direct call 방식으로 추가하지 않는다.
- raw chain-of-thought는 어떤 경우에도 노출하지 않는다.

---

## 2. 현재 상태 요약

현재 AuraAgent는 다음 상태다.

- LangGraph 기반 메인 그래프가 존재하며, screener → intake → planner → execute → critic → verify → hitl_pause/reporter → finalizer 흐름으로 동작한다.
- **execute** 단계는 **LangChain tool 호출 루프**로 동작한다. `get_langchain_tools()`로 취득한 tool맵과 plan 기반으로 `SkillContextInput` → `tool.ainvoke()`만 사용하며, registry direct dispatch는 제거된 상태이다.
- **planner / critic / verifier / reporter**는 **structured output 스키마**(`agent/output_models.py`)를 사용하며, 각 노드가 해당 모델을 생성·`model_dump()`로 state에 저장한다. 다만 LLM이 스키마에 직접 바인딩되어 생성하는 구조는 아니고, 코드에서 모델 객체를 조립하는 transitional 구현이다.
- **HITL**은 **verify → hitl_pause/reporter 조건 분기**로 동작하며, HITL 필요 시 `hitl_pause` 노드에서 `interrupt()`로 일시정지한다. 재개 시 **같은 run_id(thread_id)**로 `Command(resume=...)` 호출하여 reporter로 이어지며, `checkpointer=MemorySaver()`를 사용한다. API 응답의 `resumed_run_id`는 동일 run_id를 그대로 반환한다. (`docs/langgraphPlan2.md` Phase D 결정 기준: 정식 HITL 적용.)
- UI는 에이전트 대화(라이브 스트림) / 사고 과정 / 실행 로그 / 결과 / 스튜디오 / RAG 라이브러리 / 시연 데이터 제어를 갖추고 있으며, run 단위 diagnostics API(`GET .../diagnostics`)로 관찰 지표를 확인할 수 있다.
- 잔여 작업은 문서 현행화, 테스트 전략 구현, Phase F/H 고도화, 발표용 UX 마감 등으로 `docs/langgraphPlan2.md` merge 목록에 정리되어 있다.

---

## 3. 최종 목표

최종 목표는 아래 8개를 만족하는 것이다.

1. LangGraph = 상태 그래프와 실행 제어
2. LangChain = 모델, tool, structured output 계층
3. Tool = LangChain `@tool` 또는 동등한 tool 객체
4. Execute = ToolNode 또는 동등한 tool-calling loop
5. Planner / Critic / Verifier / Reporter = structured output 노드
6. Verifier = interrupt / resume 기반 HITL
7. Streaming = orchestration stream + reasoning note stream 분리
8. Persistence = checkpoint + final result + event log 분리

---

## 4. 구현 범위

### 4.1 포함 범위

- agent/ 내부 LangGraph 런타임 리팩토링
- skills/tool 계층 정식화
- structured output 도입
- HITL interrupt / resume 정식화
- RAG retrieval 정교화
- Streamlit UI의 스트림/리뷰/스튜디오 표시 개선
- 저장 구조 정리 (run / event / final result)

### 4.2 제외 범위 (현 단계 Non-goals)

- 외부 MCP 서버 전면 도입
- vector DB 교체
- production-grade 멀티 인스턴스 orchestration
- multi-agent federation 전면 적용
- 기존 Java BE API 구조 재현

---

## 5. 구현 순서

## Phase 0. 준비 및 안전장치

- [x] Phase 0 시작
- [x] 현재 LangGraph/UI 연결 지점 재확인
- [x] smoke test 시나리오 정리
- [x] 최소 회귀 체크리스트 작성
- [x] Phase 0 완료

**산출물**: [`docs/phase0-prep.md`](phase0-prep.md) — 연결 지점·transitional 경로·smoke 시나리오·회귀 체크리스트. 대상 파일에 transitional 주석 추가됨.

### 목표
현재 PoC를 깨지 않도록 리팩토링 안전장치를 먼저 만든다.

### 작업
- 현재 LangGraph 흐름과 UI 연결 지점을 문서 기준으로 재확인 → phase0-prep.md Section 1
- 기존 `SKILL_REGISTRY` 경로를 transitional path로 명시 → phase0-prep.md Section 2, 코드 주석 추가
- 주요 경로 smoke test 정리 → phase0-prep.md Section 3
- 최소 회귀 체크리스트 작성 → phase0-prep.md Section 4

### 대상 파일
- `agent/langgraph_agent.py` (transitional 주석 추가)
- `agent/skills.py` (transitional 주석 추가)
- `ui/workspace.py`
- `ui/studio.py` (transitional 주석 추가)
- `main.py`

### 완료 기준
- 현재 기능을 유지한 채 다음 Phase를 진행할 수 있어야 한다.
- 최소 smoke test 시나리오가 문서화되어 있어야 한다. → **phase0-prep.md로 문서화 완료. 1회 smoke 실행 후 Phase A 착수 권장.**

### 점검 내용
- PASS
- `docs/phase0-prep.md` 존재 확인
- LangGraph ↔ UI 연결 지점, transitional path, smoke test, 회귀 체크리스트가 모두 문서화되어 있음
- 특이사항: 실제 smoke test 실행 결과를 이 문서 자체에는 남기지 않았으므로, 구현 진행 중 1회 수동 검증 로그 확보 권장

---

## Phase A. Tool 정식화

- [x] Phase A 시작
- [x] capability -> LangChain tool schema 정의
- [x] tool 설명/docstring 정비
- [x] registry를 tool 등록용으로 제한
- [x] Phase A 완료


### 목표
현재 skills를 LangChain tool 계약으로 승격한다.

### 작업
- `agent/skills.py`의 각 capability를 LangChain tool 객체로 정식화
- 각 tool에 이름, 설명, 타입힌트, 입력 schema, 결과 schema 부여
- tool 설명은 planner가 선택 가능하도록 충분히 구체적으로 작성
- transitional design으로 registry는 일시 유지하되, registry는 tool 등록용으로만 사용

### 대상 파일
- `agent/skills.py`
- `agent/tool_schemas.py` 또는 동등 파일 신규 생성 가능
- `agent/docs/` 또는 `docs/` 내 tool 설명 문서 업데이트

### 완료 기준
- 모든 실행 capability가 LangChain tool schema를 갖는다.
- 신규 capability는 registry direct call 방식으로 추가되지 않는다.

### 점검 내용
- PASS
- `agent/skills.py`에서 `StructuredTool` 사용 확인
- `agent/tool_schemas.py`에서 `SkillContextInput`, `ToolResultEnvelope` 정의 확인
- `get_langchain_tools()` 경로 존재 확인
- 특이사항:
  - 현재 tool 입력 schema는 정식화됐으나, 출력은 `ToolResultEnvelope`로 문서화된 수준이며 실제 `StructuredTool` 반환 타입 강제까지는 들어가 있지 않음
  - 이는 Phase A 범위에서는 허용 가능하나, Phase C 이전에 tool 결과 schema 검증 지점을 추가하는 것이 바람직함

---

## Phase B. Structured Output 정식화

- [x] Phase B 시작
- [x] planner output schema 정의
- [x] critic output schema 정의
- [x] verifier output schema 정의
- [x] reporter output schema 정의
- [x] UI schema 렌더 확인
- [x] Phase B 완료


### 목표
planner / critic / verifier / reporter를 schema 기반 노드로 전환한다.

### 작업
- planner output 스키마 정의
- critic output 스키마 정의
- verifier output 스키마 정의
- reporter output 스키마 정의
- 자유문장 기반 로직을 structured output 기반으로 변경
- LLM 호출부가 schema validation 실패 시 안전하게 fallback 하도록 설계

### 대상 파일
- `agent/langgraph_agent.py`
- `agent/reasoning_notes.py`
- `agent/output_models.py` 신규 생성 가능

### 완료 기준
- planner / critic / verifier / reporter가 자유문장만 반환하지 않는다.
- UI는 schema 기반 결과를 정상 렌더링한다.

### 점검 내용
- PASS
- `agent/output_models.py`에서 `PlannerOutput`, `CriticOutput`, `VerifierOutput`, `ReporterOutput` 정의 확인
- `agent/langgraph_agent.py`에서 각 노드가 위 모델을 생성하고 `model_dump()`로 state에 저장하는 경로 확인
- 특이사항:
  - 현재는 “structured output 스키마 정의 및 state 저장”은 완료되었음
  - 다만 planner / critic / verifier / reporter가 **LLM structured output binding**으로 직접 생성되는 구조는 아니고, 현재는 코드에서 모델 객체를 조립하는 transitional 구현임
  - 따라서 Phase B는 완료로 볼 수 있으나, `docs/langgraph-langchain-comparison.md` 기준의 정석 적용은 Phase C 이후 추가 리팩토링이 필요함

---

## Phase C. Execute 단계 ToolNode 전환

- [x] Phase C 시작
- [x] execute 책임 축소
- [x] planner output 기반 tool selection 적용
- [x] ToolNode/tool-calling loop 도입
- [x] registry direct dispatch 제거
- [x] Phase C 완료


### 목표
`execute`에서 registry 직접 호출을 제거하고 tool-calling loop로 전환한다.

### 작업
- `execute` 단일 노드의 책임 축소
- planner output 기반 tool selection 적용
- ToolNode 또는 동등한 정식 tool-calling loop 도입
- tool result를 state에 합치는 규칙 정리
- 필요 시 `tool_router` / `execute_loop` 보조 노드 분리

### 대상 파일
- `agent/langgraph_agent.py`
- `agent/skills.py`
- `main.py`(필요 시 event payload 반영)
- `ui/workspace.py`(tool trace 표시 조정)

### 완료 기준
- execute에서 registry direct dispatch 제거
- tool 실행 경로가 LangGraph / LangChain 정식 패턴으로 정리됨

### 점검 내용
- 특이사항 있음
- `agent/langgraph_agent.py`의 `build_agent_graph()`에는 `ToolNode` 또는 `tools_condition` 기반 실행 경로가 없음
- `execute_node()` 내부에 여전히 registry direct dispatch가 남아 있음
  - 코드 주석에도 `Phase C 완료 시 ToolNode 전환으로 제거`라고 명시되어 있음
  - `SKILL_REGISTRY.get(...)`, `skill.handler(...)` 방식의 직접 호출 흐름이 유지됨
- `get_langchain_tools()`는 추가되었으나, 실제 LangGraph 실행 그래프에 ToolNode 바인딩으로 연결되지는 않음
- **재검토 반영:** 현재 코드 기준 registry direct dispatch 제거됨. execute_node는 get_langchain_tools() 기반 tool.ainvoke() 루프만 사용. **결론: Phase C 완료로 확정.** (docs/langgraphPlan2.md 잔여작업 merge 반영.)

### 점검 내용 답변
- 현재 구현: execute_node는 get_langchain_tools() 기반 tool.ainvoke() 루프만 사용. registry direct dispatch 제거됨. Phase C 완료.

---

## Phase D. HITL interrupt / resume 정식화

- [x] Phase D 시작
- [x] interrupt 조건 정의
- [x] human input request payload 정의
- [x] resume path 구현
- [x] UI HITL 상태 반영
- [x] Phase D 완료


### 목표
verify 단계에서 사람 개입을 LangGraph 패턴으로 정식 반영한다.

### 작업
- verifier에서 interrupt 발생 조건 정리
- human input request payload 정의
- resume path 정의
- HITL 응답 후 reporter 재실행 경로 확정
- UI에서 HITL 요청/응답/재개 상태 명확히 표시

### 대상 파일
- `agent/langgraph_agent.py`
- `agent/hitl.py`
- `main.py`
- `ui/workspace.py`
- `services/runtime_persistence_service.py`

### 완료 기준
- verifier가 interrupt / resume 기반으로 동작한다.
- HITL 후 재개 성공률을 측정 가능하다 (목표: 95% 이상, 공식 4.2 Phase C 참고).

**Phase D 결정 기준:** 정식 HITL vs Transitional 유지 판정은 `docs/langgraphPlan2.md`의 "Phase D 결정 기준"을 따른다. 현재는 **정식 HITL(same-run interrupt/resume + checkpointer)** 적용 상태이다.

### 점검 내용
- `build_agent_graph()`에 `workflow.compile(checkpointer=_get_checkpointer())` 적용. PoC는 `MemorySaver`.
- `hitl_pause_node`에서 `interrupt(hitl_request)` 호출로 일시정지. 재개 시 `Command(resume=hitl_payload)`로 같은 thread에서 reporter로 이어짐.
- `main.py`의 `POST .../hitl`은 **새 run을 생성하지 않고** 동일 `run_id`로 `_run_analysis_task(..., resume_value=...)`를 호출하여 같은 run에서 재개. API 응답의 `resumed_run_id`는 동일 run_id를 그대로 반환.
- **결론: Phase D는 정식 LangGraph interrupt/resume + checkpointer 기준으로 완료.**

### 점검 내용 답변
- 현재 구현: verify → `hitl_pause`(interrupt) → 사용자 응답 → **같은 run_id(thread_id)**로 `graph.astream(Command(resume=...), config)` 재호출 → hitl_pause 노드에서 hitl_response 반영 후 reporter → finalizer. UI·diagnostics에서 hitl_requested / resume_success 측정 가능.

---

## Phase E. Persistence / Event Log 정리

- [x] Phase E 시작
- [x] checkpoint 저장 방식 정리
- [x] event log / final result 분리
- [x] latest / history 동작 정의
- [x] Phase E 완료


### 목표
실행 상태, 이벤트, 최종 결과를 분리 저장한다.

### 작업
- checkpoint 저장 방식 정리
- orchestration event log와 final result 저장 분리
- 기존 저장 테이블과 projection 관계 정리
- 같은 케이스 반복 분석 시 latest / history 동작 정의

### 대상 파일
- `services/runtime_persistence_service.py`
- `services/persistence_service.py`
- `main.py`
- `docs/aura_db.md` 참조하여 DB 매핑 점검

### 완료 기준
- latest 결과와 history가 명확히 구분된다.
- 이벤트 로그와 최종 분석 결과가 분리 저장된다.

### 점검 내용
- PASS (부분 특이사항 포함)
- `services/runtime_persistence_service.py`
  - `agent_activity_log`에 이벤트 저장
  - latest/history 조회 함수 존재
- `services/persistence_service.py`
  - `case_analysis_result`에 최종 결과 저장
- `main.py`
  - `analysis/latest`, `analysis/history`, `analysis-runs/{run_id}/events` 조회 경로 존재
- latest 결과와 history 조회는 API 기준으로 분리되어 있음
- 이벤트 로그와 최종 분석 결과 저장도 코드상 분리되어 있음
- 특이사항:
  - 문서 목표 중 `checkpoint 저장 방식 정리`는 아직 확인되지 않음 (`MemorySaver`, `SqliteSaver` 등 없음)
  - 따라서 **Persistence 분리 저장은 완료**, **checkpoint 관점의 정식 정리는 미완료**

---

## Phase F. RAG / Retrieval 고도화

- [x] Phase F 시작
- [x] query rewrite 개선
- [x] parent/child retrieval 강화
- [x] rerank 검토/적용
- [x] citation binding 강화
- [x] evidence verification 적용
- [x] Phase F 완료 (baseline 완료, 고급화 잔여)


### 목표
규정 근거 검색을 hierarchical retrieval + evidence verification 방향으로 정리한다.

### 작업
- query rewrite 개선
- parent/child chunk 활용 강화
- metadata filter 정비
- rerank 단계 추가 가능성 검토
- sentence-level citation binding 강화
- evidence verification 적용

### 대상 파일
- `services/policy_service.py`
- `agent/langgraph_agent.py`
- `ui/rag.py`
- `services/rag_chunk_lab_service.py`

### 완료 기준
- sentence-level citation coverage를 측정 가능해야 한다 (목표: 90% 이상, 공식 4.2 Phase D 참고).
- 규정 근거가 reporter output에 구조적으로 연결되어야 한다.

### 점검 내용
- PASS (특이사항 포함)
- `services/policy_service.py`
  - `query_rewrite_for_retrieval()` 존재
  - parent/child chunk 메타 활용 및 `hierarchical_keyword_rerank` 경로 존재
- `agent/output_models.py`
  - `Citation`, `ReporterSentence`, `ReporterOutput` 모델 정의 존재
- `agent/langgraph_agent.py`
  - reporter 단계에서 citation을 구조화된 output으로 연결하는 흐름 확인
- `services/citation_metrics.py`
  - sentence-level citation coverage 계산 경로 존재
- 특이사항:
  - 현재 rerank는 규칙 기반 hierarchical rerank 중심이며, cross-encoder/LLM rerank는 아직 아님
  - evidence verification은 일부 반영됐으나 별도 독립 검증 계층으로 완전히 분리되지는 않음
- 결론: **Phase F는 baseline 완료. cross-encoder/LLM rerank, evidence verification 독립 계층 등 고급화는 잔여.**

---

## Phase G. UI 대응

- [x] Phase G 시작
- [x] 라이브 스트림/리뷰/결과 구분
- [x] 에이전트 스튜디오 런타임 정보 반영
- [x] RAG 라이브러리 설명/실험 강화
- [x] Phase G 완료


### 목표
리팩토링된 agent 구조가 UI에서 왜곡 없이 보이도록 한다.

### 작업
- `에이전트 대화` = 라이브 reasoning note stream
- `사고 과정` = 실행 후 구조화 리뷰
- `실행 로그` = orchestration event log
- `결과` = verdict + score + citation + 검증 메모
- `에이전트 스튜디오` = 실제 runtime graph / tools / prompt / model 정보 반영
- `규정문서 라이브러리` = retrieval / chunk 실험과 설명 강화

### 대상 파일
- `ui/workspace.py`
- `ui/studio.py`
- `ui/rag.py`
- `ui/shared.py`

### 완료 기준
- UI가 오케스트레이션 구조를 왜곡하지 않는다.
- 라이브/리뷰/결과가 명확히 구분된다.

### 점검 내용
- PASS
- `ui/workspace.py`
  - `에이전트 대화`(라이브 스트림) / `사고 과정`(실행 후 구조화 리뷰) / `실행 로그` / `결과` 구분 존재
- `ui/studio.py`
  - runtime graph / skill / prompt / model 정보 표시 구조 존재
- `ui/rag.py`
  - 문서 상세와 청킹 실험실이 분리되어 있으며 retrieval 설명 흐름 존재
- 특이사항:
  - 역할 분리와 정보구조는 문서 기준에 부합함
  - 디자인/표현 품질은 계속 개선 가능하지만, 구조 관점의 완료 기준은 충족

---

## Phase H. 관찰 지표 및 검증

- [x] Phase H 시작
- [x] tool call success rate 측정
- [x] interrupt/resume success 측정
- [x] citation coverage 측정
- [x] fallback usage rate 측정
- [x] Phase H 완료 (run diagnostics 기반 최소 구현 완료. 운영형 observability 아님)


### 목표
정석 적용 여부를 측정 가능하게 만든다.

### 작업
- tool call success rate
- interrupt rate
- resume success rate
- grounded citation coverage
- overclaim rejection rate
- fallback usage rate

### 대상 파일
- `main.py`
- `services/runtime_persistence_service.py`
- `ui/studio.py` 또는 별도 diagnostics 화면

### 완료 기준
- 최소한 run 단위에서 tool success, HITL, citation coverage, fallback usage를 확인 가능해야 한다.
- 진단 결과를 API 또는 UI에서 재확인 가능해야 한다.

### 점검 내용
- PASS (run diagnostics 기반 최소 구현 완료. **운영형 observability 완성이 아님.**)
- `main.py`
  - `/api/v1/analysis-runs/{run_id}/diagnostics` 엔드포인트 존재
- `services/run_diagnostics.py`
  - run 단위 진단 지표 계산 존재
- `services/citation_metrics.py`
  - citation coverage 계산 경로 존재
- 확인 가능한 지표:
  - tool call success 관련 집계
  - HITL request / response / resume_success
  - citation coverage
  - fallback usage
- 특이사항:
  - run 단위 진단/검증까지가 현재 완료 범위. 운영 대시보드·장기 시계열·run 간 비교 차트 등은 미구현(후속 과제).

### 완료 기준
- 위 핵심 지표를 run 단위로 확인 가능하면 최소 완료. (운영형 observability는 별도 과제.)

테스트 전략( graph unit test, tool schema contract test, interrupt/resume replay test, citation binding regression test )은 공식 문서 Section 8.13을 따른다.

---

## 6. 구현 시 참고 소스

### 내부 기준 문서
- `docs/langgraph-langchain-comparison.md`
- `docs/langgraph.md`
- `docs/aura_db.md`

### 참고 원본 소스 (공식 문서 Section 11과 동일)

- 프론트엔드: `/Users/joonbinchoi/Work/dwp/dwp-front`
- 백엔드: `/Users/joonbinchoi/Work/dwp/dwp-backend`
- Aura 플랫폼: `/Users/joonbinchoi/Work/dwp/aura-platform`

원칙:
- UI/용어/동선은 dwp-front 참고
- DB 의미와 저장 구조는 dwp-backend·aura-platform 참고
- 구현 기준은 `langgraph-langchain-comparison.md`를 우선한다.

---

## 7. 즉시 착수 순서

실제 작업은 아래 순서로 시작한다.

1. Phase A — Tool 정식화
2. Phase B — Structured Output 정식화
3. Phase C — Execute ToolNode 전환
4. Phase D — HITL interrupt / resume
5. Phase E — Persistence 정리
6. Phase F — RAG / Retrieval 고도화
7. Phase G — UI 대응
8. Phase H — 관찰 지표 및 검증

---

## 8. 작업 중 금지 사항

공식 문서 8.9 안티패턴 10개를 준수한다. 그중 특히 다음을 강조한다.

- 신규 기능을 registry direct call 방식으로 추가
- raw chain-of-thought 노출
- UI 요구 때문에 graph 구조를 왜곡 (UI는 관찰/제어 계층이며 구현 기준을 지배하지 않음)
- tool / state / persistence 책임 혼합
- 기준 문서 없이 임의 구조로 LangGraph / LangChain 패턴을 바꾸는 것

전체 10개: `docs/langgraph-langchain-comparison.md` Section 8.9 참고.

---

## 9. 최종 한 줄 결론

이 로드맵은 `docs/langgraph-langchain-comparison.md`를 실제 구현 순서로 번역한 문서이며,
AuraAgent를 **“LangGraph / LangChain을 정석적으로 적용한 엔터프라이즈급 agentic AI PoC”**로 완성하기 위한 실행 계획이다.
