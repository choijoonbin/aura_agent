"""
Citation binding regression test — reporter citation 구조·coverage 유지.
공식 문서 8.13 테스트 전략.
"""
import unittest


class TestCitationBinding(unittest.TestCase):
    """Reporter output sentences와 citation 연결 검증."""

    def test_citation_coverage_empty(self):
        from services.citation_metrics import citation_coverage

        self.assertEqual(citation_coverage(None), 0.0)
        self.assertEqual(citation_coverage({}), 0.0)
        self.assertEqual(citation_coverage({"sentences": []}), 0.0)

    def test_citation_coverage_partial(self):
        from services.citation_metrics import citation_coverage

        ro = {
            "sentences": [
                {"sentence": "a", "citations": [{"article": "1"}]},
                {"sentence": "b", "citations": []},
            ]
        }
        self.assertEqual(citation_coverage(ro), 0.5)

    def test_evidence_grounded(self):
        from services.citation_metrics import evidence_grounded

        self.assertFalse(evidence_grounded([]))
        self.assertTrue(evidence_grounded([{"citations": [{}]}]))
        self.assertFalse(evidence_grounded([{"citations": []}]))


if __name__ == "__main__":
    unittest.main()
