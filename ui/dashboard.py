"""운영 대시보드: run 진단 스냅샷 기반 KPI / 추세 / 품질 지표 / 케이스 비교."""
from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from ui.shared import stylable_container

from db.session import SessionLocal
from services.dashboard_service import (
    build_case_compare_frame,
    build_dashboard_overview,
    build_quality_signal_frame,
    build_recent_runs_frame,
    build_trend_frames,
    fetch_dashboard_snapshots,
    snapshots_to_frame,
)
from ui.shared import fmt_dt_korea, render_empty_state, render_kpi_card, render_page_header, render_panel_header


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _fmt_score(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}"

_DISPLAY_LABELS = {
    "citation_coverage": "인용 커버리지",
    "tool_success_rate": "도구 성공률",
    "tool_call_success_rate": "도구 성공률",
    "fallback_rate": "Fallback 비율",
    "fallback_usage_rate": "Fallback 비율",
    "hitl_rate": "HITL 발생률",
}


def _display_label(name: str) -> str:
    return _DISPLAY_LABELS.get(name, name)


def _metric_grid(overview: dict[str, Any]) -> None:
    row1 = st.columns(4)
    row2 = st.columns(4)
    cards = [
        ("총 진단 run", str(overview["total_runs"]), "저장된 RUN_DIAGNOSTICS_SNAPSHOT 기준"),
        ("고유 케이스", str(overview["unique_cases"]), "voucher_key / case_id 기준"),
        ("평균 인용 커버리지", _fmt_pct(overview["avg_citation_coverage"]), "문장별 citation 연결 평균"),
        ("도구 성공률", _fmt_pct(overview["avg_tool_success_rate"]), "tool_call_success_rate 평균"),
        ("HITL 발생률", _fmt_pct(overview["hitl_rate"]), "담당자 검토가 필요했던 비율"),
        ("재개 성공률", _fmt_pct(overview["resume_success_rate"]), "interrupt 이후 same-run resume 성공 비율"),
        ("fallback 사용률", _fmt_pct(overview["fallback_rate"]), "LLM note 대신 fallback note 사용 비율"),
        ("평균 점수", _fmt_score(overview["avg_score"]), "저장된 최종 score 평균"),
    ]
    for idx, (label, value, foot) in enumerate(cards[:4]):
        with row1[idx]:
            render_kpi_card(label, value, foot)
    for idx, (label, value, foot) in enumerate(cards[4:]):
        with row2[idx]:
            render_kpi_card(label, value, foot)




def _plotly_layout(title: str | None = None) -> dict[str, Any]:
    axis_base = {
        "tickfont": {"size": 11, "color": "#0f172a"},
        "color": "#0f172a",
    }
    return {
        "template": "plotly_white",
        "title": {"text": title or "", "x": 0.02, "font": {"size": 14, "color": "#0f172a"}},
        "paper_bgcolor": "#ffffff",
        "plot_bgcolor": "#ffffff",
        "margin": {"l": 52, "r": 28, "t": 44, "b": 48},
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 11, "color": "#0f172a"},
            "title": {"text": ""},
        },
        "xaxis": {"showgrid": False, "linecolor": "#cbd5e1", **axis_base},
        "yaxis": {"gridcolor": "#e2e8f0", "zeroline": False, **axis_base},
        "font": {"family": "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif", "color": "#0f172a"},
        "hoverlabel": {"bgcolor": "#0f172a", "font": {"color": "#ffffff"}},
    }


def _sanitize_trend_frame(frame: pd.DataFrame, cols: list[str], *, x_col: str = "date") -> pd.DataFrame:
    if frame.empty:
        return frame
    chart = frame.copy()
    if x_col == "date":
        chart[x_col] = pd.to_datetime(chart[x_col], errors="coerce")
        chart = chart.dropna(subset=[x_col])
        chart[x_col] = chart[x_col].dt.strftime("%Y-%m-%d")
    keep = [c for c in cols if c in chart.columns]
    if not keep:
        return pd.DataFrame(columns=[x_col])
    chart = chart[[x_col, *keep]].copy()
    for c in keep:
        chart[c] = pd.to_numeric(chart[c], errors="coerce")
    chart = chart.dropna(how="all", subset=keep)
    return chart


def _render_line_plot(frame: pd.DataFrame, cols: list[str], *, x_col: str = "date", height: int = 300, x_title: str | None = None, y_title: str | None = None, single_point_mode: str = "markers") -> None:
    chart = _sanitize_trend_frame(frame, cols, x_col=x_col)
    if chart.empty:
        render_empty_state("표시할 차트 데이터가 없습니다.")
        return
    fig = go.Figure()
    palette = ["#2563eb", "#10b981", "#f59e0b", "#ef4444"]
    single_x = chart[x_col].nunique(dropna=True) <= 1
    for idx, col in enumerate(cols):
        if col not in chart.columns:
            continue
        if chart[col].dropna().empty:
            continue
        mode = "markers" if single_x and single_point_mode == "markers" else "lines+markers"
        fig.add_trace(go.Scatter(
            x=chart[x_col],
            y=chart[col],
            mode=mode,
            name=_display_label(col),
            line={"width": 3, "color": palette[idx % len(palette)]},
            marker={"size": 9},
        ))
    if not fig.data:
        render_empty_state("표시할 차트 데이터가 없습니다.")
        return
    fig.update_layout(**_plotly_layout())
    fig.update_layout(height=height)
    fig.update_xaxes(
        title_text=x_title or ("날짜" if x_col == "date" else x_col),
        tickfont={"size": 11, "color": "#0f172a"},
        title_font={"size": 12, "color": "#0f172a"},
        type="category" if x_col == "date" else None,
        categoryorder="array" if x_col == "date" else None,
        categoryarray=chart[x_col].tolist() if x_col == "date" else None,
    )
    fig.update_yaxes(title_text=y_title or "값", tickfont={"size": 11, "color": "#0f172a"}, title_font={"size": 12, "color": "#0f172a"})
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def _render_bar_plot(frame: pd.DataFrame, x: str, y: str, *, height: int = 260, color: str = "#2563eb", x_title: str | None = None, y_title: str | None = None) -> None:
    data = frame.copy()
    if x in data.columns and x == "date":
        data[x] = pd.to_datetime(data[x], errors="coerce")
        data = data.dropna(subset=[x])
        data[x] = data[x].dt.strftime("%Y-%m-%d")
    data[y] = pd.to_numeric(data[y], errors="coerce")
    data = data.dropna(subset=[y])
    if data.empty:
        render_empty_state("표시할 차트 데이터가 없습니다.")
        return
    fig = px.bar(data, x=x, y=y)
    fig.update_traces(marker_color=color, marker_line_color="#1d4ed8", marker_line_width=0.6)
    fig.update_layout(**_plotly_layout())
    fig.update_layout(height=height, showlegend=False)
    fig.update_xaxes(
        title_text=x_title or x,
        tickfont={"size": 11, "color": "#0f172a"},
        title_font={"size": 12, "color": "#0f172a"},
        type="category" if x == "date" else None,
        categoryorder="array" if x == "date" else None,
        categoryarray=data[x].tolist() if x == "date" else None,
    )
    fig.update_yaxes(title_text=y_title or y, tickfont={"size": 11, "color": "#0f172a"}, title_font={"size": 12, "color": "#0f172a"})
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def _render_overview_tab(frame: pd.DataFrame) -> None:
    quality_trend, volume_trend = build_trend_frames(frame)
    left, right = st.columns([1.15, 0.85])

    with left:
        with stylable_container(
            key="dash_quality_trend",
            css_styles="""
            {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05); min-height: 520px;}
            """,
        ):
            render_panel_header("품질 추세", "citation coverage · tool 성공률 · fallback 비율의 일자별 평균")
            if quality_trend.empty:
                render_empty_state("표시할 품질 추세 데이터가 없습니다.")
            else:
                _render_line_plot(
                    quality_trend,
                    ["citation_coverage", "tool_success_rate", "fallback_rate"],
                    height=340,
                    x_title="일자",
                    y_title="비율",
                )
                st.caption("높을수록 좋은 지표: citation coverage, tool 성공률 / 낮을수록 좋은 지표: fallback 비율")

    with right:
        with stylable_container(
            key="dash_volume_trend",
            css_styles="""
            {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05); min-height: 520px; display:flex; flex-direction:column;}
            """,
        ):
            render_panel_header("활동량 추세", "일자별 run 수와 HITL 발생률")
            if volume_trend.empty:
                render_empty_state("표시할 활동량 추세 데이터가 없습니다.")
            else:
                st.markdown("<div class='mt-caption-strong'>일별 분석 실행 수</div>", unsafe_allow_html=True)
                _render_bar_plot(volume_trend, "date", "run_count", height=210, color="#6366f1", x_title="일자", y_title="실행 수")
                st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
                st.markdown("<div class='mt-caption-strong'>일별 HITL 발생률</div>", unsafe_allow_html=True)
                _render_line_plot(volume_trend, ["hitl_rate"], height=170, x_title="일자", y_title="비율")
                st.caption("활동량과 HITL은 서로 다른 축 의미를 가지므로 차트를 분리해 해석성을 높였습니다.")

    with stylable_container(
        key="dash_recent_runs",
        css_styles="""
        {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05); margin-top: 14px;}
        """,
    ):
        render_panel_header("최근 진단 스냅샷", "가장 최근 run들의 핵심 지표를 한 번에 확인합니다.")
        recent = build_recent_runs_frame(frame)
        if recent.empty:
            render_empty_state("최근 run 스냅샷이 없습니다.")
        else:
            present = recent.copy()
            present["occurred_at"] = present["occurred_at"].apply(fmt_dt_korea)
            present["citation_coverage"] = present["citation_coverage"].apply(_fmt_pct)
            present["tool_call_success_rate"] = present["tool_call_success_rate"].apply(_fmt_pct)
            present["fallback_usage_rate"] = present["fallback_usage_rate"].apply(_fmt_pct)
            present["hitl_requested"] = present["hitl_requested"].map({1: "예", 0: "아니오"})
            present["resume_success_raw"] = present["resume_success_raw"].map({True: "성공", False: "실패", None: "-"})
            present = present.rename(columns={
                "occurred_at": "발생 시각",
                "run_id": "실행 ID",
                "severity": "심각도",
                "score": "점수",
                "citation_coverage": "인용 커버리지",
                "tool_call_success_rate": "도구 성공률",
                "hitl_requested": "HITL 요청",
                "resume_success_raw": "재개 성공",
                "fallback_usage_rate": "Fallback 비율",
                "event_count": "이벤트 수",
                "quality_signals": "품질 신호",
                "source_mode": "집계 방식",
            })
            st.dataframe(present, use_container_width=True, hide_index=True, height=400)


def _render_quality_tab(frame: pd.DataFrame) -> None:
    signal_df = build_quality_signal_frame(frame)
    left, right = st.columns([1.1, 0.9])

    with left:
        with stylable_container(
            key="dash_quality_signals",
            css_styles="""
            {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05);}
            """,
        ):
            render_panel_header("품질 지표 분포", "analysis_quality_signals의 누적 발생 횟수")
            if signal_df.empty:
                render_empty_state("집계할 품질 지표가 없습니다.")
                st.caption("분석 run이 1건 있어도 verification/quality signal이 생성되지 않았거나, 실패 run만 존재하면 이 영역은 비어 있을 수 있습니다.")
            else:
                _render_bar_plot(signal_df, "signal", "count", height=360, color="#8b5cf6", x_title="품질 신호", y_title="발생 횟수")
                signal_present = signal_df.rename(columns={"signal": "품질 신호", "count": "건수"})
                st.dataframe(signal_present, use_container_width=True, hide_index=True, height=260)

    with right:
        with stylable_container(
            key="dash_severity_mix",
            css_styles="""
            {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05);}
            """,
        ):
            render_panel_header("심각도 믹스", "최근 진단에서 어떤 심각도가 많이 나왔는지 확인합니다.")
            if frame.empty or frame["severity"].dropna().empty:
                render_empty_state("심각도 데이터가 없습니다.")
                st.caption("최종 결과 severity가 저장되지 않았고, SCREENING_RESULT severity도 복원되지 않은 run만 있으면 비어 있을 수 있습니다.")
            else:
                sev = frame["severity"].fillna("UNKNOWN").value_counts().rename_axis("severity").reset_index(name="count")
                _render_bar_plot(sev, "severity", "count", height=220, color="#14b8a6", x_title="심각도", y_title="건수")
                sev_present = sev.rename(columns={"severity": "심각도", "count": "건수"})
                st.dataframe(sev_present, use_container_width=True, hide_index=True, height=220)

        with stylable_container(
            key="dash_lineage_mix",
            css_styles="""
            {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05); margin-top: 14px;}
            """,
        ):
            render_panel_header("실행 계보", "resume / root run 관계와 이벤트 수를 함께 봅니다.")
            if frame.empty:
                render_empty_state("실행 계보 데이터가 없습니다.")
            else:
                lineage = (
                    frame[["lineage_mode", "event_count"]]
                    .fillna({"lineage_mode": "ROOT"})
                    .groupby("lineage_mode", dropna=False)
                    .agg(run_count=("event_count", "count"), avg_events=("event_count", "mean"))
                    .reset_index()
                )
                lineage_present = lineage.rename(columns={"lineage_mode": "실행 계보", "run_count": "run 수", "avg_events": "평균 이벤트 수"})
                st.dataframe(lineage_present, use_container_width=True, hide_index=True, height=220)


def _render_compare_tab(frame: pd.DataFrame) -> None:
    with stylable_container(
        key="dash_compare_header",
        css_styles="""
        {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05);}
        """,
    ):
        render_panel_header("케이스별 run 비교", "같은 voucher_key에 대한 여러 run의 진단 품질을 나란히 비교합니다.")
        if frame.empty or frame["voucher_key"].dropna().empty:
            render_empty_state("비교할 케이스 이력이 없습니다.")
            return
        voucher_options = sorted([v for v in frame["voucher_key"].dropna().unique().tolist() if v])
        selected_voucher = st.selectbox("비교할 전표 선택", voucher_options, key="dash_compare_voucher")

    compare_df = build_case_compare_frame(frame, selected_voucher)
    if compare_df.empty:
        render_empty_state("선택한 전표의 비교 가능한 run이 없습니다.")
        return

    top_left, top_right = st.columns([1, 1])
    with top_left:
        with stylable_container(
            key="dash_compare_table",
            css_styles="""
            {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05);}
            """,
        ):
            render_panel_header("run 비교표", "심각도·점수·인용커버리지·tool 성공률을 비교합니다.")
            present = compare_df.copy()
            present["occurred_at"] = present["occurred_at"].apply(fmt_dt_korea)
            present["citation_coverage"] = present["citation_coverage"].apply(_fmt_pct)
            present["tool_call_success_rate"] = present["tool_call_success_rate"].apply(_fmt_pct)
            present["fallback_usage_rate"] = present["fallback_usage_rate"].apply(_fmt_pct)
            present["hitl_requested"] = present["hitl_requested"].map({1: "예", 0: "아니오"})
            present["resume_success_raw"] = present["resume_success_raw"].map({True: "성공", False: "실패", None: "-"})
            present = present.rename(columns={
                "occurred_at": "발생 시각",
                "run_id": "실행 ID",
                "severity": "심각도",
                "score": "점수",
                "citation_coverage": "인용 커버리지",
                "tool_call_success_rate": "도구 성공률",
                "hitl_requested": "HITL 요청",
                "resume_success_raw": "재개 성공",
                "fallback_usage_rate": "Fallback 비율",
                "event_count": "이벤트 수",
                "quality_signals": "품질 신호",
                "source_mode": "집계 방식",
            })
            st.dataframe(present, use_container_width=True, hide_index=True, height=400)

    with top_right:
        with stylable_container(
            key="dash_compare_trend",
            css_styles="""
            {padding:18px 20px; border-radius:18px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 8px 24px rgba(15,23,42,0.05);}
            """,
        ):
            render_panel_header("선택 케이스 추이", "run 순서에 따른 핵심 지표 변화를 봅니다.")
            chart_df = compare_df.copy().sort_values("occurred_at")
            chart_df["run_seq"] = range(1, len(chart_df) + 1)
            _render_line_plot(
                chart_df,
                ["citation_coverage", "tool_call_success_rate", "fallback_usage_rate"],
                x_col="run_seq",
                height=320,
                x_title="실행 순번",
                y_title="비율",
            )
            st.caption("같은 전표를 반복 실행했을 때 인용 커버리지·도구 성공률·fallback 비율이 어떻게 달라졌는지 확인합니다.")


def render_dashboard_page() -> None:
    render_page_header(
        "운영 대시보드",
        "실행된 에이전트 run의 진단 스냅샷을 기반으로 품질·관찰성·반복 실행 품질을 한 화면에서 확인합니다.",
    )

    filters = st.columns([0.22, 0.18, 0.60])
    with filters[0]:
        days = st.selectbox("조회 기간", [7, 14, 30, 90], index=2, key="dashboard_days")
    with filters[1]:
        limit = st.selectbox("최대 run 수", [100, 200, 400, 800], index=2, key="dashboard_limit")
    with filters[2]:
        st.caption("RUN_DIAGNOSTICS_SNAPSHOT + case_analysis_result를 결합해 운영 지표를 생성합니다.")

    db = SessionLocal()
    try:
        snapshots = fetch_dashboard_snapshots(db, days=int(days), limit=int(limit))
    finally:
        db.close()

    frame = snapshots_to_frame(snapshots)
    overview = build_dashboard_overview(frame)

    if frame.empty:
        render_empty_state("선택한 기간에 적재된 운영 스냅샷이 없습니다. AI 워크스페이스에서 분석을 실행한 뒤 다시 확인하세요.")
        return

    if overview.get("source_mode") == "derived":
        st.warning("진단 스냅샷이 아직 적재되지 않아, 현재 대시보드는 AGENT_EVENT·RUN_COMPLETED·HITL 로그를 기반으로 복원 집계한 값입니다.")

    _metric_grid(overview)
    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    tabs = st.tabs(["개요", "품질 지표", "케이스 비교"])
    with tabs[0]:
        _render_overview_tab(frame)
    with tabs[1]:
        _render_quality_tab(frame)
    with tabs[2]:
        _render_compare_tab(frame)
