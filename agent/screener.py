"""
Screening engine.

- `rule` mode: deterministic scoring/classification (existing behavior).
- `hybrid` mode: LLM initial classification + deterministic guardrail correction.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from utils.config import get_mcc_sets, settings
from utils.llm_azure import completion_kwargs_for_azure

logger = logging.getLogger(__name__)

_ALLOWED_CASE_TYPES = frozenset(
    {
        "HOLIDAY_USAGE",
        "LIMIT_EXCEED",
        "PRIVATE_USE_RISK",
        "UNUSUAL_PATTERN",
        "NORMAL_BASELINE",
    }
)


def _extract_signals(body: dict[str, Any]) -> dict[str, Any]:
    """Extract all relevant boolean/categorical signals from body_evidence."""
    mcc_sets = get_mcc_sets()
    is_holiday = bool(body.get("isHoliday"))

    hr_raw = str(body.get("hrStatus") or body.get("hrStatusRaw") or "").upper()
    is_leave = hr_raw in {"LEAVE", "OFF", "VACATION"}

    occurred = str(body.get("occurredAt") or "")
    is_night = False
    hour: int | None = None
    try:
        hour = int(occurred[11:13])
        is_night = hour >= 22 or hour < 6
    except Exception:
        pass

    budget_exceeded = bool(body.get("budgetExceeded"))
    mcc = str(body.get("mccCode") or "").strip()
    amount = float(body.get("amount") or 0)

    return {
        "is_holiday": is_holiday,
        "is_leave": is_leave,
        "is_night": is_night,
        "hour": hour,
        "budget_exceeded": budget_exceeded,
        "mcc_code": mcc,
        "mcc_high_risk": mcc in mcc_sets["high_risk"],
        "mcc_leisure": mcc in mcc_sets["leisure"],
        "mcc_medium_risk": mcc in mcc_sets["medium_risk"],
        "amount": amount,
        "hr_status": hr_raw,
    }


def _score_signals(signals: dict[str, Any]) -> tuple[int, list[str]]:
    """Score deterministically. Returns (score, reasons list)."""
    score = 0
    reasons: list[str] = []

    if signals["is_holiday"]:
        score += 35
        reasons.append("주말/휴일 발생")
    if signals["is_leave"]:
        score += 20
        reasons.append(f"근태 상태 {signals['hr_status']} (휴가/결근)")
    if signals["is_night"]:
        score += 10
        reasons.append(f"심야 시간대 사용 ({signals['hour']:02d}시)")
    if signals["budget_exceeded"]:
        score += 10
        reasons.append("예산 한도 초과 플래그")
    if signals["mcc_high_risk"]:
        score += 30
        reasons.append(f"고위험 업종 가맹점 업종 코드(MCC) {signals['mcc_code']}")
    elif signals["mcc_leisure"]:
        score += 25
        reasons.append(f"레저/오락 업종 가맹점 업종 코드(MCC) {signals['mcc_code']}")
    elif signals["mcc_medium_risk"]:
        score += 15
        reasons.append(f"일반 식음료 업종 가맹점 업종 코드(MCC) {signals['mcc_code']}")

    return min(score, 100), reasons


def _derive_case_type(signals: dict[str, Any], score: int) -> str:
    """Priority-based case type derivation."""
    is_holiday = signals["is_holiday"]
    is_leave = signals["is_leave"]
    budget_exceeded = signals["budget_exceeded"]
    mcc_leisure = signals["mcc_leisure"]

    if is_holiday and is_leave:
        return "HOLIDAY_USAGE"
    if is_holiday and signals["mcc_high_risk"]:
        return "HOLIDAY_USAGE"
    if budget_exceeded and score >= 25:
        return "LIMIT_EXCEED"
    if mcc_leisure and is_leave:
        return "PRIVATE_USE_RISK"
    if score >= 30:
        return "UNUSUAL_PATTERN"
    return "NORMAL_BASELINE"


def _derive_severity(score: int) -> str:
    if score >= 70:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 30:
        return "MEDIUM"
    return "LOW"


def _build_reason_text(case_type: str, signals: dict[str, Any], reasons: list[str]) -> str:
    label_map = {
        "HOLIDAY_USAGE": "휴일/휴무 중 사용 의심",
        "LIMIT_EXCEED": "한도 초과 의심",
        "PRIVATE_USE_RISK": "사적 사용 위험",
        "UNUSUAL_PATTERN": "비정상 패턴",
        "NORMAL_BASELINE": "정상 범위",
    }
    label = label_map.get(case_type, case_type)
    if not reasons:
        return f"스크리닝 결과: {label} — 특이사항 없음"
    detail = " / ".join(reasons)
    return f"스크리닝 결과: {label} — {detail}"


def _run_rule_based(body_evidence: dict[str, Any]) -> dict[str, Any]:
    signals = _extract_signals(body_evidence)
    score, reasons = _score_signals(signals)
    case_type = _derive_case_type(signals, score)
    severity = _derive_severity(score)
    reason_text = _build_reason_text(case_type, signals, reasons)
    return {
        "case_type": case_type,
        "severity": severity,
        "score": score,
        "signals": signals,
        "reasons": reasons,
        "reason_text": reason_text,
        "screening_mode": "rule",
        "screening_source": "deterministic_only",
    }


def _normalize_case_type(raw_case_type: Any) -> str:
    value = str(raw_case_type or "").strip().upper().replace(" ", "_")
    if value not in _ALLOWED_CASE_TYPES:
        return "UNUSUAL_PATTERN"
    return value


def _guardrail_signal_snapshot(signals: dict[str, Any]) -> dict[str, Any]:
    """Log-friendly subset for guardrail diagnostics."""
    return {
        "is_holiday": bool(signals.get("is_holiday")),
        "is_leave": bool(signals.get("is_leave")),
        "is_night": bool(signals.get("is_night")),
        "budget_exceeded": bool(signals.get("budget_exceeded")),
        "mcc_high_risk": bool(signals.get("mcc_high_risk")),
        "mcc_leisure": bool(signals.get("mcc_leisure")),
        "mcc_medium_risk": bool(signals.get("mcc_medium_risk")),
        "mcc_code": signals.get("mcc_code"),
        "hr_status": signals.get("hr_status"),
        "hour": signals.get("hour"),
        "amount": signals.get("amount"),
    }


def _truncate_text(value: Any, max_chars: int = 140) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    code_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if code_match:
        try:
            parsed = json.loads(code_match.group(1))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    start = text.find("{")
    if start >= 0:
        depth = 0
        for idx in range(start, len(text)):
            if text[idx] == "{":
                depth += 1
            elif text[idx] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                        return parsed if isinstance(parsed, dict) else {}
                    except json.JSONDecodeError:
                        break
    return {}


def _build_llm_screening_prompt(body: dict[str, Any], signals: dict[str, Any]) -> tuple[str, str]:
    # Azure: response_format json_object 사용 시 messages에 'json' 포함 필수
    system_prompt = (
        "당신은 기업 경비 전표 사전 스크리닝 분류기다.\n"
        "Respond with a single JSON object only. 반드시 JSON 객체만 반환하라.\n"
        "허용 case_type: HOLIDAY_USAGE, LIMIT_EXCEED, PRIVATE_USE_RISK, UNUSUAL_PATTERN, NORMAL_BASELINE.\n"
        "출력 형식: {\"case_type\":\"...\",\"reason\":\"한국어 1문장\",\"confidence\":0.0~1.0}\n"
        "입력에 없는 사실을 만들지 마라."
    )
    payload = {
        "voucher": {
            "occurredAt": body.get("occurredAt"),
            "amount": body.get("amount"),
            "hrStatus": body.get("hrStatus"),
            "hrStatusRaw": body.get("hrStatusRaw"),
            "mccCode": body.get("mccCode"),
            "budgetExceeded": body.get("budgetExceeded"),
            "isHoliday": body.get("isHoliday"),
        },
        "signals": signals,
    }
    user_prompt = (
        "아래 입력을 보고 가장 적합한 case_type 1개를 선택하라.\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    return system_prompt, user_prompt


def _screening_llm_error_detail(exc: Exception) -> tuple[int | None, str]:
    """400 원인 파악: 예외에서 status_code와 response_body 추출."""
    status: int | None = None
    body = ""
    try:
        resp = getattr(exc, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
            if hasattr(resp, "text"):
                body = (getattr(resp, "text") or "")[:1200]
            elif hasattr(resp, "content"):
                raw = getattr(resp, "content") or b""
                body = (raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw))[:1200]
        if not body and hasattr(exc, "body"):
            body = (str(getattr(exc, "body", "")) or "")[:1200]
    except Exception:
        pass
    return status, body or str(exc)


def _create_screening_llm_client() -> Any | None:
    if not getattr(settings, "openai_api_key", None):
        return None
    try:
        from openai import AzureOpenAI, OpenAI
    except Exception:
        return None

    base_url = (getattr(settings, "openai_base_url", None) or "").strip()
    timeout = float(getattr(settings, "screening_llm_timeout_seconds", 8.0))
    is_azure = ".openai.azure.com" in base_url
    if is_azure:
        azure_endpoint = base_url.rstrip("/")
        if azure_endpoint.endswith("/openai/v1"):
            azure_endpoint = azure_endpoint[: -len("/openai/v1")]
        return AzureOpenAI(
            api_key=settings.openai_api_key,
            azure_endpoint=azure_endpoint,
            api_version=getattr(settings, "openai_api_version", "2024-12-01-preview"),
            timeout=timeout,
        )

    kwargs: dict[str, Any] = {"api_key": settings.openai_api_key, "timeout": timeout}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _invoke_llm_case_type(body: dict[str, Any], signals: dict[str, Any]) -> dict[str, Any] | None:
    client = _create_screening_llm_client()
    if client is None:
        return None

    primary_model = getattr(settings, "screening_llm_model", None) or getattr(settings, "reasoning_llm_model", "gpt-4o-mini")
    fallback_model = str(getattr(settings, "screening_llm_fallback_model", "") or "").strip()
    model_candidates: list[str] = [str(primary_model)]
    if fallback_model and fallback_model not in model_candidates:
        model_candidates.append(fallback_model)
    system_prompt, user_prompt = _build_llm_screening_prompt(body, signals)
    max_out = int(getattr(settings, "screening_llm_max_tokens", 220))
    requested_temp = float(getattr(settings, "screening_llm_temperature", 0.0))
    base_url = (getattr(settings, "openai_base_url", None) or "").strip()
    is_azure = ".openai.azure.com" in base_url

    for model_name in model_candidates:
        try:
            response_format = {"type": "json_object"}
            req_base = {
                "model": model_name,
                "response_format": response_format,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            messages_combined = (system_prompt + " " + user_prompt).lower()
            logger.info(
                "screening llm request: model=%s response_format=%s messages_contain_json=%s",
                model_name,
                response_format,
                "json" in messages_combined,
            )

            def _call(
                *,
                use_max_completion_tokens: bool,
                include_temperature: bool,
            ) -> Any:
                kwargs = dict(req_base)
                if include_temperature:
                    kwargs["temperature"] = requested_temp
                if use_max_completion_tokens:
                    kwargs["max_completion_tokens"] = max_out
                else:
                    kwargs["max_tokens"] = max_out
                kwargs = completion_kwargs_for_azure(base_url, **kwargs)
                return client.chat.completions.create(**kwargs)

            response = None
            last_error: Exception | None = None
            for use_max_completion_tokens in (True, False):
                try:
                    response = _call(
                        use_max_completion_tokens=use_max_completion_tokens,
                        include_temperature=True,
                    )
                    break
                except TypeError as exc:
                    last_error = exc
                    msg = str(exc).lower()
                    token_kw = "max_completion_tokens" if use_max_completion_tokens else "max_tokens"
                    logger.warning(
                        "screening llm TypeError: model=%s use_max_completion_tokens=%s exc=%s",
                        model_name, use_max_completion_tokens, exc,
                    )
                    if token_kw in msg:
                        continue
                    try:
                        response = _call(
                            use_max_completion_tokens=use_max_completion_tokens,
                            include_temperature=False,
                        )
                        break
                    except Exception as exc2:
                        last_error = exc2
                        status2, body2 = _screening_llm_error_detail(exc2)
                        logger.warning(
                            "screening llm call error (TypeError retry): status=%s response_body=%s",
                            status2, body2,
                        )
                        msg2 = str(exc2).lower()
                        if "unsupported parameter" in msg2 and token_kw in msg2:
                            continue
                except Exception as exc:
                    last_error = exc
                    msg = str(exc).lower()
                    token_kw = "max_completion_tokens" if use_max_completion_tokens else "max_tokens"
                    status_code, response_body = _screening_llm_error_detail(exc)
                    logger.warning(
                        "screening llm call error (400 원인 파악): model=%s use_max_completion_tokens=%s include_temperature=True status=%s response_body=%s",
                        model_name,
                        use_max_completion_tokens,
                        status_code,
                        response_body,
                    )

                    if "unsupported value" in msg and "temperature" in msg:
                        try:
                            response = _call(
                                use_max_completion_tokens=use_max_completion_tokens,
                                include_temperature=False,
                            )
                            break
                        except Exception as exc2:
                            last_error = exc2
                            status2, body2 = _screening_llm_error_detail(exc2)
                            logger.warning(
                                "screening llm call error (retry no temp): status=%s response_body=%s",
                                status2,
                                body2,
                            )
                            msg2 = str(exc2).lower()
                            if "unsupported parameter" in msg2 and token_kw in msg2:
                                continue
                            raise

                    if "unsupported parameter" in msg and token_kw in msg:
                        continue
                    raise

            if response is None and last_error is not None:
                raise last_error

            raw = (response.choices[0].message.content or "").strip()
            parsed = _extract_json_object(raw)
            if not parsed:
                logger.warning(
                    "screening llm returned empty/unparseable payload: model=%s finish_reason=%s raw_preview=%s",
                    model_name,
                    getattr(response.choices[0], "finish_reason", None),
                    (raw[:160] + "...") if len(raw) > 160 else raw,
                )
                continue
            case_type = _normalize_case_type(parsed.get("case_type") or parsed.get("caseType"))
            reason = str(parsed.get("reason") or parsed.get("reasonText") or "").strip()
            confidence_raw = parsed.get("confidence")
            try:
                confidence = float(confidence_raw)
                confidence = max(0.0, min(1.0, confidence))
            except Exception:
                confidence = None
            logger.info(
                "screening llm proposal: model=%s llm_case_type=%s llm_confidence=%s llm_reason=%s",
                model_name,
                case_type,
                confidence,
                _truncate_text(reason, 240),
            )
            return {
                "case_type": case_type,
                "reason_text": reason,
                "confidence": confidence,
                "raw": parsed,
            }
        except Exception as exc:
            status_code, response_body = _screening_llm_error_detail(exc)
            logger.warning(
                "screening llm failed: model=%s response_format=%s status=%s response_body=%s exc=%s",
                model_name,
                response_format,
                status_code,
                response_body,
                exc,
            )

    return None


def _align_hybrid_case_type(
    llm_case_type: str,
    deterministic_case_type: str,
    deterministic_score: int,
    signals: dict[str, Any],
    llm_confidence: float | None,
) -> tuple[str, str]:
    if llm_case_type == deterministic_case_type:
        return llm_case_type, "llm_rule_agree"

    min_override_conf = float(getattr(settings, "screening_llm_override_min_confidence", 0.75))

    if llm_case_type == "HOLIDAY_USAGE" and not (signals["is_holiday"] or signals["is_leave"]):
        return deterministic_case_type, "llm_holiday_without_signal"
    if llm_case_type == "LIMIT_EXCEED" and not signals["budget_exceeded"]:
        return deterministic_case_type, "llm_limit_without_budget_signal"
    if llm_case_type == "PRIVATE_USE_RISK" and not (signals["mcc_leisure"] or signals["mcc_high_risk"]):
        return deterministic_case_type, "llm_private_without_signal"
    if llm_case_type == "NORMAL_BASELINE" and deterministic_score >= 30:
        return deterministic_case_type, "llm_normal_blocked_by_deterministic_score"

    if deterministic_case_type == "HOLIDAY_USAGE" and llm_case_type != "HOLIDAY_USAGE":
        return deterministic_case_type, "deterministic_holiday_priority"
    if deterministic_case_type == "LIMIT_EXCEED" and signals["budget_exceeded"] and llm_case_type == "NORMAL_BASELINE":
        return deterministic_case_type, "deterministic_limit_priority"
    if deterministic_case_type == "NORMAL_BASELINE":
        # 정상군 과탐 억제: 점수가 매우 낮으면 위험군 승격을 원칙적으로 차단한다.
        if deterministic_score < 20 and llm_case_type != "HOLIDAY_USAGE":
            return deterministic_case_type, "normal_baseline_protected_low_score"
        if deterministic_score < 30 and llm_case_type in {"UNUSUAL_PATTERN", "PRIVATE_USE_RISK"}:
            return deterministic_case_type, "normal_baseline_protected_pattern_escalation"
    if llm_case_type == "UNUSUAL_PATTERN" and deterministic_score < 25:
        return deterministic_case_type, "llm_unusual_blocked_by_low_score"
    if llm_confidence is None or llm_confidence < min_override_conf:
        return deterministic_case_type, "llm_confidence_too_low_or_missing"

    return llm_case_type, "llm_preferred_confident"


def _run_hybrid(body_evidence: dict[str, Any]) -> dict[str, Any]:
    deterministic = _run_rule_based(body_evidence)
    signals = deterministic["signals"]
    llm = _invoke_llm_case_type(body_evidence, signals)
    if not llm:
        deterministic["screening_mode"] = "hybrid"
        deterministic["screening_source"] = "hybrid_fallback_rule"
        deterministic["hybrid_case_align_reason"] = "llm_unavailable_or_failed"
        logger.info(
            "screening hybrid fallback: final_case_type=%s score=%s severity=%s align_reason=%s",
            deterministic["case_type"],
            deterministic["score"],
            deterministic["severity"],
            deterministic["hybrid_case_align_reason"],
        )
        return deterministic

    min_override_conf = float(getattr(settings, "screening_llm_override_min_confidence", 0.75))
    logger.info(
        "screening guardrail input: deterministic_case_type=%s deterministic_score=%s deterministic_severity=%s llm_case_type=%s llm_confidence=%s min_override_confidence=%s signals=%s",
        deterministic["case_type"],
        deterministic["score"],
        deterministic["severity"],
        llm["case_type"],
        llm.get("confidence"),
        min_override_conf,
        json.dumps(_guardrail_signal_snapshot(signals), ensure_ascii=False, sort_keys=True),
    )

    aligned_case_type, align_reason = _align_hybrid_case_type(
        llm_case_type=llm["case_type"],
        deterministic_case_type=deterministic["case_type"],
        deterministic_score=int(deterministic["score"]),
        signals=signals,
        llm_confidence=llm.get("confidence"),
    )
    logger.info(
        "screening guardrail decision: final_case_type=%s align_reason=%s deterministic_case_type=%s deterministic_score=%s llm_case_type=%s llm_confidence=%s",
        aligned_case_type,
        align_reason,
        deterministic["case_type"],
        deterministic["score"],
        llm["case_type"],
        llm.get("confidence"),
    )

    reason_text = llm.get("reason_text") if aligned_case_type == llm["case_type"] else ""
    if not reason_text:
        reason_text = _build_reason_text(aligned_case_type, signals, deterministic["reasons"])

    result = {
        "case_type": aligned_case_type,
        "severity": deterministic["severity"],
        "score": deterministic["score"],
        "signals": signals,
        "reasons": deterministic["reasons"],
        "reason_text": reason_text,
        "screening_mode": "hybrid",
        "screening_source": "llm_plus_deterministic_guardrail",
        "llm_case_type": llm["case_type"],
        "llm_reason_text": llm.get("reason_text"),
        "llm_confidence": llm.get("confidence"),
        "hybrid_case_align_reason": align_reason,
    }
    logger.info(
        "screening hybrid result: llm_case_type=%s final_case_type=%s score=%s severity=%s align_reason=%s",
        llm["case_type"],
        aligned_case_type,
        result["score"],
        result["severity"],
        align_reason,
    )
    return result


def run_screening(body_evidence: dict[str, Any]) -> dict[str, Any]:
    """
    Screening entry point.

    Returns:
        {
            case_type: str,
            severity: str,
            score: int,
            signals: dict,
            reasons: list[str],
            reason_text: str,
        }
    """
    mode = str(getattr(settings, "screening_mode", "hybrid") or "hybrid").strip().lower()
    if mode == "hybrid":
        return _run_hybrid(body_evidence)
    return _run_rule_based(body_evidence)
