from __future__ import annotations

import json
from typing import Any, Iterator

import requests
import streamlit as st
from ui.shared import stylable_container

from ui.api_client import API, get, post
from ui.shared import (
    budget_exceeded_display,
    case_type_badge,
    case_type_display_name,
    fmt_dt,
    fmt_dt_korea,
    fmt_num,
    hr_status_display_name,
    mcc_display_name,
    render_empty_state,
    render_kpi_card,
    render_page_header,
    render_panel_header,
    severity_badge,
    severity_display_name,
    status_badge,
    status_display_name,
)


def _format_agent_event_line(obj: dict[str, Any]) -> str:
    node = obj.get("node") or "agent"
    event_type = obj.get("event_type") or "event"
    parts = [f"[{node}/{event_type}] {obj.get('message') or ''}"]
    if obj.get("thought"):
        parts.append(f"  - 생각: {obj['thought']}")
    if obj.get("action"):
        parts.append(f"  - 행동: {obj['action']}")
    if obj.get("observation"):
        parts.append(f"  - 관찰: {obj['observation']}")
    return "\n".join(parts) + "\n"


def _tool_caption_fragment(ev_type: str, tool: str | None, tool_description: str | None, html_tooltip: bool = False) -> str:
    """TOOL_* 이벤트용 캡션 조각: 'TOOL_CALL: toolname' 형식. html_tooltip=True이면 도구명에 title 툴팁."""
    if not tool or str(ev_type).upper() not in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"}:
        return ""
    if html_tooltip and tool_description:
        desc = (tool_description or "").replace('"', "&quot;").replace("<", "&lt;")
        return f"{ev_type}: <span title=\"{desc}\">{tool}</span>"
    return f"{ev_type}: {tool}"


def _stream_card_chunks(obj: dict[str, Any]) -> Iterator[str]:
    """Yields markdown card content in chunks for typing effect. Card = header + message (word-by-word) + 생각/행동/관찰."""
    ts = fmt_dt_korea(obj.get("timestamp")) or "-"
    node = obj.get("node") or "agent"
    ev_type = obj.get("event_type") or "event"
    tool = obj.get("tool")
    meta = obj.get("metadata") or {}
    note_source = meta.get("note_source", "")
    note_model = meta.get("note_model") or ""
    source_label = f"{note_model}" if note_source == "llm" and note_model else ("LLM" if note_source == "llm" else "정의문구" if note_source == "fallback" else "")
    tool_desc = meta.get("tool_description")

    part2 = f"{node} / {ev_type}"
    tool_frag = _tool_caption_fragment(ev_type, tool, tool_desc, html_tooltip=False)
    if tool_frag:
        part2 = f"{node} / {tool_frag}"
    header = f"**{ts}** · {part2}  \n"
    if source_label:
        header += f"*{source_label}*  \n"
    yield header

    message = (obj.get("message") or "").strip()
    if message:
        for word in message.split():
            yield word + " "
        yield "  \n\n"

    thought = (obj.get("thought") or "").strip()
    action = (obj.get("action") or "").strip()
    observation = (obj.get("observation") or "").strip()
    if thought or action or observation:
        if thought:
            yield f"**생각** {thought}  \n\n"
        if action:
            yield f"**행동** {action}  \n\n"
        if observation:
            yield f"**관찰** {observation}  \n\n"
    yield "---  \n\n"


def sse_text_stream(stream_url: str) -> Iterator[str]:
    with requests.get(stream_url, stream=True, timeout=300) as response:
        response.raise_for_status()
        event = None
        first_event = True
        for raw in response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = line.split(":", 1)[1].strip()
            if payload == "[DONE]":
                yield "\n\n**분석 스트림 종료**\n"
                break
            try:
                obj = json.loads(payload)
                if event == "AGENT_EVENT":
                    if not first_event:
                        yield "\n\n⏳ *다음 이벤트 수신 중...*  \n\n"
                    for chunk in _stream_card_chunks(obj):
                        yield chunk
                    first_event = False
                elif event == "completed":
                    yield f"\n\n**[최종]** {obj.get('reasonText') or obj.get('summary') or '완료'}\n"
                elif event == "failed":
                    yield f"\n\n**[실패]** {obj.get('error', 'unknown error')}\n"
                else:
                    detail = obj.get("detail") or obj.get("message") or obj.get("content") or payload
                    yield f"[{event}] {detail}\n"
            except Exception:
                yield f"[{event}] {payload}\n"


def fetch_case_bundle(voucher_key: str) -> dict[str, Any]:
    latest = get(f"/api/v1/cases/{voucher_key}/analysis/latest")
    history = get(f"/api/v1/cases/{voucher_key}/analysis/history")
    if latest.get("run_id"):
        events = get(f"/api/v1/analysis-runs/{latest['run_id']}/events")
        latest["timeline"] = events.get("events") or []
    else:
        latest["timeline"] = []
    latest["history"] = history.get("items") or []
    return latest


def summarize_tool_results(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for tool in tool_results:
        skill = tool.get("skill") or "unknown"
        facts = tool.get("facts") or {}
        entry = {"skill": skill, "detail": tool.get("summary") or "-"}
        if skill == "policy_rulebook_probe":
            refs = facts.get("policy_refs") or []
            entry.update(
                metric_label="규정 근거",
                metric_value=f"{len(refs)}건",
                detail=", ".join(filter(None, [ref.get("article") for ref in refs[:3]])) or "-",
            )
        elif skill == "document_evidence_probe":
            entry.update(metric_label="전표 라인", metric_value=f"{facts.get('lineItemCount', 0)}건", detail="수집 완료")
        elif skill == "legacy_aura_deep_audit":
            entry.update(metric_label="전문감사", metric_value="실행", detail=((facts.get("reasonText") or facts.get("summary") or "-")[:80]))
        else:
            details = [f"{k}={facts.get(k)}" for k in ("holidayRisk", "budgetExceeded", "merchantRisk") if k in facts]
            entry.update(metric_label="확인 결과", metric_value="OK" if tool.get("ok") else "CHECK", detail=", ".join(details) or "-")
        cards.append(entry)
    return cards


def render_tool_trace_summary(tool_results: list[dict[str, Any]]) -> None:
    cards = summarize_tool_results(tool_results)
    if not cards:
        render_empty_state("도구 실행 요약이 없습니다.")
        return
    cols = st.columns(min(3, len(cards)))
    for idx, card in enumerate(cards):
        with cols[idx % len(cols)]:
            with stylable_container(key=f"tool_summary_{idx}", css_styles="""{padding: 16px 18px; border-radius: 16px; border: 1px solid #e5e7eb; background: #fff; box-shadow: 0 8px 22px rgba(15,23,42,0.04); min-height: 158px;}"""):
                st.caption(card["skill"])
                st.markdown(f"**{card['metric_label']}**")
                st.subheader(card["metric_value"])
                st.caption(card["detail"])


def render_timeline_cards(events: list[dict[str, Any]], *, view_mode: str = "business") -> None:
    if not events:
        render_empty_state("표시할 스트림 이벤트가 없습니다.")
        return
    with stylable_container(key="timeline_shell", css_styles="""{background: radial-gradient(circle at 1px 1px, rgba(15,23,42,0.10) 1px, transparent 0); background-size: 14px 14px; background-color:#f8fafc; border:1px dashed #dbe2ea; border-radius:18px; padding:14px;}"""):
        for index, event in enumerate(events):
            payload = event.get("payload") or {}
            if event.get("event_type") != "AGENT_EVENT":
                continue
            with stylable_container(key=f"timeline_{index}", css_styles="""{padding: 14px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: #fff; margin-bottom: 10px;}"""):
                meta = payload.get("metadata") or {}
                ev_type = str(payload.get("event_type") or "").upper()
                part2 = f"{payload.get('node') or '-'} / {ev_type}"
                tool_frag = _tool_caption_fragment(ev_type, payload.get("tool"), meta.get("tool_description"), html_tooltip=True)
                if tool_frag:
                    part2 = f"{payload.get('node') or '-'} / {tool_frag}"
                cap = f"{fmt_dt_korea(event.get('at') or payload.get('timestamp')) or '-'} · {part2}"
                st.caption(cap, unsafe_allow_html=True)
                ns = meta.get("note_source", "")
                note_model = meta.get("note_model") or ""
                if ns:
                    lbl = note_model if ns == "llm" and note_model else ("LLM" if ns == "llm" else "정의문구")
                    st.caption(f"*{lbl}*")
                st.markdown(f"**{payload.get('node') or '-'} / {payload.get('event_type') or '-'}**")
                if payload.get("message"):
                    st.write(payload["message"])
                cols = st.columns(3)
                if payload.get("thought"):
                    cols[0].caption("생각")
                    cols[0].write(payload["thought"])
                if payload.get("action"):
                    cols[1].caption("행동")
                    cols[1].write(payload["action"])
                if payload.get("observation"):
                    cols[2].caption("관찰")
                    cols[2].write(payload["observation"])
                if view_mode == "debug":
                    st.json(payload)


# 대표 메시지 선택 우선순위 (docs/work_info/langgraphPlan3.md 추가답변)
_REPR_MSG_PRIORITY = ["NODE_END", "GATE_APPLIED", "TOOL_RESULT", "PLAN_READY", "NODE_START"]


def _pick_representative_message(bucket: dict[str, Any]) -> str:
    """우선순위에 따라 대표 메시지 1개 선택. NODE_END.message > GATE_APPLIED.message > TOOL_RESULT.observation > PLAN_READY.message > NODE_START.message"""
    by_type = bucket.get("by_type") or {}
    for ev in _REPR_MSG_PRIORITY:
        v = by_type.get(ev)
        if v and str(v).strip():
            return str(v).strip()
    msgs = bucket.get("messages") or []
    return msgs[-1] if msgs else "-"


def summarize_process_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node_order = ["screener", "intake", "planner", "execute", "critic", "verify", "hitl_pause", "reporter", "finalizer"]
    node_labels = {
        "screener": "전표 분석 / 케이스 분류",
        "intake": "입력 해석",
        "planner": "조사 계획 수립",
        "execute": "근거 수집 실행",
        "critic": "비판적 검토",
        "verify": "검증 및 HITL 판단",
        "hitl_pause": "HITL 대기 (run 조기 종료)",
        "reporter": "보고 문장 생성",
        "finalizer": "결과 확정",
    }
    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        node = str(payload.get("node") or "").lower()
        if node not in node_labels:
            continue
        bucket = grouped.setdefault(
            node,
            {
                "node": node,
                "label": node_labels[node],
                "started_at": event.get("at") or payload.get("timestamp") or "-",
                "messages": [],
                "thoughts": [],
                "actions": [],
                "observations": [],
                "tool_count": 0,
                "last_event_type": None,
                "by_type": {},
                "first_tool": None,
                "last_tool": None,
            },
        )
        ev_type = str(payload.get("event_type") or "").upper()
        if ev_type == "SCREENING_RESULT":
            bucket["last_event_type"] = "NODE_END"
        else:
            bucket["last_event_type"] = ev_type
        message = payload.get("message")
        thought = payload.get("thought")
        action = payload.get("action")
        observation = payload.get("observation")
        if message:
            bucket["messages"].append(str(message))
        if thought:
            bucket["thoughts"].append(str(thought))
        if action:
            bucket["actions"].append(str(action))
        if observation:
            bucket["observations"].append(str(observation))
        if ev_type in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"}:
            bucket["tool_count"] += 1
            tool_name = payload.get("tool") or ""
            if tool_name:
                if bucket["first_tool"] is None:
                    bucket["first_tool"] = tool_name
                bucket["last_tool"] = tool_name
        if ev_type == "NODE_END" and message:
            bucket.setdefault("by_type", {})["NODE_END"] = message
        elif ev_type == "GATE_APPLIED" and message:
            bucket.setdefault("by_type", {})["GATE_APPLIED"] = message
        elif ev_type == "TOOL_RESULT" and observation:
            bucket.setdefault("by_type", {})["TOOL_RESULT"] = observation
        elif ev_type == "PLAN_READY" and message:
            bucket.setdefault("by_type", {})["PLAN_READY"] = message
        elif ev_type == "NODE_START" and message:
            bucket.setdefault("by_type", {})["NODE_START"] = message

    result = []
    for node in node_order:
        if node not in grouped:
            continue
        bucket = grouped[node]
        result.append(
            {
                "node": node,
                "label": bucket["label"],
                "started_at": bucket["started_at"],
                "summary": _pick_representative_message(bucket),
                "thought": (bucket["thoughts"][-1] if bucket["thoughts"] else ""),
                "action": (bucket["actions"][-1] if bucket["actions"] else ""),
                "observation": (bucket["observations"][-1] if bucket["observations"] else ""),
                "tool_count": bucket["tool_count"],
                "first_tool": bucket.get("first_tool"),
                "last_tool": bucket.get("last_tool"),
                "last_event_type": bucket["last_event_type"] or "-",
            }
        )
    return result


def render_process_story(events: list[dict[str, Any]], *, debug_mode: bool = False) -> None:
    rows = summarize_process_timeline(events)
    if not rows:
        render_empty_state("분석 완료 후 노드별 사고 과정을 여기에 요약해 보여줍니다.")
        return
    for idx, row in enumerate(rows, start=1):
        with stylable_container(
            key=f"process_story_{idx}_{row['node']}",
            css_styles="""{padding: 15px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04); margin-bottom: 0.7rem;}""",
        ):
            top_left, top_right = st.columns([0.78, 0.22])
            with top_left:
                st.markdown(f"**{idx}. {row['label']}**")
                st.caption(fmt_dt_korea(row.get("started_at")) or "-")
            with top_right:
                st.markdown(
                    status_badge("COMPLETED" if str(row["last_event_type"]).upper() in {"NODE_END", "REPORT_READY", "RESULT_FINALIZED"} else "IN_REVIEW"),
                    unsafe_allow_html=True,
                )
            st.write(row["summary"])
            meta = []
            if row.get("tool_count"):
                meta.append(f"도구 호출 {row['tool_count']}건")
            if row.get("first_tool") or row.get("last_tool"):
                ft, lt = row.get("first_tool"), row.get("last_tool")
                if ft and lt and ft != lt:
                    meta.append(f"첫 도구: {ft} → 마지막: {lt}")
                elif ft:
                    meta.append(f"도구: {ft}")
            if row.get("observation"):
                meta.append(f"관찰: {(row['observation'] or '')[:80]}{'…' if len(str(row.get('observation') or '')) > 80 else ''}")
            if meta:
                st.caption(" · ".join(meta))
            detail_cols = st.columns(2)
            with detail_cols[0]:
                if row["thought"]:
                    st.caption("핵심 판단")
                    st.write(row["thought"])
            with detail_cols[1]:
                if row["action"]:
                    st.caption("수행 행동")
                    st.write(row["action"])
            if debug_mode:
                st.json(row)


def render_hitl_history(history: list[dict[str, Any]]) -> None:
    rows = [item for item in history if item.get("hitl_request") or item.get("hitl_response")]
    if not rows:
        render_empty_state("HITL 이력이 없습니다.")
        return
    for idx, item in enumerate(rows):
        with stylable_container(key=f"hitl_history_{idx}", css_styles="""{padding: 14px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: #fff; margin-bottom: 10px;}"""):
            st.markdown(f"**run_id** `{item.get('run_id')}`")
            lineage = item.get("lineage") or {}
            st.caption(f"mode={lineage.get('mode') or '-'} / parent={lineage.get('parent_run_id') or '-'}")
            if item.get("hitl_request"):
                st.markdown("**요청**")
                st.json(item["hitl_request"])
            if item.get("hitl_response"):
                st.markdown("**응답**")
                st.json(item["hitl_response"])


def _extract_workspace_result_context(latest_bundle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    result = dict(((latest_bundle.get("result") or {}).get("result") or {}))
    timeline = latest_bundle.get("timeline") or []
    screening_meta: dict[str, Any] = {}
    derived_policy_refs: list[dict[str, Any]] = []
    for event in timeline:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        if payload.get("event_type") == "SCREENING_RESULT":
            screening_meta = payload.get("metadata") or {}
        if payload.get("event_type") == "TOOL_RESULT":
            meta = payload.get("metadata") or {}
            if meta.get("skill") == "policy_rulebook_probe":
                derived_policy_refs = ((meta.get("facts") or {}).get("policy_refs") or [])
    if not result.get("severity") and screening_meta.get("severity"):
        result["severity"] = screening_meta.get("severity")
    if not result.get("score") and screening_meta.get("score") is not None:
        result["score"] = screening_meta.get("score")
    if not result.get("status") and latest_bundle.get("hitl_request"):
        result["status"] = "HITL_REQUIRED"
    return result, screening_meta, result.get("policy_refs") or derived_policy_refs or []


def _has_pending_hitl(latest_bundle: dict[str, Any]) -> bool:
    result = ((latest_bundle.get("result") or {}).get("result") or {})
    timeline = latest_bundle.get("timeline") or []
    if latest_bundle.get("hitl_response"):
        return False
    if latest_bundle.get("hitl_request"):
        return True
    if str(result.get("status") or "").upper() == "HITL_REQUIRED":
        return True
    for event in timeline:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        if str(payload.get("event_type") or "").upper() in {"HITL_REQUESTED", "HITL_PAUSE"}:
            return True
    return False


def _fallback_hitl_request(latest_bundle: dict[str, Any]) -> dict[str, Any]:
    result, screening_meta, _policy_refs = _extract_workspace_result_context(latest_bundle)
    verification_summary = result.get("verification_summary") or {}
    reasons = []
    if screening_meta.get("reasonText"):
        reasons.append(str(screening_meta.get("reasonText")))
    gate_policy = verification_summary.get("gate_policy")
    if gate_policy:
        reasons.append(f"검증 게이트 판정이 {gate_policy} 상태입니다.")
    quality_codes = result.get("quality_gate_codes") or []
    if quality_codes:
        reasons.append(f"품질 신호: {', '.join(str(x) for x in quality_codes)}")
    if not reasons:
        reasons.append("심층 감사 결과 미확보 또는 근거 검증 미통과로 사람 검토가 필요합니다.")
    questions = [
        "업무 목적과 사전 승인 여부를 확인해 주세요.",
        "참석자와 증빙이 규정 기준을 충족하는지 확인해 주세요.",
    ]
    return {
        "required": True,
        "handoff": "FINANCE_REVIEWER",
        "reasons": reasons,
        "questions": questions,
    }


def _build_hitl_summary_sections(latest_bundle: dict[str, Any]) -> dict[str, list[str]]:
    result, screening_meta, policy_refs = _extract_workspace_result_context(latest_bundle)
    hitl_request = latest_bundle.get("hitl_request") or {}
    verification_summary = result.get("verification_summary") or {}

    review_reasons = [str(x) for x in (hitl_request.get("reasons") or []) if x]
    if not review_reasons:
        review_reasons = ["자동 판정을 진행하기에 근거 또는 검증 정보가 부족합니다."]

    stop_reasons: list[str] = []
    gate_policy = verification_summary.get("gate_policy")
    if gate_policy:
        stop_reasons.append(f"검증 게이트 판정: {gate_policy}")
    if verification_summary:
        covered = verification_summary.get("covered", 0)
        total = verification_summary.get("total", 0)
        ratio = verification_summary.get("coverage_ratio")
        if total:
            ratio_text = f" ({(ratio or 0) * 100:.0f}%)" if ratio is not None else ""
            stop_reasons.append(f"근거 연결률 {covered}/{total}{ratio_text}로 자동 확정 기준을 충족하지 못했습니다.")
        missing = verification_summary.get("missing_citations") or []
        if missing:
            stop_reasons.append(f"누락된 citation 문장이 {len(missing)}건 있습니다.")
    quality_codes = result.get("quality_gate_codes") or []
    if quality_codes:
        stop_reasons.append(f"검증 신호: {', '.join(str(x) for x in quality_codes)}")
    if not stop_reasons:
        stop_reasons = ["사람 검토 없이 최종 상태·심각도·점수·규정 근거를 확정하기 어려워 자동 확정을 중단했습니다."]

    questions = [str(x) for x in (hitl_request.get("questions") or []) if x]
    if not questions:
        questions = [
            "이 거래를 정상으로 볼 수 있는 업무 목적과 사전 승인 여부를 확인해 주세요.",
            "참석자, 증빙, 전표 입력 정보가 규정 기준을 충족하는지 확인해 주세요.",
        ]

    evidence_lines: list[str] = []
    case_type = screening_meta.get("caseType") or screening_meta.get("case_type") or result.get("case_type")
    if case_type:
        evidence_lines.append(f"위험 유형: {case_type_display_name(case_type)}")
    if result.get("severity"):
        evidence_lines.append(f"심각도: {severity_display_name(result.get('severity'))}")
    if result.get("score") is not None:
        evidence_lines.append(f"점수: {result.get('score')}")
    if screening_meta.get("reasonText"):
        evidence_lines.append(f"스크리닝 요약: {screening_meta.get('reasonText')}")
    if verification_summary:
        covered = verification_summary.get("covered", 0)
        total = verification_summary.get("total", 0)
        evidence_lines.append(f"검증 대상 문장: {covered}/{total}건 근거 연결")
    if policy_refs:
        ref_preview = [f"{ref.get('article') or '-'} / {ref.get('parent_title') or '-'}" for ref in policy_refs[:3]]
        evidence_lines.append("연결 규정: " + " | ".join(ref_preview))
    if hitl_request.get("handoff"):
        evidence_lines.append(f"검토 담당: {hitl_request.get('handoff')}")
    if not evidence_lines:
        evidence_lines = ["현재 화면에서 표시 가능한 근거 요약이 아직 충분하지 않습니다. run diagnostics와 실행 로그를 함께 확인해 주세요."]

    return {
        "review_reasons": review_reasons,
        "stop_reasons": stop_reasons,
        "questions": questions,
        "evidence_lines": evidence_lines,
        "debug": {
            "hitl_request": hitl_request,
            "verification_summary": verification_summary,
            "quality_gate_codes": quality_codes,
        },
    }


def render_hitl_panel(latest_bundle: dict[str, Any]) -> None:
    run_id = latest_bundle.get("run_id")
    hitl_request = latest_bundle.get("hitl_request") or _fallback_hitl_request(latest_bundle)
    if not run_id or not _has_pending_hitl(latest_bundle):
        return
    st.markdown(
        """
        <style>
        /* 팝업 전체 기본 텍스트 색상: 하얀 배경에서 검정 계열로 가독성 확보 */
        div[data-testid="stDialog"],
        div[data-testid="stDialog"] * {
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] {
          width: min(94vw, 1520px) !important;
          max-width: 1520px !important;
          max-height: 90vh !important;
          overflow: hidden !important;
          background: #ffffff !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div {
          width: 100% !important;
          max-width: 1520px !important;
          max-height: 90vh !important;
          overflow-y: auto !important;
          background: #ffffff !important;
        }
        div[data-testid="stDialog"] [data-testid="stVerticalBlock"] {
          max-width: 100% !important;
        }
        div[data-testid="stDialog"] [data-testid="stVerticalBlock"] > div:first-child {
          margin-top: 0 !important;
          padding-top: 0 !important;
        }
        /* 제목과 본문 사이 간격 축소 */
        div[data-testid="stDialog"] [data-testid="stDialogHeader"],
        div[data-testid="stDialog"] div[role="dialog"] > div:first-child {
          margin-bottom: 0 !important;
          padding-bottom: 0 !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div:last-child {
          margin-top: 0 !important;
          padding-top: 0.125rem !important;
        }
        div[data-testid="stDialog"] button[aria-label="Close"] {
          color: #0f172a !important;
          background: #ffffff !important;
          border: 1px solid #cbd5e1 !important;
          border-radius: 999px !important;
          width: 32px !important;
          height: 32px !important;
        }
        div[data-testid="stDialog"] button[aria-label="Close"]:hover {
          background: #f8fafc !important;
          border-color: #94a3b8 !important;
        }
        /* 제목·본문·라벨 등 모든 텍스트 노드 검정 계열 (#0f172a) */
        div[data-testid="stDialog"] h1,
        div[data-testid="stDialog"] h2,
        div[data-testid="stDialog"] h3,
        div[data-testid="stDialog"] [data-testid="stDialogHeader"],
        div[data-testid="stDialog"] [data-testid="stDialogHeader"] *,
        div[data-testid="stDialog"] [data-testid="stHeading"],
        div[data-testid="stDialog"] [data-testid="stHeading"] *,
        div[data-testid="stDialog"] [data-testid="stMarkdown"],
        div[data-testid="stDialog"] [data-testid="stMarkdown"] *,
        div[data-testid="stDialog"] p,
        div[data-testid="stDialog"] label,
        div[data-testid="stDialog"] span,
        div[data-testid="stDialog"] small {
          color: #0f172a !important;
        }
        /* 검토 요청 원본 보기: 배경 흰색, 텍스트 검정 */
        div[data-testid="stDialog"] details,
        div[data-testid="stDialog"] details summary,
        div[data-testid="stDialog"] [data-testid="stExpanderDetails"],
        div[data-testid="stDialog"] [data-testid="stExpanderDetails"] * {
          background: #ffffff !important;
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] [data-testid="stJson"],
        div[data-testid="stDialog"] [data-testid="stJson"] *,
        div[data-testid="stDialog"] [data-testid="stJson"] pre,
        div[data-testid="stDialog"] [data-testid="stJson"] code {
          background: #ffffff !important;
          color: #0f172a !important;
        }
        /* 검토 요청 원본 보기 행: expander 오른쪽 끝을 위 컴포넌트(검토 의견) 오른쪽 끝과 맞춤 */
        div[data-testid="stDialog"] [data-testid="stForm"] [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:last-child {
          display: flex !important;
          align-items: stretch !important;
          gap: 0 !important;
        }
        div[data-testid="stDialog"] [data-testid="stForm"] [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:last-child > div:first-child {
          flex: 1 1 0% !important;
          min-width: 0 !important;
          max-width: none !important;
        }
        div[data-testid="stDialog"] [data-testid="stForm"] [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:last-child > div:last-child {
          flex: 0 0 auto !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with stylable_container(
        key=f"workspace_hitl_skin_{run_id}",
        css_styles="""
        {
          border: 1px solid #e5e7eb;
          border-radius: 18px;
          background: rgba(255,255,255,0.98);
          padding: 6px 16px 10px 16px;
          box-shadow: 0 8px 22px rgba(15,23,42,0.05);
        }
        .mt-hitl-note {
          margin: 0 0 4px 0;
          padding: 8px 12px;
          border-radius: 14px;
          border: 1px solid #bfdbfe;
          background: #eff6ff;
          color: #1e3a8a;
          font-size: 0.89rem;
          line-height: 1.35;
        }
        .mt-hitl-grid {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 10px;
          margin: 4px 0 10px 0;
        }
        .mt-hitl-box {
          border: 1px solid #e5e7eb;
          border-radius: 16px;
          background: #ffffff;
          padding: 12px 13px;
          min-height: 0;
        }
        .mt-hitl-box--reason {
          border-color: #fecaca;
          background: #fff7f7;
        }
        .mt-hitl-box--stop {
          border-color: #fde68a;
          background: #fffbeb;
        }
        .mt-hitl-box--question {
          border-color: #bfdbfe;
          background: #eff6ff;
        }
        .mt-hitl-box--evidence {
          border-color: #c7d2fe;
          background: #f5f3ff;
        }
        .mt-hitl-box-title {
          color: #0f172a;
          font-size: 0.92rem;
          font-weight: 700;
          margin-bottom: 6px;
          display: flex;
          align-items: center;
          gap: 6px;
        }
        .mt-hitl-icon {
          width: 22px;
          height: 22px;
          border-radius: 999px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          font-size: 0.8rem;
          font-weight: 700;
          flex-shrink: 0;
        }
        .mt-hitl-icon--reason {
          background: #fee2e2;
          color: #b91c1c;
        }
        .mt-hitl-icon--stop {
          background: #fef3c7;
          color: #92400e;
        }
        .mt-hitl-icon--question {
          background: #dbeafe;
          color: #1d4ed8;
        }
        .mt-hitl-icon--evidence {
          background: #ede9fe;
          color: #6d28d9;
        }
        .mt-hitl-list {
          margin: 0;
          padding-left: 16px;
          color: #334155;
          line-height: 1.45;
          font-size: 0.88rem;
        }
        .mt-hitl-list li + li {
          margin-top: 4px;
        }
        .mt-hitl-checklist {
          margin-top: 8px;
          padding: 8px 10px;
          border-radius: 12px;
          background: rgba(255,255,255,0.82);
          border: 1px dashed #cbd5e1;
        }
        .mt-hitl-checklist-title {
          color: #0f172a;
          font-size: 0.84rem;
          font-weight: 700;
          margin-bottom: 4px;
        }
        .mt-hitl-checklist ul {
          margin: 0;
          padding-left: 18px;
          color: #475569;
          font-size: 0.84rem;
          line-height: 1.4;
        }
        .mt-hitl-form-grid {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 10px;
          margin: 4px 0 8px 0;
        }
        .mt-hitl-actions {
          display: grid;
          grid-template-columns: 1fr auto 1fr;
          align-items: center;
          gap: 12px;
          margin-top: 10px;
        }
        .mt-hitl-actions-left {
          justify-self: start;
          min-width: 0;
        }
        .mt-hitl-actions-center {
          justify-self: center;
          width: 280px;
        }
        div[data-baseweb="radio"] label,
        div[role="radiogroup"] label,
        div[role="radiogroup"] * {
          color: #0f172a !important;
        }
        [data-testid="stTextInput"] label,
        [data-testid="stTextArea"] label,
        [data-testid="stCheckbox"] label,
        [data-testid="stCheckbox"] span {
          color: #0f172a !important;
        }
        [data-testid="stTextInput"] input,
        [data-testid="stTextArea"] textarea {
          background: #ffffff !important;
          color: #0f172a !important;
          -webkit-text-fill-color: #0f172a !important;
          caret-color: #0f172a !important;
          border: 1px solid #cbd5e1 !important;
          border-radius: 12px !important;
        }
        [data-testid="stTextInput"] input::placeholder,
        [data-testid="stTextArea"] textarea::placeholder {
          color: #64748b !important;
          opacity: 1 !important;
        }
        [data-testid="stTextInput"] > div,
        [data-testid="stTextArea"] > div {
          background: transparent !important;
        }
        [data-testid="stCheckbox"] > div {
          background: transparent !important;
        }
        [data-testid="stJson"],
        [data-testid="stJson"] *,
        [data-testid="stJson"] pre,
        [data-testid="stJson"] code,
        [data-testid="stJson"] pre * {
          background: #ffffff !important;
          color: #0f172a !important;
        }
        [data-testid="stAlert"] {
          background: #fffbeb !important;
          border: 1px solid #fde68a !important;
          color: #92400e !important;
        }
        [data-testid="stAlert"] * {
          color: #92400e !important;
        }
        div[data-testid="stDialog"] [data-testid="stFormSubmitButton"] button,
        div[data-testid="stDialog"] button[kind="primary"],
        div[data-testid="stDialog"] button[data-testid="baseButton-primary"] {
          background: #2563eb !important;
          color: #ffffff !important;
          border: 1px solid #2563eb !important;
        }
        div[data-testid="stDialog"] [data-testid="stFormSubmitButton"] button:hover,
        div[data-testid="stDialog"] button[kind="primary"]:hover,
        div[data-testid="stDialog"] button[data-testid="baseButton-primary"]:hover {
          background: #1d4ed8 !important;
          border-color: #1d4ed8 !important;
        }
        div[data-testid="stDialog"] button[kind="secondary"],
        div[data-testid="stDialog"] button[data-testid="baseButton-secondary"] {
          background: #ffffff !important;
          color: #0f172a !important;
          border: 1px solid #cbd5e1 !important;
        }
        div[data-testid="stDialog"] details,
        div[data-testid="stDialog"] details * {
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] details summary {
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] details > div,
        div[data-testid="stDialog"] details [data-testid="stExpanderDetails"] {
          background: #ffffff !important;
        }
        @media (max-width: 1200px) {
          .mt-hitl-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
          }
        }
        @media (max-width: 900px) {
          .mt-hitl-form-grid {
            grid-template-columns: 1fr;
          }
          .mt-hitl-actions {
            grid-template-columns: 1fr;
          }
          .mt-hitl-actions-center {
            width: 100%;
          }
        }
        """,
    ):
        summary = _build_hitl_summary_sections(latest_bundle)
        st.markdown(
            '<div class="mt-hitl-note"><strong>사람 검토가 필요한 상태입니다.</strong> 아래 4개 영역에서 검토 사유, 자동 확정 중단 이유, 필요한 답변, 현재 확보된 근거를 먼저 확인한 뒤 입력을 제출해 주세요.</div>',
            unsafe_allow_html=True,
        )

        def _render_box(title: str, lines: list[str], tone: str, icon: str, checklist: list[str] | None = None) -> str:
            items = "".join(f"<li>{line}</li>" for line in lines)
            checklist_html = ""
            if checklist:
                checks = "".join(f"<li>{item}</li>" for item in checklist)
                checklist_html = (
                    '<div class="mt-hitl-checklist">'
                    '<div class="mt-hitl-checklist-title">검토 포인트</div>'
                    f"<ul>{checks}</ul>"
                    "</div>"
                )
            return (
                f'<div class="mt-hitl-box mt-hitl-box--{tone}">'
                f'<div class="mt-hitl-box-title"><span class="mt-hitl-icon mt-hitl-icon--{tone}">{icon}</span>{title}</div>'
                f'<ul class="mt-hitl-list">{items}</ul>'
                f"{checklist_html}"
                "</div>"
            )

        st.markdown(
            '<div class="mt-hitl-grid">'
            + _render_box(
                "검토 필요 사유",
                summary["review_reasons"],
                "reason",
                "!",
                ["추가 소명 또는 증빙이 없으면 자동 확정 않습니다"],
            )
            + _render_box(
                "자동 확정 중단 이유",
                summary["stop_reasons"],
                "stop",
                "■",
                ["근거 연결률과 게이트 판정이 자동 확정 가능 수준인지 확인합니다."],
            )
            + _render_box(
                "검토자가 답해야 할 질문",
                summary["questions"],
                "question",
                "?",
                ["업무 목적", "사전 승인 여부", "참석자 및 증빙 확인"],
            )
            + _render_box(
                "현재 확보된 근거 요약",
                summary["evidence_lines"],
                "evidence",
                "i",
                ["위험 유형과 연결 규정을 먼저 읽고 의견 작성."],
            )
            + '</div>',
            unsafe_allow_html=True,
        )
        with st.form(key=f"hitl_form_{run_id}"):
            decision = st.radio(
                "판단 선택",
                options=["보류/추가 검토", "승인 가능"],
                horizontal=True,
                label_visibility="collapsed",
            )
            info_cols = st.columns(3)
            with info_cols[0]:
                reviewer = st.text_input("검토자", value="FINANCE_REVIEWER")
            with info_cols[1]:
                business_purpose = st.text_input("업무 목적", placeholder="예: 주말 장애 대응 회의")
            with info_cols[2]:
                attendees_raw = st.text_input("참석자(쉼표 구분)", placeholder="예: 홍길동, 김민수, 외부 파트너 1명")
            comment = st.text_area(
                "검토 의견",
                height=96,
                placeholder="왜 승인 또는 보류로 판단했는지 핵심 근거를 적습니다.\n예: 주말 대응 프로젝트로 야간 회의 후 식대 사용. 사전 승인 메일 확인됨.",
            )
            approved = decision == "승인 가능"
            row_cols = st.columns([1, 0.22])
            with row_cols[0]:
                with st.expander("검토 요청 원본 보기", expanded=False):
                    st.json(summary["debug"])
            with row_cols[1]:
                submitted = st.form_submit_button("검토 응답 제출 후 재분석")
    if submitted:
        response = post(
            f"/api/v1/analysis-runs/{run_id}/hitl",
            json_body={
                "reviewer": reviewer,
                "comment": comment,
                "approved": approved,
                "business_purpose": business_purpose,
                "attendees": [p.strip() for p in attendees_raw.split(",") if p.strip()],
            },
        )
        resumed_id = response.get("resumed_run_id") or response.get("run_id") or run_id
        st.session_state.pop(f"mt_hitl_dismissed_{run_id}", None)
        st.success(f"HITL 응답 저장 완료: run_id={resumed_id}")
        st.rerun()


@st.dialog("HITL 검토 요청", width="large")
def render_hitl_dialog(latest_bundle: dict[str, Any]) -> None:
    render_hitl_panel(latest_bundle)


def build_workspace_plan_steps(latest_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = latest_bundle.get("timeline") or []
    node_order = ["screener", "intake", "planner", "execute", "critic", "verify", "hitl_pause", "reporter", "finalizer"]
    meta = {
        "screener": ("전표 분석 / 케이스 분류", "전표 데이터(발생 시각·근태·가맹점 업종 코드(MCC)·예산 등)를 분석해 위반 유형을 식별합니다."),
        "intake": ("입력 해석", "전표 입력값과 위험 지표를 정규화합니다."),
        "planner": ("조사 계획 수립", "검증할 사실과 사용할 skill 순서를 계획합니다."),
        "execute": ("근거 수집 실행", "휴일/예산/업종/전표/규정 근거를 실제로 조회합니다."),
        "critic": ("비판적 검토", "과잉 주장과 반례 가능성을 다시 점검합니다."),
        "verify": ("검증 및 HITL 판단", "자동 판정 가능 여부와 사람 검토 필요 여부를 결정합니다."),
        "hitl_pause": ("HITL 대기", "사람 검토 필요로 일시정지. HITL 응답 후 같은 run(thread)으로 재개됩니다."),
        "reporter": ("보고 문장 생성", "근거 중심 설명 문장과 최종 요약을 만듭니다."),
        "finalizer": ("결과 확정", "상태, 점수, 이력, 저장 payload를 최종 확정합니다."),
    }
    seen: set[str] = set()
    completed: set[str] = set()
    running = None
    for event in timeline:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        node = str(payload.get("node") or "").lower()
        event_type = str(payload.get("event_type") or "").upper()
        if node in meta:
            seen.add(node)
            if event_type in {"NODE_END", "COMPLETE", "REPORT_READY", "RESULT_FINALIZED", "HITL_PAUSE"}:
                completed.add(node)
            if event_type in {"NODE_START", "PLAN_READY", "TOOL_CALL", "TOOL_RESULT"}:
                running = node
    steps = []
    for order, node in enumerate(node_order, start=1):
        title, description = meta[node]
        status = "완료" if node in completed else "진행중" if node == running else "수행" if node in seen else "대기"
        steps.append({"order": order, "title": title, "description": description, "status": status})
    return steps


def build_workspace_execution_logs(latest_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for event in latest_bundle.get("timeline") or []:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        event_type = str(payload.get("event_type") or "").upper()
        if event_type not in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED", "HITL_REQUESTED", "SCREENING_RESULT"}:
            continue
        rows.append(
            {
                "at": event.get("at") or payload.get("timestamp") or "-",
                "node": payload.get("node") or "-",
                "event_type": event_type,
                "tool": payload.get("tool") or "-",
                "message": payload.get("message") or "-",
                "observation": payload.get("observation") or "",
            }
        )
    return rows


@st.dialog("케이스 정보")
def render_case_preview_dialog(case_item: dict[str, Any]) -> None:
    kv = [
        ("전표키", case_item.get("voucher_key") or "-"),
        ("가맹점", case_item.get("merchant_name") or "-"),
        ("금액", f"{fmt_num(case_item.get('amount'))} {case_item.get('currency') or ''}"),
        ("발생일시", fmt_dt(case_item.get("occurred_at")) or "-"),
        ("상태", status_display_name(case_item.get("case_status"))),
        ("심각도", severity_display_name(case_item.get("severity"))),
        ("유형", case_type_display_name(case_item.get("case_type"))),
        ("근태", hr_status_display_name(case_item.get("hr_status"))),
        ("업종", mcc_display_name(case_item.get("mcc_code"))),
        ("예산", budget_exceeded_display(case_item.get("budget_exceeded"))),
    ]
    kv_html = "".join(
        f'<span class="mt-kv-key">{k}</span><span class="mt-kv-value">{v}</span>' for k, v in kv
    )
    st.markdown(f'<div class="mt-kv-grid">{kv_html}</div>', unsafe_allow_html=True)
    if st.button("이 케이스 열기", use_container_width=True, type="primary"):
        st.session_state["mt_selected_voucher"] = case_item.get("voucher_key")
        st.session_state["mt_case_preview"] = None
        st.rerun()
    if st.button("닫기", use_container_width=True):
        st.session_state["mt_case_preview"] = None
        st.rerun()


def render_workspace_case_queue(items: list[dict[str, Any]], selected_key: str | None) -> None:
    render_panel_header("케이스", "시연용 전표 목록에서 한 건을 선택하면 AI가 실제 추론과 검증을 수행합니다.")
    tabs = st.tabs(["전체", "검토 필요"])
    grouped = {
        "전체": items,
        "검토 필요": [item for item in items if str(item.get("case_status") or "").upper() in {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "HITL_REQUIRED"}],
    }
    for tab, label in zip(tabs, ["전체", "검토 필요"]):
        with tab:
            with stylable_container(
                key=f"workspace_case_scroll_{label}",
                css_styles="""
                {
                  max-height: 66vh;
                  overflow-y: auto;
                  padding-right: 6px;
                }
                """,
            ):
                for item in grouped[label]:
                    case_key = item["voucher_key"]
                    selected_css = (
                        "border: 2px solid #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.08), 0 12px 26px rgba(15,23,42,0.08);"
                        if case_key == selected_key
                        else "border: 1px solid #e5e7eb; box-shadow: 0 8px 22px rgba(15,23,42,0.04);"
                    )
                    with stylable_container(
                        key=f"workspace_case_{label}_{case_key}",
                        css_styles=f"""
                        {{
                          background: rgba(255,255,255,0.98);
                          {selected_css}
                          border-radius: 18px;
                          padding: 0.85rem 1rem 1rem 1rem;
                          margin-bottom: 0.8rem;
                        }}
                        """,
                    ):
                        st.markdown(
                            status_badge(item.get("case_status")) + severity_badge(item.get("severity")) + case_type_badge(item.get("case_type")),
                            unsafe_allow_html=True,
                        )
                        kv_pairs = [
                            ("전표키", case_key),
                            ("가맹점", item.get("merchant_name") or "-"),
                            ("금액", f"{fmt_num(item.get('amount'))} {item.get('currency') or ''}"),
                            ("발생일시", fmt_dt(item.get("occurred_at")) or "-"),
                            ("상태", status_display_name(item.get("case_status"))),
                            ("심각도", severity_display_name(item.get("severity"))),
                            ("유형", case_type_display_name(item.get("case_type"))),
                            ("근태", hr_status_display_name(item.get("hr_status"))),
                            ("업종", mcc_display_name(item.get("mcc_code"))),
                            ("예산", budget_exceeded_display(item.get("budget_exceeded"))),
                        ]
                        kv_html = "".join(
                            f'<span class="mt-kv-key">{k}</span><span class="mt-kv-value">{v}</span>' for k, v in kv_pairs
                        )
                        st.markdown(f'<div class="mt-kv-grid">{kv_html}</div>', unsafe_allow_html=True)
                        action = st.columns([0.45, 0.55])
                        if action[0].button("상세", key=f"preview_{label}_{case_key}", use_container_width=True):
                            st.session_state["mt_case_preview"] = item
                            st.rerun()
                        if action[1].button(
                            "선택",
                            key=f"select_{label}_{case_key}",
                            use_container_width=True,
                            type="primary" if case_key == selected_key else "secondary",
                        ):
                            st.session_state["mt_selected_voucher"] = case_key
                            st.session_state["mt_case_preview"] = None
                            st.rerun()


def render_workspace_chat_panel(selected: dict[str, Any], latest_bundle: dict[str, Any]) -> None:
    result = ((latest_bundle.get("result") or {}).get("result") or {})
    timeline = latest_bundle.get("timeline") or []
    render_panel_header("에이전트 대화", "실제 LangGraph 실행 중 공개 가능한 작업 메모 스트림(reasoning note·도구 호출)을 실시간으로 표시합니다.")
    st.markdown(
        status_badge(result.get("status") if result else selected.get("case_status"))
        + severity_badge(result.get("severity") if result else selected.get("severity"))
        + case_type_badge(selected.get("case_type")),
        unsafe_allow_html=True,
    )
    kv_pairs = [
        ("전표키", selected.get("voucher_key") or "-"),
        ("가맹점", selected.get("merchant_name") or "-"),
        ("금액", f"{fmt_num(selected.get('amount'))} {selected.get('currency') or ''}"),
        ("발생일시", fmt_dt(selected.get("occurred_at")) or "-"),
        ("상태", status_display_name(result.get("status") if result else selected.get("case_status"))),
        ("심각도", severity_display_name(result.get("severity") if result else selected.get("severity"))),
        ("유형", case_type_display_name(selected.get("case_type"))),
        ("근태", hr_status_display_name(selected.get("hr_status"))),
        ("업종", mcc_display_name(selected.get("mcc_code"))),
        ("예산", budget_exceeded_display(selected.get("budget_exceeded"))),
    ]
    kv_html = "".join(
        f'<span class="mt-kv-key">{k}</span><span class="mt-kv-value">{v}</span>' for k, v in kv_pairs
    )
    st.markdown(f'<div class="mt-kv-grid">{kv_html}</div>', unsafe_allow_html=True)

    # --- Screening status banner (inner columns so info/success do not overflow right) ---
    vkey = selected.get("voucher_key") or ""
    is_unscreened = str(selected.get("case_type") or "").upper() == "UNSCREENED"
    _sb_l, sb_mid, _sb_r = st.columns([0.02, 0.96, 0.02])
    with sb_mid:
        if is_unscreened:
            st.info("이 전표는 아직 스크리닝되지 않았습니다. 분석 시작 시 자동으로 케이스 유형을 분류합니다.", icon="🔍")
        else:
            screened_label_map = {
                "HOLIDAY_USAGE": "휴일/휴무 중 사용 의심",
                "LIMIT_EXCEED": "한도 초과 의심",
                "PRIVATE_USE_RISK": "사적 사용 위험",
                "UNUSUAL_PATTERN": "비정상 패턴",
                "NORMAL_BASELINE": "정상 범위",
            }
            ct = selected.get("case_type") or "-"
            ct_label = screened_label_map.get(ct, ct)
            st.success(f"스크리닝 완료 — {ct_label} / 심각도 {selected.get('severity') or '-'}")

    # Button is inside inner columns to prevent it from overflowing the panel's CSS padding.
    # chat panel is inside 'right' (level 1 column), so these are level 2 - allowed.
    _lb, btn_c, _rb = st.columns([0.01, 0.98, 0.01])
    with btn_c:
        run_clicked = st.button("분석 시작", key=f"workspace_run_{vkey}", use_container_width=True, type="primary")
    if run_clicked:
        response = post(f"/api/v1/cases/{vkey}/analysis-runs")
        st.session_state.pop(f"mt_hitl_dismissed_{response['run_id']}", None)
        st.success(f"분석 시작: run_id={response['run_id']}")
        st.write_stream(sse_text_stream(f"{API}{response['stream_path']}"))
        st.rerun()
    if _has_pending_hitl(latest_bundle):
        st.warning("이 분석은 사람 검토가 필요합니다. 검토 의견을 입력하면 같은 run으로 재개됩니다.")
        _hl, hitl_btn_col, _hr = st.columns([0.01, 0.98, 0.01])
        with hitl_btn_col:
            if st.button("HITL 검토 입력 열기", key=f"workspace_hitl_open_{vkey}", use_container_width=True):
                render_hitl_dialog(latest_bundle)
        run_id = latest_bundle.get("run_id")
        if run_id:
            key = f"mt_hitl_dismissed_{run_id}"
            st.session_state.setdefault(key, False)
            if not st.session_state.get(key, False):
                render_hitl_dialog(latest_bundle)
            render_hitl_dialog(latest_bundle)
    if not timeline:
        _es_l, es_mid, _es_r = st.columns([0.02, 0.96, 0.02])
        with es_mid:
            st.caption("에이전트 대화: 실행 중 reasoning note·도구 호출이 실시간으로 표시됩니다.")
            render_empty_state("분석을 시작하면 LangGraph 실행 로그와 보고 문장이 여기에 실시간으로 표시됩니다.")
        return
    # role: PoC에서 도구 호출/결과는 "user"(사람 아이콘), 노드 진행은 "assistant"(로봇 아이콘)로 구분해 표시합니다.
    for idx, event in enumerate(timeline[-14:]):
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        ev_type = str(payload.get("event_type") or "").upper()
        role = "user" if ev_type in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"} else "assistant"
        tool_name = payload.get("tool")
        meta = payload.get("metadata") or {}
        tool_desc = meta.get("tool_description")
        caption_first = f"{fmt_dt_korea(event.get('at') or payload.get('timestamp')) or '-'}"
        part2 = f"{payload.get('node') or '-'} / {ev_type}"
        tool_frag = _tool_caption_fragment(ev_type, tool_name, tool_desc, html_tooltip=True)
        if tool_frag:
            part2 = f"{payload.get('node') or '-'} / {tool_frag}"
        note_source = meta.get("note_source", "")
        note_model = meta.get("note_model") or ""
        source_label = note_model if note_source == "llm" and note_model else ("LLM" if note_source == "llm" else "정의문구" if note_source == "fallback" else "")
        with st.chat_message(role):
            st.caption(f"{caption_first} · {part2}", unsafe_allow_html=True)
            if source_label:
                st.caption(f"*{source_label}*")
            if payload.get("message"):
                st.write(payload["message"])
            cols = st.columns(3)
            if payload.get("thought"):
                cols[0].caption("생각")
                cols[0].write(payload["thought"])
            if payload.get("action"):
                cols[1].caption("행동")
                cols[1].write(payload["action"])
            if payload.get("observation"):
                cols[2].caption("관찰")
                cols[2].write(payload["observation"])


def render_workspace_results(latest_bundle: dict[str, Any], debug_mode: bool) -> None:
    result = dict(((latest_bundle.get("result") or {}).get("result") or {}))
    timeline = latest_bundle.get("timeline") or []

    screening_meta = {}
    derived_policy_refs: list[dict[str, Any]] = []
    for event in timeline:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        if payload.get("event_type") == "SCREENING_RESULT":
            screening_meta = payload.get("metadata") or {}
        if payload.get("event_type") == "TOOL_RESULT":
            meta = payload.get("metadata") or {}
            if meta.get("skill") == "policy_rulebook_probe":
                derived_policy_refs = ((meta.get("facts") or {}).get("policy_refs") or [])

    if not result.get("severity") and screening_meta.get("severity"):
        result["severity"] = screening_meta.get("severity")
    if not result.get("score") and screening_meta.get("score") is not None:
        result["score"] = screening_meta.get("score")
    if not result.get("status"):
        if result.get("error"):
            result["status"] = "FAILED"
        elif latest_bundle.get("hitl_request"):
            result["status"] = "HITL_REQUIRED"

    failed = bool(result.get("error"))
    critique = result.get("critique") or {}
    policy_refs = result.get("policy_refs") or derived_policy_refs or []
    hero_title = "분석 실패" if failed else (result.get("status") or "결과 없음")
    hero_sub = (
        f"{result.get('stage') or 'runner'} 단계에서 오류가 발생했습니다: {result.get('error')}"
        if failed
        else (result.get("reasonText") or "최종 판단이 아직 생성되지 않았습니다.")
    )
    render_panel_header("결과", "최종 판단 + 규정 근거 + 검증 메모 + run diagnostics를 하나의 결과 화면으로 보여줍니다.")
    st.markdown(
        f"""
        <div class="mt-card-quiet">
          <div class="mt-hero-title">{hero_title}</div>
          <div class="mt-hero-sub">{hero_sub}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("상태", "FAILED" if failed else str(result.get("status") or "-"))
    c2.metric("심각도", severity_display_name(result.get("severity")) if result.get("severity") else "-")
    score_val = result.get("score")
    if isinstance(score_val, float) and 0 <= score_val <= 1:
        score_text = f"{score_val:.2f}"
    else:
        score_text = str(score_val or "-")
    c3.metric("점수", score_text)
    if failed and debug_mode:
        st.json(result)
    if result.get("score_breakdown"):
        sb = result["score_breakdown"]
        st.caption(f"정책점수 {sb.get('policy_score', '-')} · 근거점수 {sb.get('evidence_score', '-')} · 최종점수 {sb.get('final_score', '-')}")
    verification_summary = result.get("verification_summary") or {}
    if verification_summary:
        st.markdown("#### Evidence verification")
        v1, v2, v3 = st.columns(3)
        with v1:
            ratio = verification_summary.get("coverage_ratio")
            st.metric("근거 연결률", f"{(ratio or 0) * 100:.0f}%" if ratio is not None else "-", f"{verification_summary.get('covered', 0)}/{verification_summary.get('total', 0)}")
        with v2:
            missing = verification_summary.get("missing_citations") or []
            st.metric("누락 citation 수", str(len(missing)), "검증 대상 대비")
        with v3:
            gate = verification_summary.get("gate_policy") or "-"
            st.metric("게이트 판정", str(gate), "")
        if verification_summary.get("missing_citations"):
            with st.expander("누락된 검증 대상 문장", expanded=False):
                for i, s in enumerate(verification_summary["missing_citations"], 1):
                    st.caption(f"{i}. {(s or '')[:120]}{'…' if len(str(s or '')) > 120 else ''}")
    st.markdown('<div class="mt-divider"></div>', unsafe_allow_html=True)
    st.markdown("#### 규정 근거")
    if policy_refs:
        for idx, ref in enumerate(policy_refs, start=1):
            title = f"C{idx}. {ref.get('article') or '-'} / {ref.get('parent_title') or '-'}"
            with st.expander(title, expanded=(idx == 1)):
                meta = []
                if ref.get("retrieval_score") is not None:
                    meta.append(f"score={ref.get('retrieval_score')}")
                if ref.get("source_strategy"):
                    meta.append(str(ref.get("source_strategy")))
                if meta:
                    st.caption(" · ".join(meta))
                st.write(ref.get("chunk_text") or "")
                if debug_mode:
                    st.json(ref)
    else:
        render_empty_state("연결된 규정 근거가 없습니다.")
    if critique:
        st.markdown("#### 검증 메모")
        st.json(critique if debug_mode else {"quality_gate_codes": critique.get("quality_gate_codes") or result.get("quality_gate_codes")})


def render_ai_workspace_page() -> None:
    render_page_header("AI 워크스페이스", "전표 기반 자율형 에이전트가 실제로 추론하고, 도구를 호출하고, 규정 근거를 바탕으로 판단하는 메인 시연 화면입니다.")
    items = get("/api/v1/vouchers?queue=all&limit=50").get("items") or []
    debug_mode = bool(st.session_state.get("mt_debug_mode", False))
    selected_key = st.session_state.get("mt_selected_voucher") or (items[0]["voucher_key"] if items else None)
    latest_bundle = fetch_case_bundle(selected_key) if selected_key else {"timeline": [], "history": []}

    # review_count = len([i for i in items if str(i.get("case_status") or "").upper() in {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "HITL_REQUIRED"}])
    # analyzed_count = len([i for i in items if str(i.get("case_status") or "").upper() in {"COMPLETED", "RESOLVED", "OK"}])
    # high_risk = len([i for i in items if str(i.get("severity") or "").upper() in {"HIGH", "CRITICAL"}])
    # k1, k2, k3, k4 = st.columns(4)
    # with k1:
    #     render_kpi_card("총 검토 전표", str(len(items)), "전체 큐 기준")
    # with k2:
    #     render_kpi_card("검토 필요", str(review_count), "사람 또는 추가 검증 필요")
    # with k3:
    #     render_kpi_card("고위험 탐지", str(high_risk), "HIGH/CRITICAL")
    # with k4:
    #     render_kpi_card("분석 완료", str(analyzed_count), "완료/해결 상태")

    left, right = st.columns([0.95, 1.45], gap="large")
    with left:
        with stylable_container(key="workspace_case_queue_card", css_styles="""{padding: 18px 20px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); min-height: 540px;}"""):
            render_workspace_case_queue(items, selected_key)
        preview = st.session_state.get("mt_case_preview")
        if preview:
            render_case_preview_dialog(preview)
    with right:
        with stylable_container(key="workspace_chat_card", css_styles="""{padding: 18px 20px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); margin-bottom: 12px;}"""):
            if not selected_key:
                render_empty_state("선택된 케이스가 없습니다.")
            else:
                selected = next((item for item in items if item["voucher_key"] == selected_key), None) or {}
                render_workspace_chat_panel(selected, latest_bundle)
        with stylable_container(key="workspace_result_card", css_styles="""{padding: 18px 20px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); min-height: 880px;}"""):
            if not selected_key:
                render_empty_state("케이스를 선택하면 AI 워크스페이스가 표시됩니다.")
            else:
                timeline = latest_bundle.get("timeline") or []
                plan_steps = build_workspace_plan_steps(latest_bundle)
                exec_logs = build_workspace_execution_logs(latest_bundle)
                tabs = st.tabs(["사고 과정", "작업 계획", "실행 로그", "결과"])
                with tabs[0]:
                    render_panel_header("사고 과정", "실행 후 같은 이벤트를 노드 기준으로 구조화한 리뷰 화면입니다.")
                    render_process_story(timeline, debug_mode=debug_mode)
                with tabs[1]:
                    render_panel_header("작업 계획", "Planner가 생성한 실행 단계와 현재 진행 상태입니다.")
                    for step in plan_steps:
                        with stylable_container(key=f"plan_{selected_key}_{step['order']}", css_styles="""{background: rgba(255,255,255,0.98); border: 1px solid #e5e7eb; border-radius: 16px; padding: 0.95rem 1rem; margin-bottom: 0.7rem; box-shadow: 0 8px 22px rgba(15,23,42,0.04);}"""):
                            left_step, right_step = st.columns([0.8, 0.2])
                            with left_step:
                                st.markdown(f"**{step['order']}. {step['title']}**")
                                st.caption(step["description"])
                            with right_step:
                                st.markdown(status_badge(step["status"] if step["status"] != "진행중" else "IN_REVIEW"), unsafe_allow_html=True)
                with tabs[2]:
                    tool_results = ((latest_bundle.get("result") or {}).get("result") or {}).get("tool_results") or []
                    render_panel_header("실행 로그 (orchestration)", "TOOL_CALL / TOOL_RESULT / HITL 등 오케스트레이션 이벤트와 도구 실행 요약입니다.")
                    render_tool_trace_summary(tool_results)
                    if exec_logs:
                        st.markdown("#### 실행 이벤트")
                        for idx, log in enumerate(exec_logs):
                            with stylable_container(key=f"log_{idx}_{selected_key}", css_styles="""{background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 14px; padding: 0.85rem 0.95rem; margin-bottom: 0.55rem;}"""):
                                st.caption(f"{fmt_dt_korea(log.get('at')) or '-'} · {log['node']} / {log['event_type']}")
                                st.markdown(f"**{log['tool']}**")
                                st.write(log["message"])
                                if log["observation"]:
                                    st.caption(log["observation"])
                    else:
                        render_empty_state("표시할 실행 로그가 없습니다.")
                with tabs[3]:
                    render_workspace_results(latest_bundle, debug_mode)
                    run_id = latest_bundle.get("run_id")
                    if run_id:
                        with st.expander("Run 진단 (관찰 지표)", expanded=False):
                            try:
                                diag = get(f"/api/v1/analysis-runs/{run_id}/diagnostics")
                                history = latest_bundle.get("history") or []
                                other_runs = [h for h in history if h.get("run_id") and h.get("run_id") != run_id]
                                compare_run_id = None
                                if other_runs:
                                    opts = ["— 비교 안 함 —"] + [f"{h['run_id'][:8]}… ({h.get('status') or '-'})" for h in other_runs]
                                    sel = st.selectbox("다른 Run과 비교", opts, key=f"run_compare_{selected_key or 'default'}")
                                    if sel and sel != "— 비교 안 함 —":
                                        compare_run_id = other_runs[opts.index(sel) - 1].get("run_id")
                                if compare_run_id:
                                    diag2 = get(f"/api/v1/analysis-runs/{compare_run_id}/diagnostics")
                                    st.caption(f"왼쪽: 현재 Run ({run_id[:8]}…) · 오른쪽: 비교 Run ({compare_run_id[:8]}…)")
                                    col_a, col_b = st.columns(2)
                                    with col_a:
                                        st.metric("Tool 성공률", f"{(diag.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag.get("tool_call_success_rate") is not None else "-", f"{diag.get('tool_call_ok', 0)}/{diag.get('tool_call_total', 0)}")
                                        st.metric("Citation coverage", f"{(diag.get('citation_coverage') or 0) * 100:.1f}%" if diag.get("citation_coverage") is not None else "-", "")
                                        st.metric("HITL 요청", "예" if diag.get("hitl_requested") else "아니오", "재개 성공" if diag.get("resume_success") else "")
                                        st.metric("Fallback 비율", f"{(diag.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag.get('event_count', 0)}건")
                                    with col_b:
                                        st.metric("Tool 성공률", f"{(diag2.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag2.get("tool_call_success_rate") is not None else "-", f"{diag2.get('tool_call_ok', 0)}/{diag2.get('tool_call_total', 0)}")
                                        st.metric("Citation coverage", f"{(diag2.get('citation_coverage') or 0) * 100:.1f}%" if diag2.get("citation_coverage") is not None else "-", "")
                                        st.metric("HITL 요청", "예" if diag2.get("hitl_requested") else "아니오", "재개 성공" if diag2.get("resume_success") else "")
                                        st.metric("Fallback 비율", f"{(diag2.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag2.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag2.get('event_count', 0)}건")
                                else:
                                    c1, c2, c3, c4 = st.columns(4)
                                    c1.metric("Tool 성공률", f"{(diag.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag.get("tool_call_success_rate") is not None else "-", f"{diag.get('tool_call_ok', 0)}/{diag.get('tool_call_total', 0)}")
                                    c2.metric("Citation coverage", f"{(diag.get('citation_coverage') or 0) * 100:.1f}%" if diag.get("citation_coverage") is not None else "-", "")
                                    c3.metric("HITL 요청", "예" if diag.get("hitl_requested") else "아니오", "재개 성공" if diag.get("resume_success") else "")
                                    c4.metric("Fallback 비율", f"{(diag.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag.get('event_count', 0)}건")
                            except Exception:
                                st.caption("진단 API를 불러올 수 없습니다.")
                    result_result = (latest_bundle.get("result") or {}).get("result") or {}
                    retrieval_snapshot = result_result.get("retrieval_snapshot")
                    if retrieval_snapshot:
                        with st.expander("Retrieval 인용 현황", expanded=False):
                            candidates = retrieval_snapshot.get("candidates_after_rerank") or []
                            adopted = retrieval_snapshot.get("adopted_citations") or []
                            st.caption("표시 기준: after rerank 후보 · 최종 채택 citation · 채택 이유(adoption_reason)")
                            st.caption(f"후보 청크 {len(candidates)}건 · 채택 인용 {len(adopted)}건")
                            if adopted:
                                st.markdown("**최종 채택 citation**")
                                for i, c in enumerate(adopted[:10], 1):
                                    art = c.get("article") or c.get("title") or "-"
                                    reason = c.get("adoption_reason") or "규정 근거로 채택"
                                    st.markdown(f"**{i}. {art}**  \n채택 이유: {reason}")
                            if candidates:
                                st.markdown("**후보 목록 (after rerank)**")
                                for i, g in enumerate(candidates[:5], 1):
                                    reason = g.get("adoption_reason")
                                    line = f"{i}. {g.get('article') or '-'} · {g.get('parent_title') or '-'} (score={g.get('retrieval_score') or '-'})"
                                    if reason:
                                        line += f" — {reason}"
                                    st.caption(line)
                    st.markdown("#### 분석 이력")
                    render_hitl_history(latest_bundle.get("history") or [])
