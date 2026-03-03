"""
Tool schema contract test — 각 tool 입출력 스키마 검증.
공식 문서 8.13 테스트 전략.
"""
import unittest


class TestToolSchema(unittest.TestCase):
    """LangChain tool 입력/출력 스키마 검증."""

    def test_skill_context_input_schema(self):
        from agent.tool_schemas import SkillContextInput

        inp = SkillContextInput(case_id="C1", body_evidence={}, intended_risk_type=None)
        self.assertEqual(inp.case_id, "C1")
        d = inp.model_dump()
        self.assertIn("case_id", d)
        self.assertIn("body_evidence", d)
        self.assertIn("intended_risk_type", d)

    def test_tool_result_envelope_schema(self):
        from agent.tool_schemas import ToolResultEnvelope

        out = ToolResultEnvelope(skill="test_skill", ok=True, facts={}, summary="ok")
        self.assertEqual(out.skill, "test_skill")
        self.assertTrue(out.ok)

    def test_get_langchain_tools_returns_tools_with_schema(self):
        from agent.skills import get_langchain_tools

        tools = get_langchain_tools()
        self.assertGreater(len(tools), 0)
        for t in tools:
            self.assertTrue(hasattr(t, "name"))
            self.assertTrue(hasattr(t, "args_schema"))
            self.assertTrue(hasattr(t, "ainvoke") or hasattr(t, "invoke"))


if __name__ == "__main__":
    unittest.main()
