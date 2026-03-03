# Phase 0. 준비 및 안전장치

> 기준: `docs/langgraphPlan.md` Phase 0  
> 목적: PoC를 깨지 않도록 리팩토링 전 연결 지점·transitional 경로·smoke test·회귀 체크리스트를 고정한다.

---

## 1. LangGraph ↔ UI 연결 지점 (재확인)

### 1.1 분석 실행 경로 (백엔드)

| 단계 | 파일·위치 | 설명 |
|------|-----------|------|
| 1 | `main.py` | `POST /api/v1/cases/{voucher_key}/analysis-runs` → `start_analysis()` |
| 2 | `main.py` | `_run_analysis_task(run_id, case_id, body_evidence, intended_risk_type)` 생성 |
| 3 | `agent/aura_bridge.py` | `run_agent_analysis()` → `settings.enable_langgraph_if_available` 시 `run_langgraph_agentic_analysis()` 호출 |
| 4 | `agent/langgraph_agent.py` | `run_langgraph_agentic_analysis()` → `build_agent_graph().astream(initial_state, stream_mode="updates", config)` |
| 5 | `main.py` | `runtime.publish(run_id, ev_type, data)` 로 이벤트 전달 |
| 6 | `main.py` | `GET /api/v1/analysis-runs/{run_id}/stream` → SSE 스트림 (UI가 구독) |

### 1.2 UI 수신 경로

| 단계 | 파일·위치 | 설명 |
|------|-----------|------|
| 1 | `ui/workspace.py` | AI 워크스페이스: 전표 선택 → 분석 시작 → `stream_path` SSE 구독 |
| 2 | `ui/workspace.py` | SSE 이벤트 수신 → `AGENT_EVENT` 등 카드/타임라인 표시 |
| 3 | `ui/workspace.py` | `get_run_events(run_id)` 또는 timeline API로 완료 후 이벤트 목록 조회 |
| 4 | `main.py` | `GET /api/v1/analysis-runs/{run_id}/events` → `runtime.get_timeline(run_id)` 등 |

### 1.3 에이전트/스튜디오·RAG (조회만)

| 경로 | 파일 | 설명 |
|------|------|------|
| 에이전트 목록 | `main.py` GET `/api/v1/agents` | `list_agents(db)` |
| 에이전트 상세 | `main.py` GET `/api/v1/agents/{id}` | `get_agent_detail(db, id)` |
| 스튜디오 UI | `ui/studio.py` | `build_agent_graph()`, 스킬 목록 표시용 `SKILL_REGISTRY` 참조 (표시 전용, 아래 transitional 경로 참고) |

---

## 2. SKILL_REGISTRY transitional path (명시)

Phase A 완료 전까지 **registry 직접 참조**는 허용되나, **transitional(과도기)** 로만 사용한다. Phase C 완료 시 ToolNode 전환과 함께 registry direct dispatch는 제거한다.

### 2.1 정의

| 파일 | 위치 | 용도 |
|------|------|------|
| `agent/skills.py` | `SKILL_REGISTRY: dict[str, AgentSkill]` | 스킬 정의·등록. Phase A에서 LangChain tool schema로 승격 후에도 registry는 tool 등록용으로만 유지 가능. |

### 2.2 사용처 (transitional)

| 파일 | 대략 위치 | 용도 | 비고 |
|------|------------|------|------|
| `agent/langgraph_agent.py` | `execute_node()` 내부 | `SKILL_REGISTRY.get(step["tool"])`, `SKILL_REGISTRY[step["tool"]]` — plan의 tool 이름으로 스킬 조회 후 `skill.handler(...)` 호출 | **Phase C에서 ToolNode 전환 시 제거 대상** |
| `ui/studio.py` | 런타임 스킬 탭 | `SKILL_REGISTRY.items()` 로 스킬 목록·설명 표시 | 표시용. Phase A 이후에는 tool list에서 표시하도록 변경 가능 |

### 2.3 금지 (Phase 0 시점부터)

- 신규 기능을 **registry direct call** 방식으로 추가하지 않는다 (공식 문서 4.3·8.9).
- `execute_node` 밖에서 `SKILL_REGISTRY`를 실행 경로로 사용하는 코드를 새로 두지 않는다.

---

## 3. Smoke test 시나리오

아래 시나리오는 Phase A～H 진행 중 **회귀 여부**를 볼 때 최소한으로 실행할 경로이다.

### 3.1 API

| # | 시나리오 | 방법 | 기대 |
|---|----------|------|------|
| 1 | 헬스 체크 | `GET /health` | 200, `ok: true`, `agent_runtime_mode`, `enable_langgraph_if_available` 포함 |
| 2 | 전표 목록 | `GET /api/v1/vouchers?queue=all&limit=10` | 200, `items` 배열 |
| 3 | 에이전트 목록 | `GET /api/v1/agents` | 200, `items` (최소 1개) |
| 4 | 분석 시작 | `POST /api/v1/cases/{voucher_key}/analysis-runs` (유효 voucher_key) | 201, `run_id`, `stream_path` |
| 5 | 스트림 구독 | `GET /api/v1/analysis-runs/{run_id}/stream` (4에서 받은 run_id) | SSE 스트림, `AGENT_EVENT` 또는 `completed` 수신 |
| 6 | 이벤트 조회 | `GET /api/v1/analysis-runs/{run_id}/events` | 200, `events` 배열, `run_id` |

### 3.2 UI (수동)

| # | 시나리오 | 동작 | 기대 |
|---|----------|------|------|
| 1 | 워크스페이스 진입 | Streamlit AI 워크스페이스 메뉴 클릭 | 전표 목록 또는 빈 상태 표시 |
| 2 | 분석 실행 | 전표 선택 → 분석 시작 | SSE로 이벤트 카드/타임라인 표시, 완료 시 결과 요약 |
| 3 | 에이전트 스튜디오 | 스튜디오 메뉴 → 에이전트 선택 → 도구 탭 | `SKILL_REGISTRY` 기반 스킬 목록 표시 |
| 4 | 그래프 탭 | 스튜디오 → 그래프 탭 | 메인 오케스트레이션·스킬 실행 흐름 표시 (또는 fallback 메시지) |

---

## 4. 최소 회귀 체크리스트

Phase A～H 중 **각 Phase 완료 후** 또는 **PR 전**에 아래를 확인한다.

- [ ] `GET /health` 200
- [ ] `POST /api/v1/cases/{voucher_key}/analysis-runs` 로 분석 1회 성공 (run_id 반환)
- [ ] 해당 run_id 로 `GET /api/v1/analysis-runs/{run_id}/stream` SSE 수신 (최소 1개 이벤트 이상)
- [ ] Streamlit 워크스페이스에서 전표 선택 후 분석 실행 → 이벤트가 화면에 표시되고 완료 시 결과 노출
- [ ] Streamlit 스튜디오에서 에이전트·도구 탭 진입 시 오류 없음
- [ ] `agent/langgraph_agent.py` `execute_node` 실행 경로에서 `SKILL_REGISTRY` 참조 시 plan에 있는 tool 이름과 일치하는 스킬만 호출 (기존 6개 스킬 동작 유지)

---

## 5. Phase 0 완료 기준 점검

- [x] 현재 LangGraph 흐름과 UI 연결 지점을 문서 기준으로 재확인 (Section 1)
- [x] 기존 `SKILL_REGISTRY` 경로를 transitional path로 명시 (Section 2)
- [x] 주요 경로 smoke test 시나리오 정리 (Section 3)
- [x] 최소 회귀 체크리스트 작성 (Section 4)

**완료 조건**: 현재 기능을 유지한 채 다음 Phase(A)를 진행할 수 있어야 하며, 위 최소 smoke test 시나리오가 문서화되어 있다.  
→ **이 문서로 Phase 0 문서화 완료. 실제 smoke 1회 실행으로 확인 후 Phase A 착수.**
