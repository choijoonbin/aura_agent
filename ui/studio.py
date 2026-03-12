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
        tabs = st.tabs(["모델", "프롬프트", "도구", "지식", "그래프"])
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
            render_panel_header("연결 지식", "이 에이전트가 참조하는 문서와 지식 자산입니다.")
            docs = detail.get("documents") or []
            if docs:
                for doc in docs:
                    st.markdown(f"- **{doc.get('title')}** · status={doc.get('status')} · doc_id={doc.get('doc_id')}")
            else:
                render_empty_state("연결된 지식 문서가 없습니다.")
        with tabs[4]:
            graph_tabs = st.tabs(["메인 오케스트레이션", "딥 레인 스크리닝", "스킬 실행 흐름"])
            with graph_tabs[0]:
                st.caption(
                    "실제 컴파일된 LangGraph 객체에서 자동 추출한 그래프입니다. "
                    "코드 변경 시 다이어그램이 자동으로 갱신됩니다. "
                    "그래프 우측 상단 카메라 아이콘으로 PNG 내보내기 후 보고서·발표 자료에 활용하세요."
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
                        "메인 오케스트레이션 그래프",
                        draw_agent_graph_langgraph(), None,
                        "메인 오케스트레이션: 전체 노드 흐름.",
                    )
                if _plotly_err:
                    with st.expander("⚠️ Plotly 렌더링 오류 상세", expanded=False):
                        st.code(str(_plotly_err), language="text")
                mermaid_src = get_agent_graph_mermaid()
                if mermaid_src:
                    with st.expander("Mermaid 소스 보기 (발표 자료·문서용 — mermaid.live에 붙여넣기)", expanded=False):
                        st.code(mermaid_src, language="text")
                        st.caption("위 텍스트를 복사해 mermaid.live / Notion / GitHub README에 붙여넣으면 다이어그램으로 렌더링됩니다.")
                st.markdown("""
**단계별 설명**

1. **START** → **Start Router**: 사전 스크리닝 여부에 따라 Intake 직행 또는 Screener로 분기
2. **Screener**: 전표 기반 케이스 유형 분류(rule/hybrid). Fast Lane + **Deep Lane** 승격 판단
3. **Intake Agent**: 전표 입력과 위험 지표 정규화
4. **Planner Agent**: 조사 계획과 tool 순서 수립
5. **Execute Agent**: 실제 LangChain tool 호출
6. **Critic Agent**: 과잉 주장과 반례 검토
7. **Verifier Agent**: 자동 판정 가능 여부 검증 → 필요 시 **HITL Pause** → **HITL Validate** 후 Reporter로 재개
8. **Reporter Agent**: 설명 문장과 최종 요약 생성
9. **Finalizer**: 상태/점수/이력 최종 확정
10. **END**: 저장/조회 가능한 결과로 종료
""")
            with graph_tabs[1]:
                st.caption("Screener 노드 내부 Deep Lane 서브그래프입니다. 승격 조건 충족 시에만 실행되며, 실패 또는 타임아웃 시 Fast 결과로 폴백합니다.")
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
                        "딥 레인 스크리닝 서브그래프",
                        draw_deep_screening_graph_langgraph(), None,
                        "Deep Lane: 4단계 LLM 재검증 서브그래프.",
                    )
                    with st.expander("⚠️ Plotly 오류", expanded=False):
                        st.code(str(_e2), language="text")
                deep_mermaid = get_deep_screening_graph_mermaid()
                if deep_mermaid:
                    with st.expander("Mermaid 소스 보기", expanded=False):
                        st.code(deep_mermaid, language="text")
                st.markdown("""
**Deep Lane 승격 조건** (4가지 중 하나 충족 시 Deep Lane 실행)

| 조건 | 기준 |
|------|------|
| **rule_llm_mismatch** | 규칙 판정 case_type ≠ LLM 판정 case_type |
| **llm_low_confidence** | LLM 신뢰도 < 0.70 |
| **boundary_score** | 점수 45 ≤ score ≤ 65 (경계 구간) |
| **normal_baseline_with_risk_signals** | NORMAL_BASELINE + 위험 신호 ≥ 2개 |

**폴백**: 타임아웃 또는 에러 발생 시 Fast Lane 결과 사용. `screening_meta.lane = "fast"`로 기록.
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
                        "하위 실행 도구 그래프: execute 노드 내부에서 호출되는 LangChain tool 순서입니다.",
                    )
                    with st.expander("⚠️ Plotly 오류", expanded=False):
                        st.code(str(_e3), language="text")
                st.markdown("""
**단계별 설명**

1. **execute**: 조사 계획의 실제 실행 허브
2. **holiday_compliance_probe**: 휴일/휴무/시간대 검증
3. **budget_risk_probe**: 예산 초과 및 금액 리스크 확인
4. **merchant_risk_probe**: 가맹점 업종 코드(MCC)/거래처 기반 업종 리스크 판별
5. **document_evidence_probe**: 전표 라인/문서 증거 수집
6. **policy_rulebook_probe**: 규정 조항 검색 및 연결
7. **legacy_aura_deep_audit**: 필요 시 specialist 심층 감사 호출
8. **score_breakdown**: 정량 점수와 품질 지표 집계
""")
