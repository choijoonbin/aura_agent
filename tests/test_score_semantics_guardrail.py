import unittest

from agent.langgraph_reasoning import _extract_score_snapshot, _sanitize_reasoning_score_semantics
from agent.langgraph_scoring import _sanitize_summary_reason


class TestScoreSemanticsGuardrail(unittest.TestCase):
    def test_sanitize_summary_reason_removes_policy_praise_when_high_risk(self):
        text = (
            "이번 감사는 4개의 도구를 사용했으며, 정책점수는 100점으로 매우 우수한 결과입니다. "
            "근거점수는 57점으로 보통 수준입니다."
        )
        out = _sanitize_summary_reason(text, 100, 57, 83)
        self.assertIn("정책점수 100점은 위반/위험 신호 강도", out)
        self.assertNotIn("매우 우수한 결과", out)
        self.assertIn("근거점수 57점", out)
        self.assertIn("현재 최종점수는 83점", out)

    def test_extract_score_snapshot_supports_confidence_risk_signals(self):
        snapshot = _extract_score_snapshot(
            {
                "confidence_risk_signals": {
                    "policy_score": 100,
                    "evidence_score": 57,
                    "final_score": 81,
                }
            }
        )
        self.assertEqual(snapshot.get("policy_score"), 100)
        self.assertEqual(snapshot.get("evidence_score"), 57)
        self.assertEqual(snapshot.get("final_score"), 81)

    def test_reasoning_sanitizer_removes_conflicting_policy_sentence(self):
        raw = (
            "정책점수는 100점으로 매우 우수합니다. "
            "근거점수는 57점입니다. 추가 검토가 필요합니다."
        )
        out = _sanitize_reasoning_score_semantics(
            raw,
            {"policy_score": 100, "evidence_score": 57, "final_score": 81},
        )
        self.assertNotIn("매우 우수합니다", out)
        self.assertIn("정책점수 100점은 위반/위험 신호 강도로 높을수록 불리", out)


if __name__ == "__main__":
    unittest.main()
