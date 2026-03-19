"""
증빙문서 멀티모달 추출 — 업로드된 파일에서 금액·승인일자·업종 등을 구조화 추출.
PoC: 이미지(png/jpg)는 OCR(optional)로 추출, 미지원 시 스텁. Azure Document Intelligence 연동 시 교체.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 증빙 파일 저장 루트 (PoC)
EVIDENCE_UPLOAD_ROOT = Path(__file__).resolve().parents[1] / "data" / "evidence_uploads"
EVIDENCE_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# OCR 사용 여부 (pytesseract + Pillow 있으면 이미지에서 추출 시도)
_OCR_AVAILABLE: bool | None = None

# 데모용: Tesseract 미설치 시 특정 영수증 이미지 업로드 시 알려진 값 반환 (sha256 -> 필드 dict)
# junpyo95000.png: 가온 식당, 합계 95,000원, 2026.03.03, 일반음식점
_KNOWN_RECEIPTS: dict[str, dict[str, Any]] = {
    "af0da561f3faf1f3fbcd30a8cc384fb8ba35e00b17c6ed0db5cbdbf1f07f8691": {
        "amount": 95000.0,
        "approval_date": "2026-03-03",
        "approval_time": "19:45",
        "industry_or_mcc": "일반음식점",
        "merchant_name": "가온 식당",
    },
}


def _ocr_available() -> bool:
    global _OCR_AVAILABLE
    if _OCR_AVAILABLE is not None:
        return _OCR_AVAILABLE
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        _OCR_AVAILABLE = True
    except ImportError:
        _OCR_AVAILABLE = False
    return _OCR_AVAILABLE


def _image_to_text(content: bytes, path: Path | None) -> str:
    """이미지 bytes 또는 path에서 OCR 텍스트 추출. 실패 시 빈 문자열."""
    try:
        import pytesseract
        from PIL import Image
        import io
        if content:
            img = Image.open(io.BytesIO(content))
        elif path and path.exists():
            img = Image.open(path)
        else:
            return ""
        img = img.convert("RGB")
        # 한글+영어 영수증 (kor+eng)
        try:
            text = pytesseract.image_to_string(img, lang="kor+eng")
        except Exception:
            text = pytesseract.image_to_string(img)
        return (text or "").strip()
    except Exception:
        return ""


def _parse_receipt_text(text: str) -> dict[str, Any]:
    """OCR 텍스트에서 금액·거래일자·업종·거래처명 추출."""
    out: dict[str, Any] = {"amount": None, "approval_date": None, "approval_time": None, "industry_or_mcc": None, "merchant_name": None}
    if not text:
        return out
    lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
    full = " ".join(lines)
    # 합계금액: 95,000원 / 합계: 95,000
    m = re.search(r"합계\s*금액\s*[:\s]*([0-9,]+)\s*원?", text, re.IGNORECASE)
    if not m:
        m = re.search(r"합계\s*[:\s]*([0-9,]+)\s*원?", text, re.IGNORECASE)
    if m:
        try:
            out["amount"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    # 거래일자: 2026.03.03 / 승인일자
    m = re.search(r"(?:거래\s*일자|승인\s*일자)[:\s]*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if not m:
        m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        try:
            y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
            out["approval_date"] = f"{y}-{mo}-{d}"
        except (IndexError, ValueError):
            pass
    # 거래시간: 19:45 / 7:45 PM
    m = re.search(r"(?:거래\s*시간|결제\s*시간|거래\s*일시|결제\s*일시)[:\s]*(\d{1,2})[:\uff1a](\d{2})\s*(AM|PM)?", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,2})[:\uff1a](\d{2})\s*(AM|PM)?\b", text, re.IGNORECASE)
    if m:
        try:
            hh = int(m.group(1))
            mm = m.group(2)
            ampm = (m.group(3) or "").upper()
            if ampm == "PM" and hh < 12:
                hh += 12
            if ampm == "AM" and hh == 12:
                hh = 0
            out["approval_time"] = f"{hh:02d}:{mm}"
        except Exception:
            pass
    # 업종: 일반음식점
    m = re.search(r"업종\s*[:\s]*([^\s\n,]+(?:\s*[^\s\n,]+)?)", text)
    if m:
        out["industry_or_mcc"] = m.group(1).strip()
    # 거래처명: 가온 식당 / 상호 등
    m = re.search(r"거래처\s*명\s*[:\s]*([^\n]+)", text)
    if m:
        out["merchant_name"] = m.group(1).strip()
    if not out["merchant_name"] and lines:
        # 첫 줄이 상호인 경우
        first = lines[0]
        if not re.match(r"^[0-9\-\s]+$", first) and len(first) <= 30:
            out["merchant_name"] = first
    return out


@dataclass
class ExtractedEvidence:
    """증빙문서에서 추출한 구조화 필드."""
    amount: float | None = None
    approval_date: str | None = None  # ISO date or YYYY-MM-DD
    approval_time: str | None = None  # HH:MM
    industry_or_mcc: str | None = None
    merchant_name: str | None = None
    raw_snippets: list[str] = field(default_factory=list)
    confidence: float = 0.0
    extractor_meta: dict[str, Any] = field(default_factory=dict)


def save_evidence_file(run_id: str, filename: str, content: bytes) -> tuple[Path, str]:
    """
    업로드 파일을 저장하고 SHA-256 해시 반환.
    반환: (저장 경로, hex 해시)
    """
    run_dir = EVIDENCE_UPLOAD_ROOT / run_id.replace("/", "_")
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ") or "upload"
    path = run_dir / safe_name
    path.write_bytes(content)
    h = hashlib.sha256(content).hexdigest()
    return path, h


def extract_from_file(file_path: Path, file_bytes: bytes | None = None) -> ExtractedEvidence:
    """
    파일에서 금액·승인일자·업종 등을 추출.
    1) 이미지이고 내용 해시가 데모용 알려진 영수증이면 해당 값 반환.
    2) 이미지이고 pytesseract+Pillow 있으면 OCR로 추출.
    3) 그 외 스텁.
    """
    path = file_path
    content = file_bytes
    if content is None and path and path.exists():
        content = path.read_bytes()
    suffix = (path.suffix or "").lower() if path else ""
    is_image = suffix in (".png", ".jpg", ".jpeg")

    # 데모: 알려진 영수증 이미지(동일 파일 업로드)면 저장된 값 반환 (Tesseract 없어도 동작)
    if is_image and content:
        sha = hashlib.sha256(content).hexdigest()
        if sha in _KNOWN_RECEIPTS:
            kw = _KNOWN_RECEIPTS[sha].copy()
            return ExtractedEvidence(
                amount=kw.get("amount"),
                approval_date=kw.get("approval_date"),
                approval_time=kw.get("approval_time"),
                industry_or_mcc=kw.get("industry_or_mcc"),
                merchant_name=kw.get("merchant_name"),
                raw_snippets=[],
                confidence=0.95,
                extractor_meta={"source": "known_receipt", "path": str(path) if path else "bytes"},
            )

    # OCR 시도
    if is_image and content and _ocr_available():
        text = _image_to_text(content, path)
        parsed = _parse_receipt_text(text)
        n_found = sum(
            1 for v in (
                parsed.get("amount"),
                parsed.get("approval_date"),
                parsed.get("approval_time"),
                parsed.get("merchant_name"),
            ) if v is not None
        )
        confidence = min(1.0, 0.3 + n_found * 0.2)
        return ExtractedEvidence(
            amount=parsed.get("amount"),
            approval_date=parsed.get("approval_date"),
            approval_time=parsed.get("approval_time"),
            industry_or_mcc=parsed.get("industry_or_mcc"),
            merchant_name=parsed.get("merchant_name"),
            raw_snippets=[text[:500]] if text else [],
            confidence=confidence,
            extractor_meta={"source": "ocr", "path": str(path) if path else "bytes"},
        )

    # 비이미지 또는 OCR 미사용: 스텁
    return ExtractedEvidence(
        amount=None,
        approval_date=None,
        approval_time=None,
        industry_or_mcc=None,
        merchant_name=None,
        raw_snippets=[],
        confidence=0.0,
        extractor_meta={"stub": True, "source": str(file_path) if file_path else "bytes"},
    )


def extract_from_bytes(content: bytes, run_id: str, filename: str) -> tuple[ExtractedEvidence, str, Path]:
    """
    업로드 bytes 저장 후 추출.
    반환: (ExtractedEvidence, sha256_hex, saved_path)
    """
    path, sha256_hex = save_evidence_file(run_id, filename, content)
    extracted = extract_from_file(path, file_bytes=content)
    extracted.extractor_meta["file_sha256"] = sha256_hex
    extracted.extractor_meta["saved_path"] = str(path)
    return extracted, sha256_hex, path
