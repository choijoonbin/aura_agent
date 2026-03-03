# 운영 차원 점검 (잔여작업 1~10 반영 후)

> 검수 중 참고용. 누락 여부·파일 크기·모듈화·구조화 관점 정리.

---

## 1. 누락 여부

| 항목 | 반영 여부 | 비고 |
|------|-----------|------|
| §2 현재 상태 요약 현행화 | ✅ | `langgraphPlan.md` 수정 완료 |
| Phase C/D 점검 결론 반영 | ✅ | 완료 확정·Transitional HITL 명시 |
| Phase D 결정 기준 문서화 | ✅ | `langgraphPlan2.md`에 정의됨 |
| 테스트 전략 4종 스켈레톤 | ✅ | `tests/test_*.py` 4개, pytest 11 passed |
| Phase F rerank/evidence 스텁 | ✅ | `services/retrieval_quality.py` |
| Phase H diagnostics UI | ✅ | workspace 결과 탭 Run 진단 expander |
| UX 마감(워크스페이스·스튜디오·RAG) | ✅ | 캡션·설명 보강 |
| 선택 과제(9번) | 문서만 정의, 코드 미구현 | 의도된 범위 |

**추가 권장(선택)**  
- `README.md`에 테스트 실행 방법 한 줄 추가: `pytest tests/ -v`

---

## 2. 파일 라인 수 (기준: 단일 파일 수백 라인 초과 시 분리 검토)

| 파일 | 라인 수 | 판단 |
|------|---------|------|
| **agent/langgraph_agent.py** | **924** | ⚠️ 분리 권장 (목표: 노드·헬퍼·그래프 빌드 분리) |
| **ui/workspace.py** | **774** | ⚠️ 분리 권장 (목표: 헬퍼·컴포넌트·페이지 오케스트레이션 분리) |
| main.py | 503 | △ 500선, 당장 분리 필수는 아님 |
| services/case_service.py | 400 | 양호 |
| ui/shared.py | 386 | 양호 |
| services/policy_service.py | 304 | 양호 |
| 기타 agent/ui/services | 200 이하 | 양호 |

---

## 3. 모듈화 제안

### 3.1 `agent/langgraph_agent.py` (924줄)

**현재:** 상태 정의·헬퍼·노드 10개·그래프 빌드·실행 진입점이 한 파일에 있음.

**제안:**

| 분리 후 파일 | 담당 내용 | 예상 라인 |
|--------------|-----------|-----------|
| `agent/state.py` | `AgentState` TypedDict, `_get_tools_by_name` | ~40 |
| `agent/graph_helpers.py` | `_format_occurred_at`, `_find_tool_result`, `_top_policy_refs`, `_should_skip_skill`, `_build_grounded_reason`, `_derive_flags`, `_plan_from_flags`, `_score` | ~150 |
| `agent/nodes.py` | `screener_node` ~ `finalizer_node`, `_route_after_verify` | ~650 |
| `agent/langgraph_agent.py` | `build_agent_graph`, `run_langgraph_agentic_analysis`, 위 모듈 import·재노출 | ~120 |

**주의:** `nodes.py`는 `AgentState`, `graph_helpers`, `output_models` 등에 의존하므로 import 순서·순환 참조만 정리하면 됨.

### 3.2 `ui/workspace.py` (774줄)

**현재:** 스트림/이벤트 포맷·API 호출·타임라인 요약·각종 render_*·페이지 진입점이 한 파일에 있음.

**제안:**

| 분리 후 파일 | 담당 내용 | 예상 라인 |
|--------------|-----------|-----------|
| `ui/workspace_helpers.py` | `_format_agent_event_line`, `_tool_caption_fragment`, `_stream_card_chunks`, `sse_text_stream`, `fetch_case_bundle`, `summarize_tool_results`, `summarize_process_timeline`, `build_workspace_plan_steps`, `build_workspace_execution_logs` | ~280 |
| `ui/workspace_components.py` | `render_tool_trace_summary`, `render_timeline_cards`, `render_process_story`, `render_hitl_*`, `render_case_preview_dialog`, `render_workspace_case_queue`, `render_workspace_chat_panel`, `render_workspace_results` | ~420 |
| `ui/workspace.py` | `render_ai_workspace_page` (레이아웃·탭·조건부 호출만) | ~100 |

**주의:** `workspace.py`가 `workspace_helpers`·`workspace_components`를 import하고, 기존 `from ui.workspace import ...` 사용처는 `render_ai_workspace_page`만 쓰는지 확인 후 필요 시 `workspace_components`에서 세부 함수 재노출.

### 3.3 `main.py` (503줄)

- 현재는 한 파일에 라우트·비즈니스 로직·runtime 연동이 함께 있음.
- 우선순위는 낮게 두고, 라우트가 더 늘어나면 `main.py`는 앱 생성·라우터 등록만 두고 `routers/` 또는 `api/` 하위로 엔드포인트 그룹 분리 검토.

---

## 4. 구조화 관점 정리

- **agent/**  
  - 이미 `event_schema`, `hitl`, `output_models`, `reasoning_notes`, `screener`, `skills`, `tool_schemas`, `aura_bridge`, `native_agent` 등으로 역할이 나뉘어 있음.  
  - 유일하게 **`langgraph_agent.py`만 900줄대로 비대**하므로, 위 3.1 분리만 적용해도 운영·리뷰에 유리함.

- **ui/**  
  - `workspace.py`가 “AI 워크스페이스” 한 페이지 전담이지만 774줄이므로, **헬퍼 / 컴포넌트 / 페이지** 3단 분리(3.2) 권장.

- **services/**  
  - `citation_metrics`, `run_diagnostics`, `retrieval_quality` 등 이미 기능별로 잘 쪼개져 있음.  
  - `policy_service` 304줄은 아직 단일 파일 유지 가능한 수준.

- **tests/**  
  - `test_graph`, `test_tool_schema`, `test_interrupt_resume`, `test_citation_binding`로 전략별 분리된 상태.  
  - 추후 시나리오 테스트가 늘어나면 `tests/e2e/` 등 서브 디렉터리로 묶어도 됨.

---

## 5. 요약

| 구분 | 내용 |
|------|------|
| **누락** | 잔여작업 1~10 범위 내에서는 누락 없음. 선택적으로 README에 `pytest tests/ -v` 안내 추가 권장. |
| **수백 라인 초과** | `agent/langgraph_agent.py`(924), `ui/workspace.py`(774) 두 파일이 해당. |
| **모듈화** | `langgraph_agent` → state / graph_helpers / nodes / langgraph_agent 4개로, `workspace` → workspace_helpers / workspace_components / workspace 3개로 분리 시 단일 파일당 수백 라인 미만으로 유지 가능. |
| **구조화** | agent·services는 이미 역할 분리가 잘 되어 있음. UI만 workspace 한 파일에 집중되어 있어 위 분리 적용 시 운영·유지보수에 유리함. |

이 문서는 검수 시 참고용이며, 실제 분리 작업은 리스크·일정을 고려해 단계적으로 진행하는 것을 권장합니다.
