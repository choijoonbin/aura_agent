# Cursor 작업 프롬프트 — Critic→Planner 자기교정 루프 구현

## 배경 및 목적

`agent/langgraph_agent.py`의 LangGraph StateGraph는 현재 아래의 **단방향 선형 구조**로 고정되어 있다:

```
START → screener → intake → planner → execute → critic → verify → [hitl_pause / reporter] → finalizer → END
```

`critic_node`는 `recommend_hold`, `overclaim_risk` 등의 판단을 내리지만,
이 결과가 그래프 흐름에 전혀 반영되지 않는다. critic이 "재조사 필요"라고 해도
항상 `verify`로만 넘어간다. 이것이 핵심 문제다.

이번 작업의 목표는 **critic 판단 결과에 따라 planner로 돌아가는 자기교정 루프**를 추가하여
에이전트가 스스로 오류를 감지하고 재조사를 시도하는 자율형 구조로 전환하는 것이다.

---

## 작업 범위 (수정 파일 목록)

| 파일 | 작업 내용 |
|------|----------|
| `agent/langgraph_agent.py` | ① AgentState에 루프 카운터 추가, ② `_route_after_critic()` 라우터 함수 신규 작성, ③ `critic_node` 반환값에 루프 트리거 추가, ④ `planner_node`에 재계획 컨텍스트 반영, ⑤ `build_agent_graph()`에서 엣지 수정 |
| `agent/output_models.py` | ⑥ `CriticOutput`에 `replan_required`, `replan_reason` 필드 추가 |
| `tests/test_graph.py` | ⑦ 루프 라우터 단위 테스트 추가 |

---

## 상세 구현 명세

---

### ① AgentState에 루프 카운터 필드 추가

**파일:** `agent/langgraph_agent.py`
**위치:** `class AgentState(TypedDict, total=False):` 블록 내부

기존 필드들 아래에 다음 두 필드를 추가한다:

```python
class AgentState(TypedDict, total=False):
    # ... 기존 필드 유지 ...
    
    # Critic→Planner 자기교정 루프용
    critic_loop_count: int        # 현재까지 critic→planner 재시도 횟수 (기본 0)
    replan_context: dict[str, Any] | None  # 재계획 시 planner에 전달할 critic 피드백
```

---

### ② `output_models.py`에 CriticOutput 필드 추가

**파일:** `agent/output_models.py`
**위치:** `class CriticOutput(BaseModel):` 블록 내부, 기존 필드 아래에 추가

```python
class CriticOutput(BaseModel):
    # ... 기존 필드 유지 (overclaim_risk, contradictions, 등) ...
    
    replan_required: bool = Field(
        default=False,
        description="재계획(planner 재실행) 필요 여부. overclaim_risk=True이고 재시도 가능할 때 True."
    )
    replan_reason: str = Field(
        default="",
        description="재계획이 필요한 이유. planner에게 전달되어 보완 조사 방향을 안내함."
    )
```

---

### ③ `_route_after_critic()` 라우터 함수 신규 작성

**파일:** `agent/langgraph_agent.py`
**위치:** 기존 `_route_after_verify()` 함수 바로 위에 신규 추가

이 함수는 `critic` 노드 다음에 호출되는 조건부 라우터다.

```python
# 최대 재시도 횟수 상수 (모듈 상단 상수 영역에 추가)
_MAX_CRITIC_LOOP = 2


def _route_after_critic(state: AgentState) -> str:
    """
    critic 판단 결과에 따라 다음 노드를 결정한다.

    판단 기준:
    - critic_output.replan_required=True  AND
    - critic_loop_count < _MAX_CRITIC_LOOP  AND
    - hasHitlResponse=False (HITL 응답이 이미 있으면 재계획 불필요)
    → "planner" (자기교정 재시도)

    그 외 모든 경우:
    → "verify" (기존 흐름)
    """
    critic_out = state.get("critic_output") or {}
    loop_count = state.get("critic_loop_count") or 0
    has_hitl_response = (state.get("flags") or {}).get("hasHitlResponse", False)

    replan_required = bool(critic_out.get("replan_required"))
    under_limit = loop_count < _MAX_CRITIC_LOOP

    if replan_required and under_limit and not has_hitl_response:
        return "planner"
    return "verify"
```

---

### ④ `critic_node` 수정 — replan_required 판단 로직 추가

**파일:** `agent/langgraph_agent.py`
**위치:** 기존 `async def critic_node(state: AgentState)` 함수 내부

기존 `critique` dict 생성 부분과 `CriticOutput` 생성 부분을 아래와 같이 수정한다.
기존 로직은 **그대로 유지**하고, `replan_required`/`replan_reason`/`replan_context` 도출 로직만 추가한다.

**기존 코드 (변경 전):**
```python
    critique = {
        "has_legacy_result": bool(legacy and legacy.get("facts")),
        "missing_fields": missing,
        "risk_of_overclaim": bool(missing),
        "recommend_hold": bool(missing and not state["flags"].get("hasHitlResponse")),
    }
    ...
    critic_output = CriticOutput(
        overclaim_risk=critique["risk_of_overclaim"],
        contradictions=[],
        missing_counter_evidence=missing,
        recommend_hold=critique["recommend_hold"],
        rationale="입력 누락 필드가 있으면 과잉 주장 위험이 있어 보류를 권고한다." if missing else "추가 보류 조건 없이 진행 가능하다.",
        has_legacy_result=critique["has_legacy_result"],
        verification_targets=verification_targets,
    )
```

**변경 후:**
```python
    critique = {
        "has_legacy_result": bool(legacy and legacy.get("facts")),
        "missing_fields": missing,
        "risk_of_overclaim": bool(missing),
        "recommend_hold": bool(missing and not state["flags"].get("hasHitlResponse")),
    }

    # 재계획 필요 여부 판단: 누락 필드가 있고 아직 루프 여지가 있을 때
    loop_count = state.get("critic_loop_count") or 0
    replan_required = bool(
        critique["risk_of_overclaim"]
        and not state["flags"].get("hasHitlResponse")
        and loop_count < _MAX_CRITIC_LOOP
    )
    replan_reason = ""
    replan_context: dict[str, Any] | None = None
    if replan_required:
        replan_reason = (
            f"누락 필드 {missing}로 인해 과잉 주장 위험이 감지되었습니다. "
            f"재조사 시 해당 필드를 보완하는 도구를 우선 실행하십시오."
        )
        replan_context = {
            "critic_feedback": replan_reason,
            "missing_fields": missing,
            "loop_count": loop_count + 1,
            "previous_tool_results": [r.get("skill") for r in state.get("tool_results", [])],
        }

    critic_output = CriticOutput(
        overclaim_risk=critique["risk_of_overclaim"],
        contradictions=[],
        missing_counter_evidence=missing,
        recommend_hold=critique["recommend_hold"],
        rationale="입력 누락 필드가 있으면 과잉 주장 위험이 있어 보류를 권고한다." if missing else "추가 보류 조건 없이 진행 가능하다.",
        has_legacy_result=critique["has_legacy_result"],
        verification_targets=verification_targets,
        replan_required=replan_required,        # 신규
        replan_reason=replan_reason,            # 신규
    )
```

그리고 `return` 문을 아래와 같이 수정하여 `critic_loop_count`와 `replan_context`를 상태에 포함시킨다:

**기존 return (변경 전):**
```python
    return {
        "critique": critique,
        "critic_output": critic_output.model_dump(),
        "pending_events": [...],
    }
```

**변경 후:**
```python
    return {
        "critique": critique,
        "critic_output": critic_output.model_dump(),
        "critic_loop_count": loop_count + 1 if replan_required else loop_count,
        "replan_context": replan_context,
        "pending_events": [...],  # 기존 pending_events 내용 유지
    }
```

---

### ⑤ `planner_node` 수정 — 재계획 컨텍스트 반영

**파일:** `agent/langgraph_agent.py`
**위치:** `async def planner_node(state: AgentState)` 함수 내부

`_plan_from_flags()`를 호출하기 전에 `replan_context` 존재 여부를 확인하고,
재계획 시에는 이미 실행된 도구를 제외한 보완 계획을 수립한다.

**기존 코드 (변경 전):**
```python
async def planner_node(state: AgentState) -> AgentState:
    plan = _plan_from_flags(state["flags"])
```

**변경 후:**
```python
async def planner_node(state: AgentState) -> AgentState:
    replan_context = state.get("replan_context")
    
    if replan_context:
        # 재계획 모드: 이미 실행된 도구를 제외하고 보완 도구만 실행
        already_run = set(replan_context.get("previous_tool_results") or [])
        base_plan = _plan_from_flags(state["flags"])
        # 이미 실행된 도구는 제외, 단 policy_rulebook_probe와 document_evidence_probe는
        # 누락 필드 보완을 위해 재실행 허용
        ALWAYS_RERUN = {"policy_rulebook_probe", "document_evidence_probe"}
        plan = [
            step for step in base_plan
            if step["tool"] not in already_run or step["tool"] in ALWAYS_RERUN
        ]
        # plan이 비어있으면 전체 재실행 (엣지 케이스 방어)
        if not plan:
            plan = base_plan
    else:
        # 최초 계획 (기존 로직)
        plan = _plan_from_flags(state["flags"])
```

`generate_working_note()` 호출 시 `context`에 재계획 여부를 추가한다:

```python
    start_note = await generate_working_note(
        node="planner",
        role="planner_agent",
        context={
            "voucher_summary": _voucher_summary_for_context(state["body_evidence"]),
            "flags": state["flags"],
            "is_replan": bool(replan_context),                        # 신규
            "critic_feedback": (replan_context or {}).get("critic_feedback", ""),  # 신규
            "loop_count": (replan_context or {}).get("loop_count", 0),             # 신규
        },
        # fallback 문구도 재계획 상황을 반영
        fallback_message="비판 검토 결과를 반영해 보완 조사 계획을 수립합니다." if replan_context else "조사 계획을 수립합니다.",
        fallback_thought="이전 조사에서 부족했던 부분을 보완할 도구를 선택합니다." if replan_context else "위험 유형별로 어떤 조사 순서가 효율적인지 정해야 합니다.",
        fallback_action="누락 필드를 보완하는 도구 순서를 재계산합니다." if replan_context else "위험 유형별 조사 순서를 계산합니다.",
        fallback_observation="재계획 완료. 보완 조사를 시작합니다." if replan_context else "계획 수립에 필요한 정보를 검토합니다.",
    )
```

---

### ⑥ `build_agent_graph()` 엣지 수정

**파일:** `agent/langgraph_agent.py`
**위치:** `def build_agent_graph():` 함수 내부

**기존 코드 (변경 전):**
```python
    workflow.add_edge("execute", "critic")
    workflow.add_edge("critic", "verify")
```

**변경 후:**
```python
    workflow.add_edge("execute", "critic")
    # critic 이후: 재계획 필요 시 planner로, 아니면 verify로
    workflow.add_conditional_edges(
        "critic",
        _route_after_critic,
        {
            "planner": "planner",   # 자기교정 재시도
            "verify": "verify",     # 기존 흐름
        },
    )
```

`_COMPILED_GRAPH = None` 초기화가 있으므로, 이 함수가 변경된 후에는
**반드시 모듈을 재시작하거나 `_COMPILED_GRAPH = None` 리셋이 필요함**을 주석으로 명시한다.

---

### ⑦ `tests/test_graph.py`에 단위 테스트 추가

**파일:** `tests/test_graph.py`
**위치:** 기존 `TestAgentGraph` 클래스 내부 하단에 추가

```python
    def test_route_after_critic_returns_planner_when_replan_required(self):
        """replan_required=True이고 루프 한도 미만이면 planner로 라우팅."""
        from agent.langgraph_agent import _route_after_critic

        state = {
            "critic_output": {"replan_required": True, "replan_reason": "누락 필드"},
            "critic_loop_count": 0,
            "flags": {"hasHitlResponse": False},
        }
        self.assertEqual(_route_after_critic(state), "planner")

    def test_route_after_critic_returns_verify_when_loop_limit_reached(self):
        """루프 한도(_MAX_CRITIC_LOOP=2)에 도달하면 재계획하지 않고 verify로."""
        from agent.langgraph_agent import _route_after_critic

        state = {
            "critic_output": {"replan_required": True},
            "critic_loop_count": 2,   # _MAX_CRITIC_LOOP와 동일
            "flags": {"hasHitlResponse": False},
        }
        self.assertEqual(_route_after_critic(state), "verify")

    def test_route_after_critic_returns_verify_when_hitl_response_exists(self):
        """HITL 응답이 이미 있으면 재계획 불필요 → verify."""
        from agent.langgraph_agent import _route_after_critic

        state = {
            "critic_output": {"replan_required": True},
            "critic_loop_count": 0,
            "flags": {"hasHitlResponse": True},
        }
        self.assertEqual(_route_after_critic(state), "verify")

    def test_route_after_critic_returns_verify_when_no_replan(self):
        """replan_required=False이면 항상 verify."""
        from agent.langgraph_agent import _route_after_critic

        state = {
            "critic_output": {"replan_required": False},
            "critic_loop_count": 0,
            "flags": {"hasHitlResponse": False},
        }
        self.assertEqual(_route_after_critic(state), "verify")

    def test_build_agent_graph_has_critic_conditional_edges(self):
        """그래프에 critic→planner, critic→verify 조건부 엣지가 모두 존재해야 한다."""
        from agent.langgraph_agent import build_agent_graph

        graph = build_agent_graph()
        w = graph.get_graph()
        edge_targets = [e[1] for e in w.edges if e[0] == "critic"]
        self.assertIn("planner", edge_targets, "critic→planner 엣지가 없음")
        self.assertIn("verify", edge_targets, "critic→verify 엣지가 없음")
```

---

## 구현 완료 검증 체크리스트

작업을 마친 후 아래 순서로 확인한다:

```bash
# 1. 단위 테스트 실행
python -m pytest tests/test_graph.py -v

# 2. 그래프 구조 시각 확인 (선택)
python -c "
from agent.langgraph_agent import build_agent_graph
g = build_agent_graph()
w = g.get_graph()
print('Nodes:', list(w.nodes))
print('Edges:', list(w.edges))
"

# 3. 정상 케이스 — 재계획 없는 흐름 확인
# critic_loop_count=0, replan_required=False → verify → reporter → finalizer

# 4. 재계획 케이스 — 누락 필드 시나리오
# body_evidence에 dataQuality.missingFields 값을 포함시켜 실행
# critic → planner (loop_count=1) → execute → critic → verify 흐름 확인
```

---

## 주의사항 및 사이드이펙트

| 항목 | 내용 |
|------|------|
| `_COMPILED_GRAPH` 싱글톤 | `build_agent_graph()`는 전역 캐시를 사용한다. 구현 후 서버 재시작 필요 |
| 재계획 시 `tool_results` 누적 | 재계획 후 execute_node는 기존 tool_results에 추가한다. 중복 실행 도구의 결과는 마지막 것이 사용된다 (list append 구조) |
| 스트림 이벤트 표시 | planner가 재실행되면 스트림에 "보완 조사 계획 수립" 이벤트가 추가로 표시된다. 이는 정상 동작이며 에이전트가 살아있음을 보여주는 핵심 시각 효과다 |
| HITL과의 관계 | `hasHitlResponse=True`이면 루프 진입을 차단한다. HITL이 이미 개입했다면 재계획은 불필요하기 때문이다 |
| `_MAX_CRITIC_LOOP = 2` | 무한 루프 방지를 위한 상한. 필요 시 `utils/config.py`의 settings로 이전 가능 |

---

## 병행 작업 제안 (단위 테스트를 위해 함께 진행 권장)

이 작업과 동시에 진행해야 독립적 단위 테스트가 가능한 작업들:

### A. `reasoning_notes.py` 프롬프트 개선 (독립 작업, 병행 가능)
- **내용:** `generate_working_note()`의 system_prompt에서 voucher_summary 반복 출력 금지 지시 추가
- **왜 병행해야 하나:** planner가 재실행될 때 "재계획 중"이라는 고유한 메시지가 출력되어야 스트림에서 루프 동작을 확인할 수 있다. 지금은 모든 노드가 동일한 전표 요약을 반복해서 루프 여부를 스트림으로 구분하기 어렵다
- **수정 위치:** `reasoning_notes.py` L102~113 system_prompt 문자열

### B. `AgentState`에 `replan_history` 추가 (선택적 병행)
- **내용:** 재계획 이력을 누적 저장하는 필드 추가 (디버깅 및 감사 추적용)
- **왜 병행해야 하나:** critic→planner 루프가 몇 번 돌았는지, 각 루프에서 무엇이 달라졌는지 테스트 시 확인 필요
- **수정 위치:** `AgentState` TypedDict에 `replan_history: list[dict[str, Any]]` 추가