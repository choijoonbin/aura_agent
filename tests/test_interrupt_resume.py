"""
Interrupt/resume replay test — HITL 시나리오 재현·재개.
공식 문서 8.13. 현재는 Transitional HITL(새 run 재개) 기준.
"""
import unittest


class TestInterruptResume(unittest.TestCase):
    """HITL hitl_pause 및 새 run 재개 경로 검증."""

    def test_route_after_verify_when_hitl_requested(self):
        from agent.langgraph_agent import _route_after_verify

        state = {"hitl_request": {"reasons": ["need review"]}}
        self.assertEqual(_route_after_verify(state), "hitl_pause")

    def test_route_after_verify_when_no_hitl(self):
        from agent.langgraph_agent import _route_after_verify

        state = {"hitl_request": None}
        self.assertEqual(_route_after_verify(state), "reporter")

    def test_hitl_pause_node_returns_final_result_with_status(self):
        from agent.langgraph_agent import hitl_pause_node

        async def run():
            state = {
                "case_id": "C1",
                "score_breakdown": {"final_score": 50},
                "verification": {"quality_signals": ["HITL_REQUIRED"]},
                "hitl_request": {"reasons": ["x"]},
                "tool_results": [],
                "critique": None,
                "planner_output": None,
                "critic_output": None,
                "verifier_output": None,
            }
            out = await hitl_pause_node(state)
            self.assertIn("final_result", out)
            self.assertEqual(out["final_result"].get("status"), "HITL_REQUIRED")

        import asyncio
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
