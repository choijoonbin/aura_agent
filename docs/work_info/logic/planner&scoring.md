# Cursor 작업 프롬프트 [2/2] — 도구 결과 상호참조 + Planner↔Scoring 피드백 루프

## 왜 2개 항목을 하나의 프롬프트로 묶었는가

소스 분석 결과 두 항목이 **동일한 함수를 동시에 수정**해야 함:
- `execute_node()` — B에서 tool 호출 context 변경, C에서 return부에 `plan_achievement` 추가
- `AgentState` — B에서 `prior_tool_results` 흐름, C에서 `plan_achievement` 필드 추가
- `_score()` — C의 plan 달성도 반영 대상

두 항목을 별도 작업하면 `execute_node()` 내부가 중간 상태에서 충돌.

---

## 현황 진단 (소스 직접 분석)

### B. 도구 결과 상호참조 없음

**현재 tool 호출 코드 (execute_node L536~542):**
```python
inp = SkillContextInput(
    case_id=state["case_id"],
    body_evidence=state["body_evidence"],   # ← 항상 원본 body만 전달
    intended_risk_type=state.get("intended_risk_type"),
)
result = await tool.ainvoke(inp.model_dump())
tool_results.append(result)   # ← 이전 결과 누적만, 다음 도구에 전달 안 됨
```

**`SkillContextInput` 현재 스키마 (tool_schemas.py):**
```python
class SkillContextInput(BaseModel):
    case_id: str
    body_evidence: dict[str, Any]
    intended_risk_type: str | None
    # prior_tool_results 필드 없음 ← 핵심 문제
```

**실제로 발생하는 상황:**

`holiday_compliance_probe` 실행 → `{"holidayRisk": True, "hrStatus": "LEAVE"}` 반환

바로 다음 `merchant_risk_probe` 호출 시:
```python
async def merchant_risk_probe(context):
    body = context["body_evidence"]   # 원본 body만 접근 가능
    mcc = body.get("mccCode")         # "5813"
    risk = "HIGH" if mcc in {"5813", "7992"} else "MEDIUM"
    # holiday_probe가 이미 "휴일+근태 충돌"을 확인했음에도
    # 이 사실을 전혀 모름 → 복합 위험 판단 불가
```

**개선 후 가능한 것:**
- `merchant_probe`가 휴일 결과를 참조 → 휴일+주류 복합 위험 `CRITICAL` 상향
- `policy_probe`가 앞선 도구 위험 신호를 종합 → 더 정교한 키워드로 검색

---

### C. Scoring과 Planner의 단방향성

**현재 흐름:**
```
planner_node() → _plan_from_flags(flags) → plan 고정 (flags만 참조)
execute_node() → 도구 순차 실행 → _score(flags, tool_results)
                                          ↑
                  plan 달성 여부가 score에 전혀 반영 안 됨
```

**`_score()` 현재 코드 (L212~246):**
```python
def _score(flags, tool_results):
    policy_score = 0
    evidence_score = 30
    # flags 체크 (isHoliday, hrStatus, isNight, budgetExceeded)
    # tool_results 체크 (document_evidence_probe, policy_rulebook_probe 성공 여부)
    final_score = min(100, int(policy_score * 0.6 + evidence_score * 0.4))
    return {"policy_score": ..., "evidence_score": ..., "final_score": ...}
    # plan 달성도(몇 개 성공/실패/스킵) 반영 없음
```

**문제:**
- 계획한 3개 도구 중 2개가 실패해도 score에 영향 없음
- "증거 수집 완성도"가 score에 반영되지 않음
- planner가 세운 계획의 달성 여부를 아무도 평가하지 않음

---

## 작업 범위

| 파일 | 함수/클래스 | 작업 |
|------|------------|------|
| `agent/tool_schemas.py` | `SkillContextInput` | `prior_tool_results` 필드 추가 |
| `agent/langgraph_agent.py` | `execute_node()` | tool 호출 시 누적 결과 전달 + return부 확장 |
| `agent/langgraph_agent.py` | `_compute_plan_achievement()` | 신규 함수 추가 |
| `agent/langgraph_agent.py` | `_score()` | plan 달성도 보정 블록 추가 |
| `agent/langgraph_agent.py` | `AgentState` | `plan_achievement` 필드 추가 |
| `agent/skills.py` | `merchant_risk_probe()` | prior_results 참조로 복합 위험 판단 |
| `agent/skills.py` | `policy_rulebook_probe()` | prior_results로 키워드 보강 |
| `services/policy_service.py` | `build_policy_keywords()` | `_enriched_` 키 처리 추가 |
| `tests/test_graph.py` | `TestToolCrossRef`, `TestPlanAchievement` | 단위 테스트 추가 |

---

## 상세 구현 명세

---

### ① `agent/tool_schemas.py` — `SkillContextInput`에 `prior_tool_results` 추가

```python
class SkillContextInput(BaseModel):
    """스킬 호출 시 공통 입력. LangChain tool input schema."""

    case_id: str = Field(description="분석 대상 케이스 ID")
    body_evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="전표/입력 증거 (occurredAt, amount, mccCode, document 등)"
    )
    intended_risk_type: str | None = Field(
        default=None,
        description="스크리닝된 위험 유형"
    )
    prior_tool_results: list[dict[str, Any]] = Field(
        default_factory=list,
        description="현재 도구 호출 이전에 완료된 도구 결과 목록 (상호참조용). "
                    "각 원소는 {skill, ok, facts, summary} 형태."
    )
```

---

### ② `agent/langgraph_agent.py` — `execute_node()` 두 곳 수정

**수정 위치 1 — `inp = SkillContextInput(...)` 부분 교체**

기존:
```python
        inp = SkillContextInput(
            case_id=state["case_id"],
            body_evidence=state["body_evidence"],
            intended_risk_type=state.get("intended_risk_type"),
        )
```

변경 후:
```python
        inp = SkillContextInput(
            case_id=state["case_id"],
            body_evidence=state["body_evidence"],
            intended_risk_type=state.get("intended_risk_type"),
            prior_tool_results=list(tool_results),   # ← 현재까지 누적된 결과 전달
        )
```

**수정 위치 2 — `execute_node()` return 문 교체**

기존:
```python
    return {"tool_results": tool_results, "score_breakdown": score, "pending_events": pending_events}
```

변경 후:
```python
    plan_achievement = _compute_plan_achievement(state.get("plan") or [], tool_results)
    return {
        "tool_results": tool_results,
        "score_breakdown": score,
        "pending_events": pending_events,
        "plan_achievement": plan_achievement,   # ← 신규
    }
```

---

### ③ `agent/langgraph_agent.py` — `_compute_plan_achievement()` 신규 함수

`_score()` 함수 **바로 위**에 추가:

```python
def _compute_plan_achievement(
    plan: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    planner가 수립한 계획 대비 실제 실행 달성도 평가.

    반환:
    {
      "total_planned": 5,
      "executed": 4,
      "succeeded": 3,
      "failed": 1,
      "skipped": 1,
      "achievement_rate": 0.75,
      "step_results": [{"tool": ..., "status": "success"|"failed"|"skipped", ...}]
    }
    """
    executed_map = {r.get("skill"): r for r in tool_results}
    planned_tools = [step.get("tool", "") for step in plan]

    step_results: list[dict[str, Any]] = []
    succeeded = failed = skipped = 0

    for tool_name in planned_tools:
        if tool_name not in executed_map:
            step_results.append({"tool": tool_name, "status": "skipped", "ok": None})
            skipped += 1
        else:
            result = executed_map[tool_name]
            ok = bool(result.get("ok"))
            step_results.append({
                "tool": tool_name,
                "status": "success" if ok else "failed",
                "ok": ok,
                "facts_keys": list((result.get("facts") or {}).keys()),
            })
            if ok:
                succeeded += 1
            else:
                failed += 1

    total    = len(planned_tools)
    executed = succeeded + failed
    rate     = round(succeeded / total, 3) if total else 0.0

    return {
        "total_planned": total,
        "executed": executed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "achievement_rate": rate,
        "step_results": step_results,
    }
```

---

### ④ `agent/langgraph_agent.py` — `_score()` plan 달성도 보정 블록 추가

`_score()` 내부, `final_score = min(100, ...)` **바로 앞**에 삽입:

```python
    # ── plan 달성도 보정 (신규) ────────────────────────────────────────────
    _total  = len(tool_results)
    _ok     = sum(1 for r in tool_results if r.get("ok"))
    _rate   = (_ok / _total) if _total else 0.0

    if _total > 0 and _rate < 0.5:
        # 절반 이상 도구 실패 → evidence_score 하향 (최대 -20)
        _penalty = round((0.5 - _rate) * 40, 1)
        evidence_score = max(0.0, evidence_score - _penalty)
        reasons.append(f"도구 실행 성공률 {_rate:.0%} → evidence_score -{_penalty}점")

    if _total >= 3 and _rate == 1.0:
        # 계획 전체 성공 시 소량 보너스 (+5)
        evidence_score = min(100.0, evidence_score + 5)
        reasons.append(f"계획한 {_total}개 도구 전체 성공 → evidence_score +5점")
    # ─────────────────────────────────────────────────────────────────────────
```

---

### ⑤ `agent/langgraph_agent.py` — `AgentState`에 `plan_achievement` 필드 추가

```python
class AgentState(TypedDict, total=False):
    # ... 기존 필드 전체 유지 ...
    plan_achievement: dict[str, Any]   # 신규: planner 계획 달성도 (execute_node 반환)
```

---

### ⑥ `agent/skills.py` — `merchant_risk_probe()` 상호참조 추가

기존 함수 전체를 아래 코드로 교체:

```python
async def merchant_risk_probe(context: dict[str, Any]) -> dict[str, Any]:
    body     = context["body_evidence"]
    mcc      = body.get("mccCode")
    merchant = body.get("merchantName")

    # ── 기본 MCC 위험 분류 ──────────────────────────────────────────────────
    _HIGH_MCC   = {"5813", "7992", "5912", "7997", "5999"}
    _MEDIUM_MCC = {"5812", "5814", "7011", "4722"}
    mcc_str = str(mcc or "")
    if mcc_str in _HIGH_MCC:
        base_risk = "HIGH"
    elif mcc_str in _MEDIUM_MCC or mcc_str:
        base_risk = "MEDIUM"
    else:
        base_risk = "UNKNOWN"

    # ── 상호참조: holiday_probe 결과 반영 (신규) ─────────────────────────────
    prior = context.get("prior_tool_results") or []
    holiday_facts = next(
        (r.get("facts") or {} for r in prior if r.get("skill") == "holiday_compliance_probe"),
        {}
    )
    compound_flags: list[str] = []
    holiday_risk = bool(holiday_facts.get("holidayRisk"))

    if holiday_risk and base_risk == "HIGH":
        risk = "CRITICAL"
        compound_flags.append("휴일+고위험업종 복합")
    elif holiday_risk and base_risk == "MEDIUM":
        risk = "HIGH"
        compound_flags.append("휴일+중간위험업종 복합")
    else:
        risk = base_risk

    return {
        "skill": "merchant_risk_probe",
        "ok": True,
        "facts": {
            "mccCode": mcc,
            "merchantName": merchant,
            "merchantRisk": risk,
            "compoundRiskFlags": compound_flags,
            "holidayRiskConsidered": holiday_risk,
        },
        "summary": (
            "거래처/가맹점 업종 코드(MCC) 기반 위험도를 평가했습니다."
            + (f" 복합 위험 감지: {', '.join(compound_flags)}" if compound_flags else "")
        ),
    }
```

---

### ⑦ `agent/skills.py` — `policy_rulebook_probe()` 상호참조 추가

기존 함수 내부, `engine = create_engine(...)` **바로 앞**에 enriched_body 생성 블록을 추가하고,
이후 `search_policy_chunks()` 호출 시 `context["body_evidence"]` 대신 `enriched_body` 사용:

```python
async def policy_rulebook_probe(context: dict[str, Any]) -> dict[str, Any]:
    # ── 상호참조: prior_tool_results로 body_evidence 보강 (신규) ──────────────
    prior = context.get("prior_tool_results") or []
    enriched_body = dict(context["body_evidence"])

    for r in prior:
        skill = r.get("skill", "")
        facts = r.get("facts") or {}
        if skill == "holiday_compliance_probe" and facts.get("holidayRisk"):
            enriched_body["_enriched_holidayRisk"] = True
            if facts.get("hrStatus"):
                enriched_body.setdefault("hrStatus", facts["hrStatus"])
        elif skill == "merchant_risk_probe":
            m_risk = str(facts.get("merchantRisk") or "").upper()
            if m_risk in {"CRITICAL", "HIGH"}:
                enriched_body["_enriched_merchantRisk"] = m_risk
                enriched_body["_extra_keywords"] = ["고위험", "업종", "강화승인"]
    # ─────────────────────────────────────────────────────────────────────────

    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as db:
        candidates = search_policy_chunks(db, enriched_body, limit=20)   # enriched_body 사용
        refs = []
        for r in candidates[:5]:
            ref = dict(r)
            ref["adoption_reason"] = _adoption_reason_for_ref(ref, enriched_body)
            refs.append(ref)
        candidates_with_reason = []
        for r in candidates:
            ref = dict(r)
            ref.setdefault("adoption_reason", _adoption_reason_for_ref(ref, enriched_body))
            candidates_with_reason.append(ref)

    return {
        "skill": "policy_rulebook_probe",
        "ok": True,
        "facts": {
            "policy_refs": refs,
            "ref_count": len(refs),
            "retrieval_candidates": candidates_with_reason,
            "enriched_from_prior": [r.get("skill") for r in prior],   # 신규: 상호참조 기록
        },
        "summary": (
            f"규정집에서 관련 조항 {len(refs)}건을 조회했습니다."
            + (f" (prior {len(prior)}개 결과 반영)" if prior else "")
        ),
    }
```

---

### ⑧ `services/policy_service.py` — `build_policy_keywords()` 보강 키워드 처리

기존 `build_policy_keywords()` 함수의 **return 직전**에 아래 블록 추가:

```python
    # ── 상호참조 보강 키워드 처리 (신규) ────────────────────────────────────
    if body_evidence.get("_enriched_holidayRisk"):
        for kw in ["휴일", "주말", "공휴일"]:
            if kw not in keywords:
                keywords.append(kw)

    for kw in (body_evidence.get("_extra_keywords") or []):
        if kw not in keywords:
            keywords.append(kw)
    # ─────────────────────────────────────────────────────────────────────────

    return keywords   # 기존 return 유지
```

---

### ⑨ `tests/test_graph.py` — 단위 테스트 2개 클래스 추가

```python
class TestToolCrossReference(unittest.TestCase):
    """도구 간 prior_tool_results 상호참조 검증."""

    def _holiday_result(self, holiday_risk=True):
        return {
            "skill": "holiday_compliance_probe", "ok": True,
            "facts": {"holidayRisk": holiday_risk, "isHoliday": True, "hrStatus": "LEAVE"},
            "summary": "휴일/휴무/연차와 결제 시점을 교차 검증했습니다.",
        }

    def test_merchant_probe_upgrades_to_critical_on_holiday_compound(self):
        """holiday_risk=True + MCC 5813 → merchantRisk=CRITICAL."""
        import asyncio
        from agent.skills import merchant_risk_probe
        context = {
            "body_evidence": {"mccCode": "5813", "merchantName": "POC 심야 식대"},
            "prior_tool_results": [self._holiday_result(holiday_risk=True)],
        }
        result = asyncio.get_event_loop().run_until_complete(merchant_risk_probe(context))
        self.assertEqual(result["facts"]["merchantRisk"], "CRITICAL")
        self.assertTrue(result["facts"]["holidayRiskConsidered"])
        self.assertIn("휴일+고위험업종 복합", result["facts"]["compoundRiskFlags"])

    def test_merchant_probe_no_upgrade_without_holiday(self):
        """holiday_risk=False → MCC 5813은 HIGH 유지 (CRITICAL 아님)."""
        import asyncio
        from agent.skills import merchant_risk_probe
        context = {
            "body_evidence": {"mccCode": "5813", "merchantName": "POC 식대"},
            "prior_tool_results": [self._holiday_result(holiday_risk=False)],
        }
        result = asyncio.get_event_loop().run_until_complete(merchant_risk_probe(context))
        self.assertEqual(result["facts"]["merchantRisk"], "HIGH")

    def test_merchant_probe_without_prior_works_normally(self):
        """prior_tool_results 없을 때 기존 동작 유지."""
        import asyncio
        from agent.skills import merchant_risk_probe
        context = {
            "body_evidence": {"mccCode": "5813", "merchantName": "POC"},
            "prior_tool_results": [],
        }
        result = asyncio.get_event_loop().run_until_complete(merchant_risk_probe(context))
        self.assertEqual(result["facts"]["merchantRisk"], "HIGH")

    def test_skill_context_input_has_prior_field(self):
        """SkillContextInput에 prior_tool_results 필드 정상 동작."""
        from agent.tool_schemas import SkillContextInput
        inp = SkillContextInput(
            case_id="C1",
            body_evidence={},
            prior_tool_results=[{"skill": "test", "ok": True, "facts": {}, "summary": ""}],
        )
        self.assertEqual(len(inp.prior_tool_results), 1)

    def test_skill_context_input_prior_defaults_empty(self):
        """prior_tool_results 미전달 시 빈 리스트 기본값."""
        from agent.tool_schemas import SkillContextInput
        inp = SkillContextInput(case_id="C1", body_evidence={})
        self.assertEqual(inp.prior_tool_results, [])


class TestPlanAchievement(unittest.TestCase):
    """plan 달성도 계산 및 score 반영 검증."""

    def test_full_success_achievement_rate_1(self):
        """모든 도구 성공 → achievement_rate=1.0."""
        from agent.langgraph_agent import _compute_plan_achievement
        plan = [{"tool": "holiday_compliance_probe"}, {"tool": "merchant_risk_probe"}]
        results = [
            {"skill": "holiday_compliance_probe", "ok": True, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe",       "ok": True, "facts": {}, "summary": ""},
        ]
        ach = _compute_plan_achievement(plan, results)
        self.assertEqual(ach["achievement_rate"], 1.0)
        self.assertEqual(ach["succeeded"], 2)
        self.assertEqual(ach["failed"], 0)

    def test_partial_failure_reduces_rate(self):
        """일부 실패 → achievement_rate < 1.0."""
        from agent.langgraph_agent import _compute_plan_achievement
        plan = [{"tool": "holiday_compliance_probe"}, {"tool": "merchant_risk_probe"}]
        results = [
            {"skill": "holiday_compliance_probe", "ok": False, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe",       "ok": True,  "facts": {}, "summary": ""},
        ]
        ach = _compute_plan_achievement(plan, results)
        self.assertEqual(ach["achievement_rate"], 0.5)
        self.assertEqual(ach["failed"], 1)

    def test_skipped_tool_counted(self):
        """plan에 있으나 실행 안 된 도구 → skipped."""
        from agent.langgraph_agent import _compute_plan_achievement
        plan = [{"tool": "holiday_compliance_probe"}, {"tool": "legacy_aura_deep_audit"}]
        results = [
            {"skill": "holiday_compliance_probe", "ok": True, "facts": {}, "summary": ""},
        ]
        ach = _compute_plan_achievement(plan, results)
        self.assertEqual(ach["skipped"], 1)

    def test_low_success_rate_penalizes_evidence_score(self):
        """도구 성공률 < 50% → evidence_score 하향."""
        from agent.langgraph_agent import _score
        flags = {"isHoliday": False, "hrStatus": "WORKING", "isNight": False,
                 "budgetExceeded": False, "amount": 50000, "hasHitlResponse": False}

        failed = [
            {"skill": "holiday_compliance_probe", "ok": False, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe",       "ok": False, "facts": {}, "summary": ""},
            {"skill": "policy_rulebook_probe",     "ok": False, "facts": {"ref_count": 0}, "summary": ""},
        ]
        ok = [
            {"skill": "holiday_compliance_probe", "ok": True, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe",       "ok": True, "facts": {"merchantRisk": "LOW"}, "summary": ""},
            {"skill": "policy_rulebook_probe",     "ok": True, "facts": {"ref_count": 2}, "summary": ""},
        ]
        score_fail = _score(flags, failed)
        score_ok   = _score(flags, ok)
        self.assertGreater(score_ok["evidence_score"], score_fail["evidence_score"])

    def test_full_plan_achievement_bonus_in_reasons(self):
        """3개 이상 전체 성공 → reasons에 보너스 메시지 포함."""
        from agent.langgraph_agent import _score
        flags = {"isHoliday": False, "hrStatus": "WORKING", "isNight": False,
                 "budgetExceeded": False, "amount": 50000, "hasHitlResponse": False}
        results = [
            {"skill": "holiday_compliance_probe", "ok": True, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe",       "ok": True, "facts": {}, "summary": ""},
            {"skill": "policy_rulebook_probe",     "ok": True, "facts": {"ref_count": 1}, "summary": ""},
            {"skill": "document_evidence_probe",   "ok": True, "facts": {"lineItemCount": 1}, "summary": ""},
        ]
        result = _score(flags, results)
        self.assertTrue(
            any("전체 성공" in r for r in result.get("reasons", [])),
            f"전체 성공 보너스 메시지 없음. reasons={result.get('reasons')}"
        )
```

---

## 검증 커맨드

```bash
# 1. 단위 테스트
python -m pytest tests/test_graph.py::TestToolCrossReference -v
python -m pytest tests/test_graph.py::TestPlanAchievement -v

# 2. 상호참조 동작 수동 확인
python3 -c "
import asyncio
from agent.skills import merchant_risk_probe

prior = [{
    'skill': 'holiday_compliance_probe', 'ok': True,
    'facts': {'holidayRisk': True, 'isHoliday': True, 'hrStatus': 'LEAVE'},
}]
context = {
    'body_evidence': {'mccCode': '5813', 'merchantName': 'POC 심야 식대'},
    'prior_tool_results': prior,
}
result = asyncio.get_event_loop().run_until_complete(merchant_risk_probe(context))
print('merchantRisk:', result['facts']['merchantRisk'])        # 기대: CRITICAL
print('compoundRiskFlags:', result['facts']['compoundRiskFlags'])
print('holidayRiskConsidered:', result['facts']['holidayRiskConsidered'])
"

# 3. plan 달성도 확인
python3 -c "
from agent.langgraph_agent import _compute_plan_achievement
plan = [
    {'tool': 'holiday_compliance_probe'},
    {'tool': 'merchant_risk_probe'},
    {'tool': 'policy_rulebook_probe'},
]
results = [
    {'skill': 'holiday_compliance_probe', 'ok': True,  'facts': {}, 'summary': ''},
    {'skill': 'merchant_risk_probe',       'ok': True,  'facts': {}, 'summary': ''},
    {'skill': 'policy_rulebook_probe',     'ok': False, 'facts': {}, 'summary': ''},
]
ach = _compute_plan_achievement(plan, results)
print('achievement_rate:', ach['achievement_rate'])  # 기대: 0.667
print('succeeded:', ach['succeeded'])                # 기대: 2
print('failed:', ach['failed'])                      # 기대: 1
"

# 4. 전체 테스트 영향 없음 확인
python -m pytest tests/ -v
```

---

## 변경 전/후 비교

| 항목 | 변경 전 | 변경 후 |
|------|---------|---------|
| tool 호출 context | `body_evidence`만 | `body_evidence + prior_tool_results` |
| merchant_probe | MCC 단독 판단 | holiday 결과 참조 → CRITICAL 상향 가능 |
| policy_probe 키워드 | 원본 body만 | holiday/merchant 결과로 키워드 보강 |
| plan 달성도 평가 | 없음 | `_compute_plan_achievement()` |
| score ← plan | 연결 없음 | 도구 성공률로 evidence_score 보정 |
| 전체 성공 보상 | 없음 | +5점 evidence 보너스 |
| `AgentState` | `plan_achievement` 없음 | 필드 추가, execute_node 반환 |