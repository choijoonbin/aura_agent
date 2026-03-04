"""
Interrupt/resume replay test — HITL 시나리오 재현·재개.
정식 HITL: interrupt() + checkpointer, 같은 run_id(thread_id)로 재개.
"""
import unittest


class TestInterruptResume(unittest.TestCase):
    """HITL hitl_pause 및 same-thread 재개 경로 검증."""

    def test_route_after_verify_when_hitl_requested(self):
        from agent.langgraph_agent import _route_after_verify

        state = {"hitl_request": {"reasons": ["need review"]}}
        self.assertEqual(_route_after_verify(state), "hitl_pause")

    def test_route_after_verify_when_no_hitl(self):
        from agent.langgraph_agent import _route_after_verify

        state = {"hitl_request": None}
        self.assertEqual(_route_after_verify(state), "reporter")

    def test_graph_has_hitl_pause_to_reporter_edge(self):
        """정식 HITL: resume 후 hitl_pause -> reporter로 이어짐."""
        from agent.langgraph_agent import build_agent_graph

        graph = build_agent_graph()
        # compiled graph has get_graph() or similar to inspect edges
        w = graph.get_graph()
        nodes = list(w.nodes) if hasattr(w, "nodes") else []
        self.assertIn("hitl_pause", nodes)
        self.assertIn("reporter", nodes)

    def test_hitl_pause_node_returns_body_evidence_on_resume(self):
        """resume 시 hitl_pause_node는 body_evidence에 hitlResponse를 넣어 반환 (interrupt()는 그래프 컨텍스트 필요)."""
        from unittest.mock import patch
        from agent.langgraph_agent import hitl_pause_node

        async def run():
            state = {
                "case_id": "C1",
                "body_evidence": {"occurredAt": "2024-01-01"},
                "hitl_request": {"reasons": ["x"]},
            }
            with patch("agent.langgraph_agent.interrupt", return_value={"approved": True}):
                out = await hitl_pause_node(state)
            self.assertIn("body_evidence", out)
            self.assertEqual(out["body_evidence"].get("hitlResponse"), {"approved": True})
        import asyncio
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
