"""
RAG 청킹 및 검색 단위 테스트.
"""
import unittest

SAMPLE_TEXT = """
제3장 경비 유형별 기준
======================================================================

제23조 (식대)
① 업무상 식대는 인당 기준한도 및 참석자 기준을 충족하여야 하며, 사적 목적 식대를 금지한다.
② 다음 각 호에 해당하는 식대는 검토 대상으로 분류한다.
1. 23:00~06:00 심야 식대
2. 주말/공휴일 식대(예외 승인 없는 경우)
3. 인당 한도 초과 식대
③ 식대 증빙은 참석자 명단과 영수증을 포함하여야 한다.

제24조 (접대비)
① 접대비는 사전 승인을 받아야 한다.
② 접대비 한도는 별표에 따른다.
"""


class TestHierarchicalChunking(unittest.TestCase):

    def test_article_extraction(self):
        """조문이 ARTICLE 노드로 추출된다. 초단편은 병합되므로 1개 이상이면 통과."""
        from services.rag_chunk_lab_service import hierarchical_chunk

        nodes = hierarchical_chunk(SAMPLE_TEXT)
        article_nodes = [n for n in nodes if n.node_type == "ARTICLE"]
        self.assertGreaterEqual(len(article_nodes), 1, "ARTICLE 노드가 1개 이상 추출되어야 함")
        # 병합 시 제23조·제24조 내용이 한 ARTICLE에 포함될 수 있음
        combined = " ".join(n.chunk_text for n in article_nodes)
        self.assertIn("제23조", combined)
        self.assertIn("제24조", combined)

    def test_clause_extraction(self):
        """항(①②③)이 CLAUSE 노드로 추출되어야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk

        nodes = hierarchical_chunk(SAMPLE_TEXT)
        clause_nodes = [n for n in nodes if n.node_type == "CLAUSE"]
        self.assertGreater(len(clause_nodes), 0, "CLAUSE 노드가 1개 이상 추출되어야 함")

    def test_clause_has_parent_reference(self):
        """CLAUSE 노드에 맥락 정보(contextual_header 또는 regulation_article)가 있어야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk

        nodes = hierarchical_chunk(SAMPLE_TEXT)
        clause_nodes = [n for n in nodes if n.node_type == "CLAUSE"]
        for node in clause_nodes:
            self.assertTrue(
                node.contextual_header or node.regulation_article,
                f"CLAUSE 노드에 맥락 정보 없음: {node.chunk_text[:50]}",
            )

    def test_regulation_article_populated(self):
        """모든 ARTICLE 노드에 regulation_article이 채워져야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk

        nodes = hierarchical_chunk(SAMPLE_TEXT)
        for node in nodes:
            if node.node_type == "ARTICLE":
                self.assertIsNotNone(node.regulation_article)
                self.assertIn("제", node.regulation_article)

    def test_search_text_excludes_prefix(self):
        """search_text는 조문 번호 prefix 없이 순수 본문만 포함해야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk

        nodes = hierarchical_chunk(SAMPLE_TEXT)
        for node in [n for n in nodes if n.node_type == "CLAUSE"]:
            self.assertNotIn(
                "제23조",
                node.search_text[:10],
                "search_text 앞에 조문 번호가 들어가서는 안 됨",
            )

    def test_rrf_fusion(self):
        """RRF 융합이 두 결과를 올바르게 합산해야 한다."""
        from services.policy_service import _reciprocal_rank_fusion

        bm25 = [{"chunk_id": 1, "bm25_score": 0.9}, {"chunk_id": 2, "bm25_score": 0.5}]
        dense = [{"chunk_id": 2, "dense_score": 0.95}, {"chunk_id": 3, "dense_score": 0.8}]
        result = _reciprocal_rank_fusion(bm25, dense, k=60)
        ids = [r["chunk_id"] for r in result]
        self.assertEqual(ids[0], 2, "양쪽 결과에 모두 있는 chunk_id=2가 1위여야 함")

    def test_rrf_empty_dense(self):
        """Dense 결과가 없어도 BM25만으로 반환되어야 한다."""
        from services.policy_service import _reciprocal_rank_fusion

        bm25 = [{"chunk_id": 1, "bm25_score": 0.9}]
        result = _reciprocal_rank_fusion(bm25, [], k=60)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["chunk_id"], 1)

    def test_preview_chunks_hierarchical(self):
        """preview_chunks_hierarchical이 기존 인터페이스와 호환되는 형태를 반환해야 한다."""
        from services.rag_chunk_lab_service import preview_chunks_hierarchical

        results = preview_chunks_hierarchical(SAMPLE_TEXT)
        self.assertGreater(len(results), 0)
        for item in results:
            self.assertIn("title", item)
            self.assertIn("content", item)
            self.assertIn("length", item)
            self.assertIn("strategy", item)
            self.assertEqual(item["strategy"], "hierarchical_parent_child")
