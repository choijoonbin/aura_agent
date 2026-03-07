# Agent 고도화 프롬프트 6개 전수 검증 보고서

> **검증 대상:** `aura_agent-main.zip` 반영 소스  
> **검증 파일:** `agent/langgraph_agent.py`, `agent/screener.py`, `agent/agent_tools.py`, `services/evidence_verification.py`, `utils/config.py`

---

## 검증 요약표

| # | 프롬프트 | 반영 여부 | 세부 판정 |
|---|---------|---------|---------|
| 1 | ReAct 동적 LLM 플래너 | ✅ 반영 | 구현 정상, **기본값 불일치 버그 1건** |
| 2 | Critic 트리거 조건 확장 | ✅ 반영 | 5개 조건 모두 구현됨 |
| 3 | 도구 병렬 실행 | ✅ 반영 | 의존성 그래프·순차 후처리 포함 완전 구현 |
| 4 | 동적 Coverage 임계값 | ✅ 반영 | severity·score·compound 3중 보정 구현 |
| 5 | 영속 Checkpointer | ✅ 반영 | 팩토리 패턴·MemorySaver fallback 구현, **Uvicorn 환경 잠재 이슈** |
| 6 | MCC 데이터 중앙화 | ✅ 반영 | `get_mcc_sets()` 중앙화, env/json/db 다중 소스 완전 구현 |

---

## 프롬프트 1 — ReAct 동적 LLM 플래너

### 구현 확인

**`_invoke_llm_planner()` 신규 함수 존재** (L1024~1103)
- LLM에게 케이스 유형·심각도·플래그·재계획 문맥을 전달해 도구 실행 순서를 JSON으로 생성
- `valid_names` 필터로 존재하지 않는 도구명 차단
- LLM 실패 시 빈 리스트 반환

**`planner_node()` LLM 우선 → 규칙 fallback 패턴 구현** (L1106~1215)
```python
if getattr(settings, "enable_llm_planner", False) and valid_tool_names:
    plan = await _invoke_llm_planner(...)   # LLM 먼저
if not plan:
    plan = _plan_from_flags(flags)          # 규칙 기반 fallback
```
`plan_source` 구분: `"llm"` / `"fallback_rule"` / `"rule"` → 이벤트에 포함

### 🔴 발견된 버그: `getattr` 기본값 불일치

```python
# config.py L88
enable_llm_planner: bool = os.getenv("ENABLE_LLM_PLANNER", "true").lower() == "true"
# → 환경변수 미설정 시 True

# langgraph_agent.py L1114, L1129
if getattr(settings, "enable_llm_planner", False) and valid_tool_names:
#                                              ↑ False ← True와 불일치
```

`settings` 객체는 정상 임포트 시 항상 `enable_llm_planner=True`를 가지므로 실제로는 `getattr` 기본값이 사용되지 않습니다. 하지만 `settings` 임포트 실패나 mock 환경에서는 LLM 플래너가 **의도치 않게 비활성화**됩니다. 수정이 권장됩니다:

```python
# 수정 전
if getattr(settings, "enable_llm_planner", False):

# 수정 후
if getattr(settings, "enable_llm_planner", True):   # config 기본값과 일치
```

### 판정: ✅ 반영됨 / ⚠️ getattr 기본값 불일치 수정 권장

---

## 프롬프트 2 — Critic 재계획 트리거 확장

### 구현 확인

프롬프트 지시대로 **5가지 트리거 조건** 모두 구현됨 (L1680~1699):

| 조건 | 구현 코드 | 임계값 |
|------|---------|-------|
| 누락 필드 | `if missing:` | 1개 이상 |
| 핵심 도구 실패 | `if critical_tool_failed:` | `policy_rulebook_probe` 또는 `document_evidence_probe` |
| 도구 실패율 | `if tool_failure_rate >= 0.5 and len(tool_results) >= 2:` | 50% 이상 |
| 경계 점수 | `if borderline_score:` (48~62점) | MEDIUM/HIGH 경계 ±7점 |
| 복합 위험 + 낮은 evidence | `if weak_evidence_with_risk:` | 휴일+MCC && evidence_score < 30 |

**재계획 진입 조건도 올바름:**
```python
replan_required = bool(
    replan_reasons
    and not state["flags"].get("hasHitlResponse")  # HITL 응답 후 재계획 방지
    and loop_count < _MAX_CRITIC_LOOP              # 최대 2회 제한
)
```

### 판정: ✅ 완전 반영됨

---

## 프롬프트 3 — 도구 병렬 실행

### 구현 확인

`execute_node()` 내부에 `use_parallel` 분기 완전 구현 (L1233~1479):

**핵심 구현 요소:**

```python
# 순차 처리 도구 목록 (prior_tool_results 의존)
_SEQUENTIAL_LAST_TOOLS = frozenset({"policy_rulebook_probe", "legacy_aura_deep_audit"})

# 도구 간 의존성 정의 (holiday 결과 확인 후 merchant 실행)
_PARALLEL_TOOL_DEPENDENCIES: dict[str, frozenset[str]] = {
    "merchant_risk_probe": frozenset({"holiday_compliance_probe"}),
}
```

**병렬 실행 흐름:**
1. `parallel_steps` = `_SEQUENTIAL_LAST_TOOLS` 제외 도구 → `asyncio.gather()` 병렬 실행
2. 의존성 있는 도구는 `finished_parallel_tools` 추적으로 순서 보장
3. 순환 의존 방어 로직 포함 (blocked 상태에서 강행 실행)
4. `sequential_steps` = `policy_rulebook_probe`, `legacy_aura_deep_audit` → 병렬 완료 후 순차 실행
5. `enable_parallel_tool_execution=false`면 기존 순차 실행 경로 유지

**`enable_parallel_tool_execution` 기본값 `true`** (config.py L89): 즉시 활성화 상태.

### 판정: ✅ 완전 반영됨 (프롬프트보다 향상: 의존성 그래프 추가)

---

## 프롬프트 4 — 동적 Coverage 임계값

### 구현 확인

`services/evidence_verification.py`에 `get_dynamic_coverage_thresholds()` 구현 (L21~45):

```python
severity_delta = {
    "CRITICAL": 0.25,   # 0.5 → 0.75 (CRITICAL 케이스)
    "HIGH":     0.15,   # 0.5 → 0.65
    "MEDIUM":   0.05,
    "LOW":      0.0,
}.get(sev, 0.0)

score_delta    = 0.1 if final_score >= 80 else (0.05 if final_score >= 65 else 0.0)
compound_delta = 0.1 if compound_multiplier >= 1.3 else 0.0

hold_threshold = min(0.9, base_hold + total_delta)   # 최대 0.9 상한
```

**`verify_node()`에서 동적 임계값 실제 사용 확인** (L1796~1800):
```python
hold_threshold, caution_threshold = get_dynamic_coverage_thresholds(
    severity=severity,
    final_score=final_score,
    compound_multiplier=compound_multiplier,   # 복합 위험 승수
)
```

이전 문제였던 `DEFAULT_COVERAGE_THRESHOLD_HOLD = 0.5` 고정값은 여전히 상수로 존재하지만, `verify_node`는 이를 직접 사용하지 않고 동적 함수를 호출합니다.

### 판정: ✅ 완전 반영됨

---

## 프롬프트 5 — 영속 Checkpointer (AsyncPostgresSaver 팩토리)

### 구현 확인

`_get_checkpointer()` 싱글톤 팩토리 구현 (L2150~2205):

```python
def _get_checkpointer():
    backend = getattr(settings, "checkpointer_backend", "memory").lower()

    if backend == "postgres":
        # AsyncPostgresSaver.from_conn_string() + setup()
        # 이벤트 루프 상태에 따라 두 가지 초기화 경로:
        # ① 루프 실행 중 → ThreadPoolExecutor로 별도 루프에서 초기화
        # ② 루프 없음 → new_event_loop()으로 직접 초기화
        ...
    else:
        # MemorySaver() fallback
```

`build_agent_graph()`에서 `workflow.compile(checkpointer=_get_checkpointer())` 호출로 연결됨 (L2235).

`langgraph-checkpoint-postgres` 미설치 시 `ImportError` 잡아 `MemorySaver` fallback + `RuntimeWarning` 발생.

### ⚠️ 잠재 이슈: Uvicorn(asyncio) 환경 초기화 타이밍

`_get_checkpointer()`는 동기 함수지만 내부에서 비동기 `AsyncPostgresSaver`를 초기화합니다. `ThreadPoolExecutor` 우회 경로가 구현돼 있으나, **Uvicorn의 이벤트 루프 위에서 `build_agent_graph()`가 처음 호출될 때** 다음 시나리오에서 15초 타임아웃이나 데드락이 발생할 수 있습니다:

- `concurrent.futures` 스레드 내에서 `asyncio.run()` 호출 시, 일부 환경에서 중첩 루프 제한에 걸림
- `future.result(timeout=15)` 대기 중 FastAPI 워커가 블로킹됨

**권장 수정:** `build_agent_graph()`를 `async def`로 변경하거나, 앱 시작 시(`lifespan`) 명시적으로 초기화하는 방식으로 개선:

```python
# app.py 또는 main.py의 lifespan에서
from agent.langgraph_agent import build_agent_graph

@asynccontextmanager
async def lifespan(app: FastAPI):
    await build_agent_graph_async()   # 앱 시작 시 1회 초기화
    yield
```

### 판정: ✅ 반영됨 / ⚠️ Uvicorn 환경 초기화 타이밍 이슈 확인 필요

---

## 프롬프트 6 — MCC 데이터 중앙화 (버그 수정)

### 구현 확인

이전 문제: `screener.py`의 `_MCC_HIGH_RISK`와 `agent_tools.py`의 `high_mcc`가 **각각 하드코딩** → 불일치 버그 존재.

**수정 후:**

```python
# utils/config.py — 단일 소스
mcc_high_risk:   str = os.getenv("MCC_HIGH_RISK",   "5813,7993,7994,5912,7992,5999")
mcc_leisure:     str = os.getenv("MCC_LEISURE",     "7996,7997,7941,7011")
mcc_medium_risk: str = os.getenv("MCC_MEDIUM_RISK", "5812,5811,5814,4722")
mcc_source:      str = os.getenv("MCC_SOURCE",      "env")   # env | json | db

def get_mcc_sets() -> dict[str, frozenset[str]]: ...
```

```python
# agent/screener.py L16
from utils.config import get_mcc_sets
mcc_sets = get_mcc_sets()
mcc in mcc_sets["high_risk"]   # config 중앙 참조
```

```python
# agent/agent_tools.py L18
from utils.config import get_mcc_sets, settings
mcc_sets = get_mcc_sets()
mcc_str in mcc_sets["high_risk"]   # config 중앙 참조
```

추가로 **env / json / db 세 가지 소스** 지원 및 외부 소스 실패 시 env 기본값 자동 fallback까지 구현됨. 프롬프트 지시보다 더 발전된 구현입니다.

### 판정: ✅ 완전 반영됨 (프롬프트보다 향상: 다중 소스 + fallback 추가)

---

## 전체 요약 및 잔여 수정 사항

### 반영 완료 (수정 불필요)
- Critic 트리거 5개 조건 확장 ✅
- 도구 병렬 실행 (`asyncio.gather` + 의존성 그래프) ✅
- 동적 Coverage 임계값 (severity·score·compound 3중 보정) ✅
- MCC 중앙화 + env/json/db 다중 소스 ✅

### 수정 권장 사항 2건

**[낮음] 프롬프트1 — `getattr` 기본값 불일치**
```python
# agent/langgraph_agent.py L1114, L1129
# 수정 전
if getattr(settings, "enable_llm_planner", False):
# 수정 후 (config 기본값 True와 일치)
if getattr(settings, "enable_llm_planner", True):
```

**[중간] 프롬프트5 — Checkpointer 초기화 타이밍**  
Uvicorn 환경에서 `CHECKPOINTER_BACKEND=postgres` 사용 시 첫 그래프 빌드 시점에 15초 블로킹 또는 데드락 가능성. 앱 시작 `lifespan` 훅에서 명시적 사전 초기화 권장.