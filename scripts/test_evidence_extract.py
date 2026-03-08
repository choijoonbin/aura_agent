#!/usr/bin/env python3
"""
증빙 이미지 추출 테스트: 규정집/priceimg/junpyo95000.png 로 extract_from_file 호출 후 결과 출력.
실행: python scripts/test_evidence_extract.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 프로젝트 루트
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.evidence_extraction import _ocr_available, extract_from_file

def main() -> None:
    image_path = ROOT / "규정집" / "priceimg" / "junpyo95000.png"
    if not image_path.exists():
        print(f"파일 없음: {image_path}")
        sys.exit(1)
    print(f"OCR 사용 가능: {_ocr_available()}")
    print(f"테스트 파일: {image_path}")
    print("-" * 50)
    extracted = extract_from_file(image_path)
    print(f"amount:         {extracted.amount}")
    print(f"approval_date:  {extracted.approval_date}")
    print(f"industry_or_mcc:{extracted.industry_or_mcc}")
    print(f"merchant_name:  {extracted.merchant_name}")
    print(f"confidence:     {extracted.confidence}")
    print(f"extractor_meta: {extracted.extractor_meta}")
    if extracted.raw_snippets:
        print(f"raw_snippet(len): {len(extracted.raw_snippets[0])} chars")
    print("-" * 50)
    ok = extracted.amount is not None and extracted.approval_date is not None
    print("추출 성공" if ok else "추출 실패(스텁 또는 OCR 미동작)")

if __name__ == "__main__":
    main()
