"""
Azure OpenAI 호출 시 temperature=0.0 미지원으로 인한 400 방지.
전체 LLM chat.completions.create 호출에서 동일 규칙 적용용 공통 유틸.
멀티모달 이미지 분석 함수(analyze_visual_evidence) 포함.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ── 키워드+공간 기반 추출 상수 ────────────────────────────────────────────────
_AMOUNT_LABEL_KW = frozenset(("합계금액", "결제금액", "총금액", "합 계", "청구금액", "실결제금액"))
_AMOUNT_SKIP_KW = frozenset(("공급가액", "부가가치세", "VAT", "소계"))
_MERCHANT_LABEL_KW = frozenset(("가맹점명", "거래처명", "상호명", "업체명"))
_DATETIME_LABEL_KW = frozenset(("결제일시", "거래일시", "거래일자", "거래시간", "결제시간", "일시"))

_MULTIMODAL_SYSTEM_PROMPT = """당신은 대한민국 기업 경비 증빙(영수증/전표)에서 핵심 객체를 찾아 위치(bbox)와 데이터를 추출하는 전문 감사 에이전트입니다.
아래 규칙을 엄격히 준수하여 시연 데이터 생성 및 감사 분석에 필요한 정보를 반환하세요.

[좌표 및 스케일 규칙]
- 모든 bbox 좌표는 이미지 전체(좌상단=(0,0), 우하단=(1000,1000)) 기준으로 0~1000 정수로 반환합니다.
- 텍스트가 실제로 위치한 영역을 정확하게 감싸도록 bbox를 설정하세요.
- bbox는 반드시 4개의 명시적 키를 가진 객체로 반환합니다:
  - "xmin": 왼쪽 경계 (0~1000)
  - "ymin": 위쪽 경계 (0~1000)
  - "xmax": 오른쪽 경계 (0~1000)
  - "ymax": 아래쪽 경계 (0~1000)
- 주의: xmin < xmax, ymin < ymax 를 반드시 지켜야 합니다.
- 중요: 영수증 용지는 이미지 중앙에 위치합니다. 좌우 여백(배경, 테이블)에는 텍스트가 없습니다.
  bbox는 반드시 실제 영수증 용지 위의 텍스트 위치여야 하며, 이미지 모서리 배경에 그려서는 안 됩니다.
- bbox 최소 크기: 너비(xmax-xmin) ≥ 80, 높이(ymax-ymin) ≥ 30 이어야 합니다.
  텍스트를 정확히 찾지 못했다면 confidence를 0.4 미만으로 낮추세요.

[추출 대상 및 정규화]
1. merchant_name: 반드시 "거래처명", "가맹점명", "상호명" 등 레이블이 붙은 행의 값을 추출합니다.
   영수증 상단의 큰 타이틀/로고 텍스트는 절대 merchant_name으로 사용하지 마십시오.
   "거래처명 : 가온 식당" → text="가온 식당", bbox_key는 "거래처명 :" 위치, bbox는 "가온 식당" 위치.
2. date_occurrence: "거래일자", "결제일시", "거래일" 등 레이블이 붙은 행의 날짜 값, 반드시 YYYY-MM-DD
3. amount_total: "합계금액", "총금액", "결제금액" 등 최종 합계 레이블의 숫자만(통화기호/콤마 제거)

[bbox 2종 추출 — 핵심 규칙]
각 엔티티마다 bbox를 2개 반환합니다:
- "bbox_key": 영수증에서 항목명(레이블) 텍스트의 위치. 예: "가맹점명:", "결제일시 :", "합계금액" 텍스트 영역.
- "bbox": 해당 항목의 실제 값 텍스트 위치. 예: "가온식당 강남점", "2026-03-14", "68,000원" 텍스트 영역.
두 bbox는 별개의 텍스트 영역을 각각 정확히 감싸야 합니다.

[이미지 품질 진단]
- image_condition: ['clear', 'blurry', 'damaged', 'partial_cut'] 중 하나
- 불확실하면 confidence를 0.5 미만으로 설정

[출력 형식]
- 반드시 순수 JSON만 반환
{
  "image_analysis": {
    "condition": "clear",
    "has_stamp": true
  },
  "entities": [
    {
      "id": "item_1",
      "label": "merchant_name",
      "text": "가온 식당",
      "bbox_key": {"xmin": 50, "ymin": 285, "xmax": 310, "ymax": 330},
      "bbox":     {"xmin": 320, "ymin": 285, "xmax": 650, "ymax": 330},
      "confidence": 0.95
    },
    {
      "id": "item_2",
      "label": "date_occurrence",
      "text": "2026-03-14",
      "bbox_key": {"xmin": 50, "ymin": 200, "xmax": 280, "ymax": 240},
      "bbox":     {"xmin": 290, "ymin": 200, "xmax": 700, "ymax": 240},
      "confidence": 0.97
    },
    {
      "id": "item_3",
      "label": "amount_total",
      "text": "97042",
      "bbox_key": {"xmin": 50, "ymin": 840, "xmax": 320, "ymax": 880},
      "bbox":     {"xmin": 420, "ymin": 840, "xmax": 900, "ymax": 880},
      "confidence": 0.98
    }
  ],
  "suggested_summary": "에이전트가 제안하는 1줄 적요",
  "audit_comment": "시각적으로 감지된 특이사항"
}

[사용자 지시]
증빙 이미지에서 금액, 날짜, 식당명을 찾아 항목명 위치(bbox_key)와 값 위치(bbox)를 함께 추출하고,
규정 위반 여부 판단에 필요한 시각 단서를 보고하십시오."""

# ──────────────────────────────────────────────────────────────────────────────
# OCR+LLM 2단계 파이프라인 — LLM은 텍스트 의미 해석만, 좌표는 OCR이 담당
# ──────────────────────────────────────────────────────────────────────────────

_OCR_FIELD_MATCH_PROMPT = """당신은 한국 영수증 OCR 결과 분석기입니다.
아래는 OCR 엔진이 영수증에서 추출한 텍스트 목록입니다 (인덱스: 텍스트 형식).
각 인덱스는 이미지 상의 실제 텍스트 한 줄을 의미합니다.

다음 4가지 필드를 찾아 JSON으로만 반환하세요 (마크다운 코드블록 금지):

1. merchant_name: "거래처명", "가맹점명", "상호명" 레이블 행 → 상호명 값
   - 반드시 "거래처명:", "가맹점명:", "상호명:" 등 레이블이 붙은 행에서만 찾으세요.
   - 영수증 최상단의 크게 인쇄된 상호명/타이틀 텍스트(레이블 없음)는 절대 선택 금지.
   - key_index: "거래처명" / "가맹점명" / "상호명" 레이블 텍스트의 인덱스
   - value_index: 해당 레이블 옆 또는 같은 줄의 상호명 값 텍스트 인덱스
   - 레이블과 값이 같은 줄에 있으면 value_index = key_index (동일 인덱스)
   - text에는 상호명 값만 (레이블 제외, 예: "가온 식당")
2. date_occurrence: "거래일자", "거래일시", "결제일시", "거래일", "일시" 레이블 행 → 날짜 값 (YYYY-MM-DD 변환)
   - "결제일시"처럼 날짜+시간이 같은 줄("2026-03-14 23:42")에 있으면 날짜 부분(YYYY-MM-DD)만 추출
   - value_index는 해당 줄의 인덱스, text는 날짜만 ("2026-03-14")
3. time_occurrence: "거래시간", "결제시간", "거래일시", "결제일시", "일시" 레이블 행 → 시간 값 (HH:MM 변환)
   - "결제일시"처럼 날짜+시간이 같은 줄에 있으면 시간 부분(HH:MM)만 추출
   - 이 경우 key_index와 value_index 모두 date_occurrence와 동일한 줄 인덱스를 사용
   - text: 24시간제 HH:MM 형식으로 반환 (예: "23:42")
   - AM/PM 표기는 24시간제로 변환 (예: "7:45 PM" → "19:45", "11:30 AM" → "11:30")
   - 초(seconds) 제거 후 HH:MM만 반환
4. amount_total: "합계금액", "총금액", "결제금액" 레이블 행 → 해당 라벨과 동일 라인의 최종 합계 금액
   - value_index는 key_index 기준 가까운 인접 인덱스(|value_index - key_index| ≤ 4)여야 합니다.
   - 멀리 떨어진(key_index+5 이상) 인덱스의 금액은 절대 선택하지 마세요.
   - 중요: "공급가액", "부가가치세", "소계", "VAT" 행의 금액은 절대 선택하지 마세요.
   - 영수증에 여러 금액이 있을 때 "합계금액" 라벨이 명시된 행의 인접 금액이 최종 합계입니다.
   - text: 숫자만 반환 (콤마/원/공백 제거, 예: "68000")

반환 형식 (순수 JSON):
{
  "merchant_name": {"key_index": <int>, "value_index": <int>, "text": "<상호명>"},
  "date_occurrence": {"key_index": <int>, "value_index": <int>, "text": "<YYYY-MM-DD>"},
  "time_occurrence": {"key_index": <int>, "value_index": <int>, "text": "<HH:MM>"},
  "amount_total": {"key_index": <int>, "value_index": <int>, "text": "<숫자만>"}
}
찾지 못한 필드는 null로 설정."""


def _apply_combined_datetime_fix(
    entities: "list[Any]",
    matched: "dict[str, Any]",
    ocr_words: "list[Any]",
) -> None:
    """결제일시처럼 날짜+시간이 같은 OCR 값 블록에 있을 때 bbox를 분리해 덮어쓴다.

    두 케이스를 모두 처리:
    - Case A: key_idx == val_idx (라벨+날짜+시간이 한 블록)
      → [라벨 | 날짜 | 시간] 3구역 분할
    - Case B: key_idx != val_idx (라벨/값이 별도 블록, 값 블록에 날짜+시간 혼재)
      → 라벨 박스 = key 블록 전체, 값 블록만 [날짜 | 시간] 2구역 분할

    LLM이 time_occurrence를 null로 반환했을 때:
      → 날짜 값 블록에서 HH:MM 패턴을 자동 탐색해 time 엔티티를 생성
    """
    import re as _re

    from agent.output_models import VisualBox, VisualEntity
    from utils.ocr_paddle import OcrWord

    d_match = dict(matched.get("date_occurrence") or {})
    t_match = dict(matched.get("time_occurrence") or {})
    d_vidx = d_match.get("value_index")
    d_kidx = d_match.get("key_index")

    if d_vidx is None or not (0 <= d_vidx < len(ocr_words)):
        return

    t_vidx = t_match.get("value_index")
    val_block = ocr_words[d_vidx]

    # ── LLM이 time_occurrence를 반환하지 않았으면 값 블록에서 자동 감지 ──────
    if t_vidx is None or t_vidx != d_vidx:
        m = _re.search(r"\b(\d{1,2}:\d{2})(?::\d{2})?\b", val_block.text)
        if m:
            t_match = {
                "key_index": d_kidx,
                "value_index": d_vidx,
                "text": m.group(1),
            }
            t_vidx = d_vidx
            matched["time_occurrence"] = t_match
            logger.info("_apply_combined_datetime_fix: auto-detected time '%s'", m.group(1))
        else:
            return  # 시간 정보 없음

    d_text_raw = str(d_match.get("text") or "").strip()
    t_text = str(t_match.get("text") or "").strip()
    full = val_block.text

    if not d_text_raw or not t_text:
        return

    # ── LLM이 날짜에 시간을 포함해 반환했을 때 날짜 부분만 추출 ────────────
    # 예: "2026-03-14 23:42" / "2026.03.14 23:42" → "2026-03-14"
    _date_norm_m = _re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", d_text_raw)
    d_text = (
        f"{_date_norm_m.group(1)}-{_date_norm_m.group(2).zfill(2)}-{_date_norm_m.group(3).zfill(2)}"
        if _date_norm_m
        else d_text_raw
    )

    # ── 시간 텍스트를 OCR 블록에서 위치 탐색 (ASCII/유니코드 콜론 모두 허용) ──
    # PaddleOCR가 full-width 콜론(：U+FF1A)을 쓸 경우 literal find 실패 방지
    d_pos = full.find(d_text)
    if d_pos < 0:
        # OCR 원문 날짜가 "2026.03.14"처럼 다른 구분자를 쓰는 경우를 허용.
        date_in_full = _re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", full)
        if date_in_full:
            d_pos = date_in_full.start()
    t_pos = full.find(t_text)
    if t_pos < 0:
        # 유니코드 변형 콜론 또는 소폭 OCR 차이 → 패턴으로 재탐색
        _t_fallback = _re.search(r"\d{1,2}[:\uff1a]\d{2}", full)
        if _t_fallback:
            t_pos = _t_fallback.start()
            # ASCII HH:MM으로 정규화
            t_text = _re.sub(r"[\uff1a]", ":", _t_fallback.group(0))
            matched["time_occurrence"] = dict(t_match, text=t_text)
            logger.info("_apply_combined_datetime_fix: 시간 패턴 fallback 탐색 → '%s'", t_text)

    if d_pos < 0 or t_pos < 0 or d_pos >= t_pos:
        return

    # ── 한글/CJK 표시 폭을 반영한 비율 기반 x좌표 분할 ───────────────────────
    # 한글/CJK 한 글자는 ASCII 두 글자 너비 → 단순 문자 수 대신 display 폭 사용
    def _disp_width(text: str) -> int:
        """텍스트의 화면 표시 폭 계산 (한글/CJK=2, 그 외=1)."""
        return sum(
            2 if "\uac00" <= ch <= "\ud7a3" or "\u4e00" <= ch <= "\u9fff" else 1
            for ch in text
        )

    disp_total = max(_disp_width(full), 1)
    disp_d = _disp_width(full[:d_pos])
    disp_t = _disp_width(full[:t_pos])
    vw = val_block.xmax - val_block.xmin
    x_d = val_block.xmin + int(vw * disp_d / disp_total)
    x_t = val_block.xmin + int(vw * disp_t / disp_total)

    def _make_box(src: "OcrWord", xmin: int, xmax: int) -> VisualBox:
        w = OcrWord(
            text="",
            xmin=max(xmin, src.xmin),
            ymin=src.ymin,
            xmax=min(xmax, src.xmax),
            ymax=src.ymax,
            confidence=src.confidence,
            img_width=src.img_width,
            img_height=src.img_height,
        )
        return VisualBox(
            ymin=w.norm_ymin, xmin=w.norm_xmin,
            ymax=w.norm_ymax, xmax=w.norm_xmax,
        )

    # ── 라벨 박스 결정 ────────────────────────────────────────────────────────
    # 값 블록에 '결제일시/거래일시' 라벨이 함께 있는 경우, LLM key_index 신뢰보다
    # 실제 값 블록 분할(라벨|날짜|시간)을 우선해 라벨 하이라이트를 안정화한다.
    has_datetime_label_in_value = any(kw in full for kw in _DATETIME_LABEL_KW)

    if has_datetime_label_in_value and d_pos > 0:
        shared_key_box = _make_box(val_block, val_block.xmin, x_d)
    elif d_kidx is not None and d_kidx != d_vidx and 0 <= d_kidx < len(ocr_words):
        # Case B: 별도 key 블록 → 그 블록 전체를 라벨 박스로 사용
        key_block = ocr_words[d_kidx]
        shared_key_box = VisualBox(
            ymin=key_block.norm_ymin, xmin=key_block.norm_xmin,
            ymax=key_block.norm_ymax, xmax=key_block.norm_xmax,
        )
    elif d_pos > 0:
        # Case A: 한 블록 안에 라벨+날짜+시간 → 날짜 앞 구간이 라벨
        shared_key_box = _make_box(val_block, val_block.xmin, x_d)
    else:
        # d_pos==0: 값 블록에 라벨 텍스트가 없으므로 기존 bbox_key 유지
        existing = next((e for e in entities if e.label == "date_occurrence"), None)
        shared_key_box = existing.bbox_key if existing and existing.bbox_key else None

    date_box = _make_box(val_block, x_d, x_t)
    time_box = _make_box(val_block, x_t, val_block.xmax)

    # ── 엔티티 인플레이스 교체 ────────────────────────────────────────────────
    # d_text_raw: LLM 원본 (날짜+시간 혼재 가능), d_text: YYYY-MM-DD 정규화값
    # 엔티티 text 매칭은 raw 값으로, 저장은 정규화값으로 수행
    time_updated = False
    for i, e in enumerate(entities):
        if e.label == "date_occurrence" and (
            e.text == d_text or e.text == d_text_raw
        ):
            try:
                entities[i] = VisualEntity(
                    id=e.id, label="date_occurrence",
                    # LLM이 시간 포함 반환 시 YYYY-MM-DD로 정규화
                    text=d_text,
                    bbox=date_box,
                    bbox_key=shared_key_box if shared_key_box else e.bbox_key,
                    confidence=e.confidence,
                )
            except Exception as exc:
                logger.warning("combined datetime date fix 실패: %s", exc)
        elif e.label == "time_occurrence" and e.text == t_text:
            try:
                entities[i] = VisualEntity(
                    id=e.id, label="time_occurrence", text=t_text,
                    bbox=time_box,
                    bbox_key=shared_key_box if shared_key_box else e.bbox_key,
                    confidence=e.confidence,
                )
                time_updated = True
            except Exception as exc:
                logger.warning("combined datetime time fix 실패: %s", exc)

    # ── time 엔티티가 아직 없으면 새로 생성 (auto-detect 케이스) ──────────────
    if not time_updated and t_text and time_box:
        try:
            entities.append(VisualEntity(
                id=f"item_{len(entities) + 1}",
                label="time_occurrence",
                text=t_text,
                bbox=time_box,
                bbox_key=shared_key_box,
                confidence=float(val_block.confidence),
            ))
            logger.info("_apply_combined_datetime_fix: created time entity '%s'", t_text)
        except Exception as exc:
            logger.warning("time 엔티티 자동 생성 실패: %s", exc)


def _fix_amount_nearby(
    entities: "list[Any]",
    matched: "dict[str, Any]",
    ocr_words: "list[Any]",
) -> None:
    """합계금액 value가 라벨과 다른 줄에 있으면 같은 줄의 인접 금액으로 교체.

    LLM이 '61,818원(공급가액)' 또는 footer '합 계' 금액을 잘못 선택하는 경우를 보정.
    Y좌표 비교를 기준으로 라벨(key)과 같은 수평선의 금액을 우선 선택한다.

    PaddleOCR는 ymin 기준으로 정렬하므로, 같은 라인이더라도 큰 bold 숫자("68,000원")의
    ymin이 레이블("합계금액") ymin보다 작아 key_idx 앞에 위치할 수 있다.
    이를 처리하기 위해 앞·뒤 양방향 탐색을 수행한다.
    """
    import re

    from agent.output_models import VisualBox, VisualEntity

    a_match = matched.get("amount_total") or {}
    key_idx = a_match.get("key_index")
    val_idx = a_match.get("value_index")

    if key_idx is None or val_idx is None:
        return
    if not (0 <= key_idx < len(ocr_words)) or not (0 <= val_idx < len(ocr_words)):
        return

    key_word = ocr_words[key_idx]
    val_word = ocr_words[val_idx]

    def _vertical_overlap_ratio(a: Any, b: Any) -> float:
        overlap = max(0, min(a.ymax, b.ymax) - max(a.ymin, b.ymin))
        h = max(min(a.ymax - a.ymin, b.ymax - b.ymin), 1)
        return overlap / h

    # 이미 같은 줄이면 아무것도 하지 않음 (중심 거리 대신 세로 overlap으로 판정)
    if _vertical_overlap_ratio(key_word, val_word) >= 0.45:
        return

    # 같은 줄에 더 가까운 숫자 블록을 탐색 (앞·뒤 양방향)
    # 이유: PaddleOCR ymin-sort 시 큰 bold 숫자가 레이블보다 ymin이 작으면 key_idx 앞에 위치
    _digit_re = re.compile(r"\d[\d,]+")
    _skip_kw = ("공급가액", "부가가치세", "VAT", "소계")

    # 인덱스 근접보다 "같은 줄 + 라벨 우측"을 우선.
    # ocr_words는 ymin sort라 행 내 순서가 불안정할 수 있어 전체 탐색으로 후보를 모은다.
    candidates: list[tuple[int, Any]] = []
    for candidate_idx, candidate in enumerate(ocr_words):
        if candidate_idx == key_idx:
            continue
        if candidate.xmin <= key_word.xmax:
            continue  # 라벨 좌측/겹침 제외
        if _vertical_overlap_ratio(key_word, candidate) < 0.45:
            continue
        if any(kw in candidate.text for kw in _skip_kw):
            continue
        if not _digit_re.search(candidate.text):
            continue

        new_text = re.sub(r"[^\d]", "", candidate.text)
        # 너무 짧은 숫자(예: "68" / "000"원으로 분리된 bold 숫자) 제외
        # 영수증 금액은 최소 4자리(1,000원) 이상이어야 함
        if not new_text or len(new_text) < 4:
            continue
        candidates.append((candidate_idx, candidate))

    if not candidates:
        return

    # 라벨 우측의 가장 오른쪽 숫자 블록을 선택 (보통 합계 금액 컬럼)
    candidate_idx, candidate = max(candidates, key=lambda x: x[1].xmin)
    new_text = re.sub(r"[^\d]", "", candidate.text)
    new_box = VisualBox(
        ymin=candidate.norm_ymin, xmin=candidate.norm_xmin,
        ymax=candidate.norm_ymax, xmax=candidate.norm_xmax,
    )
    for i, e in enumerate(entities):
        if e.label == "amount_total":
            try:
                entities[i] = VisualEntity(
                    id=e.id, label="amount_total", text=new_text,
                    bbox=new_box, bbox_key=e.bbox_key,
                    confidence=float(candidate.confidence),
                )
                logger.info(
                    "_fix_amount_nearby: 잘못된 값(val_idx=%d) → 교체(idx=%d, text=%s)",
                    val_idx, candidate_idx, new_text,
                )
            except Exception as exc:
                logger.warning("amount nearby fix 실패: %s", exc)
            return

    return


def _keyword_spatial_override(
    entities: "list[Any]",
    ocr_words: "list[Any]",
) -> None:
    """키워드+공간 탐색으로 LLM 인덱스 선택 오류를 근본적으로 교정.

    합계금액: 라벨 키워드 블록 → 동일 Y-라인 우측 숫자 블록 (4자리 이상).
    가맹점명: 라벨 블록 내 ': ' 이후 추출 또는 우측 동일 Y 블록.

    LLM 결과가 없거나 잘못 선택된 경우를 모두 처리.
    Y-허용치 = 라벨 높이의 0.5× (인접 행 오인 방지).
    """
    import re

    from agent.output_models import VisualBox, VisualEntity

    def _yc(w: "Any") -> float:
        return (w.ymin + w.ymax) / 2

    def _y_overlap_ratio(a: "Any", b: "Any") -> float:
        overlap = max(0, min(a.ymax, b.ymax) - max(a.ymin, b.ymin))
        h = max(min(a.ymax - a.ymin, b.ymax - b.ymin), 1)
        return overlap / h

    def _best_amount_digits(label_block: "Any") -> tuple[str, "Any"] | None:
        """합계 라벨 행 우측에서 가장 타당한 금액 문자열을 찾는다.

        - 단일 토큰(예: 97,042원) 우선
        - OCR 분할 토큰(예: "68" + "000원")도 결합해서 후보화
        """
        import re

        row_words = [
            w for w in ocr_words
            if w.xmin > label_block.xmax and _y_overlap_ratio(w, label_block) >= 0.35
        ]
        if not row_words:
            return None
        row_words = sorted(row_words, key=lambda w: w.xmin)

        candidates: list[tuple[str, Any, int, int]] = []  # (digits, anchor_word, x_end, score)
        for w in row_words:
            if any(kw in w.text for kw in _AMOUNT_SKIP_KW):
                continue
            digits = re.sub(r"[^\d]", "", w.text)
            if len(digits) >= 4:
                # 단일 토큰 점수: 길이 + 우측 위치
                score = len(digits) * 10 + int(w.xmax / 10)
                candidates.append((digits, w, w.xmax, score))

        # 분할 토큰 결합 후보 (예: "68" + "000원" => "68000")
        for i in range(len(row_words)):
            seq_digits = ""
            anchor = row_words[i]
            prev = row_words[i]
            if any(kw in prev.text for kw in _AMOUNT_SKIP_KW):
                continue
            d0 = re.sub(r"[^\d]", "", prev.text)
            if not d0:
                continue
            seq_digits += d0
            for j in range(i + 1, min(i + 4, len(row_words))):
                cur = row_words[j]
                gap = cur.xmin - prev.xmax
                # 같은 행에서 가까운 토큰만 결합 (너무 멀면 다른 컬럼/행으로 간주)
                if gap > 40:
                    break
                if any(kw in cur.text for kw in _AMOUNT_SKIP_KW):
                    break
                d = re.sub(r"[^\d]", "", cur.text)
                if not d:
                    break
                seq_digits += d
                prev = cur
                if len(seq_digits) >= 4:
                    score = len(seq_digits) * 12 + int(prev.xmax / 10)
                    candidates.append((seq_digits, prev, prev.xmax, score))

        if not candidates:
            return None
        # 우측 컬럼 + 충분한 길이 후보를 우선
        best_digits, best_word, _, _ = max(candidates, key=lambda c: (c[3], c[2]))
        return best_digits, best_word

    # ── 합계금액 공간 탐색 ────────────────────────────────────────────────────
    for label_block in ocr_words:
        if not any(kw in label_block.text for kw in _AMOUNT_LABEL_KW):
            continue

        best_amount = _best_amount_digits(label_block)
        if not best_amount:
            # 첫 번째 라벨에서 실패하더라도 다음 라벨(예: 하단 "합계:")을 탐색
            continue

        best_digits, best = best_amount
        best_box = VisualBox(
            ymin=best.norm_ymin, xmin=best.norm_xmin,
            ymax=best.norm_ymax, xmax=best.norm_xmax,
        )
        key_box = VisualBox(
            ymin=label_block.norm_ymin, xmin=label_block.norm_xmin,
            ymax=label_block.norm_ymax, xmax=label_block.norm_xmax,
        )

        updated = False
        for i, e in enumerate(entities):
            if e.label == "amount_total":
                try:
                    entities[i] = VisualEntity(
                        id=e.id, label="amount_total", text=best_digits,
                        bbox=best_box, bbox_key=e.bbox_key or key_box,
                        confidence=float(best.confidence),
                    )
                    updated = True
                    logger.info(
                        "_keyword_spatial_override[amount]: 교체 → text=%s", best_digits
                    )
                except Exception as exc:
                    logger.warning("amount override 실패: %s", exc)
                break

        if not updated:
            try:
                entities.append(VisualEntity(
                    id=f"item_{len(entities) + 1}", label="amount_total",
                    text=best_digits, bbox=best_box, bbox_key=key_box,
                    confidence=float(best.confidence),
                ))
                logger.info(
                    "_keyword_spatial_override[amount]: 새로 추가 → text=%s", best_digits
                )
            except Exception as exc:
                logger.warning("amount 새 추가 실패: %s", exc)
        break  # 첫 번째 합계금액 라벨만 사용

    # ── 가맹점명 공간 탐색 ────────────────────────────────────────────────────
    for label_block in ocr_words:
        if not any(kw in label_block.text for kw in _MERCHANT_LABEL_KW):
            continue

        merchant_text: str | None = None
        merchant_block = label_block

        # 1) 동일 블록 내 ': ' 또는 '：' 이후 값
        for sep in (": ", "：", ":"):
            idx = label_block.text.find(sep)
            if idx >= 0:
                merchant_text = label_block.text[idx + len(sep):].strip()
                break

        # 2) 값이 없으면 우측 동일 Y 블록
        if not merchant_text:
            right_blocks = [
                w for w in ocr_words
                if w.xmin > label_block.xmax and _y_overlap_ratio(w, label_block) >= 0.45
            ]
            if right_blocks:
                merchant_block = min(right_blocks, key=lambda w: w.xmin)
                merchant_text = merchant_block.text.strip()

        if not merchant_text:
            break

        best_box = VisualBox(
            ymin=merchant_block.norm_ymin, xmin=merchant_block.norm_xmin,
            ymax=merchant_block.norm_ymax, xmax=merchant_block.norm_xmax,
        )
        key_box = VisualBox(
            ymin=label_block.norm_ymin, xmin=label_block.norm_xmin,
            ymax=label_block.norm_ymax, xmax=label_block.norm_xmax,
        )

        updated = False
        for i, e in enumerate(entities):
            if e.label == "merchant_name":
                try:
                    entities[i] = VisualEntity(
                        id=e.id, label="merchant_name", text=merchant_text,
                        bbox=best_box, bbox_key=e.bbox_key or key_box,
                        confidence=float(merchant_block.confidence),
                    )
                    updated = True
                    logger.info(
                        "_keyword_spatial_override[merchant]: 교체 → text=%s", merchant_text
                    )
                except Exception as exc:
                    logger.warning("merchant override 실패: %s", exc)
                break

        if not updated:
            try:
                entities.append(VisualEntity(
                    id=f"item_{len(entities) + 1}", label="merchant_name",
                    text=merchant_text, bbox=best_box, bbox_key=key_box,
                    confidence=float(merchant_block.confidence),
                ))
                logger.info(
                    "_keyword_spatial_override[merchant]: 새로 추가 → text=%s", merchant_text
                )
            except Exception as exc:
                logger.warning("merchant 새 추가 실패: %s", exc)
        break  # 첫 번째 가맹점명 라벨만 사용


def _recover_datetime_from_ocr(
    entities: "list[Any]",
    ocr_words: "list[Any]",
) -> None:
    """LLM이 date/time 매핑을 놓친 경우 OCR 라벨 라인에서 date/time을 복구한다.

    정책:
    - date_occurrence/time_occurrence가 이미 모두 있으면 아무것도 하지 않음.
    - 누락된 필드만 보완한다.
    - 1순위: 거래일시/결제일시 한 줄(날짜+시간 동시 포함)에서 분리.
    - 분리 실패 시: 동일 값 블록 bbox를 date/time 공통으로 사용.
    """
    import re

    from agent.output_models import VisualBox, VisualEntity
    from utils.ocr_paddle import OcrWord

    def _has_usable_keybox(label: str) -> bool:
        for e in entities:
            if e.label != label:
                continue
            if not e.bbox_key:
                return False
            if (e.bbox_key.xmax - e.bbox_key.xmin) < 20:
                return False
            return True
        return False

    has_date = any(e.label == "date_occurrence" for e in entities)
    has_time = any(e.label == "time_occurrence" for e in entities)
    # date/time 둘 다 있고 라벨 박스도 충분하면 복구 불필요
    if has_date and has_time and _has_usable_keybox("date_occurrence") and _has_usable_keybox("time_occurrence"):
        return

    date_re = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})")
    time_re = re.compile(r"(\d{1,2})[:\uff1a](\d{2})(?::\d{2})?\s*(AM|PM)?", re.IGNORECASE)

    label_candidates = [w for w in ocr_words if any(kw in w.text for kw in _DATETIME_LABEL_KW)]
    if not label_candidates:
        return

    def _score(word: Any) -> tuple[int, float, int]:
        # 날짜+시간 동시 포함 + 대표 라벨 우선
        label_bonus = 2 if any(kw in word.text for kw in ("결제일시", "거래일시", "거래일자", "거래시간", "결제시간")) else 1
        return (
            label_bonus + (int(bool(date_re.search(word.text))) + int(bool(time_re.search(word.text)))) * 3,
            float(getattr(word, "confidence", 0.0)),
            -word.ymin,
        )

    label_block = max(label_candidates, key=_score)
    full = label_block.text
    d_match = date_re.search(full)
    t_match = time_re.search(full)
    d_source = label_block if d_match else None
    t_source = label_block if t_match else None

    def _yc(word: Any) -> float:
        return (word.ymin + word.ymax) / 2

    def _nearby_words(anchor: Any) -> list[Any]:
        h = max(anchor.ymax - anchor.ymin, 1)
        out: list[tuple[int, float, int, float, Any]] = []
        for w in ocr_words:
            if w is anchor:
                continue
            # 같은 라인 또는 인접 라인만 후보화
            if abs(_yc(w) - _yc(anchor)) > h * 2.2:
                continue
            right_pref = 0 if w.xmin >= anchor.xmin - 10 else 1
            x_gap = abs(w.xmin - anchor.xmax)
            out.append((right_pref, abs(_yc(w) - _yc(anchor)), x_gap, -float(getattr(w, "confidence", 0.0)), w))
        out.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        return [item[4] for item in out]

    nearby = _nearby_words(label_block)
    if d_match is None:
        for w in nearby:
            m = date_re.search(w.text)
            if m:
                d_match = m
                d_source = w
                break
    if t_match is None:
        for w in nearby:
            m = time_re.search(w.text)
            if m:
                t_match = m
                t_source = w
                break

    d_text = ""
    t_text = ""
    d_pos = -1
    t_pos = -1
    if d_match:
        yyyy, mm, dd = d_match.group(1), d_match.group(2).zfill(2), d_match.group(3).zfill(2)
        d_text = f"{yyyy}-{mm}-{dd}"
        d_pos = d_match.start()
    if t_match:
        hh = int(t_match.group(1))
        minute = t_match.group(2)
        ampm = (t_match.group(3) or "").upper()
        if ampm == "PM" and hh < 12:
            hh += 12
        if ampm == "AM" and hh == 12:
            hh = 0
        t_text = f"{hh:02d}:{minute}"
        t_pos = t_match.start()

    def _disp_width(text: str) -> int:
        return sum(
            2 if "\uac00" <= ch <= "\ud7a3" or "\u4e00" <= ch <= "\u9fff" else 1
            for ch in text
        )

    def _make_box(src: "OcrWord", xmin: int, xmax: int) -> VisualBox:
        w = OcrWord(
            text="",
            xmin=max(xmin, src.xmin),
            ymin=src.ymin,
            xmax=min(xmax, src.xmax),
            ymax=src.ymax,
            confidence=src.confidence,
            img_width=src.img_width,
            img_height=src.img_height,
        )
        return VisualBox(ymin=w.norm_ymin, xmin=w.norm_xmin, ymax=w.norm_ymax, xmax=w.norm_xmax)

    key_box = VisualBox(
        ymin=label_block.norm_ymin,
        xmin=label_block.norm_xmin,
        ymax=label_block.norm_ymax,
        xmax=label_block.norm_xmax,
    )

    # 날짜/시간 값 박스 기본: 각 source 블록 전체
    if d_source is not None:
        date_box = VisualBox(
            ymin=d_source.norm_ymin,
            xmin=d_source.norm_xmin,
            ymax=d_source.norm_ymax,
            xmax=d_source.norm_xmax,
        )
    else:
        date_box = key_box

    if t_source is not None:
        time_box = VisualBox(
            ymin=t_source.norm_ymin,
            xmin=t_source.norm_xmin,
            ymax=t_source.norm_ymax,
            xmax=t_source.norm_xmax,
        )
    else:
        time_box = key_box

    # 날짜/시간이 같은 블록이면 위치 분할로 정밀 보정
    if d_source is not None and t_source is not None and d_source is t_source and d_pos >= 0 and t_pos >= 0 and d_pos < t_pos:
        src = d_source
        disp_total = max(_disp_width(src.text), 1)
        vw = src.xmax - src.xmin
        x_d = src.xmin + int(vw * _disp_width(src.text[:d_pos]) / disp_total)
        x_t = src.xmin + int(vw * _disp_width(src.text[:t_pos]) / disp_total)
        date_box = _make_box(src, x_d, x_t)
        time_box = _make_box(src, x_t, src.xmax)
        # 라벨+값이 한 줄이면 라벨 키 박스는 날짜 시작 전 구간으로 축소
        if src is label_block and d_pos > 0:
            key_box = _make_box(src, src.xmin, x_d)

    def _add_missing(label: str, text: str, box: VisualBox) -> None:
        if not text:
            return
        if any(e.label == label for e in entities):
            return
        try:
            entities.append(
                VisualEntity(
                    id=f"item_{len(entities) + 1}",
                    label=label,  # type: ignore[arg-type]
                    text=text,
                    bbox=box,
                    bbox_key=key_box,
                    confidence=float(getattr((d_source or t_source or label_block), "confidence", 0.8)),
                )
            )
        except Exception as exc:
            logger.warning("_recover_datetime_from_ocr: %s 생성 실패: %s", label, exc)

    # 값까지는 못 읽어도 라벨은 명확한 경우, date 라벨 하이라이트를 남겨 사용자 보정을 유도
    if not d_text and not t_text and not has_date:
        try:
            entities.append(
                VisualEntity(
                    id=f"item_{len(entities) + 1}",
                    label="date_occurrence",
                    text="",
                    bbox=key_box,
                    bbox_key=key_box,
                    confidence=min(float(getattr(label_block, "confidence", 0.7)), 0.7),
                )
            )
        except Exception as exc:
            logger.warning("_recover_datetime_from_ocr: date label placeholder 생성 실패: %s", exc)
        return

    if not has_date:
        _add_missing("date_occurrence", d_text, date_box)
    if not has_time:
        _add_missing("time_occurrence", t_text, time_box)

    # 기존 엔티티가 있으나 bbox_key가 없거나 지나치게 작으면 보강
    if key_box is not None:
        for i, e in enumerate(list(entities)):
            if e.label not in ("date_occurrence", "time_occurrence"):
                continue
            cur = e.bbox_key
            too_small = (cur is None) or ((cur.xmax - cur.xmin) < 20)
            if not too_small:
                continue
            try:
                entities[i] = VisualEntity(
                    id=e.id,
                    label=e.label,
                    text=e.text,
                    bbox=e.bbox,
                    bbox_key=key_box,
                    confidence=e.confidence,
                )
            except Exception as exc:
                logger.warning("_recover_datetime_from_ocr: bbox_key 보강 실패(%s): %s", e.label, exc)


def _assess_ocr_image_condition(
    ocr_words: "list[Any]",
    entities: "list[Any]",
) -> tuple[str, dict[str, Any]]:
    """OCR 결과와 추출 완성도를 함께 반영해 이미지 상태를 판정한다."""
    if not ocr_words:
        return "damaged", {"avg_confidence": 0.0, "low_conf_ratio": 1.0, "missing_fields": []}

    confs: list[float] = []
    for w in ocr_words:
        try:
            confs.append(max(0.0, min(1.0, float(getattr(w, "confidence", 0.0)))))
        except Exception:
            confs.append(0.0)
    avg_conf = sum(confs) / max(len(confs), 1)
    low_conf_ratio = sum(1 for c in confs if c < 0.55) / max(len(confs), 1)

    extracted = {
        str(getattr(e, "label", "")).strip(): str(getattr(e, "text", "")).strip()
        for e in entities
    }
    has_merchant_label = any(any(kw in str(w.text) for kw in _MERCHANT_LABEL_KW) for w in ocr_words)
    has_datetime_label = any(any(kw in str(w.text) for kw in _DATETIME_LABEL_KW) for w in ocr_words)
    has_amount_label = any(any(kw in str(w.text) for kw in _AMOUNT_LABEL_KW) for w in ocr_words)

    merchant_found = bool(extracted.get("merchant_name"))
    datetime_found = bool(extracted.get("date_occurrence") or extracted.get("time_occurrence"))
    amount_found = bool(extracted.get("amount_total"))

    missing_fields: list[str] = []
    if has_merchant_label and not merchant_found:
        missing_fields.append("merchant_name")
    if has_datetime_label and not datetime_found:
        missing_fields.append("date_time")
    if has_amount_label and not amount_found:
        missing_fields.append("amount_total")

    if len(ocr_words) < 3:
        return "partial_cut", {"avg_confidence": round(avg_conf, 3), "low_conf_ratio": round(low_conf_ratio, 3), "missing_fields": missing_fields}
    if avg_conf < 0.45:
        return "damaged", {"avg_confidence": round(avg_conf, 3), "low_conf_ratio": round(low_conf_ratio, 3), "missing_fields": missing_fields}
    if avg_conf < 0.62 or low_conf_ratio >= 0.4 or missing_fields:
        return "blurry", {"avg_confidence": round(avg_conf, 3), "low_conf_ratio": round(low_conf_ratio, 3), "missing_fields": missing_fields}
    return "clear", {"avg_confidence": round(avg_conf, 3), "low_conf_ratio": round(low_conf_ratio, 3), "missing_fields": missing_fields}


def _display_width(text: str) -> int:
    """한글/CJK를 고려한 표시 폭."""
    return sum(
        2 if "\uac00" <= ch <= "\ud7a3" or "\u4e00" <= ch <= "\u9fff" else 1
        for ch in str(text or "")
    )


def _normalize_for_find(text: str) -> str:
    return str(text or "").replace("：", ":").replace(" ", "").strip().lower()


def _find_value_start_idx(full_text: str, field_name: str, value_text: str) -> int:
    """같은 OCR 블록에서 값 시작 위치를 일반 규칙으로 찾는다.

    우선순위:
    1) field 패턴(날짜/시간/금액)
    2) value_text exact/정규화 매칭
    3) 라벨 키워드 이후 위치
    """
    import re

    full = str(full_text or "")
    full_norm = _normalize_for_find(full)
    value = str(value_text or "").strip()
    value_norm = _normalize_for_find(value)

    # 1) 필드 패턴
    if field_name == "date_occurrence":
        m = re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", full)
        if m:
            return m.start()
    elif field_name == "time_occurrence":
        m = re.search(r"\d{1,2}[:\uff1a]\d{2}(?::\d{2})?\s*(AM|PM)?", full, re.IGNORECASE)
        if m:
            return m.start()
    elif field_name == "amount_total":
        # 같은 줄의 첫 금액 토큰을 값 시작으로 간주
        m = re.search(r"\d[\d,\s]*(?:\.\d+)?\s*원?", full)
        if m:
            return m.start()

    # 2) 값 문자열 매칭 (구분자 차이 허용)
    if value:
        pos = full.find(value)
        if pos >= 0:
            return pos
        if value_norm and value_norm in full_norm:
            # 공백/콜론 제거로 찾았으므로 원문 인덱스 역매핑
            compact_idx = full_norm.find(value_norm)
            if compact_idx >= 0:
                acc = 0
                for i, ch in enumerate(full):
                    if ch in {" ", "："}:
                        continue
                    if ch == ":":
                        ch_norm = ":"
                    else:
                        ch_norm = ch.lower()
                    if acc == compact_idx:
                        return i
                    acc += 1

    # 3) 라벨 키워드 이후 위치 (구분자 미존재 케이스 포함)
    kw_map = {
        "merchant_name": _MERCHANT_LABEL_KW,
        "date_occurrence": _DATETIME_LABEL_KW,
        "time_occurrence": _DATETIME_LABEL_KW,
        "amount_total": _AMOUNT_LABEL_KW,
    }
    kws = kw_map.get(field_name, ())
    best_end = -1
    for kw in kws:
        p = full.find(str(kw))
        if p >= 0:
            best_end = max(best_end, p + len(str(kw)))
    if best_end >= 0:
        # 라벨 뒤 공백/구분자(:,：,-,|) 스킵
        j = best_end
        while j < len(full) and full[j] in {" ", ":", "：", "-", "|", "]", "）", ")"}:
            j += 1
        return min(j, max(len(full) - 1, 0))

    return -1


def _split_word_by_field(
    word: "Any",
    *,
    field_name: str,
    value_text: str,
) -> "tuple[Any, Any] | None":
    """OcrWord 한 블록을 라벨/값으로 일반 분리한다.

    - ':' 유무와 무관하게 동작
    - 날짜/시간/금액 패턴 우선
    """
    from utils.ocr_paddle import OcrWord

    full = str(getattr(word, "text", "") or "")
    if not full:
        return None
    start = _find_value_start_idx(full, field_name, value_text)
    if start <= 0:
        return None

    total_w = max(_display_width(full), 1)
    left_w = _display_width(full[:start])
    split_x = int(word.xmin + (word.xmax - word.xmin) * (left_w / total_w))
    # 박스 최소 폭 보장
    if split_x - word.xmin < 8 or word.xmax - split_x < 8:
        return None

    key_word = OcrWord(
        text=full[:start].strip(),
        xmin=word.xmin,
        ymin=word.ymin,
        xmax=split_x,
        ymax=word.ymax,
        confidence=float(getattr(word, "confidence", 0.0)),
        img_width=int(getattr(word, "img_width", 0)),
        img_height=int(getattr(word, "img_height", 0)),
    )
    val_word = OcrWord(
        text=full[start:].strip(),
        xmin=split_x,
        ymin=word.ymin,
        xmax=word.xmax,
        ymax=word.ymax,
        confidence=float(getattr(word, "confidence", 0.0)),
        img_width=int(getattr(word, "img_width", 0)),
        img_height=int(getattr(word, "img_height", 0)),
    )
    return key_word, val_word


def _analyze_with_paddle_ocr(
    image_bytes: bytes,
    *,
    client: "Any",
    model: str,
    base_url: str,
    timeout_sec: float,
) -> "MultimodalAuditResult":
    """PaddleOCR(bbox) + LLM(의미 해석) 2단계 파이프라인.

    Stage 1: PaddleOCR → 전체 텍스트 + 픽셀 단위 정확한 bbox
    Stage 2: LLM (텍스트만, 이미지 없음) → 어느 OCR 항목이 merchant/date/amount인지 식별
    Result : OCR bbox를 그대로 사용 → 좌표 오차 0%

    Raises:
        ImportError: paddleocr 미설치 시 (호출자가 fallback 결정)
        Exception: OCR 또는 LLM 오류
    """
    from agent.output_models import MultimodalAuditResult, VisualBox, VisualEntity
    from utils.ocr_paddle import run_paddle_ocr  # ImportError → 호출자로 전파

    # ── Stage 1: PaddleOCR ────────────────────────────────────────────────────
    ocr_words = run_paddle_ocr(image_bytes)
    logger.info("PaddleOCR 추출 완료: %d개 텍스트 블록", len(ocr_words))

    if not ocr_words:
        return MultimodalAuditResult(
            image_analysis={"condition": "damaged", "has_stamp": False},
            fallback_used=False,
            source="ocr_llm",
            audit_comment="OCR 결과 없음: 이미지 품질 문제 가능",
        )

    # 직인 키워드 탐지
    stamp_keywords = {"인감", "도장", "직인", "날인", "STAMP"}
    has_stamp = any(kw in w.text for w in ocr_words for kw in stamp_keywords)

    # ── Stage 2: LLM 텍스트 매핑 ──────────────────────────────────────────────
    ocr_text_list = "\n".join(f"{i}: {w.text}" for i, w in enumerate(ocr_words))
    user_msg = f"OCR 텍스트 목록:\n{ocr_text_list}"

    response = client.chat.completions.create(
        **completion_kwargs_for_azure(
            base_url,
            model=model,
            messages=[
                {"role": "system", "content": _OCR_FIELD_MATCH_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=512,
            temperature=0.0,
        ),
        timeout=timeout_sec,
    )
    raw = (response.choices[0].message.content or "").strip()
    matched: dict[str, Any] = _parse_multimodal_response(raw)

    # ── Stage 3: OCR bbox → VisualEntity 변환 ─────────────────────────────────
    entities: list[VisualEntity] = []
    label_map = {
        "merchant_name": "merchant_name",
        "date_occurrence": "date_occurrence",
        "time_occurrence": "time_occurrence",
        "amount_total": "amount_total",
    }

    for field_name, label in label_map.items():
        match = matched.get(field_name)
        if not match:
            continue

        val_idx: int | None = match.get("value_index")
        key_idx: int | None = match.get("key_index")
        text: str = str(match.get("text") or "").strip()

        if val_idx is None or not (0 <= val_idx < len(ocr_words)):
            continue
        if not text:
            continue

        val_word = ocr_words[val_idx]

        # ── 값(bbox) — OCR 좌표 그대로 ──────────────────────────────────────
        # 레이블+값이 같은 줄이면 값만 분리 시도
        split_pair = _split_word_by_field(val_word, field_name=field_name, value_text=text)
        if split_pair is not None:
            _, split_val = split_pair
            bbox_val = VisualBox(
                ymin=split_val.norm_ymin, xmin=split_val.norm_xmin,
                ymax=split_val.norm_ymax, xmax=split_val.norm_xmax,
            )
        else:
            bbox_val = VisualBox(
                ymin=val_word.norm_ymin, xmin=val_word.norm_xmin,
                ymax=val_word.norm_ymax, xmax=val_word.norm_xmax,
            )

        # ── 레이블(bbox_key) ─────────────────────────────────────────────────
        bbox_key: VisualBox | None = None
        if key_idx is not None and 0 <= key_idx < len(ocr_words):
            key_word = ocr_words[key_idx]
            if key_idx == val_idx:
                # 같은 줄에 레이블+값 → 레이블 부분만 추출
                split_pair_key = _split_word_by_field(key_word, field_name=field_name, value_text=text)
                if split_pair_key is not None:
                    split_key, _ = split_pair_key
                    bbox_key = VisualBox(
                        ymin=split_key.norm_ymin, xmin=split_key.norm_xmin,
                        ymax=split_key.norm_ymax, xmax=split_key.norm_xmax,
                    )
            elif key_idx != val_idx:
                bbox_key = VisualBox(
                    ymin=key_word.norm_ymin, xmin=key_word.norm_xmin,
                    ymax=key_word.norm_ymax, xmax=key_word.norm_xmax,
                )

        try:
            entities.append(
                VisualEntity(
                    id=f"item_{len(entities) + 1}",
                    label=label,  # type: ignore[arg-type]
                    text=text,
                    bbox=bbox_val,
                    bbox_key=bbox_key,
                    confidence=float(val_word.confidence),
                )
            )
        except Exception as exc:
            logger.warning("VisualEntity 생성 실패 (%s): %s", field_name, exc)

    # ── 후처리 1: 결제일시처럼 날짜+시간이 같은 OCR 블록일 때 3분할 ──────────
    _apply_combined_datetime_fix(entities, matched, ocr_words)

    # ── 후처리 2: 합계금액 value_index가 key_index와 너무 멀면 가까운 값으로 교체 ──
    _fix_amount_nearby(entities, matched, ocr_words)

    # ── 후처리 3: 키워드+공간 탐색으로 LLM 선택 오류 근본 교정 ─────────────
    # LLM 비결정적 인덱스 선택과 무관하게 올바른 블록을 직접 탐색해 덮어씀.
    _keyword_spatial_override(entities, ocr_words)

    # ── 후처리 4: 거래일시/결제일시 OCR 라인 기반 date/time 복구 ────────────
    _recover_datetime_from_ocr(entities, ocr_words)

    image_condition, cond_meta = _assess_ocr_image_condition(ocr_words, entities)
    miss = cond_meta.get("missing_fields") or []
    miss_txt = f", missing={','.join(str(x) for x in miss)}" if miss else ""
    return MultimodalAuditResult(
        image_analysis={"condition": image_condition, "has_stamp": has_stamp},
        entities=entities,
        suggested_summary="",
        audit_comment=(
            f"PaddleOCR {len(ocr_words)}개 블록 추출 → LLM 매핑 "
            f"(avg_conf={cond_meta.get('avg_confidence')}, low_conf_ratio={cond_meta.get('low_conf_ratio')}{miss_txt})"
        ),
        source="ocr_llm",
        fallback_used=False,
    )


def is_azure_openai(base_url: str | None) -> bool:
    """base_url이 Azure OpenAI 엔드포인트인지 여부."""
    return bool(base_url and ".openai.azure.com" in (base_url or "").strip())


def completion_kwargs_for_azure(base_url: str | None, **kwargs: Any) -> dict[str, Any]:
    """
    chat.completions.create에 넘길 kwargs를 반환.
    Azure이고 temperature가 0.0이면 temperature를 제거해 400 방지.
    """
    out = dict(kwargs)
    if not is_azure_openai(base_url):
        return out
    temp = out.get("temperature")
    if temp is not None and float(temp) == 0.0:
        out.pop("temperature", None)
    return out


def _clamp_bbox_value(v: Any, field: str) -> int:
    """bbox 값을 0~1000 정수로 clamp. 범위 초과 시 경고 로그."""
    try:
        iv = int(v)
    except (TypeError, ValueError):
        logger.warning("bbox field %s: invalid value %r, clamped to 0", field, v)
        return 0
    if iv < 0 or iv > 1000:
        logger.warning("bbox field %s: out-of-range value %d, clamped to [0,1000]", field, iv)
    return max(0, min(1000, iv))


def _build_text_row_map(image_bytes: bytes) -> "list[tuple[int,int]] | None":
    """PIL로 이미지를 읽어 텍스트 행(어두운 픽셀 밴드) 목록을 반환한다.

    Returns:
        [(row_ymin_norm, row_ymax_norm), ...] — 정규화 0~1000 좌표.
        PIL 미설치 등 오류 시 None 반환.
    """
    try:
        import io as _io
        from PIL import Image as _Image

        img = _Image.open(_io.BytesIO(image_bytes)).convert("L")
        w, h = img.size
        pixels = list(img.getdata())

        # 각 행의 어두운 픽셀 비율 계산 (잉크 임계값 = 160)
        INK_THRESHOLD = 160
        MIN_INK_RATIO = 0.01  # 행 너비의 1% 이상이 잉크여야 텍스트 행으로 인정

        ink_row = []
        for y in range(h):
            row = pixels[y * w : (y + 1) * w]
            dark_count = sum(1 for p in row if p < INK_THRESHOLD)
            ink_row.append(dark_count / w >= MIN_INK_RATIO)

        # 연속된 잉크 행을 하나의 텍스트 밴드로 그룹화 (작은 갭 무시)
        GAP_TOLERANCE = 3  # 3픽셀 이하 공백은 같은 행으로 처리
        bands: list[tuple[int, int]] = []
        start: int | None = None
        gap = 0
        for y, has_ink in enumerate(ink_row):
            if has_ink:
                if start is None:
                    start = y
                gap = 0
            else:
                if start is not None:
                    gap += 1
                    if gap > GAP_TOLERANCE:
                        bands.append((start, y - gap))
                        start = None
                        gap = 0
        if start is not None:
            bands.append((start, h - 1))

        # 픽셀 좌표 → 정규화 0~1000
        return [(int(a / h * 1000), int(b / h * 1000)) for a, b in bands if b - a >= 3]
    except Exception as _e:
        logger.debug("_build_text_row_map failed: %s", _e)
        return None


def _snap_bbox_to_rows(
    ymin_n: int,
    ymax_n: int,
    rows: "list[tuple[int,int]]",
    radius: int = 60,
) -> "tuple[int, int]":
    """LLM이 준 ymin/ymax를 가장 가까운 실제 텍스트 행에 스냅한다.

    Args:
        ymin_n, ymax_n: 정규화 좌표 (0~1000).
        rows: _build_text_row_map() 결과.
        radius: 탐색 반경 (정규화 단위).
    """
    if not rows:
        return ymin_n, ymax_n

    # ymin에 가장 가까운 행의 시작점
    center = (ymin_n + ymax_n) // 2
    best_row = min(rows, key=lambda r: abs((r[0] + r[1]) // 2 - center))
    row_center = (best_row[0] + best_row[1]) // 2

    # 탐색 반경 내에 있을 때만 스냅
    if abs(row_center - center) <= radius:
        return best_row[0], best_row[1]
    return ymin_n, ymax_n


def _parse_multimodal_response(raw: str) -> "dict[str, Any]":
    """LLM 응답 JSON 파싱. 마크다운 코드블록 제거 포함."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)


def analyze_visual_evidence(
    image_base64: str,
    *,
    model: str | None = None,
    timeout_ms: int | None = None,
) -> "Any":  # returns MultimodalAuditResult
    """이미지(base64)에서 핵심 엔티티(금액/일자/가맹점)와 bbox를 추출한다.

    실행 우선순위:
      1. PaddleOCR 설치 시: PaddleOCR(bbox) + LLM(의미 해석) 2단계 → 좌표 오차 0%
      2. PaddleOCR 미설치 시: Vision LLM 단일 단계 (좌표 근사)

    모델 우선순위:
      1. 인자 model 지정 시 사용
      2. 환경변수 MULTIMODAL_MODEL
      3. 기본값 gpt-4o

    실패 시 fallback_used=True의 빈 MultimodalAuditResult 반환.
    """
    from agent.output_models import MultimodalAuditResult, VisualBox, VisualEntity

    effective_model = model or os.getenv("MULTIMODAL_MODEL", "gpt-4o")
    timeout_sec = (timeout_ms or int(os.getenv("MULTIMODAL_TIMEOUT_MS", "45000"))) / 1000.0

    try:
        from utils.config import settings
        from openai import AzureOpenAI, OpenAI

        api_key = getattr(settings, "openai_api_key", None) or os.getenv("OPENAI_API_KEY")
        base_url = (getattr(settings, "openai_base_url", None) or os.getenv("OPENAI_BASE_URL") or "").strip()
        api_version = getattr(settings, "openai_api_version", "2024-12-01-preview")

        if not api_key:
            logger.warning("analyze_visual_evidence: OPENAI_API_KEY not configured, returning fallback")
            return MultimodalAuditResult(fallback_used=True, audit_comment="API 키 미설정으로 분석 불가")

        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client: Any = AzureOpenAI(
                api_key=api_key,
                azure_endpoint=azure_ep,
                api_version=api_version,
                timeout=timeout_sec,
            )
        else:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url or None,
                timeout=timeout_sec,
            )

        # ── PaddleOCR 경로 (설치된 경우 우선 사용) ───────────────────────────
        try:
            import base64 as _b64
            _img_bytes = _b64.b64decode(image_base64)
            result = _analyze_with_paddle_ocr(
                _img_bytes,
                client=client,
                model=effective_model,
                base_url=base_url,
                timeout_sec=timeout_sec,
            )
            logger.info(
                "analyze_visual_evidence: PaddleOCR 경로 성공 (%d 엔티티)",
                len(result.entities),
            )
            return result
        except ImportError:
            logger.info("paddleocr 미설치 — Vision LLM 경로로 fallback")
        except Exception as ocr_exc:
            logger.warning("PaddleOCR 경로 실패 (%s) — Vision LLM 경로로 fallback", ocr_exc)

        response = client.chat.completions.create(
            model=effective_model,
            messages=[
                {"role": "system", "content": _MULTIMODAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                        },
                        {"type": "text", "text": "위 증빙 이미지에서 금액, 날짜, 가맹점을 추출하고 bbox와 함께 JSON으로 반환하세요."},
                    ],
                },
            ],
            max_tokens=1024,
        )

        raw_content = response.choices[0].message.content or ""
        parsed = _parse_multimodal_response(raw_content)

        # PIL 텍스트 행 맵 구축 (좌표 스냅용)
        try:
            import base64 as _b64
            _img_bytes = _b64.b64decode(image_base64)
            _row_map = _build_text_row_map(_img_bytes)
        except Exception:
            _row_map = None

        # entities 변환 및 bbox clamp
        entities: list[VisualEntity] = []
        for item in parsed.get("entities") or []:
            # ── 값 bbox (bbox) 파싱 ──
            raw_bbox = item.get("bbox") or {}
            if isinstance(raw_bbox, dict):
                xmin = _clamp_bbox_value(raw_bbox.get("xmin", 0), "xmin")
                ymin = _clamp_bbox_value(raw_bbox.get("ymin", 0), "ymin")
                xmax = _clamp_bbox_value(raw_bbox.get("xmax", 0), "xmax")
                ymax = _clamp_bbox_value(raw_bbox.get("ymax", 0), "ymax")
            elif isinstance(raw_bbox, list) and len(raw_bbox) >= 4:
                xmin = _clamp_bbox_value(raw_bbox[0], "xmin")
                ymin = _clamp_bbox_value(raw_bbox[1], "ymin")
                xmax = _clamp_bbox_value(raw_bbox[2], "xmax")
                ymax = _clamp_bbox_value(raw_bbox[3], "ymax")
            else:
                xmin = ymin = xmax = ymax = 0
            if ymin > ymax:
                ymin, ymax = ymax, ymin
            if xmin > xmax:
                xmin, xmax = xmax, xmin
            # 값 bbox Y좌표 행 스냅
            if _row_map:
                ymin, ymax = _snap_bbox_to_rows(ymin, ymax, _row_map)

            # ── 항목명 bbox (bbox_key) 파싱 ──
            visual_box_key = None
            raw_bbox_key = item.get("bbox_key") or {}
            if isinstance(raw_bbox_key, dict) and raw_bbox_key:
                kxmin = _clamp_bbox_value(raw_bbox_key.get("xmin", 0), "xmin")
                kymin = _clamp_bbox_value(raw_bbox_key.get("ymin", 0), "ymin")
                kxmax = _clamp_bbox_value(raw_bbox_key.get("xmax", 0), "xmax")
                kymax = _clamp_bbox_value(raw_bbox_key.get("ymax", 0), "ymax")
                if kymin > kymax:
                    kymin, kymax = kymax, kymin
                if kxmin > kxmax:
                    kxmin, kxmax = kxmax, kxmin
                # 항목명 bbox_key Y좌표도 스냅 (값과 같은 행이므로 동일 스냅 적용)
                if _row_map:
                    kymin, kymax = _snap_bbox_to_rows(kymin, kymax, _row_map)
                kw, kh = kxmax - kxmin, kymax - kymin
                if kw >= 30 and kh >= 20:
                    try:
                        visual_box_key = VisualBox(ymin=kymin, xmin=kxmin, ymax=kymax, xmax=kxmax)
                    except Exception:
                        visual_box_key = None

            label = item.get("label", "")
            if label not in ("amount_total", "date_occurrence", "time_occurrence", "merchant_name"):
                logger.warning("analyze_visual_evidence: unknown label %r, skipping entity", label)
                continue

            confidence = float(item.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))

            # 값 bbox 최소 크기 미달 → confidence 하향
            bbox_w = xmax - xmin
            bbox_h = ymax - ymin
            if bbox_w < 80 or bbox_h < 30:
                logger.warning(
                    "analyze_visual_evidence: entity %r bbox too small (%dx%d), confidence lowered",
                    label, bbox_w, bbox_h,
                )
                confidence = min(confidence, 0.3)

            entities.append(
                VisualEntity(
                    id=str(item.get("id", f"item_{len(entities)+1}")),
                    label=label,  # type: ignore[arg-type]
                    text=str(item.get("text", "")),
                    bbox=VisualBox(ymin=ymin, xmin=xmin, ymax=ymax, xmax=xmax),
                    bbox_key=visual_box_key,
                    confidence=confidence,
                )
            )

        return MultimodalAuditResult(
            image_analysis=parsed.get("image_analysis") or {},
            entities=entities,
            suggested_summary=str(parsed.get("suggested_summary") or ""),
            audit_comment=str(parsed.get("audit_comment") or ""),
            source="vision_llm",
            fallback_used=False,
        )

    except Exception as exc:
        logger.warning("analyze_visual_evidence failed (model=%s): %s — returning fallback", effective_model, exc)
        return MultimodalAuditResult(
            fallback_used=True,
            audit_comment=f"이미지 분석 실패: {type(exc).__name__}",
        )
