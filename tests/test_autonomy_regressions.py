import asyncio
import json
from pathlib import Path


def _override_setting(settings_obj, key: str, value):
    original = getattr(settings_obj, key)
    object.__setattr__(settings_obj, key, value)
    return original


def test_execute_parallel_keeps_holiday_dependency(monkeypatch):
    from agent import langgraph_agent as lg
    from agent.agent_tools import merchant_risk_probe

    async def _fake_stream(node, text, context=None):
        return text, [], "test"

    async def _holiday_tool(payload):
        return {
            "tool": "holiday_compliance_probe",
            "ok": True,
            "facts": {"holidayRisk": True, "isHoliday": True},
            "summary": "holiday ok",
        }

    class _Tool:
        def __init__(self, handler, description):
            self._handler = handler
            self.description = description

        async def ainvoke(self, payload):
            return await self._handler(payload)

    tools = {
        "holiday_compliance_probe": _Tool(_holiday_tool, "holiday"),
        "merchant_risk_probe": _Tool(merchant_risk_probe, "merchant"),
    }
    monkeypatch.setattr(lg, "_get_tools_by_name", lambda: tools)
    monkeypatch.setattr(lg, "_stream_reasoning_events_with_llm", _fake_stream)

    original_parallel = _override_setting(lg.settings, "enable_parallel_tool_execution", True)
    try:
        state = {
            "case_id": "case-parallel-dep",
            "body_evidence": {"isHoliday": True, "mccCode": "5813", "merchantName": "test"},
            "flags": {
                "isHoliday": True,
                "hrStatus": "LEAVE",
                "mccCode": "5813",
                "isNight": False,
                "budgetExceeded": False,
                "amount": 100000,
                "hasHitlResponse": False,
            },
            "plan": [
                {"tool": "holiday_compliance_probe", "reason": "휴일 확인", "owner": "planner"},
                {"tool": "merchant_risk_probe", "reason": "업종 확인", "owner": "planner"},
            ],
        }
        out = asyncio.run(lg.execute_node(state))
        merchant = next(r for r in out["tool_results"] if (r.get("tool") or r.get("skill")) == "merchant_risk_probe")
        assert merchant["facts"]["holidayRiskConsidered"] is True
        assert merchant["facts"]["merchantRisk"] == "CRITICAL"
    finally:
        object.__setattr__(lg.settings, "enable_parallel_tool_execution", original_parallel)


def test_planner_marks_plan_source_and_uses_step_reasons(monkeypatch):
    from agent import langgraph_agent as lg

    async def _fake_stream(node, text, context=None):
        return text, [], "test"

    async def _fake_llm_planner(flags, screening, replan_context, available_tools):
        return [
            {"tool": "holiday_compliance_probe", "reason": "휴일 플래그가 있어 우선 검증", "owner": "llm_planner"},
            {"tool": "document_evidence_probe", "reason": "증빙 라인아이템 확보", "owner": "llm_planner"},
        ]

    monkeypatch.setattr(lg, "_stream_reasoning_events_with_llm", _fake_stream)
    monkeypatch.setattr(lg, "_invoke_llm_planner", _fake_llm_planner)
    monkeypatch.setattr(
        lg,
        "_available_planner_tools",
        lambda: [
            {"name": "holiday_compliance_probe", "when": "holiday"},
            {"name": "document_evidence_probe", "when": "always"},
        ],
    )

    original_llm = _override_setting(lg.settings, "enable_llm_planner", True)
    try:
        state = {
            "flags": {"isHoliday": True, "hrStatus": "LEAVE", "mccCode": None, "budgetExceeded": False},
            "screening_result": {"case_type": "HOLIDAY_USAGE", "severity": "HIGH"},
        }
        out = asyncio.run(lg.planner_node(state))
        reasoning = out["planner_output"]["reasoning"]
        assert "휴일 플래그가 있어 우선 검증" in reasoning
        assert "플래그(휴일/예산/가맹점 업종 코드(MCC) 등)와 기본 조사 경로로 도구 순서를 결정했다." not in reasoning
        plan_ready = next(e for e in out["pending_events"] if e.get("event_type") == "PLAN_READY")
        assert plan_ready["metadata"]["plan_source"] == "llm"
    finally:
        object.__setattr__(lg.settings, "enable_llm_planner", original_llm)


def test_mcc_sets_can_load_from_json(tmp_path):
    from utils import config

    payload = {
        "high_risk": ["1111", "2222"],
        "leisure": ["3333"],
        "medium_risk": ["4444"],
    }
    p = Path(tmp_path) / "mcc.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    original_source = _override_setting(config.settings, "mcc_source", "json")
    original_path = _override_setting(config.settings, "mcc_json_path", str(p))
    try:
        config.refresh_mcc_sets()
        sets = config.get_mcc_sets()
        assert sets["high_risk"] == {"1111", "2222"}
        assert sets["leisure"] == {"3333"}
        assert sets["medium_risk"] == {"4444"}
    finally:
        object.__setattr__(config.settings, "mcc_source", original_source)
        object.__setattr__(config.settings, "mcc_json_path", original_path)
        config.refresh_mcc_sets()


def test_checkpointer_memory_backend_path():
    from agent import langgraph_agent as lg

    original_backend = _override_setting(lg.settings, "checkpointer_backend", "memory")
    original_cp = lg._CHECKPOINTER
    try:
        lg._CHECKPOINTER = None
        cp = lg._get_checkpointer()
        assert cp is not None
        assert cp.__class__.__name__ in {"MemorySaver", "InMemorySaver"}
    finally:
        object.__setattr__(lg.settings, "checkpointer_backend", original_backend)
        lg._CHECKPOINTER = original_cp
