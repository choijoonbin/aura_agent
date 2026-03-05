from __future__ import annotations

import html
import re
import streamlit as st
from ui.shared import (
    stylable_container,
    render_empty_state,
    render_page_header,
    render_panel_header,
    render_rag_kpi_card,
    render_rag_meta_grid,
    render_rag_quality_report,
)

from services.rag_chunk_lab_service import (
    list_rulebook_files,
    load_rulebook_text,
    preview_chunks,
    save_uploaded_rulebook,
)
from ui.api_client import get, post


def _quality_score(chunks: list[dict], strategy: str) -> int:
    """
    전략별 품질 점수. parent_child는 ROOT(parent)만 short 판정, 그 외는 전체 기준.
    short 200자 미만 1개당 -3점.
    """
    if not chunks:
        return 0
    if strategy == "parent_child":
        parents = [c for c in chunks if c.get("chunk_type") == "parent"]
        if not parents:
            return 0
        short_count = sum(1 for c in parents if c["length"] < 200)
    else:
        short_count = sum(1 for c in chunks if c["length"] < 200)
    return max(0, 100 - short_count * 3)


def _strip_html_for_display(text: str) -> str:
    """표시용으로 HTML 조각 제거 (</div>, </span> 등이 본문에 섞였을 때)."""
    if not text:
        return text
    text = re.sub(r"</?div[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?span[^>]*>", "", text, flags=re.IGNORECASE)
    return text.strip()


def render_rag_library_page() -> None:
    # ── 페이지 헤더 ────────────────────────────────────────────────────────
    render_page_header(
        "규정문서 라이브러리",
        "규정 문서를 등록하고, 청킹 방식을 실험할 수 있습니다. "
        "AI 감사 분석 시 여기 등록된 규정이 근거 검색에 사용됩니다.",
    )
    st.markdown('<div style="height:24px"></div>', unsafe_allow_html=True)

    # ── 데이터 로딩 ────────────────────────────────────────────────────────
    data = get("/api/v1/rag/documents")
    items = data.get("items") or []
    total = data.get("total") or len(items)
    indexed = len([i for i in items if str(i.get("status") or "").upper() == "COMPLETED"])
    attention = len([i for i in items
                     if str(i.get("status") or "").upper()
                     in {"PROCESSING", "FAILED", "VECTORIZING"}])
    passed = [i for i in items
              if i.get("quality_gate_passed") is True or i.get("quality_report_passed") is True]
    pass_rate = (len(passed) / total * 100) if total else 0

    # ── KPI 카드 — 상태별 색상 코딩 ────────────────────────────────────────
    cols = st.columns(4)
    with cols[0]:
        render_rag_kpi_card(
            "전체 문서", str(total), "등록된 규정/RAG 문서",
            status="normal", icon="📄",
        )
    with cols[1]:
        render_rag_kpi_card(
            "인덱싱 완료", str(indexed), "검색 인용 준비 완료",
            status="success" if indexed == total and total > 0 else "warning",
            icon="✅",
        )
    with cols[2]:
        render_rag_kpi_card(
            "주의 필요", str(attention), "인덱싱 오류 또는 처리 중",
            status="error" if attention > 0 else "normal",
            icon="⚠️" if attention > 0 else "🟢",
        )
    with cols[3]:
        render_rag_kpi_card(
            "청킹 합격률", f"{pass_rate:.1f}%", "quality_report 기준",
            status="success" if pass_rate >= 90 else ("warning" if pass_rate >= 60 else "error"),
            icon="📊",
        )

    st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)

    # ── 3개 탭 ─────────────────────────────────────────────────────────────
    top_tabs = st.tabs(["📁 DB 라이브러리", "🧪 청킹 실험실", "🔍 Run 인용 조회"])

    # ════════════════════════════════════════════════════════════════════════
    # TAB 1: DB 라이브러리
    # ════════════════════════════════════════════════════════════════════════
    with top_tabs[0]:
        left, right = st.columns([0.38, 0.62])
        selected_doc_id = st.session_state.get("mt_selected_doc_id") or (
            items[0]["doc_id"] if items else None
        )

        # 좌측: 문서 목록
        with left:
            render_panel_header("문서 목록", "저장된 규정/RAG 문서를 상태와 함께 조회합니다.")
            st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)

            if not items:
                render_empty_state("등록된 규정 문서가 없습니다.")
            else:
                for item in items:
                    doc_id = item.get("doc_id")
                    is_selected = int(doc_id) == int(selected_doc_id) if selected_doc_id else False
                    doc_status = str(item.get("status") or "").upper()
                    status_icon = {"COMPLETED": "✅", "PROCESSING": "⏳", "FAILED": "❌"}.get(
                        doc_status, "📄"
                    )
                    q_passed = item.get("quality_gate_passed") or item.get("quality_report_passed")
                    quality_tag = "  🏆" if q_passed else ""
                    label = f"{status_icon}  {item.get('title')}  ·  doc_id={doc_id}{quality_tag}"
                    if st.button(
                        label,
                        key=f"doc_{doc_id}",
                        use_container_width=True,
                        type="primary" if is_selected else "secondary",
                    ):
                        st.session_state["mt_selected_doc_id"] = doc_id
                        st.rerun()

        # 우측: 문서 상세
        with right:
            if selected_doc_id is None:
                render_empty_state("좌측에서 문서를 선택하세요.")
            else:
                detail = get(f"/api/v1/rag/documents/{selected_doc_id}")
                doc_title = detail.get("title") or f"문서 {selected_doc_id}"
                doc_type = str(detail.get("doc_type") or "-").upper()
                doc_source = str(detail.get("source_type") or "-").upper()
                chunk_count = len(detail.get("chunks") or [])

                # 문서 Hero 카드
                with stylable_container(
                    key=f"rag_doc_hero_{selected_doc_id}",
                    css_styles="""{
                        padding: 20px 24px;
                        border-radius: 18px;
                        border: 1px solid #dbeafe;
                        background: linear-gradient(135deg, #eff6ff 0%, #ffffff 70%);
                        box-shadow: 0 10px 24px rgba(15,23,42,0.06);
                        margin-bottom: 14px;
                    }""",
                ):
                    hero_cols = st.columns([0.7, 0.3])
                    with hero_cols[0]:
                        st.markdown(
                            f'<div class="mt-hero-title">📑 {doc_title}</div>',
                            unsafe_allow_html=True,
                        )
                        st.markdown(
                            f"""<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px">
                              <span class="mt-meta-pill">🔖 TYPE: {doc_type}</span>
                              <span class="mt-meta-pill">📂 SOURCE: {doc_source}</span>
                              <span class="mt-meta-pill">🧩 청크: {chunk_count}개</span>
                            </div>""",
                            unsafe_allow_html=True,
                        )
                    with hero_cols[1]:
                        q_passed = detail.get("quality_gate_passed") or detail.get("quality_report_passed")
                        gate_color = "#059669" if q_passed else "#dc2626"
                        gate_text = "품질 게이트 통과" if q_passed else "품질 게이트 미통과"
                        gate_icon = "✅" if q_passed else "❌"
                        st.markdown(
                            f"""<div style="text-align:right;padding-top:6px">
                              <div style="font-size:1.6rem">{gate_icon}</div>
                              <div style="font-size:0.78rem;font-weight:700;color:{gate_color};margin-top:4px">
                                {gate_text}
                              </div>
                            </div>""",
                            unsafe_allow_html=True,
                        )

                # 상세 탭
                detail_tabs = st.tabs(["📋 문서 메타", "📊 품질 리포트", "🧩 청크 목록"])

                # -- 탭 1: 문서 메타 (Key-Value 그리드)
                with detail_tabs[0]:
                    meta = {
                        k: detail.get(k)
                        for k in [
                            "status", "version", "effective_from", "effective_to",
                            "lifecycle_status", "active_from", "active_to",
                            "quality_gate_passed", "updated_at",
                        ]
                    }
                    render_rag_meta_grid(meta)

                # -- 탭 2: 품질 리포트 (게이지 바)
                with detail_tabs[1]:
                    report = {
                        k: detail.get(k)
                        for k in [
                            "quality_report_passed", "quality_run_id",
                            "input_chunks", "final_chunks",
                            "article_coverage", "noise_rate",
                            "duplicate_rate", "short_chunk_rate", "missing_required", "errors",
                        ]
                    }
                    render_rag_quality_report(report)

                    # 오류 목록
                    errors = detail.get("errors") or []
                    if errors:
                        with st.expander(f"⚠ 오류 상세 ({len(errors)}건)", expanded=False):
                            for err in errors:
                                st.markdown(
                                    f'<div style="color:#dc2626;font-size:0.82rem;'
                                    f'padding:4px 0;border-bottom:1px solid #fee2e2">• {err}</div>',
                                    unsafe_allow_html=True,
                                )

                # -- 탭 3: 청크 목록 (인라인 미리보기 카드) — rag_chunk 테이블 기준
                with detail_tabs[2]:
                    chunks = detail.get("chunks") or []
                    total_count = detail.get("total_chunk_count")
                    if total_count is None:
                        total_count = len(chunks)
                    avg_len = detail.get("avg_chunk_length")
                    if avg_len is None or (chunks and avg_len == 0):
                        avg_len = sum(len(str(c.get("chunk_text") or "")) for c in chunks) / len(chunks) if chunks else 0
                    if not chunks:
                        render_empty_state("저장된 청크가 없습니다.")
                    else:
                        # 청크 통계: DB(rag_chunk) 실제 건수·평균
                        display_note = f" (표시 {len(chunks)}건)" if total_count and total_count > len(chunks) else ""
                        st.markdown(
                            f"""<div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap">
                              <span class="mt-meta-pill">🧩 총 {total_count}개{display_note}</span>
                              <span class="mt-meta-pill">📏 평균 {avg_len:.0f}자</span>
                            </div>
                            <div style="font-size:0.75rem;color:#64748b;margin-top:-6px;margin-bottom:8px">DB(rag_chunk) 저장 청크 기준</div>""",
                            unsafe_allow_html=True,
                        )
                        for c_idx, chunk in enumerate(chunks[:50], start=1):
                            article = chunk.get("regulation_article") or "—"
                            parent_title = chunk.get("parent_title") or "제목 없음"
                            chunk_text = str(chunk.get("chunk_text") or "")
                            chunk_len = len(chunk_text)

                            # 청크 길이 품질 색상
                            if chunk_len < 100:
                                q_color, q_label = "#dc2626", "초단편"
                            elif chunk_len < 200:
                                q_color, q_label = "#d97706", "단편"
                            elif chunk_len > 800:
                                q_color, q_label = "#7c3aed", "장문"
                            else:
                                q_color, q_label = "#059669", "적정"

                            with st.expander(
                                f"{article}  ·  {parent_title[:28]}  ·  {chunk_len}자",
                                expanded=False,
                            ):
                                st.markdown(
                                    f"""<div style="display:flex;align-items:center;
                                                    gap:8px;margin-bottom:8px">
                                      <span style="background:{q_color};color:#fff;
                                                   padding:1px 6px;border-radius:999px;
                                                   font-size:0.68rem;font-weight:700">
                                        {q_label} {chunk_len}자
                                      </span>
                                      <span style="background:#475569;color:#fff;
                                                   padding:1px 6px;border-radius:999px;
                                                   font-size:0.68rem;font-weight:700">
                                        page={chunk.get('page_no') or '—'} / index={chunk.get('chunk_index') or '—'}
                                      </span>
                                    </div>""",
                                    unsafe_allow_html=True,
                                )
                                with stylable_container(
                                    key=f"rag_doc_chunk_body_{selected_doc_id}_{chunk.get('chunk_id', c_idx)}",
                                    css_styles="""{
                                        background:#0f172a;
                                        color:#ffffff;
                                        border-radius:10px;
                                        padding:12px 14px;
                                        border:1px solid #334155;
                                        font-size:0.85rem;
                                        line-height:1.65;
                                    }""",
                                ):
                                    st.markdown(
                                        f'<div style="color:#ffffff;white-space:pre-wrap;">{html.escape(_strip_html_for_display(chunk_text))}</div>',
                                        unsafe_allow_html=True,
                                    )

    # ════════════════════════════════════════════════════════════════════════
    # TAB 2: 청킹 실험실
    # ════════════════════════════════════════════════════════════════════════
    with top_tabs[1]:
        render_panel_header(
            "🔬 청킹 실험실",
            "규정 텍스트에 전략을 바꿔 적용하며 청킹 결과를 비교합니다. "
            "전략 선택 → 지표 확인 → 미리보기 → DB 재청킹 순으로 진행하세요.",
        )

        local_files = list_rulebook_files()

        # Row 1: 컨트롤 3개 한 줄 (대상 문서 / 전략 / 업로드)
        ctrl1, ctrl2, ctrl3 = st.columns([0.35, 0.35, 0.30])

        with ctrl1:
            selected_file = st.selectbox(
                "대상 문서",
                options=local_files,
                format_func=lambda x: f"{x['name']} ({x['source']})",
                index=0 if local_files else 0,
                key="lab_sel_file",
            )

        with ctrl2:
            strategy = st.selectbox(
                "청킹 전략",
                options=["parent_child", "hybrid_policy", "article_first", "sliding_window"],
                format_func=lambda x: {
                    "parent_child":   "🏆 Parent-Child 계층형 (권장)",
                    "hybrid_policy":  "하이브리드 정책형",
                    "article_first":  "조항 우선",
                    "sliding_window": "슬라이딩 윈도우",
                }[x],
                key="lab_strategy",
            )

        with ctrl3:
            upload = st.file_uploader("규정 TXT 업로드", type=["txt"], key="lab_upload",
                                       label_visibility="visible")
            if upload is not None:
                save_uploaded_rulebook(upload.name, upload.getvalue())
                st.success(f"업로드: {upload.name}")
                st.rerun()

        if not local_files:
            render_empty_state("업로드된 규정 파일이 없습니다.")
            return

        text = load_rulebook_text(selected_file["path"])
        chunks = preview_chunks(text, strategy)

        # parent_child일 때는 parent(ARTICLE)만 별도 리스트로 사용
        parent_chunks = [c for c in chunks if c.get("chunk_type") == "parent"] if strategy == "parent_child" else chunks

        # Row 2: 통계 지표 4개 한 줄
        s1, s2, s3, s4 = st.columns(4)

        total_chunks = len(chunks)
        avg_len = int(sum(c["length"] for c in chunks) / total_chunks) if total_chunks else 0

        # 초단편: parent_child면 parent만, 나머지는 전체 기준
        check_chunks = parent_chunks if strategy == "parent_child" else chunks
        short_count = sum(1 for c in check_chunks if c["length"] < 200)
        short_label = f"초단편 ({'ROOT' if strategy == 'parent_child' else '전체'})"

        s1.metric("원문 길이", f"{len(text):,}자")
        s2.metric(
            "총 청크 수",
            str(total_chunks),
            delta=(
                f"Parent {len(parent_chunks)} + Leaf {total_chunks - len(parent_chunks)}"
                if strategy == "parent_child"
                else None
            ),
        )
        s3.metric("평균 길이", f"{avg_len}자")

        # 초단편 수 — 양호/개선필요 배지 (툴팁 포함)
        is_good = short_count <= 5
        badge_text = "양호" if is_good else "개선필요"
        badge_color = "#059669" if is_good else "#dc2626"
        badge_bg = "#ecfdf5" if is_good else "#fef2f2"

        if is_good:
            badge_title = (
                f"초단편 {short_count}개 (기준: 5개 이하) — 청킹 품질이 양호합니다. "
                f"200자 미만 ARTICLE이 적어 AI 검색 정확도에 유리합니다."
            )
        else:
            badge_title = (
                f"초단편 {short_count}개 (기준: 5개 이하 권장) — 재청킹을 권장합니다. "
                f"200자 미만의 짧은 ARTICLE 청크가 많으면 AI가 관련 조항을 검색할 때 "
                f"문맥이 부족해 정확도가 떨어질 수 있습니다. "
                f"'DB 재청킹 실행' 버튼을 눌러 현재 전략으로 재적재하세요."
            )

        with s4:
            st.metric(short_label, str(short_count))
            badge_title_esc = (badge_title or "").replace("'", "&#39;")
            st.markdown(
                f"<span title='{badge_title_esc}' style='cursor:help;"
                f"background:{badge_bg};color:{badge_color};"
                f"font-size:0.75rem;font-weight:700;padding:3px 10px;"
                f"border-radius:20px;border:1px solid {badge_color}33;'>"
                f"{'✅ ' if is_good else '⚠️ '}{badge_text}</span>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

        st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)

        # ── 전략 비교 카드 ───────────────────────────────────────────────
        st.markdown(
            '<div class="mt-panel-title" style="margin-bottom:8px">전략 비교</div>',
            unsafe_allow_html=True,
        )

        STRATEGY_META = {
            "parent_child":    {"label": "🆕 Parent-Child 계층형", "recommend": True,
                                "desc": "Dense 검색 최적"},
            "hybrid_policy":   {"label": "하이브리드 정책형", "recommend": False,
                                "desc": "조항 경계 유지 + 900자 초과 시만 내부 분할"},
            "article_first":   {"label": "조항 우선", "recommend": False,
                                "desc": "단순 경계 분리"},
            "sliding_window":  {"label": "슬라이딩 윈도우", "recommend": False,
                                "desc": "경계 무시"},
        }

        compare_cols = st.columns(4)
        for col, (s_key, s_meta) in zip(compare_cols, STRATEGY_META.items()):
            rows = preview_chunks(text, s_key)
            s_avg = sum(r["length"] for r in rows) / len(rows) if rows else 0
            quality_score = _quality_score(rows, s_key)
            bar_color = "#2563eb" if s_key == strategy else "#94a3b8"
            recommend_badge = (
                '<span style="background:#2563eb;color:#fff;padding:1px 7px;'
                'border-radius:999px;font-size:0.68rem;font-weight:700;'
                'margin-left:6px">추천</span>'
                if s_meta["recommend"] else ""
            )
            with col:
                with stylable_container(
                    key=f"rag_compare_{s_key}",
                    css_styles=f"""{{
                        padding: 16px 18px;
                        border-radius: 16px;
                        border: {'2px solid #2563eb' if s_key == strategy else '1px solid #e5e7eb'};
                        background: {'#eff6ff' if s_key == strategy else '#fff'};
                        box-shadow: 0 8px 22px rgba(15,23,42,0.04);
                        min-height: 170px;
                    }}""",
                ):
                    st.markdown(
                        f'<div style="font-size:0.78rem;font-weight:700;color:#64748b">'
                        f'{s_meta["label"]}{recommend_badge}</div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div style="font-size:1.6rem;font-weight:800;color:#0f172a;'
                        f'margin:6px 0 2px 0">{len(rows)}</div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div style="font-size:0.78rem;color:#64748b">평균 {s_avg:.0f}자</div>',
                        unsafe_allow_html=True,
                    )
                    # 품질 점수 (색상: 70+ 초록, 40+ 주황, 미만 빨강)
                    score_color = "#059669" if quality_score >= 70 else "#d97706" if quality_score >= 40 else "#dc2626"
                    st.markdown(
                        f"""<div style="margin-top:10px">
                          <div style="font-size:0.72rem;color:#94a3b8;margin-bottom:3px">품질점수</div>
                          <div style="font-size:1.25rem;font-weight:700;color:{score_color}">{quality_score}점</div>
                          <div style="background:#f1f5f9;border-radius:999px;height:5px;margin-top:4px">
                            <div style="width:{min(100, quality_score)}%;background:{score_color};
                                        height:100%;border-radius:999px"></div>
                          </div>
                        </div>""",
                        unsafe_allow_html=True,
                    )
                    st.caption(s_meta["desc"])

        st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)

        # ── 청크 미리보기 ────────────────────────────────────────────────
        st.markdown(
            '<div class="mt-panel-title" style="margin-bottom:4px">청크 미리보기</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            """<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;font-size:0.8rem;color:#64748b">
              <span style="background:#dc2626;color:#fff;padding:2px 8px;border-radius:999px;font-size:0.68rem;font-weight:700">초단편</span>
              <span>100자 미만</span>
              <span style="color:#cbd5e1">·</span>
              <span style="background:#059669;color:#fff;padding:2px 8px;border-radius:999px;font-size:0.68rem;font-weight:700">적정</span>
              <span>200자 이상</span>
              <span style="color:#cbd5e1">·</span>
              <span>(100~199자: 배지 없음)</span>
            </div>""",
            unsafe_allow_html=True,
        )

        for idx, chunk in enumerate(chunks[:20], start=1):
            c_type = chunk.get("chunk_type", "leaf")
            type_icon = "📦" if c_type == "parent" else "  └ 🔹"
            type_color = "#1d4ed8" if c_type == "parent" else "#64748b"
            chunk_len = chunk["length"]
            len_tag = (
                f'<span style="background:#dc2626;color:#fff;padding:1px 6px;'
                f'border-radius:999px;font-size:0.68rem;font-weight:700">초단편</span>'
                if chunk_len < 100 else
                f'<span style="background:#059669;color:#fff;padding:1px 6px;'
                f'border-radius:999px;font-size:0.68rem;font-weight:700">적정</span>'
                if chunk_len >= 200 else ""
            )
            type_label = "ARTICLE (Parent)" if c_type == "parent" else "CLAUSE (Child)" if c_type in ("leaf", "child") else c_type.upper()
            with st.expander(
                f"{type_icon}  {idx}. {chunk['title'][:40]}  ·  {chunk_len}자",
                expanded=(idx == 1 and c_type == "parent"),
            ):
                st.markdown(
                    f"""<span style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
                      <span style="font-size:0.78rem;font-weight:700;color:{type_color}">
                        {type_label}
                      </span>
                      {len_tag}
                    </span>""",
                    unsafe_allow_html=True,
                )
                with stylable_container(
                    key=f"rag_chunk_preview_body_{idx}",
                    css_styles="""{
                        background:#f8fafc;
                        border-radius:10px;
                        padding:12px 14px;
                        border:1px solid #e2e8f0;
                        font-size:0.85rem;
                        line-height:1.65;
                    }""",
                ):
                    st.text(_strip_html_for_display(chunk["content"]))

        # ── DB 재청킹 ────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown(
            '<div class="mt-panel-title" style="margin-bottom:6px">DB 재청킹</div>',
            unsafe_allow_html=True,
        )
        st.caption("현재 선택한 규정집 원문으로 지정한 문서(doc_id)에 계층 청킹·임베딩·search_tsv를 적용합니다.")
        if items:
            doc_options = [int(d["doc_id"]) for d in items]
            doc_labels = {int(d["doc_id"]): f"doc_id={d['doc_id']} · {d.get('title') or '-'}" for d in items}
            rechunk_doc_id = st.selectbox(
                "대상 문서 (doc_id)",
                options=doc_options,
                format_func=lambda x: doc_labels.get(x, str(x)),
                key="rag_rechunk_doc_id",
            )
            if st.button("현재 규정집으로 재청킹 실행", type="primary", key="rag_rechunk_btn"):
                with st.spinner("재청킹 중… (임베딩 생성 시 시간이 걸릴 수 있습니다)"):
                    try:
                        out = post(
                            f"/api/v1/rag/documents/{rechunk_doc_id}/rechunk",
                            json_body={"raw_text": text},
                            timeout=600,
                        )
                        msg = (
                            f"완료: 청크 {out.get('total_chunks', 0)}개 "
                            f"(ARTICLE {out.get('article_chunks', 0)}, CLAUSE {out.get('clause_chunks', 0)}), "
                            f"임베딩 저장={'예' if out.get('embedding_saved') else '아니오'}"
                        )
                        st.success(msg)
                        if not out.get("embedding_saved") and out.get("embedding_skip_reason"):
                            st.caption(f"임베딩 생략 사유: {out['embedding_skip_reason']}")
                    except Exception as e:
                        err = str(e)
                        try:
                            resp = getattr(e, "response", None)
                            if resp is not None:
                                body = resp.json() if resp.content else {}
                                err = body.get("detail", resp.text or err)
                        except Exception:
                            pass
                        st.error(f"재청킹 실패: {err}")
        else:
            st.info("DB에 등록된 문서가 없으면 재청킹할 대상을 선택할 수 없습니다. 먼저 문서를 등록하세요.")

    # ════════════════════════════════════════════════════════════════════════
    # TAB 3: Run 인용 조회
    # ════════════════════════════════════════════════════════════════════════
    with top_tabs[2]:
        render_panel_header(
            "Run 인용 조회",
            "분석 run_id로 해당 run에서 사용된 retrieval 후보·채택 인용을 확인합니다.",
        )

        run_id_input = st.text_input(
            "run_id",
            placeholder="UUID (AI 워크스페이스 결과 탭에서 확인)",
            key="rag_run_id_input",
        )

        if run_id_input and st.button("🔍 인용 현황 조회", key="rag_fetch_run_citations"):
            try:
                ev = get(f"/api/v1/analysis-runs/{run_id_input.strip()}/events")
                result = ev.get("result") or {}
                res_body = result.get("result") if isinstance(result.get("result"), dict) else result
                snapshot = (res_body or {}).get("retrieval_snapshot")

                if not snapshot:
                    st.info(
                        "💡 이 run에는 retrieval_snapshot이 없습니다. "
                        "(이전 버전 run이거나 policy_rulebook_probe 미호출)"
                    )
                else:
                    candidates = snapshot.get("candidates_after_rerank") or []
                    adopted = snapshot.get("adopted_citations") or []

                    # 요약 지표
                    m1, m2, m3 = st.columns(3)
                    m1.metric("후보 청크 수", len(candidates))
                    m2.metric("채택 인용 수", len(adopted))
                    adopt_rate = len(adopted) / len(candidates) * 100 if candidates else 0
                    m3.metric("채택률", f"{adopt_rate:.1f}%")

                    st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)

                    # 채택 인용 카드
                    if adopted:
                        st.markdown(
                            '<div class="mt-panel-title" style="margin-bottom:10px">'
                            '✅ 채택 인용</div>',
                            unsafe_allow_html=True,
                        )
                        for i, c in enumerate(adopted[:20], 1):
                            reason = c.get("adoption_reason") or "규정 근거로 채택"
                            st.markdown(
                                f"""<div style="padding:12px 14px;margin-bottom:8px;
                                            border-radius:12px;background:#ecfdf5;
                                            border:1px solid #a7f3d0">
                                  <div style="display:flex;justify-content:space-between;
                                              align-items:center">
                                    <div style="font-size:0.88rem;font-weight:700;color:#065f46">
                                      {i}. {c.get('article') or '—'}
                                      &nbsp;/&nbsp;
                                      {c.get('title') or '—'}
                                    </div>
                                    <span style="background:#059669;color:#fff;
                                                 padding:2px 8px;border-radius:999px;
                                                 font-size:0.68rem;font-weight:700">채택</span>
                                  </div>
                                  <div style="font-size:0.78rem;color:#047857;margin-top:4px">
                                    💬 {reason}
                                  </div>
                                </div>""",
                                unsafe_allow_html=True,
                            )

                    # 후보 목록
                    if candidates:
                        st.markdown(
                            '<div class="mt-panel-title" style="margin:14px 0 10px 0">'
                            '📋 후보 목록 (after rerank)</div>',
                            unsafe_allow_html=True,
                        )
                        adopted_articles = {c.get("article") for c in adopted}
                        for i, g in enumerate(candidates[:15], 1):
                            article = g.get("article") or "—"
                            is_adopted = article in adopted_articles
                            bg = "#ecfdf5" if is_adopted else "#f8fafc"
                            border = "#a7f3d0" if is_adopted else "#e2e8f0"
                            tag = (
                                '<span style="background:#059669;color:#fff;padding:1px 6px;'
                                'border-radius:999px;font-size:0.68rem;font-weight:700">채택</span>'
                                if is_adopted else
                                '<span style="background:#e2e8f0;color:#64748b;padding:1px 6px;'
                                'border-radius:999px;font-size:0.68rem;font-weight:700">후보</span>'
                            )
                            reason = g.get("adoption_reason") or ""
                            st.markdown(
                                f"""<div style="padding:10px 14px;margin-bottom:6px;
                                            border-radius:10px;background:{bg};
                                            border:1px solid {border};
                                            display:flex;align-items:center;
                                            justify-content:space-between">
                                  <div>
                                    <span style="font-size:0.85rem;font-weight:700;color:#0f172a">
                                      {i}. {article}
                                    </span>
                                    <span style="font-size:0.8rem;color:#64748b;margin-left:8px">
                                      {g.get('parent_title') or ''}
                                    </span>
                                    {f'<div style="font-size:0.75rem;color:#64748b;margin-top:3px">— {reason}</div>' if reason else ''}
                                  </div>
                                  {tag}
                                </div>""",
                                unsafe_allow_html=True,
                            )
            except Exception as e:
                st.error(f"❌ 조회 실패: {e}")
