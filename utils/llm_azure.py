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

[추출 대상 및 정규화]
1. merchant_name: 식당명 또는 가맹점명(상호명만)
2. date_occurrence: 결제 일자, 반드시 YYYY-MM-DD
3. amount_total: 총 결제 금액, 숫자만(통화기호/콤마 제거)

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
      "text": "가온식당 강남점",
      "bbox": {"xmin": 380, "ymin": 130, "xmax": 700, "ymax": 195},
      "confidence": 0.95
    },
    {
      "id": "item_2",
      "label": "date_occurrence",
      "text": "2026-03-14",
      "bbox": {"xmin": 350, "ymin": 300, "xmax": 760, "ymax": 360},
      "confidence": 0.97
    },
    {
      "id": "item_3",
      "label": "amount_total",
      "text": "68000",
      "bbox": {"xmin": 320, "ymin": 580, "xmax": 780, "ymax": 650},
      "confidence": 0.98
    }
  ],
  "suggested_summary": "에이전트가 제안하는 1줄 적요",
  "audit_comment": "시각적으로 감지된 특이사항"
}

[사용자 지시]
증빙 이미지에서 금액, 날짜, 식당명을 찾아 좌표와 함께 추출하고, 규정 위반 여부 판단에 필요한 시각 단서를 보고하십시오."""


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
    timeout_sec = (timeout_ms or int(os.getenv("MULTIMODAL_TIMEOUT_MS", "15000"))) / 1000.0

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

        # entities 변환 및 bbox clamp
        entities: list[VisualEntity] = []
        for item in parsed.get("entities") or []:
            raw_bbox = item.get("bbox") or {}
            if isinstance(raw_bbox, dict):
                # 우선 dict 형식: {"xmin":..., "ymin":..., "xmax":..., "ymax":...}
                xmin = _clamp_bbox_value(raw_bbox.get("xmin", 0), "xmin")
                ymin = _clamp_bbox_value(raw_bbox.get("ymin", 0), "ymin")
                xmax = _clamp_bbox_value(raw_bbox.get("xmax", 0), "xmax")
                ymax = _clamp_bbox_value(raw_bbox.get("ymax", 0), "ymax")
            elif isinstance(raw_bbox, list) and len(raw_bbox) >= 4:
                # 하위 호환: 리스트 형식 [xmin, ymin, xmax, ymax]
                xmin = _clamp_bbox_value(raw_bbox[0], "xmin")
                ymin = _clamp_bbox_value(raw_bbox[1], "ymin")
                xmax = _clamp_bbox_value(raw_bbox[2], "xmax")
                ymax = _clamp_bbox_value(raw_bbox[3], "ymax")
            else:
                xmin = ymin = xmax = ymax = 0
            # 좌표 역전 보정
            if ymin > ymax:
                ymin, ymax = ymax, ymin
            if xmin > xmax:
                xmin, xmax = xmax, xmin

            label = item.get("label", "")
            if label not in ("amount_total", "date_occurrence", "merchant_name"):
                logger.warning("analyze_visual_evidence: unknown label %r, skipping entity", label)
                continue

            confidence = float(item.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))

            entities.append(
                VisualEntity(
                    id=str(item.get("id", f"item_{len(entities)+1}")),
                    label=label,  # type: ignore[arg-type]
                    text=str(item.get("text", "")),
                    bbox=VisualBox(ymin=ymin, xmin=xmin, ymax=ymax, xmax=xmax),
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
