from agent.langgraph_routes import _route_after_verify
from agent.langgraph_scoring import _derive_rule_gate, _resolve_final_decision
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
