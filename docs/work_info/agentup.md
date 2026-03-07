# Aura Agent 고도화 프롬프트 모음

> **소스 분석 기준:** `aura_agent-main` (LangGraph StateGraph, Single Agent 구조)  
> **작성 구성:** 사용자 발견 고도화 1건 + Claude 분석 고도화 5건 = 총 6개 독립 프롬프트  
> 각 프롬프트는 Cursor에 **개별로** 전달하도록 설계되었습니다.

**검토·보완 (구현 전 확인):**
- **프롬프트 1**은 단일 LLM 호출로 계획을 생성·검증하는 **Planner LLM 구조화 출력** 방식이며, `docs/Edu/Langgraph_Logic.md` §9 권장안 A와 동일하다. (ReAct 스타일 제목이지만 구현은 “한 번의 planner 호출 + fallback”이다.)
- **evidence_verification**: `verify_evidence_coverage_claims()`는 이미 `threshold_hold`, `threshold_caution` 키워드 인자를 지원한다. 프롬프트 4에서는 `get_dynamic_coverage_thresholds()`로 계산한 값을 이 인자로 전달하면 된다.
- **replan_context**: 현재 코드에 `critic_feedback`, `missing_fields`, `previous_tool_results`가 이미 설정되어 있어 프롬프트 1·2 명세와 일치한다.

---

## 📌 현재 아키텍처 요약 (공통 컨텍스트)

```
START → screener → intake → planner → execute → critic
         ↑                                          |
         └── (replan, 최대 2회) ←──────────────────┘
                                          ↓
                                       verify → [hitl_pause / reporter] → finalizer → END
```

**핵심 파일 구조:**
- `agent/langgraph_agent.py` — 노드 로직 + 그래프 빌드
- `agent/agent_tools.py` — Tool Registry (6개 도구)
- `agent/output_models.py` — Pydantic structured output
- `agent/screener.py` — 결정론적 사전 분류
- `utils/config.py` — Settings dataclass
- `services/evidence_verification.py` — 증거 커버리지 검증

---

---

## 🔴 프롬프트 1 (사용자 발견) — LLM 기반 동적 도구 선택 (ReAct Planner)

### 배경 및 문제

현재 `planner_node`는 `_plan_from_flags(flags)` 함수를 통해 **사전 정의된 규칙으로 도구 순서를 고정** 한다.

```python
# 현재: 규칙 기반 고정 계획
def _plan_from_flags(flags: dict[str, Any]) -> list[dict[str, Any]]:
    plan = []
    if flags.get("isHoliday") or flags.get("hrStatus") in {"LEAVE", "OFF", "VACATION"}:
        plan.append({"tool": "holiday_compliance_probe", ...})
    if flags.get("budgetExceeded"):
        plan.append({"tool": "budget_risk_probe", ...})
    if flags.get("mccCode"):
        plan.append({"tool": "merchant_risk_probe", ...})
    plan.append({"tool": "document_evidence_probe", ...})
    plan.append({"tool": "policy_rulebook_probe", ...})
    return plan
```

이 방식은 다음을 할 수 없다:
- 케이스별로 도구 순서를 **LLM이 판단해서 변경**
- 중간 도구 결과를 보고 **다음 도구를 동적으로 결정** (ReAct 패턴)
- 불필요한 도구를 **스마트하게 생략**

---

### 구현 목표

`planner_node`를 LLM이 도구 순서·생략·이유를 직접 결정하는 **ReAct 스타일 동적 플래너**로 교체한다.

---

### 수정 파일 및 상세 명세

#### ① `agent/langgraph_agent.py` — `planner_node` LLM 호출 추가

**위치:** `async def planner_node(state: AgentState)` 함수 전체 교체

기존 `_plan_from_flags(flags)` 호출을 제거하고, OpenAI structured output으로 LLM이 계획을 생성하도록 교체한다.

```python
async def planner_node(state: AgentState) -> AgentState:
    """
    ReAct 스타일 LLM Planner.
    flags, screening_result, replan_context를 종합해 LLM이 도구 순서를 결정한다.
    LLM 호출 실패 시 _plan_from_flags() fallback을 사용한다.
    """
    replan_context = state.get("replan_context")

    # 사용 가능한 도구 목록 (LLM에 제공)
    available_tools = [
        {"name": "holiday_compliance_probe",
         "when": "isHoliday=True 또는 hrStatus가 LEAVE/OFF/VACATION일 때"},
        {"name": "budget_risk_probe",
         "when": "budgetExceeded=True일 때"},
        {"name": "merchant_risk_probe",
         "when": "mccCode가 있을 때"},
        {"name": "document_evidence_probe",
         "when": "항상 실행 (전표 증거 수집)"},
        {"name": "policy_rulebook_probe",
         "when": "항상 실행 (규정 조항 조회)"},
        {"name": "legacy_aura_deep_audit",
         "when": "enable_legacy_aura_specialist=True이고 증거가 부족할 때"},
    ]

    system_prompt = (
        "당신은 기업 경비 감사 에이전트의 Planner다.\n"
        "아래 케이스 정보를 분석하여 최적의 도구 실행 순서를 결정하라.\n"
        "규칙:\n"
        "1. 불필요한 도구는 생략해 효율을 높여라.\n"
        "2. 앞 도구 결과가 뒷 도구에 영향을 준다면 순서를 고려하라.\n"
        "3. 복합 위험(휴일+고위험 업종 등)이 감지되면 관련 도구를 모두 포함하라.\n"
        "4. 반드시 JSON 배열로만 응답하라. 각 항목: {\"tool\": string, \"reason\": string}\n"
        "5. 배열 외 텍스트, 마크다운 금지.\n"
    )

    flags = state["flags"]
    screening = state.get("screening_result") or {}
    user_prompt = (
        f"케이스 유형: {screening.get('case_type', 'UNKNOWN')}\n"
        f"심각도: {screening.get('severity', 'MEDIUM')}\n"
        f"플래그: isHoliday={flags.get('isHoliday')}, "
        f"hrStatus={flags.get('hrStatus')}, "
        f"budgetExceeded={flags.get('budgetExceeded')}, "
        f"mccCode={flags.get('mccCode')}, "
        f"isNight={flags.get('isNight')}, "
        f"amount={flags.get('amount')}\n"
    )
    if replan_context:
        user_prompt += (
            f"\n[재계획 모드]\n"
            f"이전 실행 도구: {replan_context.get('previous_tool_results', [])}\n"
            f"Critic 피드백: {replan_context.get('critic_feedback', '')}\n"
            f"누락 필드: {replan_context.get('missing_fields', [])}\n"
            "이미 실행된 도구는 꼭 필요한 경우에만 재포함하라."
        )
    user_prompt += f"\n\n사용 가능한 도구:\n{json.dumps(available_tools, ensure_ascii=False)}"

    plan: list[dict[str, Any]] = []
    llm_used = False

    if settings.openai_api_key:
        try:
            from openai import AsyncAzureOpenAI, AsyncOpenAI

            base_url = (settings.openai_base_url or "").strip()
            is_azure = ".openai.azure.com" in base_url
            if is_azure:
                azure_ep = base_url.rstrip("/")
                if azure_ep.endswith("/openai/v1"):
                    azure_ep = azure_ep[:-len("/openai/v1")]
                client = AsyncAzureOpenAI(
                    api_key=settings.openai_api_key,
                    azure_endpoint=azure_ep,
                    api_version=settings.openai_api_version,
                )
            else:
                kw: dict[str, Any] = {"api_key": settings.openai_api_key}
                if base_url:
                    kw["base_url"] = base_url
                client = AsyncOpenAI(**kw)

            response = await client.chat.completions.create(
                model=settings.reasoning_llm_model,
                max_tokens=600,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw = (response.choices[0].message.content or "").strip()
            parsed = json.loads(raw)
            # LLM이 {"plan": [...]} 또는 직접 [...] 형태로 반환할 수 있음
            raw_plan = parsed if isinstance(parsed, list) else parsed.get("plan") or []
            plan = [
                {"tool": step["tool"], "reason": step.get("reason", ""), "owner": "llm_planner"}
                for step in raw_plan
                if isinstance(step, dict) and step.get("tool") in {t["name"] for t in available_tools}
            ]
            llm_used = bool(plan)
        except Exception:
            plan = []

    # Fallback: 기존 규칙 기반 플래너
    if not plan:
        plan = _plan_from_flags(flags)
        if replan_context:
            already_run = set(replan_context.get("previous_tool_results") or [])
            always_rerun = {"policy_rulebook_probe", "document_evidence_probe"}
            plan = [s for s in plan if s["tool"] not in already_run or s["tool"] in always_rerun] or plan

    # 이하 기존 PlannerOutput 생성 로직 유지 (steps, events 등)
    # ...
```

#### ② `utils/config.py` — Settings에 LLM Planner 활성화 플래그 추가

```python
@dataclass(frozen=True)
class Settings:
    # ... 기존 필드 유지 ...
    enable_llm_planner: bool = os.getenv("ENABLE_LLM_PLANNER", "true").lower() == "true"
```

#### ③ `.env.example` — 신규 환경변수 추가

```
ENABLE_LLM_PLANNER=true
```

---

### 검증 방법

```bash
# 1. LLM Planner 활성화 상태에서 휴일+MCC 복합 케이스 실행
# planner_output.planner_output.rationale에 "llm_planner" owner가 나타나야 함

# 2. ENABLE_LLM_PLANNER=false로 설정 후 실행
# 기존 _plan_from_flags() 결과와 동일해야 함 (regression 없음)

# 3. OpenAI 키 미설정 상태에서 실행
# fallback으로 _plan_from_flags()가 실행되어야 하며 오류 없어야 함
```

---
---

## 🟠 프롬프트 2 (Claude 발견) — Critic 트리거 조건 다양화 (LLM 기반 판단)

### 배경 및 문제

현재 `critic_node`는 **누락 필드 존재 여부만** 재계획 트리거로 사용한다.

```python
# 현재: 단일 조건
critique = {
    "risk_of_overclaim": bool(missing),       # ← missing fields만 체크
    "recommend_hold": bool(missing and not state["flags"].get("hasHitlResponse")),
}
replan_required = bool(
    critique["risk_of_overclaim"]
    and not state["flags"].get("hasHitlResponse")
    and loop_count < _MAX_CRITIC_LOOP
)
```

다음 상황에서 재계획이 **트리거되지 않는** 문제가 있다:
- 도구 실패율이 50% 이상인데 재조사 없이 진행
- score_breakdown의 `final_score`가 임계값에 근접해 신뢰도가 낮은 경우
- execute에서 중요 도구(`policy_rulebook_probe`)가 실패한 경우
- 복합 위험 케이스인데 증거 점수(`evidence_score`)가 매우 낮은 경우

---

### 구현 목표

Critic 재계획 트리거 조건을 **4가지 추가 규칙**으로 확장하고, 각 조건에 맞는 `replan_reason`을 생성한다.

---

### 수정 파일 및 상세 명세

#### `agent/langgraph_agent.py` — `critic_node` 트리거 로직 확장

**위치:** `async def critic_node(state: AgentState)` 내부, 기존 `replan_required` 도출 블록 교체

```python
# ── Critic 재계획 트리거 다중 조건 ──────────────────────────
score = state.get("score_breakdown") or {}
execute_out = state.get("execute_output") or {}
tool_results = state.get("tool_results") or []

failed_tools = execute_out.get("failed_tools") or []
evidence_score = int(score.get("evidence_score") or 0)
final_score = int(score.get("final_score") or 0)
high_risk_compound = (
    bool(state["flags"].get("isHoliday")) and
    bool(state["flags"].get("mccCode"))
)

critical_tool_failed = "policy_rulebook_probe" in failed_tools or "document_evidence_probe" in failed_tools
tool_failure_rate = len(failed_tools) / max(len(tool_results), 1)
borderline_score = 48 <= final_score <= 62   # MEDIUM/HIGH 경계 ±7점
weak_evidence_with_risk = high_risk_compound and evidence_score < 30

# 조건별 재계획 이유 누적
replan_reasons: list[str] = []

if missing:
    replan_reasons.append(f"누락 필드 {missing} — 과잉 주장 위험")
if critical_tool_failed:
    replan_reasons.append(f"핵심 도구 실패: {[t for t in failed_tools if t in {'policy_rulebook_probe', 'document_evidence_probe'}]}")
if tool_failure_rate >= 0.5 and len(tool_results) >= 2:
    replan_reasons.append(f"도구 실패율 {tool_failure_rate:.0%} — 증거 신뢰성 저하")
if borderline_score:
    replan_reasons.append(f"최종점수 {final_score}점이 MEDIUM/HIGH 경계 ±7점 이내 — 추가 증거 필요")
if weak_evidence_with_risk:
    replan_reasons.append(f"복합 위험(휴일+MCC) 케이스인데 evidence_score={evidence_score} (30점 미만)")

replan_required = bool(
    replan_reasons
    and not state["flags"].get("hasHitlResponse")
    and loop_count < _MAX_CRITIC_LOOP
)
replan_reason = " | ".join(replan_reasons) if replan_reasons else ""
# ──────────────────────────────────────────────────────────
```

---

### 검증 방법

```bash
# 1. policy_rulebook_probe를 강제 실패시킨 케이스 실행
# → critic이 "핵심 도구 실패" 트리거로 planner 재실행해야 함

# 2. 최종점수가 55점 근처인 케이스 실행
# → critic이 "경계 점수" 트리거로 planner 재실행해야 함

# 3. 정상 케이스 (도구 전체 성공, 점수 70점 이상)
# → critic_loop_count=0 유지, 재계획 없이 verify로 직행해야 함
```

---
---

## 🟡 프롬프트 3 (Claude 발견) — Parallel Tool Execution (도구 병렬 실행)

### 배경 및 문제

현재 `execute_node`는 계획된 도구를 **완전 순차 실행**한다.

```python
# 현재: 완전 순차 (for loop)
for step in state["plan"]:
    ...
    result = await tool.ainvoke(inp.model_dump())
    tool_results.append(result)
```

대부분의 도구는 서로 독립적이며 동시에 실행 가능하다:
- `holiday_compliance_probe` — 근태 데이터 조회
- `budget_risk_probe` — 예산 데이터 조회
- `merchant_risk_probe` — MCC 위험도 계산
- `document_evidence_probe` — 전표 파싱

이들을 순차로 실행하면 **레이턴시가 도구 수에 비례해 증가**한다.  
단, `policy_rulebook_probe`는 앞선 도구 결과(`prior_tool_results`)를 enrichment에 활용하므로 **마지막에 실행**해야 한다.

---

### 구현 목표

의존성이 없는 도구들은 `asyncio.gather()`로 병렬 실행하고, `policy_rulebook_probe`는 병렬 그룹 완료 후 순차 실행한다.

---

### 수정 파일 및 상세 명세

#### `agent/langgraph_agent.py` — `execute_node` 병렬 실행 로직

**위치:** `async def execute_node(state: AgentState)` 내부 도구 실행 루프 교체

```python
import asyncio

# 도구 의존성 정의: 이 도구들은 반드시 마지막에 실행 (prior_tool_results를 활용)
_SEQUENTIAL_LAST_TOOLS = {"policy_rulebook_probe", "legacy_aura_deep_audit"}

async def execute_node(state: AgentState) -> AgentState:
    tools_by_name = _get_tools_by_name()
    tool_results: list[dict[str, Any]] = []
    skipped_tools: list[str] = []
    failed_tools: list[str] = []
    pending_events: list[dict[str, Any]] = [...]  # NODE_START 이벤트

    plan = state.get("plan") or []

    # 1단계: skip 판정
    parallel_steps = []
    sequential_steps = []
    for step in plan:
        tool_name = step.get("tool", "")
        skip, reason = _should_skip_tool(step, state=state, tool_results=tool_results)
        if skip:
            skipped_tools.append(tool_name)
            pending_events.append(AgentEvent(event_type="TOOL_SKIPPED", ...).to_payload())
            continue
        if tool_name in _SEQUENTIAL_LAST_TOOLS:
            sequential_steps.append(step)
        else:
            parallel_steps.append(step)

    # 2단계: 독립 도구 병렬 실행
    async def _run_tool(step: dict[str, Any]) -> dict[str, Any]:
        tool_name = step.get("tool", "")
        tool = tools_by_name.get(tool_name)
        if not tool:
            return {"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"}
        inp = ToolContextInput(
            case_id=state["case_id"],
            body_evidence=state["body_evidence"],
            intended_risk_type=state.get("intended_risk_type"),
            prior_tool_results=[],  # 병렬 실행 시 prior 없음
        )
        result = await tool.ainvoke(inp.model_dump())
        return result if isinstance(result, dict) else {"tool": tool_name, "ok": False, "facts": {}, "summary": str(result)}

    if parallel_steps:
        # TOOL_CALL 이벤트 먼저 발행
        for step in parallel_steps:
            pending_events.append(AgentEvent(event_type="TOOL_CALL", tool=step["tool"], ...).to_payload())

        parallel_results = await asyncio.gather(
            *[_run_tool(step) for step in parallel_steps],
            return_exceptions=False,
        )
        for result in parallel_results:
            tool_results.append(result)
            if not result.get("ok"):
                failed_tools.append(result.get("tool", ""))
            pending_events.append(AgentEvent(event_type="TOOL_RESULT", ...).to_payload())

    # 3단계: 의존성 있는 도구 순차 실행 (prior_tool_results 포함)
    for step in sequential_steps:
        tool_name = step.get("tool", "")
        tool = tools_by_name.get(tool_name)
        if not tool:
            failed_tools.append(tool_name)
            tool_results.append({"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"})
            continue
        pending_events.append(AgentEvent(event_type="TOOL_CALL", tool=tool_name, ...).to_payload())
        inp = ToolContextInput(
            case_id=state["case_id"],
            body_evidence=state["body_evidence"],
            intended_risk_type=state.get("intended_risk_type"),
            prior_tool_results=list(tool_results),  # 병렬 결과 전달
        )
        result = await tool.ainvoke(inp.model_dump())
        if not isinstance(result, dict):
            result = {"tool": tool_name, "ok": False, "facts": {}, "summary": str(result)}
        if not result.get("ok"):
            failed_tools.append(tool_name)
        tool_results.append(result)
        pending_events.append(AgentEvent(event_type="TOOL_RESULT", ...).to_payload())

    # 이하 기존 _score() 호출 및 execute_output 생성 로직 유지
    # ...
```

#### `utils/config.py` — 병렬 실행 활성화 플래그

```python
@dataclass(frozen=True)
class Settings:
    # ... 기존 필드 유지 ...
    enable_parallel_tool_execution: bool = os.getenv("ENABLE_PARALLEL_TOOL_EXECUTION", "true").lower() == "true"
```

---

### 예상 성능 향상

| 시나리오 | 순차 실행 예상 | 병렬 실행 예상 | 단축 |
|---------|-------------|-------------|------|
| 4개 병렬 + 1개 순차 | ~2.0초 | ~0.7초 | ~65% |
| 3개 병렬 + 1개 순차 | ~1.5초 | ~0.6초 | ~60% |

---
---

## 🟢 프롬프트 4 (Claude 발견) — Verifier 증거 커버리지 임계값 동적 조정

### 배경 및 문제

현재 `evidence_verification.py`의 coverage 임계값이 **하드코딩**되어 있다.

```python
# services/evidence_verification.py
DEFAULT_COVERAGE_THRESHOLD_HOLD = 0.5
DEFAULT_COVERAGE_THRESHOLD_CAUTION = 0.7
```

그리고 `verify_node`에서 HITL 결정 시 이 임계값이 **케이스 심각도에 무관하게** 동일하게 적용된다. 즉:
- `CRITICAL` 심각도 케이스도 coverage 50%이면 자동 통과
- `LOW` 심각도 케이스도 동일한 50% 기준 적용

결과: 고위험 케이스에서 증거가 불충분해도 HITL 없이 통과될 수 있음.

---

### 구현 목표

screening 심각도 + score_breakdown에 따라 커버리지 임계값을 **동적으로 조정**한다.

---

### 수정 파일 및 상세 명세

#### ① `services/evidence_verification.py` — 동적 임계값 함수 추가

**파일 하단 또는 `verify_evidence_coverage_claims()` 위에 추가:**

```python
def get_dynamic_coverage_thresholds(
    severity: str,
    final_score: float,
    compound_multiplier: float = 1.0,
) -> tuple[float, float]:
    """
    케이스 심각도와 점수에 따라 동적으로 coverage 임계값을 반환한다.
    반환: (hold_threshold, caution_threshold)
    
    설계 원칙:
    - CRITICAL/HIGH 케이스: 임계값 상향 (더 많은 근거 요구)
    - LOW/MEDIUM 케이스: 기본값 유지
    - 복합 위험 승수 >= 1.3: 추가 상향
    """
    sev = str(severity or "").upper()
    
    base_hold = DEFAULT_COVERAGE_THRESHOLD_HOLD      # 0.5
    base_caution = DEFAULT_COVERAGE_THRESHOLD_CAUTION  # 0.7
    
    # 심각도별 조정
    severity_delta = {
        "CRITICAL": 0.25,
        "HIGH": 0.15,
        "MEDIUM": 0.05,
        "LOW": 0.0,
    }.get(sev, 0.0)
    
    # 점수 기반 추가 조정 (80점 이상은 강력한 근거 요구)
    score_delta = 0.1 if final_score >= 80 else (0.05 if final_score >= 65 else 0.0)
    
    # 복합 위험 승수 기반 추가 조정
    compound_delta = 0.1 if compound_multiplier >= 1.3 else 0.0
    
    total_delta = severity_delta + score_delta + compound_delta
    
    hold_threshold = min(0.9, base_hold + total_delta)
    caution_threshold = min(0.95, base_caution + total_delta)
    
    return hold_threshold, caution_threshold
```

#### ② `agent/langgraph_agent.py` — `verify_node`에서 동적 임계값 적용

**위치:** `async def verify_node(state: AgentState)` 내부 `verify_evidence_coverage_claims` 호출 전

```python
# 동적 임계값 계산
from services.evidence_verification import (
    EVIDENCE_GATE_HOLD, EVIDENCE_GATE_REGENERATE,
    verify_evidence_coverage_claims,
    get_dynamic_coverage_thresholds,
)

score_bd = state.get("score_breakdown") or {}
severity = score_bd.get("severity", "MEDIUM")
final_score = float(score_bd.get("final_score") or 0)
compound_multiplier = float(score_bd.get("compound_multiplier") or 1.0)

hold_threshold, caution_threshold = get_dynamic_coverage_thresholds(
    severity=severity,
    final_score=final_score,
    compound_multiplier=compound_multiplier,
)

if verification_targets and retrieved_chunks:
    verification_summary = verify_evidence_coverage_claims(
        verification_targets,
        retrieved_chunks,
        hold_threshold=hold_threshold,      # 동적 임계값 전달
        caution_threshold=caution_threshold,
    )
```

#### ③ `services/evidence_verification.py` — `verify_evidence_coverage_claims()` 시그니처 확장

```python
def verify_evidence_coverage_claims(
    claims: list[str],
    chunks: list[dict[str, Any]],
    hold_threshold: float = DEFAULT_COVERAGE_THRESHOLD_HOLD,    # 기존 기본값 유지
    caution_threshold: float = DEFAULT_COVERAGE_THRESHOLD_CAUTION,
) -> dict[str, Any]:
    # 기존 로직 유지, threshold만 파라미터로 받도록 수정
    ...
```

---

### 검증 방법

```bash
# 1. severity=CRITICAL, score=85 케이스: hold_threshold >= 0.8 이어야 함
# 2. severity=LOW, score=30 케이스: hold_threshold = 0.5 (기본값 유지)
# 3. 기존 tests/test_citation_binding.py — regression 없어야 함
python -m pytest tests/test_citation_binding.py -v
```

---
---

## 🔵 프롬프트 5 (Claude 발견) — MemorySaver → 영속 Checkpointer 교체 (Redis/PostgreSQL)

### 배경 및 문제

현재 LangGraph checkpointer로 **인메모리 MemorySaver**를 사용한다.

```python
# agent/langgraph_agent.py
def _get_checkpointer():
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        from langgraph.checkpoint.memory import MemorySaver
        _CHECKPOINTER = MemorySaver()
    return _CHECKPOINTER
```

이 방식의 문제:
- **프로세스 재시작 시 모든 HITL 체크포인트 소실** → HITL resume 불가능
- **멀티 워커 환경**(uvicorn workers > 1)에서 thread_id 충돌
- **메모리 누수**: 장기 운영 시 완료된 run의 상태가 메모리에 계속 쌓임
- 현재 `settings.database_url`이 PostgreSQL을 가리키고 있어 **DB 연결이 이미 존재**

---

### 구현 목표

환경변수 기반으로 checkpointer를 선택하는 팩토리를 구현한다.  
`CHECKPOINTER_BACKEND=postgres`이면 LangGraph PostgresSaver, `memory`이면 기존 유지.

---

### 수정 파일 및 상세 명세

#### ① `agent/langgraph_agent.py` — `_get_checkpointer()` 팩토리 교체

```python
def _get_checkpointer():
    """
    CHECKPOINTER_BACKEND 환경변수에 따라 checkpointer를 선택한다.
    - "postgres": langgraph-checkpoint-postgres 사용 (영속)
    - "memory" (기본값): MemorySaver (인메모리, 개발용)
    
    PostgresSaver 사용 시 langgraph-checkpoint-postgres 패키지가 필요하다:
        pip install langgraph-checkpoint-postgres
    """
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    backend = settings.checkpointer_backend.lower()

    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            import asyncio

            async def _init_postgres_checkpointer():
                cp = await AsyncPostgresSaver.from_conn_string(settings.database_url)
                await cp.setup()  # 체크포인트 테이블 자동 생성
                return cp

            # 동기 컨텍스트에서 초기화
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 이미 실행 중인 루프: 새 루프에서 초기화
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, _init_postgres_checkpointer())
                    _CHECKPOINTER = future.result(timeout=10)
            else:
                _CHECKPOINTER = loop.run_until_complete(_init_postgres_checkpointer())

        except ImportError:
            # 패키지 미설치 시 경고 후 MemorySaver fallback
            import warnings
            warnings.warn(
                "langgraph-checkpoint-postgres 패키지가 없습니다. "
                "MemorySaver로 fallback합니다. "
                "pip install langgraph-checkpoint-postgres",
                RuntimeWarning,
                stacklevel=2,
            )
            from langgraph.checkpoint.memory import MemorySaver
            _CHECKPOINTER = MemorySaver()
    else:
        from langgraph.checkpoint.memory import MemorySaver
        _CHECKPOINTER = MemorySaver()

    return _CHECKPOINTER
```

#### ② `utils/config.py` — Settings에 checkpointer 설정 추가

```python
@dataclass(frozen=True)
class Settings:
    # ... 기존 필드 유지 ...
    checkpointer_backend: str = os.getenv("CHECKPOINTER_BACKEND", "memory")
    # "memory" | "postgres"
```

#### ③ `requirements.txt` — 조건부 의존성 주석 추가

```
# LangGraph 영속 체크포인트 (CHECKPOINTER_BACKEND=postgres 시 필요)
# langgraph-checkpoint-postgres>=2.0.0
```

#### ④ `.env.example` — 신규 환경변수 추가

```
# Checkpointer backend: "memory" (개발) | "postgres" (운영)
CHECKPOINTER_BACKEND=memory
```

---

### 검증 방법

```bash
# 1. CHECKPOINTER_BACKEND=memory (기본) — 기존 동작 동일해야 함
# 2. CHECKPOINTER_BACKEND=postgres — HITL pause 후 프로세스 재시작, resume 성공 확인
# 3. 단위 테스트
python -m pytest tests/test_interrupt_resume.py -v
```

---
---

## 🟣 프롬프트 6 (Claude 발견) — Screener 강화: 외부 캘린더 API + MCC 동적 확장

### 배경 및 문제

현재 `screener.py`의 공휴일 판단과 MCC 위험 분류가 **하드코딩된 정적 데이터**에 의존한다.

```python
# agent/screener.py — 현재 정적 MCC 셋
_MCC_HIGH_RISK = {"5813", "7993", "7994", "5912"}
_MCC_LEISURE = {"7992", "7996", "7997", "7941", "7011"}

# agent/agent_tools.py — 현재 정적 MCC 셋 (screener와 불일치!)
high_mcc = {"5813", "7992", "5912", "7997", "5999"}  # ← screener와 다름!
medium_mcc = {"5812", "5814", "7011", "4722"}
```

두 파일의 MCC 분류가 **서로 다르며** 이는 screener의 케이스 분류와 merchant_risk_probe의 위험도 판단이 불일치하는 버그를 유발할 수 있다.  
또한 새로운 고위험 MCC가 추가될 때마다 두 파일을 모두 수정해야 하는 유지보수 문제가 있다.

---

### 구현 목표

1. MCC 분류 데이터를 **단일 소스(config)로 통합**
2. DB 또는 JSON 파일에서 **런타임에 동적 로딩** 가능하도록 구조 변경
3. screener와 merchant_risk_probe가 **동일한 MCC 소스를 참조**하도록 보장

---

### 수정 파일 및 상세 명세

#### ① `utils/config.py` — MCC 분류 중앙화

```python
# MCC 위험 분류 (단일 소스 of truth)
# screener.py와 agent_tools.py 모두 이 설정을 참조한다.
MCC_HIGH_RISK: frozenset[str] = frozenset(
    os.getenv("MCC_HIGH_RISK", "5813,7993,7994,5912,7992,5999").split(",")
)
MCC_LEISURE: frozenset[str] = frozenset(
    os.getenv("MCC_LEISURE", "7996,7997,7941,7011").split(",")
)
MCC_MEDIUM_RISK: frozenset[str] = frozenset(
    os.getenv("MCC_MEDIUM_RISK", "5812,5811,5814,4722").split(",")
)
```

#### ② `agent/screener.py` — config에서 MCC 가져오도록 교체

```python
# 기존 하드코딩 제거
# _MCC_HIGH_RISK = {"5813", ...}  ← 삭제
# _MCC_LEISURE = {"7992", ...}    ← 삭제
# _MCC_MEDIUM_RISK = {"5812", ...} ← 삭제

# config에서 통합 참조
from utils.config import settings

def _extract_signals(body: dict[str, Any]) -> dict[str, Any]:
    ...
    mcc = str(body.get("mccCode") or "").strip()
    return {
        ...
        "mcc_high_risk": mcc in settings.MCC_HIGH_RISK,
        "mcc_leisure": mcc in settings.MCC_LEISURE,
        "mcc_medium_risk": mcc in settings.MCC_MEDIUM_RISK,
        ...
    }
```

#### ③ `agent/agent_tools.py` — `merchant_risk_probe` config 참조로 교체

```python
# 기존 하드코딩 제거
# high_mcc = {"5813", "7992", ...}  ← 삭제

# config에서 통합 참조
from utils.config import settings

async def merchant_risk_probe(context: dict[str, Any]) -> dict[str, Any]:
    body = context["body_evidence"]
    mcc = body.get("mccCode")
    mcc_str = str(mcc or "")

    if mcc_str in settings.MCC_HIGH_RISK:
        base_risk = "HIGH"
    elif mcc_str in settings.MCC_LEISURE or mcc_str in settings.MCC_MEDIUM_RISK:
        base_risk = "MEDIUM"
    elif mcc_str:
        base_risk = "MEDIUM"
    else:
        base_risk = "UNKNOWN"
    ...
```

#### ④ `.env.example` — MCC 환경변수 추가

```
# 고위험 MCC 코드 (쉼표 구분, 운영 환경에서 DB/관리 콘솔로 관리 권장)
MCC_HIGH_RISK=5813,7993,7994,5912,7992,5999
MCC_LEISURE=7996,7997,7941,7011
MCC_MEDIUM_RISK=5812,5811,5814,4722
```

---

### 검증 방법

```bash
# 1. screener와 merchant_risk_probe가 동일한 MCC에 대해 동일한 위험 등급을 반환하는지 확인
# screener: mcc_high_risk=True → case_type이 PRIVATE_USE_RISK 또는 UNUSUAL_PATTERN
# merchant_risk_probe: merchantRisk=HIGH

# 2. MCC_HIGH_RISK 환경변수 변경 후 재실행 → 새 코드 적용 확인
MCC_HIGH_RISK=5813,7993,9999 python -c "from utils.config import settings; print(settings.MCC_HIGH_RISK)"
# frozenset({'5813', '7993', '9999'}) 출력되어야 함

# 3. 기존 테스트 전체 실행 — regression 없어야 함
python -m pytest tests/ -v
```

---

## 📊 고도화 우선순위 요약

| 순위 | 프롬프트 | 유형 | 예상 효과 | 구현 난이도 |
|------|---------|------|----------|-----------|
| 1 | 프롬프트 3: 도구 병렬 실행 | 성능 | 레이턴시 ~60% 단축 | 낮음 |
| 2 | 프롬프트 6: MCC 통합 | 버그 수정 | screener/tool 불일치 해소 | 낮음 |
| 3 | 프롬프트 2: Critic 트리거 확장 | 자율성 | 재계획 정확도 향상 | 중간 |
| 4 | 프롬프트 4: 동적 coverage 임계값 | 정확도 | 고위험 케이스 HITL 누락 방지 | 중간 |
| 5 | 프롬프트 1: ReAct Planner | 자율성 | 진정한 동적 도구 선택 | 높음 |
| 6 | 프롬프트 5: 영속 Checkpointer | 안정성 | 운영 환경 HITL resume 보장 | 높음 |

---

## 🏗️ Multi-Agent 아키텍처 검토

현재 **Single Agent (LangGraph StateGraph)** 구조는 이 도메인(기업 경비 감사)에 적합하다.

**Multi-Agent / A2A 전환이 유리한 시점:**
- 케이스 유형이 5종 이상으로 늘어나 각 유형별 전용 전략이 필요할 때
- 대량 배치 처리(1일 수천 건)로 케이스별 격리 실행이 필요할 때
- 외부 시스템(ERP, HR 시스템)과의 실시간 연동 에이전트가 별도 필요할 때

**현 시점 권고:** Single Agent를 위 6개 프롬프트로 고도화한 후,  
운영 지표(처리 건수, 오류율, HITL 발생률)를 3개월 수집 후 Multi-Agent 전환 여부를 재판단하는 것이 효율적입니다.