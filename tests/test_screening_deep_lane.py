"""
Deep Lane Screening 회귀 테스트.

케이스:
  1. _should_promote_to_deep — 4가지 승격 조건별 True/False 검증
  2. screener_node_impl — Deep lane ainvoke 실패 시 Fast fallback 검증
  3. screener_node_impl — Deep lane 타임아웃 시 Fast fallback 검증
  4. screener_node_impl — Deep lane 성공 시 Deep 결과 반영 검증
  5. upsert_agent_case_from_screening_result — screening_meta 신규 저장 검증
  6. upsert_agent_case_from_screening_result — Fast 재실행 시 기존 Deep meta → None 초기화 검증
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# 공통 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _fast_result(**overrides) -> dict:
    """승격 조건을 의도에 따라 제어할 수 있는 Fast 결과 픽스처."""
    base: dict = {
        "case_type": "NORMAL_BASELINE",
        "severity": "LOW",
        "score": 20,
        "reason_text": "Fast 결과",
        "reasons": [],
        "screening_mode": "hybrid",
        "screening_source": "hybrid_llm",
        "llm_case_type": "NORMAL_BASELINE",
        "llm_confidence": 0.90,
        "signals": {},
    }
    return {**base, **overrides}


def _body_evidence() -> dict:
    return {
        "occurredAt": "2024-01-01T10:00:00",
        "amount": 50000.0,
        "hrStatus": "WORK",
        "hrStatusRaw": "WORKING",
        "mccCode": None,
        "budgetExceeded": False,
        "isHoliday": True,
    }


_VALID_TYPES = {
    "HOLIDAY_USAGE", "LIMIT_EXCEED",
    "PRIVATE_USE_RISK", "UNUSUAL_PATTERN", "NORMAL_BASELINE",
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. 승격 라우터 (_should_promote_to_deep)
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldPromoteToDeep(unittest.TestCase):
    """_should_promote_to_deep 4가지 승격 조건 + 비승격 케이스."""

    def _call(self, fast: dict) -> tuple[bool, str]:
        from agent.langgraph_nodes_ingest import _should_promote_to_deep
        return _should_promote_to_deep(fast)

    # ── 승격 조건 1: LLM-규칙 불일치 ──────────────────────────────────────────

    def test_promote_rule_llm_mismatch(self):
        promote, reason = self._call(
            _fast_result(case_type="NORMAL_BASELINE", llm_case_type="HOLIDAY_USAGE")
        )
        self.assertTrue(promote)
        self.assertIn("rule_llm_mismatch", reason)

    # ── 승격 조건 2: LLM 저신뢰도 ────────────────────────────────────────────

    def test_promote_llm_low_confidence(self):
        promote, reason = self._call(_fast_result(llm_confidence=0.60))
        self.assertTrue(promote)
        self.assertIn("llm_low_confidence", reason)

    # ── 승격 조건 3: 경계 점수 구간 [45, 65] ─────────────────────────────────

    def test_promote_boundary_score_mid(self):
        promote, reason = self._call(_fast_result(score=55))
        self.assertTrue(promote)
        self.assertIn("boundary_score", reason)

    def test_promote_boundary_score_lower_edge(self):
        promote, _ = self._call(_fast_result(score=45))
        self.assertTrue(promote)

    def test_promote_boundary_score_upper_edge(self):
        promote, _ = self._call(_fast_result(score=65))
        self.assertTrue(promote)

    def test_no_promote_score_below_boundary(self):
        promote, _ = self._call(_fast_result(score=44))
        self.assertFalse(promote)

    def test_no_promote_score_above_boundary(self):
        promote, _ = self._call(_fast_result(score=66))
        self.assertFalse(promote)

    # ── 승격 조건 4: NORMAL_BASELINE + 위험 신호 ≥ 2 ─────────────────────────

    def test_promote_normal_baseline_with_two_risk_signals(self):
        promote, reason = self._call(
            _fast_result(signals={"is_holiday": True, "budget_exceeded": True})
        )
        self.assertTrue(promote)
        self.assertIn("normal_baseline_with_risk_signals", reason)

    def test_no_promote_normal_baseline_single_signal(self):
        promote, _ = self._call(_fast_result(signals={"is_holiday": True}))
        self.assertFalse(promote)

    # ── 비승격: LLM이 호출되지 않은 경우 ────────────────────────────────────

    def test_no_promote_rule_only_mode(self):
        promote, reason = self._call(
            _fast_result(screening_mode="rule", screening_source="rule")
        )
        self.assertFalse(promote)
        self.assertEqual(reason, "fast_rule_only_no_promote")

    def test_no_promote_hybrid_fallback_rule(self):
        promote, reason = self._call(
            _fast_result(screening_source="hybrid_fallback_rule")
        )
        self.assertFalse(promote)
        self.assertEqual(reason, "fast_rule_only_no_promote")

    # ── 비승격: 모든 조건 미충족 ─────────────────────────────────────────────

    def test_no_promote_fast_sufficient(self):
        promote, reason = self._call(_fast_result(llm_confidence=0.92, score=80))
        self.assertFalse(promote)
        self.assertEqual(reason, "fast_sufficient")


# ─────────────────────────────────────────────────────────────────────────────
# 2~4. screener_node_impl — Deep lane 성공/실패/타임아웃
#
# get_deep_screening_graph 는 screener_node_impl 내부에서
# `from agent.screening_subgraph import get_deep_screening_graph` 로 lazy import되므로
# agent.screening_subgraph 모듈 수준에서 패치해야 한다.
# ─────────────────────────────────────────────────────────────────────────────

class TestScreenerNodeDeepLane(unittest.IsolatedAsyncioTestCase):
    """screener_node_impl Deep lane 경로 검증."""

    # 승격 조건을 확실히 충족하는 Fast 결과 (boundary_score + llm_low_confidence)
    _FAST_PROMOTE = _fast_result(
        case_type="HOLIDAY_USAGE",
        llm_case_type="HOLIDAY_USAGE",
        llm_confidence=0.60,   # 조건 2 충족
        score=55,              # 조건 3 충족
    )

    async def _run(self, deep_side_effect=None, deep_return=None) -> dict:
        from agent.langgraph_nodes_ingest import screener_node_impl
        import agent.screening_subgraph as _subgraph_mod

        run_screening = MagicMock(return_value=self._FAST_PROMOTE)

        mock_graph = MagicMock()
        if deep_side_effect is not None:
            mock_graph.ainvoke = AsyncMock(side_effect=deep_side_effect)
        else:
            mock_graph.ainvoke = AsyncMock(return_value=deep_return)

        # screener_node_impl 내부의 lazy import 패치:
        # `from agent.screening_subgraph import get_deep_screening_graph`
        # → agent.screening_subgraph.get_deep_screening_graph 를 대체한다.
        with patch.object(_subgraph_mod, "get_deep_screening_graph", return_value=mock_graph) as _mock_fn:
            # 이미 캐싱된 서브그래프 싱글톤 무효화
            _subgraph_mod._DEEP_SCREENING_GRAPH = None
            return await screener_node_impl(
                {"body_evidence": _body_evidence()},
                is_valid_screening_case_type=lambda x: x in _VALID_TYPES,
                run_screening=run_screening,
            )

    def _sr_event(self, result: dict) -> dict:
        return next(
            e for e in result["pending_events"]
            if e.get("event_type") == "SCREENING_RESULT"
        )

    # ── Deep lane Exception → Fast fallback ──────────────────────────────────

    async def test_deep_exception_falls_back_to_fast(self):
        result = await self._run(deep_side_effect=RuntimeError("LLM 연결 실패"))

        self.assertEqual(result["intended_risk_type"], "HOLIDAY_USAGE")
        self.assertEqual(self._sr_event(result)["metadata"]["lane"], "fast")

    # ── Deep lane TimeoutError → Fast fallback ────────────────────────────────

    async def test_deep_timeout_falls_back_to_fast(self):
        result = await self._run(deep_side_effect=asyncio.TimeoutError())

        self.assertEqual(result["intended_risk_type"], "HOLIDAY_USAGE")
        self.assertEqual(self._sr_event(result)["metadata"]["lane"], "fast")

    # ── Deep lane 성공 → Deep 결과 채택 ──────────────────────────────────────

    async def test_deep_success_uses_deep_result(self):
        deep_meta = {
            "lane": "deep",
            "promotion_reason": "boundary_score(55)",
            "alt_hypotheses": [{"case_type": "PRIVATE_USE_RISK", "confidence": 0.82, "reason": "사적 사용"}],
            "decision_path": [],
            "align_reason": "llm_overrides",
            "uncertainty_reason": None,
            "fast_case_type": "HOLIDAY_USAGE",
            "fast_llm_case_type": "HOLIDAY_USAGE",
            "fast_llm_confidence": 0.60,
            "fast_score": 55,
        }
        deep_final = {
            **self._FAST_PROMOTE,
            "case_type": "PRIVATE_USE_RISK",
            "severity": "HIGH",
            "screening_mode": "deep",
            "screening_source": "deep_guardrail",
            "screening_meta": deep_meta,
        }
        result = await self._run(deep_return={"final_result": deep_final})

        self.assertEqual(result["intended_risk_type"], "PRIVATE_USE_RISK")
        ev = self._sr_event(result)
        self.assertEqual(ev["metadata"]["lane"], "deep")
        self.assertIsNotNone(ev["metadata"]["screening_meta"])
        self.assertEqual(
            ev["metadata"]["screening_meta"]["promotion_reason"], "boundary_score(55)"
        )

    # ── Deep lane이 빈 final_result 반환 → Fast fallback ─────────────────────

    async def test_deep_empty_final_result_falls_back_to_fast(self):
        result = await self._run(deep_return={"final_result": {}})

        self.assertEqual(result["intended_risk_type"], "HOLIDAY_USAGE")
        self.assertEqual(self._sr_event(result)["metadata"]["lane"], "fast")


# ─────────────────────────────────────────────────────────────────────────────
# 5 & 6. upsert_agent_case_from_screening_result — screening_meta 저장/초기화
#
# case_service → db.session → create_engine(psycopg2) 체인을 막기 위해
# db.session 을 sys.modules 에서 mock으로 교체한 뒤 임포트한다.
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_db_session_module():
    """db.session 에서 필요한 심볼만 노출하는 가짜 모듈."""
    mod = types.ModuleType("db.session")
    mod.Base = MagicMock()
    mod.SessionLocal = MagicMock()
    mod.get_db = MagicMock()
    return mod


def _make_mock_db_models_module():
    """db.models 에서 필요한 심볼만 노출하는 가짜 모듈.
    AgentCase 는 MagicMock으로 만들되, 생성자가 속성을 저장하도록 설정한다.
    """
    mod = types.ModuleType("db.models")

    class _FakeAgentCase:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    mod.AgentCase = _FakeAgentCase
    mod.FiDocHeader = MagicMock()
    mod.FiDocItem = MagicMock()
    return mod


class TestUpsertScreeningMeta(unittest.TestCase):
    """screening_meta 항상 덮어쓰기(None 포함) 검증.

    case_service 내부의 sqlalchemy.select 와 AgentCase 를 패치해
    psycopg2 / DB 연결 없이 순수 Python 로직만 검증한다.
    """

    def _run_upsert(self, *, existing_case, screening_meta, case_type="NORMAL_BASELINE"):
        """
        upsert_agent_case_from_screening_result 를 격리 실행.
        - select(AgentCase) 체인 전체를 mock
        - AgentCase 는 MagicMock(side_effect=...)으로 교체:
            클래스 속성 접근(WHERE 절 비교식)은 MagicMock 이 자동 처리하고,
            생성자 호출만 실제 속성 저장 객체로 대체한다.
        """
        from services.case_service import upsert_agent_case_from_screening_result
        import services.case_service as _cs_mod

        mock_select_chain = MagicMock()
        mock_select_chain.where.return_value = mock_select_chain

        class _FakeAgentCase:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

        # MagicMock 으로 AgentCase 클래스를 대체:
        #   - 클래스 레벨 속성(tenant_id, bukrs …) → 자동 MagicMock (WHERE 절 인수로 쓰임)
        #   - 생성자 호출 AgentCase(**kw) → _FakeAgentCase(**kw) 인스턴스 반환
        mock_case_class = MagicMock(side_effect=_FakeAgentCase)

        db = MagicMock()
        db.scalar.return_value = existing_case

        with patch.object(_cs_mod, "select", return_value=mock_select_chain), \
             patch.object(_cs_mod, "AgentCase", mock_case_class):
            upsert_agent_case_from_screening_result(
                db, "1000-0000000001-2024",
                case_type=case_type,
                severity="LOW",
                score=0.10,
                reason_text="테스트",
                screening_meta=screening_meta,
            )

        return db

    # ── 신규 케이스 ───────────────────────────────────────────────────────────

    def test_new_case_stores_deep_meta(self):
        meta = {"lane": "deep", "promotion_reason": "boundary_score(55)"}
        db = self._run_upsert(existing_case=None, screening_meta=meta)

        db.add.assert_called_once()
        self.assertEqual(db.add.call_args[0][0].screening_meta, meta)
        db.commit.assert_called_once()

    def test_new_case_stores_none_for_fast(self):
        db = self._run_upsert(existing_case=None, screening_meta=None)

        db.add.assert_called_once()
        self.assertIsNone(db.add.call_args[0][0].screening_meta)

    # ── 기존 케이스 갱신 ─────────────────────────────────────────────────────

    def test_existing_case_updates_deep_meta(self):
        existing = MagicMock()
        existing.screening_meta = None
        meta = {"lane": "deep", "promotion_reason": "llm_low_confidence(0.60)"}

        db = self._run_upsert(existing_case=existing, screening_meta=meta)

        self.assertEqual(existing.screening_meta, meta)
        db.commit.assert_called_once()

    def test_fast_rerun_clears_previous_deep_meta(self):
        """Fast 재실행(meta=None)이 이전 Deep meta를 None으로 초기화한다."""
        existing = MagicMock()
        existing.screening_meta = {
            "lane": "deep",
            "alt_hypotheses": [{"case_type": "HOLIDAY_USAGE"}],
        }

        db = self._run_upsert(existing_case=existing, screening_meta=None)

        # 이전 Deep meta가 None으로 덮어써져야 한다
        self.assertIsNone(existing.screening_meta)
        db.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
