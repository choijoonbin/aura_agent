from __future__ import annotations

import json
import streamlit as st

from agent.langgraph_agent import build_agent_graph
from agent.agent_tools import TOOL_REGISTRY, get_langchain_tools
from ui.api_client import get
from ui.shared import (
    draw_agent_graph_langgraph,
    draw_agent_graph_plotly,
    draw_deep_screening_graph_langgraph,
    draw_deep_screening_graph_plotly,
    draw_tool_execution_graph,
    draw_tool_execution_graph_plotly,
    fmt_dt,
    get_agent_graph_mermaid,
    get_deep_screening_graph_mermaid,
    get_tool_display_summary_ko,
    render_empty_state,
    render_graph_image,
    render_legend,
    render_page_header,
    render_panel_header,
    stylable_container,
)

# 그래프는 LangGraph get_graph()로 실제 노드/엣지를 추출해 matplotlib PNG로 렌더링.
# CDN/Playwright 없이 동작. @st.cache_data 캐싱으로 최초 1회만 렌더링.


@st.cache_data(ttl=60, show_spinner=False)
def _get_agents() -> list[dict]:
    """에이전트 목록 API 결과를 60초 캐싱. 메뉴 전환 시 재호출 없이 즉시 반환."""
    return get("/api/v1/agents").get("items") or []


@st.cache_data(ttl=60, show_spinner=False)
def _get_agent_detail(agent_id: int) -> dict:
    """에이전트 상세 API 결과를 60초 캐싱."""
    return get(f"/api/v1/agents/{agent_id}")


def render_agent_studio_page() -> None:
    # 그래프 PNG를 미리 캐싱 (첫 방문 이후엔 즉시 반환)
    draw_agent_graph_langgraph()
    draw_deep_screening_graph_langgraph()
    draw_tool_execution_graph()

    render_page_header("에이전트 스튜디오", "에이전트 구조, 프롬프트, 런타임 스킬, 연결 지식을 한 화면에서 점검합니다.")
    agents = _get_agents()
    if not agents:
        st.info("에이전트 데이터가 없습니다.")
        return
    selected_id = st.session_state.get("mt_selected_agent_id") or agents[0]["agent_id"]
    left, right = st.columns([0.28, 0.72])
    with left:
        # Streamlit ignores CSS padding for widget width — use inner columns for side spacing.
        with stylable_container(
            key="studio_active_agent_box",
            css_styles="""{
              padding: 1rem 0 1.25rem 0;
              border-radius: 16px;
              border: 1px solid #e5e7eb;
              background: rgba(255,255,255,0.98);
              box-shadow: 0 8px 22px rgba(15,23,42,0.05);
              margin-bottom: 1rem;
              box-sizing: border-box;
            }"""
        ):
            _lpad, mid, _rpad = st.columns([0.05, 0.90, 0.05])
            with mid:
                render_panel_header("활성 에이전트", "현재 PoC에서 사용 가능한 활성 에이전트 정의입니다.")
                for agent in agents:
                    if st.button(
                        agent.get("name") or "-",
                        key=f"agent_{agent['agent_id']}",
                        type="primary" if int(agent["agent_id"]) == int(selected_id) else "secondary",
                    ):
                        st.session_state["mt_selected_agent_id"] = agent["agent_id"]
                        st.rerun()
    with right:
        detail = _get_agent_detail(int(selected_id))
        with stylable_container(key=f"studio_hero_{selected_id}", css_styles="""{padding: 18px 20px; border-radius: 18px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 10px 24px rgba(15,23,42,0.05); margin-bottom: 12px;}"""):
            st.markdown(f"## {detail.get('name') or '-'}")
            st.caption(f"agent_key={detail.get('agent_key') or '-'} / domain={detail.get('domain') or '-'}")
        tabs = st.tabs(["모델", "프롬프트", "도구", "워크플로우", "지식"])
        with tabs[0]:
            c1, c2, c3 = st.columns(3)
            c1.metric("모델", str(detail.get("model_name") or "-"))
            c2.metric("temperature", str(detail.get("temperature") or "-"))
            c3.metric("max_tokens", str(detail.get("max_tokens") or "-"))
            st.caption(f"active={detail.get('is_active')} / updated_at={fmt_dt(detail.get('updated_at'))}")
        with tabs[1]:
            render_panel_header("프롬프트", "현재 시스템 프롬프트와 변경 이력을 점검합니다.")
            current_prompt = detail.get("current_prompt") or {}
            st.text_area("Current System Prompt", value=str(current_prompt.get("system_instruction") or ""), height=320)
            with st.expander("Prompt History"):
                st.json(detail.get("prompt_history") or [])
        with tabs[2]:
            render_panel_header("런타임 도구", "실제 runtime graph에서 사용하는 LangChain tool 목록입니다.")
            st.caption("Phase C: execute 노드는 plan 기반으로 이 도구들을 호출합니다.")
            tools = get_langchain_tools()
            tool_cols = st.columns(2)
            for idx, tool in enumerate(tools):
                with tool_cols[idx % 2]:
                    with stylable_container(
                        key=f"tool_card_{getattr(tool, 'name', idx)}",
                        css_styles="""{padding: 14px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04); min-height: 128px; margin-bottom: 0.7rem;}"""
                    ):
                        tool_name = getattr(tool, "name", "-")
                        st.caption("LangChain tool")
                        st.markdown(f"**{tool_name}**")
                        tool_entry = TOOL_REGISTRY.get(tool_name) if tool_name else None
                        display_ko = get_tool_display_summary_ko(tool, getattr(tool_entry, "display_summary_ko", None) if tool_entry else None)
                        st.write(display_ko)
                        with st.expander("원본 스키마 보기", expanded=False):
                            try:
                                schema = getattr(tool, "args_schema", None)
                                if schema and hasattr(schema, "model_json_schema"):
                                    st.json(schema.model_json_schema())
                                else:
                                    st.caption("스키마 없음")
                            except Exception:
                                st.caption("스키마를 불러올 수 없습니다.")
        with tabs[3]:
            graph_tabs = st.tabs(["스크리닝", "분석 흐름", "스킬 실행 흐름"])
            with graph_tabs[1]:
                st.caption(
                    "Compiled LangGraph에서 자동 추출한 그래프입니다. "
                    "각 박스는 **노드(node)**이며, **단일 워크플로(single workflow)** 의 **스텝(step)**입니다. "
                    "독립된 다수 에이전트가 아닙니다. 코드 변경 시 다이어그램 자동 갱신, 우측 상단으로 PNG 내보내기 가능."
                )
                _plotly_err = None
                try:
                    _fig = draw_agent_graph_plotly()
                    try:
                        st.plotly_chart(_fig, use_container_width=True,
                                        config={"displayModeBar": True,
                                                "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
                                                "toImageButtonOptions": {"filename": "aura_orchestration_graph", "scale": 2}})
                    except TypeError:
                        st.plotly_chart(_fig, config={"displayModeBar": True,
                                                      "toImageButtonOptions": {"filename": "aura_orchestration_graph", "scale": 2}})
                except Exception as _e:
                    _plotly_err = _e
                    render_graph_image(
                        "분석 흐름 그래프",
                        draw_agent_graph_langgraph(), None,
                        "Main orchestration: workflow nodes and edges (single graph).",
                    )
                if _plotly_err:
                    with st.expander("⚠️ Plotly 렌더링 오류 상세", expanded=False):
                        st.code(str(_plotly_err), language="text")
                mermaid_src = get_agent_graph_mermaid()
                if mermaid_src:
                    with st.expander("Mermaid 소스 보기 (mermaid.live에 붙여넣기)", expanded=False):
                        st.code(mermaid_src, language="text")
                        st.caption("위 텍스트를 복사해 mermaid.live / Notion / GitHub README에 붙여넣으면 다이어그램으로 렌더링됩니다.")
                with stylable_container(
                    key="studio_analysis_workflow_table",
                    css_styles="""
                    {
                      border-radius: 12px;
                    }
                    table {
                      width: 100%;
                      border-collapse: collapse !important;
                      border: 1px solid #cbd5e1 !important;
                      background: #ffffff !important;
                    }
                    thead tr {
                      background: #f8fafc !important;
                    }
                    th, td {
                      border: 1px solid #cbd5e1 !important;
                      padding: 10px 12px !important;
                      vertical-align: top !important;
                    }
                    th {
                      color: #0f172a !important;
                      font-weight: 800 !important;
                    }
                    td {
                      color: #0f172a !important;
                    }
                    """
                ):
                    st.markdown("""
**분석 Workflow**

※ 각 항목은 **단일 워크플로(single workflow)** 내 **노드(node)**의 역할입니다. 여러 독립 에이전트가 협업하는 구조가 아닙니다.

| Step | Node | 설명 |
|------|------|------|
| 1 | **START** → **Start Router** | 전표가 사전분류(prescreened)인지 확인해 분기. 사전분류가 있으면 Intake로 직행, 없으면 Screener에서 1차 분류부터 시작 |
| 2 | **Screener** | 규칙+LLM 하이브리드로 케이스 유형/점수/심각도 산출. 경계구간·불일치·저신뢰 조건이면 Deep Lane 재검증 수행 |
| 3 | **Intake** | 전표 원본(body_evidence)을 표준 스키마로 정규화. 시간/금액/근태/업종 등 핵심 신호를 이후 노드가 공통 사용 가능하게 정리 |
| 4 | **Planner** | 현재 위험 유형과 상태를 기준으로 조사 계획 수립. 도구 선택/순서 기준은 ① case_type 우선(예: HOLIDAY_USAGE면 holiday/policy 우선) ② 결측 보완 우선(누락 필드 확인용 도구 선행) ③ 고위험 신호 선확인(MCC/한도/근태) ④ 마지막에 규정 매핑·점수 집계 순으로 결정 |
| 5 | **Execute** | Planner 계획대로 도구 실행. holiday/budget/merchant/policy/document probe를 호출해 증거(evidence), 규정 후보, 점수 신호 수집 |
| 6 | **Critic** | 실행 결과의 과잉 주장/근거 약함/반례를 점검. 기준: 주장-인용 범위 일치 여부(과잉 주장), 인용 수/정합성/신뢰도(근거 약함), 결론과 충돌하는 도구 신호 존재 여부(반례). 문제 있으면 Planner 재계획(retry), 수용 가능하면 Verifier로 진행 |
| 7 | **Verifier** | 최종 자동판정 가능 여부를 검증. 핵심 주장마다 근거 인용이 충분한지, 주장과 증거가 서로 모순되지 않는지, 필수 입력/증빙이 충족되는지를 확인하고 통과 시 다음 단계로 진행, 미충족 시 HITL 요청 |
| 8 | **HITL Pause** | 담당자 검토가 필요한 경우 인터럽트로 일시 중지. 검토 사유/질문/증빙 요청을 생성하고 응답이 올 때까지 대기 |
| 9 | **HITL Validate** | 담당자 응답(승인/의견/증빙)을 검증. 입력 부족 시 재요청, 유효하면 동일 run_id로 분석 재개(resume) |
| 10 | **Reporter** | 사용자에게 보여줄 판단 문장 생성. 전표 요약, 적용 규정, 점수(정책/근거/최종), 조치 권고를 읽기 쉬운 형태로 구성 |
| 11 | **Finalizer** | 상태값(REVIEW_REQUIRED/COMPLETED_AFTER_HITL 등), 점수, 근거맵, 이력 데이터를 DB에 최종 반영 |
| 12 | **END** | 분석 run 종료. 워크스페이스 목록/상세에서 결과 조회 가능 상태로 전환 |
""")
            with graph_tabs[0]:
                st.caption("Screener **node** 내부의 Deep Lane **subgraph**. 승격 조건 충족 시에만 실행되며, 실패/타임아웃 시 Fast Lane 결과로 폴백합니다.")
                try:
                    _fig2 = draw_deep_screening_graph_plotly()
                    try:
                        st.plotly_chart(_fig2, use_container_width=True,
                                        config={"displayModeBar": True,
                                                "toImageButtonOptions": {"filename": "aura_deep_screening_graph", "scale": 2}})
                    except TypeError:
                        st.plotly_chart(_fig2, config={"displayModeBar": True,
                                                       "toImageButtonOptions": {"filename": "aura_deep_screening_graph", "scale": 2}})
                except Exception as _e2:
                    render_graph_image(
                        "스크리닝 서브그래프",
                        draw_deep_screening_graph_langgraph(), None,
                        "Deep Lane subgraph: 4-node LLM re-verification (runs inside Screener node).",
                    )
                    with st.expander("⚠️ Plotly 오류", expanded=False):
                        st.code(str(_e2), language="text")
                deep_mermaid = get_deep_screening_graph_mermaid()
                if deep_mermaid:
                    with st.expander("Mermaid 소스 보기", expanded=False):
                        st.code(deep_mermaid, language="text")
                st.markdown("""
**스크리닝 단계**

| Step | Node | 설명 |
|------|------|------|
| 1 | **Fast Lane (run_screening)** | 규칙+LLM 하이브리드로 1차 분류(case_type/score/severity) 수행. 규칙 예: 휴일 사용(+35), 근태 LEAVE(+20), 심야(23~06시, +10), 고위험 MCC(+30) 등 신호 점수 합산 후 LLM 제안과 정렬 |
| 2 | **Promote Check** | Deep Lane 재검증 필요 여부 판단. 대표 조건: 규칙/LLM 유형 불일치, LLM 신뢰도 임계치 미만(기본 0.75), 경계 점수(45~65), NORMAL_BASELINE인데 위험 신호 2개 이상 |
| 3 | **intake_normalize** | Deep Lane 판단용 컨텍스트 구성(신호 표준화 + 메타). 예: isHoliday=true, hrStatus=LEAVE, mcc=5813, amount=97042, Fast 분류/점수/승격사유를 하나의 입력 객체로 정리 |
| 4 | **hypothesis_generate** | LLM이 상위 가설(Top-2)과 근거를 생성. 예: 1순위 HOLIDAY_USAGE(휴일+근태 충돌), 2순위 PRIVATE_USE_RISK(업종/시간대 정황)처럼 대안 시나리오를 함께 제시 |
| 5 | **rule_guardrail** | 가드레일 기준으로 과탐/오판 교정. 예: LLM 신뢰도가 낮으면 규칙 우선, 고위험 신호(휴일·LEAVE·고위험 MCC) 다중 충족 시 상향 유지, 증거 부족 시 보수적 유형으로 정렬 |
| 6 | **finalize_screening** | Fast/Deep 결과 병합, 최종 screening_meta 기록 |
""")
                with stylable_container(
                    key="studio_deeplane_conditions",
                    css_styles="""
                    {
                      border-radius: 12px;
                    }
                    [data-testid="stExpanderDetails"] {
                      background: #ffffff !important;
                      color: #0f172a !important;
                    }
                    [data-testid="stExpanderDetails"] * {
                      background: transparent !important;
                      color: #0f172a !important;
                    }
                    [data-testid="stExpanderDetails"] pre,
                    [data-testid="stExpanderDetails"] code,
                    [data-testid="stExpanderDetails"] table,
                    [data-testid="stExpanderDetails"] tbody,
                    [data-testid="stExpanderDetails"] thead,
                    [data-testid="stExpanderDetails"] tr,
                    [data-testid="stExpanderDetails"] td,
                    [data-testid="stExpanderDetails"] th {
                      background: transparent !important;
                      color: #0f172a !important;
                    }
                    """
                ):
                    with st.expander("Deep Lane 승격조건 (4가지 중 하나 충족 시 subgraph 실행)", expanded=False):
                        st.markdown("""
| Condition | 기준 |
|-----------|------|
| **rule_llm_mismatch** | 규칙 판정 case_type ≠ LLM 판정 case_type |
| **llm_low_confidence** | LLM 신뢰도 < 0.70 |
| **boundary_score** | 점수 45 ≤ score ≤ 65 (경계 구간) |
| **normal_baseline_with_risk_signals** | NORMAL_BASELINE + 위험 신호 ≥ 2개 |

**Fallback**: 타임아웃/에러 시 Fast Lane 결과 사용. screening_meta.lane = "fast" 로 기록.
""")
            with graph_tabs[2]:
                try:
                    _fig3 = draw_tool_execution_graph_plotly()
                    try:
                        st.plotly_chart(_fig3, use_container_width=True,
                                        config={"displayModeBar": True,
                                                "toImageButtonOptions": {"filename": "aura_skill_execution_graph", "scale": 2}})
                    except TypeError:
                        st.plotly_chart(_fig3, config={"displayModeBar": True,
                                                       "toImageButtonOptions": {"filename": "aura_skill_execution_graph", "scale": 2}})
                except Exception as _e3:
                    render_graph_image(
                        "스킬 실행 도구 그래프",
                        draw_tool_execution_graph(), None,
                        "Skill execution graph: LangChain tools invoked from the Execute node (step order).",
                    )
                    with st.expander("⚠️ Plotly 오류", expanded=False):
                        st.code(str(_e3), language="text")
                with stylable_container(
                    key="studio_skill_flow_table",
                    css_styles="""
                    {
                      border-radius: 12px;
                    }
                    table {
                      width: 100%;
                      border-collapse: collapse !important;
                      border: 1px solid #cbd5e1 !important;
                      background: #ffffff !important;
                    }
                    thead tr {
                      background: #f8fafc !important;
                    }
                    th, td {
                      border: 1px solid #cbd5e1 !important;
                      padding: 10px 12px !important;
                      vertical-align: top !important;
                    }
                    th {
                      color: #0f172a !important;
                      font-weight: 800 !important;
                    }
                    td {
                      color: #0f172a !important;
                    }
                    """
                ):
                    st.markdown("""
**스킬 실행 흐름**

| Step | Node / Tool | 설명 |
|------|-------------|------|
| 1 | **execute** | Planner가 만든 실행 계획(plan)을 읽고 도구 실행 순서를 오케스트레이션하는 허브 노드. 각 도구 결과를 누적해 다음 도구 입력으로 전달 |
| 2 | **holiday_compliance_probe** | 발생일시/휴일 여부/근태를 결합해 휴일·주말·심야 사용 신호를 검증. 예: isHoliday=true, hrStatus=LEAVE면 고위험 신호 강화 |
| 3 | **budget_risk_probe** | 전표 금액과 예산 상태를 확인해 한도 초과/임계 구간 리스크를 계산. 예: budgetExceeded=true 또는 금액 임계치 초과 시 위험도 상향 |
| 4 | **merchant_risk_probe** | 가맹점명·MCC·거래처 메타를 바탕으로 업종 위험도를 판정. 예: 고위험 MCC(주점/유흥 등)일 때 규정상 강화 승인 필요 신호 생성 |
| 5 | **document_evidence_probe** | 전표 라인, 증빙 추출 결과, 필수 입력 충족 여부를 점검해 근거 객체를 수집. 누락 항목은 이후 HITL 질문/요청 후보로 전달 |
| 6 | **policy_rulebook_probe** | 하이브리드 RAG(BM25 + Dense + RRF + rerank)로 관련 조항을 검색·채택. 조문/항/호 단위 인용 근거를 claim과 연결 |
| 7 | **legacy_aura_deep_audit** | 필요 시 추가 심층 감사 규칙을 호출해 반례/특이패턴을 재점검. 기본 경로에서는 생략 가능하며 고위험/불확실 케이스에서 보조 신호 제공 |
| 8 | **score_breakdown** | 수집된 정책 신호·근거 신호를 합산해 policy/evidence/final score를 계산하고, 가산·감점 사유 및 품질 지표를 최종 집계 |
""")
        with tabs[4]:
            render_panel_header("연결 지식", "이 에이전트가 참조하는 문서와 지식 자산입니다.")
            docs = detail.get("documents") or []
            if docs:
                for doc in docs:
                    st.markdown(f"- **{doc.get('title')}** · status={doc.get('status')} · doc_id={doc.get('doc_id')}")
            else:
                render_empty_state("연결된 지식 문서가 없습니다.")
