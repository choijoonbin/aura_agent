from __future__ import annotations

import json
from typing import Any, Iterator

import requests
import streamlit as st
from streamlit_extras.stylable_container import stylable_container

from ui.api_client import API, get, post
from ui.shared import case_type_badge, fmt_dt, fmt_num, render_kpi_card, render_page_header, severity_badge, status_badge


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


def sse_text_stream(stream_url: str) -> Iterator[str]:
    with requests.get(stream_url, stream=True, timeout=300) as response:
        response.raise_for_status()
        event = None
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
                yield "\n분석 스트림 종료\n"
                break
            try:
                obj = json.loads(payload)
                if event == "AGENT_EVENT":
                    yield _format_agent_event_line(obj)
                elif event == "completed":
                    yield f"\n[최종] {obj.get('reasonText') or obj.get('summary') or '완료'}\n"
                elif event == "failed":
                    yield f"\n[실패] {obj.get('error', 'unknown error')}\n"
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
            entry.update(metric_label="규정 근거", metric_value=f"{len(refs)}건", detail=", ".join(filter(None, [ref.get("article") for ref in refs[:3]])) or "-")
        elif skill == "document_evidence_probe":
            entry.update(metric_label="전표 라인", metric_value=f"{facts.get('lineItemCount', 0)}건")
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
        st.info("도구 실행 요약이 없습니다.")
        return
    cols = st.columns(min(3, len(cards)))
    for idx, card in enumerate(cards):
        with cols[idx % len(cols)]:
            with stylable_container(key=f"tool_summary_{idx}", css_styles="""{padding: 16px 18px; border-radius: 16px; border: 1px solid #e5e7eb; background: #fff; box-shadow: 0 8px 22px rgba(15,23,42,0.04);}"""):
                st.caption(card["skill"])
                st.markdown(f"**{card['metric_label']}**")
                st.subheader(card["metric_value"])
                st.caption(card["detail"])


def render_timeline_cards(events: list[dict[str, Any]], *, view_mode: str = "business") -> None:
    if not events:
        st.info("표시할 스트림 이벤트가 없습니다.")
        return
    with stylable_container(key="timeline_shell", css_styles="""{background: radial-gradient(circle at 1px 1px, rgba(15,23,42,0.10) 1px, transparent 0); background-size: 14px 14px; background-color:#f8fafc; border:1px dashed #dbe2ea; border-radius:18px; padding:14px;}"""):
        for index, event in enumerate(events):
            payload = event.get("payload") or {}
            if event.get("event_type") != "AGENT_EVENT":
                continue
            with stylable_container(key=f"timeline_{index}", css_styles="""{padding: 14px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: #fff; margin-bottom: 10px;}"""):
                st.caption(event.get("at") or payload.get("timestamp") or "")
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


def render_hitl_history(history: list[dict[str, Any]]) -> None:
    rows = [item for item in history if item.get("hitl_request") or item.get("hitl_response")]
    if not rows:
        st.info("HITL 이력이 없습니다.")
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


def render_hitl_panel(latest_bundle: dict[str, Any]) -> None:
    run_id = latest_bundle.get("run_id")
    hitl_request = latest_bundle.get("hitl_request")
    if not run_id or not hitl_request:
        return
    st.subheader("HITL 검토 요청")
    st.warning("이 케이스는 사람 검토가 필요합니다.")
    st.json(hitl_request)
    with st.form(key=f"hitl_form_{run_id}"):
        reviewer = st.text_input("검토자", value="FINANCE_REVIEWER")
        comment = st.text_area("검토 의견")
        business_purpose = st.text_input("업무 목적")
        attendees_raw = st.text_input("참석자(쉼표 구분)")
        approved = st.checkbox("승인/정상 가능성 있음")
        submitted = st.form_submit_button("검토 응답 제출 후 재분석")
    if submitted:
        response = post(f"/api/v1/analysis-runs/{run_id}/hitl", json_body={
            "reviewer": reviewer,
            "comment": comment,
            "approved": approved,
            "business_purpose": business_purpose,
            "attendees": [p.strip() for p in attendees_raw.split(",") if p.strip()],
        })
        st.success(f"HITL 응답 저장 완료: run_id={response.get('run_id')}")
        st.rerun()


def build_workspace_plan_steps(latest_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = latest_bundle.get("timeline") or []
    node_order = ["intake", "planner", "execute", "critic", "verify", "reporter", "finalizer"]
    meta = {
        "intake": ("입력 해석", "전표 입력값과 위험 신호를 정규화합니다."),
        "planner": ("조사 계획 수립", "검증할 사실과 사용할 skill 순서를 계획합니다."),
        "execute": ("근거 수집 실행", "휴일/예산/업종/전표/규정 근거를 실제로 조회합니다."),
        "critic": ("비판적 검토", "과잉 주장과 반례 가능성을 다시 점검합니다."),
        "verify": ("검증 및 HITL 판단", "자동 판정 가능 여부와 사람 검토 필요 여부를 결정합니다."),
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
            if event_type in {"NODE_END", "COMPLETE", "REPORT_READY", "RESULT_FINALIZED"}:
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
        if event_type not in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED", "HITL_REQUESTED"}:
            continue
        rows.append({
            "at": event.get("at") or payload.get("timestamp") or "-",
            "node": payload.get("node") or "-",
            "event_type": event_type,
            "tool": payload.get("tool") or "-",
            "message": payload.get("message") or "-",
            "observation": payload.get("observation") or "",
        })
    return rows


@st.dialog("케이스 정보")
def render_case_preview_dialog(case_item: dict[str, Any]) -> None:
    st.markdown(f"**{case_item.get('voucher_key') or '-'}**")
    st.write(case_item.get("merchant_name") or "-")
    c1, c2 = st.columns(2)
    c1.metric("금액", f"{fmt_num(case_item.get('amount'))} {case_item.get('currency') or ''}")
    c2.metric("발생시각", fmt_dt(case_item.get("occurred_at")))
    c3, c4 = st.columns(2)
    c3.metric("상태", str(case_item.get("case_status") or "-"))
    c4.metric("유형", str(case_item.get("case_type") or "-"))
    if st.button("이 케이스 열기", use_container_width=True, type="primary"):
        st.session_state["mt_selected_voucher"] = case_item.get("voucher_key")
        st.session_state["mt_case_preview"] = None
        st.rerun()
    if st.button("닫기", use_container_width=True):
        st.session_state["mt_case_preview"] = None
        st.rerun()


def render_workspace_case_queue(items: list[dict[str, Any]], selected_key: str | None) -> None:
    st.markdown("#### 케이스 큐")
    tabs = st.tabs(["전체", "검토 필요"])
    grouped = {
        "전체": items,
        "검토 필요": [item for item in items if str(item.get("case_status") or "").upper() in {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "HITL_REQUIRED"}],
    }
    for tab, label in zip(tabs, ["전체", "검토 필요"]):
        with tab:
            for item in grouped[label]:
                case_key = item["voucher_key"]
                selected_css = "border: 2px solid #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.08), 0 12px 26px rgba(15,23,42,0.08);" if case_key == selected_key else "border: 1px solid #e5e7eb; box-shadow: 0 8px 22px rgba(15,23,42,0.04);"
                with stylable_container(key=f"workspace_case_{label}_{case_key}", css_styles=f"""{{background: rgba(255,255,255,0.98); {selected_css} border-radius: 18px; padding: 0.55rem 0.65rem 0.9rem 0.65rem; margin-bottom: 0.75rem; cursor: pointer;}}"""):
                    top = st.columns([0.7, 0.3])
                    with top[0]:
                        st.markdown(status_badge(item.get("case_status")) + severity_badge(item.get("severity")) + case_type_badge(item.get("case_type")), unsafe_allow_html=True)
                        st.markdown(f'<div class="mt-case-title">{item.get("merchant_name") or "-"}</div>', unsafe_allow_html=True)
                        st.markdown(f'<div class="mt-case-sub">{fmt_num(item.get("amount"))} {item.get("currency") or ""} · {case_key}</div>', unsafe_allow_html=True)
                    with top[1]:
                        st.caption(fmt_dt(item.get("occurred_at")))
                    action = st.columns([0.45, 0.55])
                    if action[0].button("상세", key=f"preview_{label}_{case_key}", use_container_width=True):
                        st.session_state["mt_case_preview"] = item
                        st.rerun()
                    if action[1].button("선택", key=f"select_{label}_{case_key}", use_container_width=True, type="primary" if case_key == selected_key else "secondary"):
                        st.session_state["mt_selected_voucher"] = case_key
                        st.session_state["mt_case_preview"] = None
                        st.rerun()


def render_workspace_chat_panel(selected: dict[str, Any], latest_bundle: dict[str, Any]) -> None:
    result = ((latest_bundle.get("result") or {}).get("result") or {})
    timeline = latest_bundle.get("timeline") or []
    st.markdown("#### 에이전트 대화")
    st.markdown(status_badge(result.get("status") if result else selected.get("case_status")) + severity_badge(result.get("severity") if result else selected.get("severity")) + case_type_badge(selected.get("case_type")), unsafe_allow_html=True)
    st.markdown(f"**{selected.get('voucher_key') or '-'}** · {selected.get('merchant_name') or '-'}")
    st.caption(f"{fmt_num(selected.get('amount'))} {selected.get('currency') or ''} · 발생시각 {fmt_dt(selected.get('occurred_at'))}")
    if st.button("분석 시작", key=f"workspace_run_{selected.get('voucher_key')}", use_container_width=True, type="primary"):
        response = post(f"/api/v1/cases/{selected.get('voucher_key')}/analysis-runs")
        st.success(f"분석 시작: run_id={response['run_id']}")
        st.write_stream(sse_text_stream(f"{API}{response['stream_path']}"))
        st.rerun()
    if not timeline:
        st.info("분석을 시작하면 LangGraph 실행 로그와 보고 문장이 여기에 실시간으로 표시됩니다.")
        return
    for idx, event in enumerate(timeline[-14:]):
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        role = "user" if str(payload.get("event_type") or "").upper() in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"} else "assistant"
        with st.chat_message(role):
            st.caption(f"{event.get('at') or ''} · {payload.get('node') or '-'} / {payload.get('event_type') or '-'}")
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
    result = ((latest_bundle.get("result") or {}).get("result") or {})
    critique = result.get("critique") or {}
    policy_refs = result.get("policy_refs") or []
    st.markdown("#### 최종 판단")
    c1, c2, c3 = st.columns(3)
    c1.metric("상태", str(result.get("status") or "-"))
    c2.metric("심각도", str(result.get("severity") or "-"))
    c3.metric("점수", str(result.get("score") or "-"))
    if result.get("score_breakdown"):
        sb = result["score_breakdown"]
        st.caption(f"정책점수 {sb.get('policy_score', '-')} · 근거점수 {sb.get('evidence_score', '-')} · 최종점수 {sb.get('final_score', '-')}")
    st.write(result.get("reasonText") or "결과 없음")
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
        st.info("연결된 규정 근거가 없습니다.")
    if critique:
        st.markdown("#### 검증 메모")
        st.json(critique if debug_mode else {"quality_gate_codes": critique.get("quality_gate_codes") or result.get("quality_gate_codes")})


def render_ai_workspace_page() -> None:
    render_page_header("AI 워크스페이스", "전표 기반 자율형 에이전트가 실제로 추론하고, 도구를 호출하고, 규정 근거를 바탕으로 판단하는 메인 시연 화면입니다.")
    items = (get("/api/v1/vouchers?queue=all&limit=50").get("items") or [])
    debug_mode = bool(st.session_state.get("mt_debug_mode", False))
    selected_key = st.session_state.get("mt_selected_voucher") or (items[0]["voucher_key"] if items else None)
    latest_bundle = fetch_case_bundle(selected_key) if selected_key else {"timeline": [], "history": []}

    review_count = len([i for i in items if str(i.get("case_status") or "").upper() in {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "HITL_REQUIRED"}])
    analyzed_count = len([i for i in items if str(i.get("case_status") or "").upper() in {"COMPLETED", "RESOLVED", "OK"}])
    high_risk = len([i for i in items if str(i.get("severity") or "").upper() in {"HIGH", "CRITICAL"}])
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        render_kpi_card("총 검토 전표", str(len(items)), "전체 큐 기준")
    with k2:
        render_kpi_card("검토 필요", str(review_count), "사람 또는 추가 검증 필요")
    with k3:
        render_kpi_card("고위험 탐지", str(high_risk), "HIGH/CRITICAL")
    with k4:
        render_kpi_card("분석 완료", str(analyzed_count), "완료/해결 상태")

    left, right = st.columns([0.42, 0.58])
    with left:
        with stylable_container(key="workspace_case_queue_card", css_styles="""{padding: 16px 18px; border-radius: 18px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.95); box-shadow: 0 12px 30px rgba(15,23,42,0.04); max-height: min(70vh, 560px); display: flex; flex-direction: column; overflow: hidden;}"""):
            render_workspace_case_queue(items, selected_key)
        preview = st.session_state.get("mt_case_preview")
        if preview:
            render_case_preview_dialog(preview)
        with stylable_container(key="workspace_chat_card", css_styles="""{padding: 16px 18px; border-radius: 18px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.95); box-shadow: 0 12px 30px rgba(15,23,42,0.04); margin-top: 12px;}"""):
            if not selected_key:
                st.info("선택된 케이스가 없습니다.")
            else:
                selected = next((item for item in items if item["voucher_key"] == selected_key), None) or {}
                render_workspace_chat_panel(selected, latest_bundle)
    with right:
        with stylable_container(key="workspace_result_card", css_styles="""{padding: 16px 18px; border-radius: 18px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.95); box-shadow: 0 12px 30px rgba(15,23,42,0.04);}"""):
            if not selected_key:
                st.info("케이스를 선택하면 AI 워크스페이스가 표시됩니다.")
            else:
                timeline = latest_bundle.get("timeline") or []
                plan_steps = build_workspace_plan_steps(latest_bundle)
                exec_logs = build_workspace_execution_logs(latest_bundle)
                tabs = st.tabs(["사고 과정", "작업 계획", "실행 로그", "결과"])
                with tabs[0]:
                    render_timeline_cards(timeline, view_mode="debug" if debug_mode else "business")
                with tabs[1]:
                    for step in plan_steps:
                        with stylable_container(key=f"plan_{selected_key}_{step['order']}", css_styles="""{background: rgba(255,255,255,0.98); border: 1px solid #e5e7eb; border-radius: 16px; padding: 0.85rem 1rem; margin-bottom: 0.7rem; box-shadow: 0 8px 22px rgba(15,23,42,0.04);}"""):
                            st.markdown(f"**{step['order']}. {step['title']}**")
                            st.caption(step["description"])
                            st.caption(f"상태: {step['status']}")
                with tabs[2]:
                    tool_results = ((latest_bundle.get("result") or {}).get("result") or {}).get("tool_results") or []
                    render_tool_trace_summary(tool_results)
                    if exec_logs:
                        st.markdown("#### 실행 이벤트")
                        for idx, log in enumerate(exec_logs):
                            with stylable_container(key=f"log_{idx}_{selected_key}", css_styles="""{background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 14px; padding: 0.8rem 0.95rem; margin-bottom: 0.55rem;}"""):
                                st.caption(f"{log['at']} · {log['node']} / {log['event_type']}")
                                st.markdown(f"**{log['tool']}**")
                                st.write(log["message"])
                                if log["observation"]:
                                    st.caption(log["observation"])
                    else:
                        st.info("표시할 실행 로그가 없습니다.")
                with tabs[3]:
                    render_workspace_results(latest_bundle, debug_mode)
                    render_hitl_panel(latest_bundle)
                    st.markdown("#### 분석 이력")
                    render_hitl_history(latest_bundle.get("history") or [])
