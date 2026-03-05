"""
Graph unit test — 메인 그래프 노드/엣지·상태 전이.
공식 문서 8.13 테스트 전략.
"""
import unittest


class TestAgentGraph(unittest.TestCase):
    """메인 오케스트레이션 그래프 구조 및 상태 전이."""

    def test_build_agent_graph_returns_compiled_graph(self):
        from agent.langgraph_agent import build_agent_graph

        graph = build_agent_graph()
        self.assertIsNotNone(graph)
        nodes = list(graph.nodes.keys()) if hasattr(graph, "nodes") else []
        self.assertIn("screener", nodes)
        self.assertIn("planner", nodes)
        self.assertIn("execute", nodes)
        self.assertIn("verify", nodes)
        self.assertIn("reporter", nodes)
        self.assertIn("hitl_pause", nodes)

    def test_graph_has_verify_conditional_edges(self):
        from agent.langgraph_agent import _route_after_verify

        state_hitl = {"hitl_request": {"required": True}}
        state_ready = {"hitl_request": None}
        self.assertEqual(_route_after_verify(state_hitl), "hitl_pause")
        self.assertEqual(_route_after_verify(state_ready), "reporter")

    def test_graph_has_critic_conditional_edges(self):
        from agent.langgraph_agent import _route_after_critic

        state_replan = {
            "critic_output": {"replan_required": True},
            "critic_loop_count": 0,
            "flags": {"hasHitlResponse": False},
        }
        state_limit_reached = {
            "critic_output": {"replan_required": True},
            "critic_loop_count": 2,
            "flags": {"hasHitlResponse": False},
        }
        state_hitl_exists = {
            "critic_output": {"replan_required": True},
            "critic_loop_count": 0,
            "flags": {"hasHitlResponse": True},
        }
        state_no_replan = {
            "critic_output": {"replan_required": False},
            "critic_loop_count": 0,
            "flags": {"hasHitlResponse": False},
        }

        self.assertEqual(_route_after_critic(state_replan), "planner")
        self.assertEqual(_route_after_critic(state_limit_reached), "verify")
        self.assertEqual(_route_after_critic(state_hitl_exists), "verify")
        self.assertEqual(_route_after_critic(state_no_replan), "verify")


class TestScoreEngine(unittest.TestCase):
    """_score() 함수 5단계 점수 산출 검증."""

    def _make_tool_results(
        self,
        merchant_risk="MEDIUM",
        ref_count=2,
        line_count=1,
        holiday_risk=False,
    ):
        return [
            {
                "skill": "holiday_compliance_probe",
                "ok": True,
                "facts": {"holidayRisk": holiday_risk, "isHoliday": holiday_risk},
            },
            {
                "skill": "merchant_risk_probe",
                "ok": True,
                "facts": {"merchantRisk": merchant_risk, "mccCode": "5813"},
            },
            {
                "skill": "policy_rulebook_probe",
                "ok": True,
                "facts": {"ref_count": ref_count, "policy_refs": [{}] * ref_count},
            },
            {
                "skill": "document_evidence_probe",
                "ok": True,
                "facts": {"lineItemCount": line_count},
            },
        ]

    def test_holiday_only_base_score(self):
        from agent.langgraph_agent import _score

        flags = {"isHoliday": True, "hrStatus": "WORKING", "isNight": False, "budgetExceeded": False, "amount": 50000, "hasHitlResponse": False}
        result = _score(flags, [])
        self.assertGreaterEqual(result["policy_score"], 35)

    def test_high_merchant_risk_adds_policy_score(self):
        from agent.langgraph_agent import _score

        flags = {"isHoliday": False, "hrStatus": "WORKING", "isNight": False, "budgetExceeded": False, "amount": 100000, "hasHitlResponse": False}
        result_high = _score(flags, self._make_tool_results(merchant_risk="HIGH"))
        result_medium = _score(flags, self._make_tool_results(merchant_risk="MEDIUM"))
        self.assertGreater(result_high["policy_score"], result_medium["policy_score"])

    def test_more_policy_refs_higher_evidence_score(self):
        from agent.langgraph_agent import _score

        flags = {"isHoliday": False, "hrStatus": "WORKING", "isNight": False, "budgetExceeded": False, "amount": 100000, "hasHitlResponse": False}
        result_5 = _score(flags, self._make_tool_results(ref_count=5))
        result_1 = _score(flags, self._make_tool_results(ref_count=1))
        self.assertGreater(result_5["evidence_score"], result_1["evidence_score"])

    def test_compound_multiplier_applied_for_multiple_risks(self):
        from agent.langgraph_agent import _score

        flags = {
            "isHoliday": True,
            "hrStatus": "LEAVE",
            "isNight": True,
            "budgetExceeded": True,
            "amount": 300000,
            "hasHitlResponse": False,
        }
        result = _score(flags, self._make_tool_results(merchant_risk="HIGH", holiday_risk=True))
        self.assertGreater(result.get("compound_multiplier", 1.0), 1.0)

    def test_large_amount_increases_final_score(self):
        from agent.langgraph_agent import _score

        base_flags = {"isHoliday": True, "hrStatus": "LEAVE", "isNight": False, "budgetExceeded": False, "hasHitlResponse": False}
        tool_results = self._make_tool_results()
        result_large = _score({**base_flags, "amount": 3_000_000}, tool_results)
        result_small = _score({**base_flags, "amount": 30_000}, tool_results)
        self.assertGreater(result_large["final_score"], result_small["final_score"])

    def test_final_score_max_100(self):
        from agent.langgraph_agent import _score

        flags = {
            "isHoliday": True,
            "hrStatus": "LEAVE",
            "isNight": True,
            "budgetExceeded": True,
            "amount": 10_000_000,
            "hasHitlResponse": False,
        }
        result = _score(flags, self._make_tool_results(merchant_risk="HIGH", ref_count=5, line_count=5, holiday_risk=True))
        self.assertLessEqual(result["final_score"], 100)

    def test_backward_compat_keys_present(self):
        from agent.langgraph_agent import _score

        flags = {"isHoliday": False, "hrStatus": "WORKING", "isNight": False, "budgetExceeded": False, "amount": 0, "hasHitlResponse": False}
        result = _score(flags, [])
        for key in ("policy_score", "evidence_score", "final_score", "reasons"):
            self.assertIn(key, result, f"필수 키 '{key}' 누락")

    def test_signals_list_populated(self):
        from agent.langgraph_agent import _score

        flags = {"isHoliday": True, "hrStatus": "LEAVE", "isNight": True, "budgetExceeded": False, "amount": 200000, "hasHitlResponse": False}
        result = _score(flags, self._make_tool_results(merchant_risk="HIGH"))
        self.assertGreater(len(result.get("signals", [])), 0)

    def test_calculation_trace_present(self):
        from agent.langgraph_agent import _score

        flags = {"isHoliday": True, "hrStatus": "WORKING", "isNight": False, "budgetExceeded": False, "amount": 100000, "hasHitlResponse": False}
        result = _score(flags, self._make_tool_results())
        self.assertIn("calculation_trace", result)
        self.assertIn("policy", result["calculation_trace"])

    def test_amount_weight_is_continuous_around_threshold(self):
        from agent.langgraph_agent import _score

        flags = {"isHoliday": True, "hrStatus": "LEAVE", "isNight": False, "budgetExceeded": False, "hasHitlResponse": False}
        tool_results = self._make_tool_results()
        low = _score({**flags, "amount": 499_999}, tool_results)
        high = _score({**flags, "amount": 500_000}, tool_results)
        self.assertGreaterEqual(high["amount_weight"], low["amount_weight"])
        self.assertLess(high["amount_weight"] - low["amount_weight"], 0.02)

    def test_evidence_shortage_penalty_applied_on_high_risk(self):
        from agent.langgraph_agent import _score

        flags = {"isHoliday": True, "hrStatus": "LEAVE", "isNight": True, "budgetExceeded": True, "amount": 500_000, "hasHitlResponse": False}
        weak = _score(flags, self._make_tool_results(merchant_risk="HIGH", ref_count=0, line_count=0, holiday_risk=True))
        strong = _score(flags, self._make_tool_results(merchant_risk="HIGH", ref_count=5, line_count=3, holiday_risk=True))
        self.assertLess(weak["final_score"], strong["final_score"])
        self.assertTrue(any(s.get("signal") == "evidence_shortage_penalty" for s in weak.get("signals", [])))


class TestVerificationTargets(unittest.TestCase):

    def _state(
        self,
        *,
        night=False,
        holiday=False,
        hr="WORKING",
        mcc=None,
        amount=140937,
        m_risk="MEDIUM",
        h_risk=False,
        refs=None,
    ):
        return {
            "body_evidence": {
                "occurredAt": "2026-03-07T22:24:00",
                "amount": amount,
                "merchantName": "POC 심야 식대",
                "mccCode": mcc,
                "hrStatus": hr,
                "isHoliday": holiday,
            },
            "flags": {
                "isHoliday": holiday,
                "isNight": night,
                "hrStatus": hr,
                "mccCode": mcc,
                "budgetExceeded": False,
                "amount": amount,
            },
            "tool_results": [
                {
                    "skill": "holiday_compliance_probe",
                    "ok": True,
                    "facts": {"holidayRisk": h_risk, "isHoliday": holiday},
                },
                {
                    "skill": "merchant_risk_probe",
                    "ok": True,
                    "facts": {"merchantRisk": m_risk, "mccCode": mcc},
                },
                {
                    "skill": "policy_rulebook_probe",
                    "ok": True,
                    "facts": {"ref_count": len(refs or []), "policy_refs": refs or []},
                },
            ],
            "planner_output": {"steps": []},
        }

    def test_night_claim_contains_article_and_time(self):
        from agent.langgraph_agent import _build_verification_targets

        targets = _build_verification_targets(
            self._state(night=True, holiday=True, hr="LEAVE", mcc="5813", m_risk="HIGH", h_risk=True)
        )
        combined = " ".join(targets)
        self.assertTrue("제23조" in combined or "심야" in combined)
        self.assertTrue("22:24" in combined or "심야" in combined)

    def test_holiday_hr_claim_contains_leave_and_article(self):
        from agent.langgraph_agent import _build_verification_targets

        targets = _build_verification_targets(self._state(holiday=True, hr="LEAVE", h_risk=True))
        combined = " ".join(targets)
        self.assertTrue("LEAVE" in combined or "휴가" in combined)
        self.assertTrue("제39조" in combined or "주말" in combined)

    def test_high_mcc_claim_contains_code_and_article(self):
        from agent.langgraph_agent import _build_verification_targets

        targets = _build_verification_targets(self._state(mcc="5813", m_risk="HIGH"))
        combined = " ".join(targets)
        self.assertTrue("5813" in combined or "42조" in combined or "고위험" in combined)

    def test_max_4_targets(self):
        from agent.langgraph_agent import _build_verification_targets

        targets = _build_verification_targets(
            self._state(
                night=True,
                holiday=True,
                hr="LEAVE",
                mcc="5813",
                m_risk="HIGH",
                h_risk=True,
                amount=600000,
                refs=[{"article": "제23조", "parent_title": "식대"}, {"article": "제39조", "parent_title": "주말공휴일"}],
            )
        )
        self.assertLessEqual(len(targets), 4)

    def test_no_weak_applicable_pattern(self):
        import re
        from agent.langgraph_agent import _build_verification_targets

        targets = _build_verification_targets(
            self._state(night=True, holiday=True, hr="LEAVE", mcc="5813", m_risk="HIGH")
        )
        for target in targets:
            self.assertFalse(
                bool(re.match(r"^.{0,40}조항이 해당 사례에 적용될 수 있음\.$", target)),
                f"약한 주장 감지: {target}",
            )

    def test_chunk_supports_requires_article_match(self):
        from services.evidence_verification import _chunk_supports_claim

        claim = "제23조 심야 시간대 식대 위반 가능"
        wrong_art = {"chunk_text": "제38조 심야 시간대 지출 검토 대상이다", "article": "제38조"}
        self.assertFalse(_chunk_supports_claim(claim, wrong_art))

    def test_chunk_supports_requires_3_words(self):
        from services.evidence_verification import _chunk_supports_claim

        claim = "2026-03-07 22:24 심야 시간대 제23조 식대 위반 가능"
        good = {"chunk_text": "제23조 심야 시간대 식대 경고 대상이다", "article": "제23조"}
        bad = {"chunk_text": "전표 입력 기준을 준수해야 한다", "article": "제17조"}
        self.assertTrue(_chunk_supports_claim(claim, good))
        self.assertFalse(_chunk_supports_claim(claim, bad))


class TestToolCrossReference(unittest.TestCase):
    """도구 간 prior_tool_results 상호참조 검증."""

    def _holiday_result(self, holiday_risk=True):
        return {
            "skill": "holiday_compliance_probe",
            "ok": True,
            "facts": {"holidayRisk": holiday_risk, "isHoliday": True, "hrStatus": "LEAVE"},
            "summary": "휴일/휴무/연차와 결제 시점을 교차 검증했습니다.",
        }

    def test_merchant_probe_upgrades_to_critical_on_holiday_compound(self):
        import asyncio
        from agent.skills import merchant_risk_probe

        context = {
            "body_evidence": {"mccCode": "5813", "merchantName": "POC 심야 식대"},
            "prior_tool_results": [self._holiday_result(holiday_risk=True)],
        }
        result = asyncio.run(merchant_risk_probe(context))
        self.assertEqual(result["facts"]["merchantRisk"], "CRITICAL")
        self.assertTrue(result["facts"]["holidayRiskConsidered"])
        self.assertIn("휴일+고위험업종 복합", result["facts"]["compoundRiskFlags"])

    def test_merchant_probe_no_upgrade_without_holiday(self):
        import asyncio
        from agent.skills import merchant_risk_probe

        context = {
            "body_evidence": {"mccCode": "5813", "merchantName": "POC 식대"},
            "prior_tool_results": [self._holiday_result(holiday_risk=False)],
        }
        result = asyncio.run(merchant_risk_probe(context))
        self.assertEqual(result["facts"]["merchantRisk"], "HIGH")

    def test_merchant_probe_without_prior_works_normally(self):
        import asyncio
        from agent.skills import merchant_risk_probe

        context = {
            "body_evidence": {"mccCode": "5813", "merchantName": "POC"},
            "prior_tool_results": [],
        }
        result = asyncio.run(merchant_risk_probe(context))
        self.assertEqual(result["facts"]["merchantRisk"], "HIGH")

    def test_skill_context_input_has_prior_field(self):
        from agent.tool_schemas import SkillContextInput

        inp = SkillContextInput(
            case_id="C1",
            body_evidence={},
            prior_tool_results=[{"skill": "test", "ok": True, "facts": {}, "summary": ""}],
        )
        self.assertEqual(len(inp.prior_tool_results), 1)

    def test_skill_context_input_prior_defaults_empty(self):
        from agent.tool_schemas import SkillContextInput

        inp = SkillContextInput(case_id="C1", body_evidence={})
        self.assertEqual(inp.prior_tool_results, [])


class TestPlanAchievement(unittest.TestCase):
    """plan 달성도 계산 및 score 반영 검증."""

    def test_full_success_achievement_rate_1(self):
        from agent.langgraph_agent import _compute_plan_achievement

        plan = [{"tool": "holiday_compliance_probe"}, {"tool": "merchant_risk_probe"}]
        results = [
            {"skill": "holiday_compliance_probe", "ok": True, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe", "ok": True, "facts": {}, "summary": ""},
        ]
        ach = _compute_plan_achievement(plan, results)
        self.assertEqual(ach["achievement_rate"], 1.0)
        self.assertEqual(ach["succeeded"], 2)
        self.assertEqual(ach["failed"], 0)

    def test_partial_failure_reduces_rate(self):
        from agent.langgraph_agent import _compute_plan_achievement

        plan = [{"tool": "holiday_compliance_probe"}, {"tool": "merchant_risk_probe"}]
        results = [
            {"skill": "holiday_compliance_probe", "ok": False, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe", "ok": True, "facts": {}, "summary": ""},
        ]
        ach = _compute_plan_achievement(plan, results)
        self.assertEqual(ach["achievement_rate"], 0.5)
        self.assertEqual(ach["failed"], 1)

    def test_skipped_tool_counted(self):
        from agent.langgraph_agent import _compute_plan_achievement

        plan = [{"tool": "holiday_compliance_probe"}, {"tool": "legacy_aura_deep_audit"}]
        results = [{"skill": "holiday_compliance_probe", "ok": True, "facts": {}, "summary": ""}]
        ach = _compute_plan_achievement(plan, results)
        self.assertEqual(ach["skipped"], 1)

    def test_low_success_rate_penalizes_evidence_score(self):
        from agent.langgraph_agent import _score

        flags = {
            "isHoliday": False,
            "hrStatus": "WORKING",
            "isNight": False,
            "budgetExceeded": False,
            "amount": 50000,
            "hasHitlResponse": False,
        }

        failed = [
            {"skill": "holiday_compliance_probe", "ok": False, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe", "ok": False, "facts": {}, "summary": ""},
            {"skill": "policy_rulebook_probe", "ok": False, "facts": {"ref_count": 0}, "summary": ""},
        ]
        ok = [
            {"skill": "holiday_compliance_probe", "ok": True, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe", "ok": True, "facts": {"merchantRisk": "LOW"}, "summary": ""},
            {"skill": "policy_rulebook_probe", "ok": True, "facts": {"ref_count": 2}, "summary": ""},
        ]
        score_fail = _score(flags, failed)
        score_ok = _score(flags, ok)
        self.assertGreater(score_ok["evidence_score"], score_fail["evidence_score"])

    def test_full_plan_achievement_bonus_in_reasons(self):
        from agent.langgraph_agent import _score

        flags = {
            "isHoliday": False,
            "hrStatus": "WORKING",
            "isNight": False,
            "budgetExceeded": False,
            "amount": 50000,
            "hasHitlResponse": False,
        }
        results = [
            {"skill": "holiday_compliance_probe", "ok": True, "facts": {}, "summary": ""},
            {"skill": "merchant_risk_probe", "ok": True, "facts": {}, "summary": ""},
            {"skill": "policy_rulebook_probe", "ok": True, "facts": {"ref_count": 1}, "summary": ""},
            {"skill": "document_evidence_probe", "ok": True, "facts": {"lineItemCount": 1}, "summary": ""},
        ]
        result = _score(flags, results)
        self.assertTrue(
            any("전체 성공" in reason for reason in result.get("reasons", [])),
            f"전체 성공 보너스 메시지 없음. reasons={result.get('reasons')}",
        )


if __name__ == "__main__":
    unittest.main()
