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
2. date_occurrence: "거래일자", "결제일시", "거래일", "일시" 레이블 행 → 날짜 값 (YYYY-MM-DD 변환)
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
    # 예: "2026-03-14 23:42" → "2026-03-14"
    _date_norm_m = _re.search(r"\d{4}-\d{2}-\d{2}", d_text_raw)
    d_text = _date_norm_m.group(0) if _date_norm_m else d_text_raw

    # ── 시간 텍스트를 OCR 블록에서 위치 탐색 (ASCII/유니코드 콜론 모두 허용) ──
    # PaddleOCR가 full-width 콜론(：U+FF1A)을 쓸 경우 literal find 실패 방지
    d_pos = full.find(d_text)
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
    if d_kidx is not None and d_kidx != d_vidx and 0 <= d_kidx < len(ocr_words):
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

    key_y_center = (key_word.ymin + key_word.ymax) / 2
    val_y_center = (val_word.ymin + val_word.ymax) / 2
    # 라벨 높이의 0.6배를 "같은 줄" 허용 범위로 설정
    # (1.2× → 0.6×: 인접 행이 y_tol 안에 들어와 조기 리턴되는 버그 수정)
    y_tol = max(key_word.ymax - key_word.ymin, 10) * 0.6

    # 이미 같은 줄이면 아무것도 하지 않음
    if abs(val_y_center - key_y_center) <= y_tol:
        return

    # 같은 줄에 더 가까운 숫자 블록을 탐색 (앞·뒤 양방향)
    # 이유: PaddleOCR ymin-sort 시 큰 bold 숫자가 레이블보다 ymin이 작으면 key_idx 앞에 위치
    _digit_re = re.compile(r"\d[\d,]+")
    _skip_kw = ("공급가액", "부가가치세", "VAT", "소계")

    # 전방(+1..+5) → 후방(-1..-3) 순서로 탐색 (forward first, then backward)
    search_offsets = list(range(1, 6)) + list(range(-1, -4, -1))
    for offset in search_offsets:
        candidate_idx = key_idx + offset
        if not (0 <= candidate_idx < len(ocr_words)):
            continue
        candidate = ocr_words[candidate_idx]
        c_y_center = (candidate.ymin + candidate.ymax) / 2

        if abs(c_y_center - key_y_center) > y_tol:
            continue  # 다른 줄
        if any(kw in candidate.text for kw in _skip_kw):
            continue
        if not _digit_re.search(candidate.text):
            continue

        new_text = re.sub(r"[^\d]", "", candidate.text)
        # 너무 짧은 숫자(예: "68" / "000"원으로 분리된 bold 숫자) 제외
        # 영수증 금액은 최소 4자리(1,000원) 이상이어야 함
        if not new_text or len(new_text) < 4:
            continue

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

    def _ytol(w: "Any") -> float:
        return max(w.ymax - w.ymin, 10) * 0.5

    # ── 합계금액 공간 탐색 ────────────────────────────────────────────────────
    for label_block in ocr_words:
        if not any(kw in label_block.text for kw in _AMOUNT_LABEL_KW):
            continue

        key_yc = _yc(label_block)
        y_tol = _ytol(label_block)

        candidates = []
        for w in ocr_words:
            if abs(_yc(w) - key_yc) > y_tol:
                continue
            if any(kw in w.text for kw in _AMOUNT_SKIP_KW):
                continue
            digits = re.sub(r"[^\d]", "", w.text)
            if len(digits) >= 4:
                candidates.append(w)

        if not candidates:
            break

        best = max(candidates, key=lambda w: w.xmin)
        best_digits = re.sub(r"[^\d]", "", best.text)
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
            key_yc = _yc(label_block)
            y_tol = _ytol(label_block)
            right_blocks = [
                w for w in ocr_words
                if abs(_yc(w) - key_yc) <= y_tol and w.xmin > label_block.xmax
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

    # 이미지 품질 간이 판단
    image_condition = "clear" if len(ocr_words) >= 3 else "partial_cut"
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
        if text and text in val_word.text and text != val_word.text:
            _, split_val = val_word.split_key_value(text)
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
            if key_idx == val_idx and text in key_word.text and text != key_word.text:
                # 같은 줄에 레이블+값 → 레이블 부분만 추출
                split_key, _ = key_word.split_key_value(text)
                if split_key.xmin < split_key.xmax:
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

    return MultimodalAuditResult(
        image_analysis={"condition": image_condition, "has_stamp": has_stamp},
        entities=entities,
        suggested_summary="",
        audit_comment=f"PaddleOCR {len(ocr_words)}개 블록 추출 → LLM 매핑",
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
            if label not in ("amount_total", "date_occurrence", "merchant_name"):
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
