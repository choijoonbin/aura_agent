import asyncio
from unittest.mock import AsyncMock, patch

from agent.langgraph_routes import _route_after_verify
from agent.langgraph_scoring import (
    _derive_rule_gate,
    _resolve_final_decision,
    _mark_circuit,
)
import agent.langgraph_scoring as _scoring_mod
from agent.langgraph_nodes_review import _should_re_evaluate


def test_resolve_final_decision_respects_gate_priority():
    out = _resolve_final_decision(rule_gate="hold", rule_score=10, llm_score=99)
    assert out == "HOLD"


def test_derive_rule_gate_regenerate_on_critical_tool_failure():
    score = {"final_score": 80, "evidence_completeness": 0.9}
    tools = [
        {"tool": "policy_rulebook_probe", "ok": False, "facts": {}},
    ]
    assert _derive_rule_gate(score, tools) == "regenerate"


def test_should_re_evaluate_by_score_gap_and_retry_limit():
    sb = {"rule_score": 85, "llm_score": 50, "fidelity": 70}
    assert _should_re_evaluate(sb, retry_count=0, max_retries=2) is True
    assert _should_re_evaluate(sb, retry_count=2, max_retries=2) is False


def test_route_after_verify_prefers_planner_on_replan_required():
    state = {
        "verifier_output": {"replan_required": True},
        "retry_count": 0,
        "max_retries": 2,
        "flags": {"hasHitlResponse": False},
        "hitl_request": {"required": True},
    }
    assert _route_after_verify(state) == "planner"


def test_route_after_verify_hitl_when_no_replan():
    state = {
        "verifier_output": {"replan_required": False},
        "retry_count": 0,
        "max_retries": 2,
        "flags": {"hasHitlResponse": False},
        "hitl_request": {"required": True},
    }
    assert _route_after_verify(state) == "hitl_pause"


# ── TASK-03 추가 테스트 ───────────────────────────────────────────


def test_fallback_on_llm_provider_error():
    """LLM 호출 실패(provider error) 시 fallback_used=True, llm_score=rule_score."""
    from agent.output_models import FallbackReason

    # _call_llm_judge를 강제로 실패시켜 fallback 경로 검증
    async def _run():
        with patch.object(_scoring_mod, "_call_llm_judge", new=AsyncMock(side_effect=RuntimeError("api down"))):
            with patch.object(_scoring_mod, "settings") as mock_cfg:
                mock_cfg.llm_judge_enabled = True
                mock_cfg.llm_judge_max_retries = 0
                mock_cfg.llm_judge_timeout_ms = 3000
                mock_cfg.llm_judge_circuit_window = 10
                mock_cfg.llm_judge_circuit_failure_threshold = 0.5
                mock_cfg.llm_judge_conflict_threshold = 20
                mock_cfg.score_policy_weight = 0.6
                mock_cfg.score_evidence_weight = 0.4
                mock_cfg.score_compound_multiplier_max = 1.5
                mock_cfg.score_amount_multiplier_max = 1.3
                mock_cfg.enable_legacy_aura_specialist = False
                state = {"body_evidence": {"amount": 50000}}
                flags = {"isHoliday": False, "budgetExceeded": False, "isNight": False, "amount": 50000}
                result = await _scoring_mod._score_hybrid(state, flags, [])
        return result

    result = asyncio.get_event_loop().run_until_complete(_run())
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == FallbackReason.PROVIDER_ERROR.value
    # fallback 시 llm_score는 rule_score와 동일해야 한다
    assert result["llm_score"] == result["rule_score"]


def test_conflict_warning_triggers_caution_gate():
    """rule_score와 llm_score 편차 >= threshold → conflict_warning, CAUTION 결정."""
    # 편차가 threshold(20) 이상 → CAUTION
    result_caution = _resolve_final_decision(
        rule_gate="pass",
        rule_score=80,
        llm_score=55,
        conflict_warning=True,
    )
    assert result_caution == "CAUTION"

    # 편차 없음 + conflict_warning=False → PASS
    result_pass = _resolve_final_decision(
        rule_gate="pass",
        rule_score=40,
        llm_score=42,
        conflict_warning=False,
    )
    assert result_pass == "PASS"

    # gate=HOLD는 LLM 점수·conflict 여부 무관하게 최우선
    result_hold = _resolve_final_decision(
        rule_gate="hold",
        rule_score=10,
        llm_score=95,
        conflict_warning=True,
    )
    assert result_hold == "HOLD"


def test_circuit_breaker_opens_on_high_failure_rate():
    """연속 fallback 비율 >= 50% → circuit breaker 활성화."""
    # 상태 초기화
    _scoring_mod._JUDGE_CIRCUIT_HISTORY.clear()
    _scoring_mod._JUDGE_CIRCUIT_OPEN = False

    window = 10
    _scoring_mod._JUDGE_CIRCUIT_HISTORY = __import__("collections").deque(maxlen=window)

    # 10회 중 6회 실패(60%) → circuit open
    for i in range(window):
        _mark_circuit(fallback_used=(i < 6))

    assert _scoring_mod._JUDGE_CIRCUIT_OPEN is True

    # 초기화 후 실패 0회 → circuit 열리지 않음
    _scoring_mod._JUDGE_CIRCUIT_HISTORY.clear()
    _scoring_mod._JUDGE_CIRCUIT_OPEN = False
    for _ in range(window):
        _mark_circuit(fallback_used=False)

    assert _scoring_mod._JUDGE_CIRCUIT_OPEN is False
