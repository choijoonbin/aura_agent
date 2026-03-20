from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from agent.event_schema import AgentEvent


async def run_langgraph_agentic_analysis_impl(
    *,
    case_id: str,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None,
    run_id: str | None,
    resume_value: dict[str, Any] | None,
    previous_result: dict[str, Any] | None,
    enable_hitl: bool,
    build_agent_graph: Callable[[], Any],
    build_hitl_closure_graph: Callable[[], Any],
    closure_initial_state: Callable[..., dict[str, Any]],
    format_hitl_reason_for_stream: Callable[[dict[str, Any]], str],
    get_langfuse_handler: Callable[..., Any],
    checkpointer_backend: str,
    app_logger: logging.Logger,
) -> AsyncIterator[tuple[str, Any]]:
    from langgraph.types import Command

    if not run_id:
        run_id = "default-thread"
    app_logger.debug(
        "[agent] run_langgraph 진입: run_id=%s case_id=%s resume=%s",
        run_id,
        case_id,
        "있음" if resume_value else "없음",
    )
    if resume_value:
        _cmt = str(resume_value.get("comment") or "") if isinstance(resume_value, dict) else ""
        _prev = (_cmt[:80] + "…" ) if len(_cmt) > 80 else _cmt or "(없음)"
        app_logger.debug(
            "[agent] resume_value 요약: approved=%s comment_len=%s comment_preview=%s",
            resume_value.get("approved") if isinstance(resume_value, dict) else None,
            len(_cmt),
            _prev,
        )
    force_closure_resume = bool(
        isinstance(resume_value, dict) and resume_value.get("_force_closure_resume") is True
    )
    config: dict[str, Any] = {
        "configurable": {"thread_id": run_id},
        "tags": ["matertask", "analysis", f"case:{case_id}"],
    }
    handler = get_langfuse_handler(session_id=run_id)
    if handler:
        config["callbacks"] = [handler]

    graph = build_agent_graph()

    # HITL 이후 재개 시도: 우선 LangGraph의 Command(resume=...)를 사용하고,
    # 체크포인트가 없어서 'body_evidence' KeyError가 나면 동일 run_id로 새 입력으로 재시작한다.
    async def _stream_from_graph(_inputs: Any):
        async for chunk in graph.astream(_inputs, stream_mode="updates", config=config):
            yield chunk

    async def _yield_updates(chunks, path_tag: str | None = None):
        path_label = f" {path_tag}" if path_tag else ""
        async for chunk in chunks:
            if chunk.get("__interrupt__"):
                app_logger.info("[agent] graph __interrupt__ (HITL pause) — stream will end until review-submit resume")
                # HITL: interrupt()로 일시정지. 호출자에게 HITL_REQUIRED 전달 후 같은 run_id로 재개 대기
                interrupt_list = chunk["__interrupt__"]
                hitl_payload = interrupt_list[0].value if interrupt_list else {}
                reason_text = format_hitl_reason_for_stream(hitl_payload)
                base_msg = "담당자 검토가 필요합니다."
                if reason_text:
                    stream_msg = f"{base_msg} {reason_text} HITL 응답 후 같은 run으로 재개됩니다."
                    reason_final = f"담당자 검토 입력을 기다립니다. 사유: {reason_text}"
                else:
                    stream_msg = f"{base_msg} HITL 응답 후 같은 run으로 재개됩니다."
                    reason_final = "담당자 검토 입력을 기다립니다."

                yield "AGENT_EVENT", AgentEvent(
                    event_type="HITL_PAUSE",
                    node="hitl_pause",
                    phase="verify",
                    message=stream_msg,
                    observation="interrupt",
                    metadata={"hitl_request": hitl_payload, "reason": reason_text},
                ).to_payload()
                yield "completed", {
                    "status": "HITL_REQUIRED",
                    "hitl_request": hitl_payload,
                    "reasonText": reason_final,
                }
                return
            for _node, update in chunk.items():
                if _node == "__interrupt__":
                    continue
                update = update or {}
                app_logger.debug("[agent] 노드 실행: run_id=%s node=%s", run_id, _node)
                pending = (update.get("pending_events") or []) or []
                thinking_count = sum(1 for e in pending if (e or {}).get("event_type", "").upper().startswith("THINKING"))
                for ev in pending:
                    ev = ev or {}
                    if ev.get("event_type") == "SCORE_BREAKDOWN":
                        yield "confidence", {
                            "label": "RISK_SCORE_BREAKDOWN",
                            "detail": ev.get("message"),
                            "score_breakdown": (ev.get("metadata") or {}),
                        }
                    else:
                        yield "AGENT_EVENT", ev
                final = update.get("final_result")
                if final is not None:
                    yield "completed", final

    # 1차: checkpoint 기반 resume 시도
    if resume_value is not None and not force_closure_resume:
        rv_keys = list(resume_value.keys())[:12] if isinstance(resume_value, dict) else []
        app_logger.debug(
            "[agent] 1차 재개 시작: run_id=%s resume_value_keys=%s",
            run_id,
            rv_keys,
        )
        try:
            yielded_terminal = False
            async for ev in _yield_updates(_stream_from_graph(Command(resume=resume_value)), path_tag="1차"):
                ev_type = ev[0] if isinstance(ev, (list, tuple)) and len(ev) >= 1 else ""
                if ev_type in ("completed", "failed"):
                    yielded_terminal = True
                yield ev
            if yielded_terminal:
                app_logger.debug("[agent] 1차 재개 완료: run_id=%s checkpoint 재개 성공", run_id)
                return
            # 일부 환경/버전에서 Command(resume=...)가 예외 없이 이벤트 없이 끝나는 경우가 있다.
            # 이 경우 HOLD로 마감하기보다는 2차 경로(스크리닝부터 재실행)로 자동 전환해 결과를 반환한다.
            app_logger.warning(
                "[agent] 1차 재개: 터미널 이벤트 없이 종료 → 2차 전환 run_id=%s",
                run_id,
            )
        except KeyError as e:
            # 체크포인트가 없거나 깨진 경우: 'body_evidence' KeyError를 만나면 동일 run_id로 새 입력으로 재시작
            if str(e) != "'body_evidence'":
                raise
            app_logger.warning(
                "[agent] 1차 재개 실패: checkpoint 없음 (checkpointer=%s) → 2차 전환 run_id=%s",
                checkpointer_backend,
                run_id,
            )
            # fallthrough to 2차 (경량 가능 시 경량, 아니면 전체)
    elif resume_value is not None and force_closure_resume:
        app_logger.debug("[agent] 1차 skip: force_closure_resume=true → 2차 경량 run_id=%s", run_id)

    # 2차: HITL 재개 시 기존 결과(또는 hitl_request만)가 있으면 2차 경량(closure)으로 hitl_validate→reporter→finalizer만 실행.
    # score_breakdown/tool_results가 없어도 hitl_request가 있으면 경량 경로 사용(전체 재실행 시 verify에서 또 인터럽트되는 것 방지).
    if (
        resume_value is not None
        and previous_result
        and (previous_result.get("hitl_request") or previous_result.get("score_breakdown") or previous_result.get("tool_results"))
    ):
        app_logger.debug("[agent] 2차 경량 재개: run_id=%s hitl_validate→reporter→finalizer", run_id)
        body_with_hitl = dict(body_evidence or {})
        body_with_hitl["hitlResponse"] = resume_value
        body_with_hitl["_enable_hitl"] = enable_hitl
        closure_state = closure_initial_state(
            previous_result=previous_result,
            body_evidence=body_with_hitl,
            case_id=case_id,
            intended_risk_type=intended_risk_type,
            resume_value=resume_value,
        )
        closure_graph = build_hitl_closure_graph()
        closure_config: dict[str, Any] = {"configurable": {"thread_id": run_id}}
        _handler = get_langfuse_handler(session_id=run_id)
        if _handler:
            closure_config["callbacks"] = [_handler]

        async def _stream_closure():
            async for chunk in closure_graph.astream(closure_state, stream_mode="updates", config=closure_config):
                yield chunk

        async for ev in _yield_updates(_stream_closure(), path_tag="2차경량"):
            yield ev
        return

    if resume_value is not None:
        app_logger.debug("[agent] 2차 전체 재실행: run_id=%s (스크리닝부터)", run_id)
    else:
        app_logger.debug("[agent] 신규 분석 실행: run_id=%s", run_id)
    body_with_hitl = dict(body_evidence or {})
    if resume_value is not None:
        body_with_hitl["hitlResponse"] = resume_value
    body_with_hitl["_enable_hitl"] = enable_hitl
    inputs = {
        "case_id": case_id,
        "body_evidence": body_with_hitl,
        "intended_risk_type": intended_risk_type,
    }
    path_tag_2 = "2차" if resume_value is not None else ""
    async for ev in _yield_updates(_stream_from_graph(inputs), path_tag=path_tag_2 or None):
        yield ev
