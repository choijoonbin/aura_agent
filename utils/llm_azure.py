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
    """
    이미지(base64)를 Vision LLM에 전달해 핵심 엔티티(금액/일자/가맹점)와 bbox를 추출한다.

    우선순위:
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
