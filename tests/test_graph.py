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


if __name__ == "__main__":
    unittest.main()
