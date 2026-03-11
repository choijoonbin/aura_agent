import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.langgraph_nodes_ingest import _should_promote_to_deep, screener_node_impl
from services.case_service import upsert_agent_case_from_screening_result


class TestDeepPromotionRouter(unittest.TestCase):
    def _base_fast_result(self) -> dict:
        return {
            "screening_mode": "hybrid",
            "screening_source": "hybrid_llm_guardrail",
            "case_type": "UNUSUAL_PATTERN",
            "llm_case_type": "UNUSUAL_PATTERN",
            "llm_confidence": 0.9,
            "score": 70,
            "signals": {
                "is_holiday": False,
                "is_leave": False,
                "is_night": False,
                "budget_exceeded": False,
                "mcc_high_risk": False,
                "mcc_leisure": False,
            },
        }

    def test_promote_on_rule_llm_mismatch(self):
        fast = self._base_fast_result()
        fast["llm_case_type"] = "LIMIT_EXCEED"
        should_promote, reason = _should_promote_to_deep(fast)
        self.assertTrue(should_promote)
        self.assertIn("rule_llm_mismatch", reason)

    def test_promote_on_low_llm_confidence(self):
        fast = self._base_fast_result()
        fast["llm_confidence"] = 0.5
        should_promote, reason = _should_promote_to_deep(fast)
        self.assertTrue(should_promote)
        self.assertIn("llm_low_confidence", reason)

    def test_promote_on_boundary_score(self):
        fast = self._base_fast_result()
        fast["score"] = 55
        should_promote, reason = _should_promote_to_deep(fast)
        self.assertTrue(should_promote)
        self.assertIn("boundary_score", reason)

    def test_promote_on_normal_with_many_risk_signals(self):
        fast = self._base_fast_result()
        fast["case_type"] = "NORMAL_BASELINE"
        fast["llm_case_type"] = "NORMAL_BASELINE"
        fast["signals"]["is_holiday"] = True
        fast["signals"]["is_night"] = True
        should_promote, reason = _should_promote_to_deep(fast)
        self.assertTrue(should_promote)
        self.assertIn("normal_baseline_with_risk_signals", reason)

    def test_no_promote_on_fast_sufficient(self):
        fast = self._base_fast_result()
        should_promote, reason = _should_promote_to_deep(fast)
        self.assertFalse(should_promote)
        self.assertEqual(reason, "fast_sufficient")


class TestDeepLaneFallback(unittest.TestCase):
    def test_deep_lane_exception_falls_back_to_fast(self):
        state = {"body_evidence": {"occurredAt": "2026-03-11T10:00:00", "amount": 120000}}
        fast_result = {
            "case_type": "UNUSUAL_PATTERN",
            "severity": "MEDIUM",
            "score": 55,
            "reason_text": "fast result",
            "reasons": ["fast"],
            "screening_mode": "hybrid",
            "screening_source": "hybrid_llm_guardrail",
            "llm_case_type": "LIMIT_EXCEED",
            "llm_confidence": 0.9,
            "signals": {
                "is_holiday": False,
                "is_leave": False,
                "is_night": False,
                "budget_exceeded": False,
                "mcc_high_risk": False,
                "mcc_leisure": False,
            },
        }

        def fake_run_screening(_body):
            return dict(fast_result)

        class _BrokenDeepGraph:
            async def ainvoke(self, _payload):
                raise RuntimeError("deep graph failure")

        async def _run():
            with patch("agent.screening_subgraph.get_deep_screening_graph", return_value=_BrokenDeepGraph()):
                return await screener_node_impl(
                    state,
                    is_valid_screening_case_type=lambda _v: False,
                    run_screening=fake_run_screening,
                )

        result = asyncio.run(_run())
        self.assertEqual(result["screening_result"]["case_type"], fast_result["case_type"])
        self.assertEqual(result["screening_result"]["screening_mode"], fast_result["screening_mode"])
        lane = result["pending_events"][0].get("metadata", {}).get("lane")
        self.assertEqual(lane, "fast")
        self.assertEqual(result["pending_events"][1].get("metadata", {}).get("reasonText"), fast_result["reason_text"])


class TestScreeningMetaPersistence(unittest.TestCase):
    def test_upsert_clears_stale_screening_meta_when_none(self):
        existing = SimpleNamespace(
            case_type="HOLIDAY_USAGE",
            severity="HIGH",
            score=0.85,
            reason_text="old",
            status="NEW",
            screening_meta={"lane": "deep", "promotion_reason": "old"},
        )

        class _FakeDB:
            def __init__(self, obj):
                self.obj = obj
                self.committed = False

            def scalar(self, _stmt):
                return self.obj

            def add(self, _obj):
                self.obj = _obj

            def commit(self):
                self.committed = True

        db = _FakeDB(existing)
        upsert_agent_case_from_screening_result(
            db,
            "1000-H000000001-2026",
            case_type="NORMAL_BASELINE",
            severity="LOW",
            score=0.03,
            reason_text="latest",
            screening_meta=None,
        )
        self.assertTrue(db.committed)
        self.assertIsNone(existing.screening_meta)
