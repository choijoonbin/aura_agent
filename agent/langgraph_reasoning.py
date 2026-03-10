from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from agent.event_schema import AgentEvent
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

logger = logging.getLogger(__name__)

_REASONING_JSON_MARKER = '"reasoning"'
_REASONING_FALLBACK_MESSAGE = "이 단계의 추론은 LLM으로 생성되지 않았습니다. (API 비활성 또는 일시 오류)"


@dataclass
class ConsistencyCheckResult:
    is_consistent: bool
    conflict_description: str


def _compact_reasoning_for_stream(text: str) -> str:
    raw = str(text or "").replace("\\n", " ").replace("\n", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return ""

    sentence_splits = re.split(r"(?<=[.!?])\s+|(?<=다\.)\s+", normalized)
    sentence_splits = [s.strip() for s in sentence_splits if s and s.strip()]
    max_sentences = max(1, int(settings.reasoning_stream_max_sentences))
    compact = " ".join(sentence_splits[:max_sentences]) if sentence_splits else normalized

    max_chars = max(80, int(settings.reasoning_stream_max_chars))
    if len(compact) > max_chars:
        compact = compact[:max_chars].rstrip()
        compact = re.sub(r"[,:;·/\-]\s*[^,:;·/\-]*$", "", compact).rstrip()
        compact = compact.rstrip(".") + "..."
    return compact


def _get_attr(output: Any, key: str) -> Any:
    if hasattr(output, key):
        return getattr(output, key)
    if isinstance(output, dict):
        return output.get(key)
    return None


def check_reasoning_consistency(node_name: str, output: Any) -> ConsistencyCheckResult:
    reasoning = str(_get_attr(output, "reasoning") or "").lower()
    if not reasoning.strip():
        return ConsistencyCheckResult(is_consistent=True, conflict_description="")

    hold_signals = ["보류", "hold", "위반", "문제", "검토 필요", "부적합", "중단", "실패", "fail"]
    pass_signals = ["정상", "통과", "pass", "적합", "문제없", "이상없", "승인", "가능"]
    reasoning_says_hold = any(s in reasoning for s in hold_signals)
    reasoning_says_pass = any(s in reasoning for s in pass_signals)

    if node_name == "critic":
        recommend_hold = bool(_get_attr(output, "recommend_hold"))
        if recommend_hold and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=(
                    "reasoning은 '정상/통과' 취지이나 recommend_hold=True가 반환되었습니다. "
                    "reasoning과 결과값이 일치하도록 재작성하십시오."
                ),
            )
        if (not recommend_hold) and reasoning_says_hold and not reasoning_says_pass:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=(
                    "reasoning은 '보류/위반' 취지이나 recommend_hold=False가 반환되었습니다. "
                    "reasoning과 결과값이 일치하도록 재작성하십시오."
                ),
            )

    if node_name in {"verify", "verifier"}:
        gate = _get_attr(output, "gate")
        gate_str = str(gate or "").upper()
        if gate_str in {"READY", "PASS"} and reasoning_says_hold and not reasoning_says_pass:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description="reasoning은 검증 실패/보류 취지이나 gate=READY(PASS)가 반환되었습니다. 일치하도록 재작성하십시오.",
            )
        if gate_str in {"HITL_REQUIRED", "FAIL"} and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description="reasoning은 검증 통과 취지이나 gate=HITL_REQUIRED(FAIL)이 반환되었습니다. 일치하도록 재작성하십시오.",
            )

    if node_name == "reporter":
        verdict = str(_get_attr(output, "verdict") or "").upper()
        if verdict in {"HITL_REQUIRED", "HOLD", "REJECT", "HOLD_AFTER_HITL"} and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=f"reasoning은 정상 처리 취지이나 verdict={verdict}가 반환되었습니다. 일치하도록 재작성하십시오.",
            )

    if node_name == "finalizer":
        status = str(_get_attr(output, "status") or _get_attr(output, "final_status") or "").upper()
        if status in {"HITL_REQUIRED", "HOLD", "REJECT", "FAILED", "HOLD_AFTER_HITL"} and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=f"reasoning은 정상 처리 취지이나 status={status}가 반환되었습니다. 일치하도록 재작성하십시오.",
            )

    return ConsistencyCheckResult(is_consistent=True, conflict_description="")


def _repair_reasoning_for_consistency(node_name: str, output: Any, *, current_reasoning: str) -> str:
    text = (current_reasoning or "").strip()
    if node_name == "critic":
        recommend_hold = bool(_get_attr(output, "recommend_hold"))
        return (text + (" 결론: 보류가 적절하다." if recommend_hold else " 결론: 정상 진행이 가능하다.")).strip()
    if node_name in {"verify", "verifier"}:
        gate = str(_get_attr(output, "gate") or "").upper()
        return (text + (" 결론: 사람 검토(HITL)가 필요하다." if gate == "HITL_REQUIRED" else " 결론: 자동 진행이 가능하다.")).strip()
    if node_name == "reporter":
        verdict = str(_get_attr(output, "verdict") or "").upper()
        if verdict in {"HITL_REQUIRED", "HOLD", "REJECT", "HOLD_AFTER_HITL"}:
            return (text + " 결론: 보류/사람 검토가 필요하다.").strip()
        return (text + " 결론: 자동 확정 후보로 진행한다.").strip()
    if node_name == "finalizer":
        status = str(_get_attr(output, "status") or _get_attr(output, "final_status") or "").upper()
        if status in {"HITL_REQUIRED", "HOLD", "REJECT", "FAILED", "HOLD_AFTER_HITL"}:
            return (text + " 결론: 보류 또는 사람 검토가 필요하다.").strip()
        return (text + " 결론: 최종 확정을 진행한다.").strip()
    return text


def call_node_llm_with_consistency_check(
    node_name: str,
    output: Any,
    reasoning_text: str,
    *,
    max_retries: int = 1,
) -> tuple[str, ConsistencyCheckResult, bool]:
    text = (reasoning_text or "").strip()
    retried = False
    last_check = ConsistencyCheckResult(is_consistent=True, conflict_description="")
    for attempt in range(max_retries + 1):
        check_input = (
            {**(output if isinstance(output, dict) else {}), "reasoning": text}
            if isinstance(output, dict)
            else output
        )
        last_check = check_reasoning_consistency(node_name, check_input)
        if last_check.is_consistent:
            return text, last_check, retried
        if attempt < max_retries:
            retried = True
            text = _repair_reasoning_for_consistency(node_name, output, current_reasoning=text)
    return text, last_check, retried


def _reasoning_stream_events(node_name: str, reasoning_text: str) -> list[dict[str, Any]]:
    if not (reasoning_text or "").strip():
        return [
            AgentEvent(
                event_type="THINKING_DONE",
                node=node_name,
                message="",
                metadata={"reasoning": ""},
            ).to_payload(),
        ]
    text = reasoning_text.strip()
    events: list[dict[str, Any]] = []
    for word in text.split():
        if not word:
            continue
        events.append(
            AgentEvent(
                event_type="THINKING_TOKEN",
                node=node_name,
                message="",
                metadata={"token": word + " "},
            ).to_payload(),
        )
    events.append(
        AgentEvent(
            event_type="THINKING_DONE",
            node=node_name,
            message=text,
            metadata={"reasoning": text},
        ).to_payload(),
    )
    return events


def _extract_reasoning_token(delta: str, full_so_far: str, emitted_len: int) -> tuple[str, int, bool]:
    if not delta and not full_so_far:
        return "", emitted_len, False
    marker_idx = full_so_far.find(_REASONING_JSON_MARKER)
    if marker_idx < 0:
        return "", emitted_len, False
    colon_idx = full_so_far.find(":", marker_idx + len(_REASONING_JSON_MARKER))
    if colon_idx < 0:
        return "", emitted_len, False
    q0 = full_so_far.find('"', colon_idx + 1)
    if q0 < 0:
        return "", emitted_len, False

    chars: list[str] = []
    escaped = False
    i = q0 + 1
    completed = False
    while i < len(full_so_far):
        ch = full_so_far[i]
        if escaped:
            chars.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            completed = True
            break
        else:
            chars.append(ch)
        i += 1
    current = "".join(chars)
    if len(current) <= emitted_len:
        return "", emitted_len, completed
    new_piece = current[emitted_len:]
    return new_piece, len(current), completed


async def _stream_reasoning_events_with_llm(
    node_name: str,
    reasoning_text: str,
    *,
    context: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], str]:
    compact_fallback = _compact_reasoning_for_stream(reasoning_text)
    fallback_message_only = _reasoning_stream_events(node_name, _REASONING_FALLBACK_MESSAGE)

    if not settings.enable_reasoning_live_llm:
        logger.warning(
            "reasoning_llm_skipped node=%s reason=enable_reasoning_live_llm_false draft_len=%s",
            node_name,
            len(reasoning_text or ""),
        )
        return _REASONING_FALLBACK_MESSAGE, fallback_message_only, "fallback"
    if not (settings.openai_api_key or "").strip():
        logger.warning(
            "reasoning_llm_skipped node=%s reason=openai_api_key_missing draft_len=%s",
            node_name,
            len(reasoning_text or ""),
        )
        return _REASONING_FALLBACK_MESSAGE, fallback_message_only, "fallback"

    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI  # type: ignore

        base_url = (settings.openai_base_url or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_endpoint = base_url.rstrip("/")
            if azure_endpoint.endswith("/openai/v1"):
                azure_endpoint = azure_endpoint[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_endpoint,
                api_version=settings.openai_api_version,
            )
        else:
            client_kwargs: dict[str, Any] = {"api_key": settings.openai_api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = AsyncOpenAI(**client_kwargs)

        context_json = json.dumps(context or {}, ensure_ascii=False, default=str)
        min_sentences = max(4, settings.reasoning_stream_max_sentences // 2)
        prompt = (
            "당신은 엔터프라이즈 감사 에이전트의 reasoning 생성기다.\n"
            "아래 초안을 기반으로 판단 중심의 reasoning을 '식별하기 좋은 중간 길이 요약'으로 JSON 출력하라.\n"
            f"출력은 한국어 최소 {min_sentences}문장, 최대 {settings.reasoning_stream_max_sentences}문장, 최대 {settings.reasoning_stream_max_chars}자.\n"
            "장황한 체크리스트/긴 번호 목록은 금지하되, 너무 짧은 한두 문장 요약도 금지한다.\n"
            "핵심 판단, 근거, 위험 요인, 다음 행동을 구분해서 읽기 쉽게 작성한다.\n"
            "기술 용어(도구명, 영문 약어, 내부 코드)는 가능한 한 한글 설명을 괄호와 함께 병기한다.\n"
            "반드시 JSON 객체 1개만 출력하고 키는 reasoning 하나만 사용.\n"
            "불필요한 설명, 코드블록, 마크다운 금지.\n\n"
            f"[node] {node_name}\n"
            f"[context] {context_json}\n"
            f"[draft_reasoning] {reasoning_text}\n"
        )

        response_format_req = {"type": "json_object"}
        tok_kw = {"max_completion_tokens": 1200} if is_azure else {"max_tokens": 1200}
        if is_azure:
            create_kw = completion_kwargs_for_azure(
                base_url,
                model=settings.reasoning_llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Output valid JSON only. reasoning 필드만 포함된 JSON을 출력한다. "
                            "reasoning은 식별하기 쉬운 한국어 중간 길이 요약문이어야 하며, 전문 용어는 사용자 친화적으로 풀어쓴다."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                **tok_kw,
            )
            logger.info(
                "reasoning_llm_request node=%s model=%s stream=False (Azure, no response_format)",
                node_name,
                settings.reasoning_llm_model,
            )
        else:
            create_kw = completion_kwargs_for_azure(
                base_url,
                model=settings.reasoning_llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Output valid JSON only. reasoning 필드만 포함된 JSON을 출력한다. "
                            "reasoning은 식별하기 쉬운 한국어 중간 길이 요약문이어야 하며, 전문 용어는 사용자 친화적으로 풀어쓴다."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=response_format_req,
                **tok_kw,
            )
            logger.info(
                "reasoning_llm_request node=%s model=%s response_format=%s stream=False",
                node_name,
                settings.reasoning_llm_model,
                response_format_req,
            )
        full_response = ""
        events: list[dict[str, Any]] = []
        for_chunk_reasoning = ""

        response = await client.chat.completions.create(**create_kw)
        full_response = (response.choices[0].message.content or "").strip() if response.choices else ""

        if is_azure and not full_response:
            if "response_format" in create_kw:
                create_kw_no_fmt = {k: v for k, v in create_kw.items() if k != "response_format"}
                logger.warning(
                    "reasoning_llm_azure_empty_content node=%s choices_len=%s → retry without response_format",
                    node_name,
                    len(response.choices or []),
                )
                try:
                    response2 = await client.chat.completions.create(**create_kw_no_fmt)
                    full_response = (response2.choices[0].message.content or "").strip() if response2.choices else ""
                except Exception as retry_err:
                    logger.warning("reasoning_llm_azure_retry_failed node=%s error=%s", node_name, retry_err)

        parsed_reasoning = ""
        try:
            parsed = json.loads(full_response)
            parsed_reasoning = str(parsed.get("reasoning") or "").strip()
        except Exception as parse_err:
            logger.warning(
                "reasoning_llm_json_parse_failed node=%s raw_len=%s error=%s",
                node_name,
                len(full_response),
                str(parse_err),
            )
            parsed_reasoning = for_chunk_reasoning.strip()
            if not parsed_reasoning and full_response:
                m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", full_response, re.DOTALL)
                if m:
                    try:
                        p = json.loads(m.group(1))
                        parsed_reasoning = str(p.get("reasoning") or "").strip()
                    except Exception:
                        pass
                if not parsed_reasoning:
                    parsed_reasoning = full_response.strip()[: settings.reasoning_stream_max_chars]
        if not parsed_reasoning and (for_chunk_reasoning or full_response):
            logger.warning(
                "reasoning_llm_empty_reasoning node=%s raw_len=%s chunk_len=%s",
                node_name,
                len(full_response),
                len(for_chunk_reasoning or ""),
            )

        usable = (parsed_reasoning or (for_chunk_reasoning or "").strip()).strip()
        if not usable:
            logger.warning(
                "reasoning_llm_no_usable_output node=%s raw_len=%s chunk_len=%s → fallback message only",
                node_name,
                len(full_response),
                len(for_chunk_reasoning or ""),
            )
            return _REASONING_FALLBACK_MESSAGE, fallback_message_only, "fallback"

        final_reasoning = _compact_reasoning_for_stream(parsed_reasoning or for_chunk_reasoning.strip())
        events.append(
            AgentEvent(
                event_type="THINKING_DONE",
                node=node_name,
                message=final_reasoning,
                metadata={"reasoning": final_reasoning},
            ).to_payload()
        )
        logger.info("reasoning_llm_ok node=%s response_len=%s", node_name, len(final_reasoning))
        return final_reasoning, events, "llm"
    except Exception as e:
        err_detail = str(e)
        try:
            resp = getattr(e, "response", None)
            if resp is not None and hasattr(resp, "text"):
                err_detail = f"{e!s} | response_body={getattr(resp, 'text', '')[:800]}"
            elif hasattr(e, "body"):
                err_detail = f"{e!s} | body={str(getattr(e, 'body', ''))[:800]}"
        except Exception:
            pass
        logger.warning(
            "reasoning_llm_failed node=%s response_format=json_object stream=True error_type=%s detail=%s",
            node_name,
            type(e).__name__,
            err_detail,
            exc_info=True,
        )
        return _REASONING_FALLBACK_MESSAGE, fallback_message_only, "fallback"
