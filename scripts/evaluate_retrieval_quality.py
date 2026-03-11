from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.retrieval_quality import (
    CHUNKING_MODES,
    RETRIEVAL_STRATEGIES,
    evaluate_gold_dataset,
    load_gold_dataset,
)
from utils.config import settings


def _parse_csv(value: str | None, defaults: list[str]) -> list[str]:
    if not value or not value.strip():
        return defaults
    return [token.strip() for token in value.split(",") if token.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Gold dataset 기반 retrieval 품질 평가")
    parser.add_argument(
        "--dataset",
        required=True,
        help="gold dataset path (.json or .jsonl)",
    )
    parser.add_argument(
        "--strategies",
        default=",".join(RETRIEVAL_STRATEGIES),
        help="comma-separated strategy names",
    )
    parser.add_argument(
        "--chunking-modes",
        default="hybrid_hierarchical",
        help=f"comma-separated chunking modes ({', '.join(CHUNKING_MODES)})",
    )
    parser.add_argument(
        "--top-ks",
        default="3,5",
        help="comma-separated recall@k values (e.g. 3,5)",
    )
    parser.add_argument(
        "--ndcg-k",
        type=int,
        default=5,
        help="nDCG@k의 k",
    )
    parser.add_argument(
        "--output",
        default="",
        help="optional output path (.json)",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    examples = load_gold_dataset(dataset_path)

    strategies = _parse_csv(args.strategies, list(RETRIEVAL_STRATEGIES))
    chunking_modes = _parse_csv(args.chunking_modes, ["hybrid_hierarchical"])
    top_ks = tuple(int(v.strip()) for v in str(args.top_ks).split(",") if v.strip())

    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as db:
        report = evaluate_gold_dataset(
            db,
            examples,
            strategies=strategies,
            chunking_modes=chunking_modes,
            top_ks=top_ks,
            ndcg_k=int(args.ndcg_k),
        )

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"[OK] report saved: {args.output}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
