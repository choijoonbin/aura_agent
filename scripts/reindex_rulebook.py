#!/usr/bin/env python3
"""
규정집 문서를 계층 청킹 + embedding_ko + search_tsv 로 재색인.

사용 예:
  python scripts/reindex_rulebook.py --doc-id 1 --path "규정집/사내_경비_지출_관리_규정_v2.0_확장판.txt"

필요: DB 마이그레이션 방법 B 적용(embedding_ko vector(768) 컬럼), sentence-transformers 설치.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 path에 추가
_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from db.session import get_db
from services.chunking_pipeline import run_chunking_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="규정집 계층 청킹 재색인 (embedding_ko, search_tsv)")
    parser.add_argument("--doc-id", type=int, required=True, help="dwp_aura.rag_document.doc_id")
    parser.add_argument("--path", type=str, required=True, help="규정집 원문 .txt 파일 경로")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.is_file():
        print(f"파일 없음: {path}", file=sys.stderr)
        return 1

    raw_text = path.read_text(encoding="utf-8")
    if not raw_text.strip():
        print("파일 내용이 비어 있습니다.", file=sys.stderr)
        return 1

    db = next(get_db())
    try:
        result = run_chunking_pipeline(db, args.doc_id, raw_text)
        if "error" in result:
            print(result["error"], file=sys.stderr)
            return 1
        print(
            f"완료: doc_id={result['doc_id']}, "
            f"총 청크={result['total_chunks']} (ARTICLE={result['article_chunks']}, CLAUSE={result['clause_chunks']}), "
            f"embedding_saved={result['embedding_saved']}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
