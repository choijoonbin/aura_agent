# Cursor 작업 프롬프트 — 규정문서 라이브러리 UI/UX 고도화 + parent_child 청킹 전략 추가

## 작업 배경

이 화면은 임원·사원 앞 시연에서 직접 노출되는 핵심 관리 화면이다.
현재 상태는 기능은 동작하나 **시각적 밀도·정보 계층·인터랙션 품질** 모두 시연 수준에 미치지 못한다.

### 현재 UI의 구체적 문제 (소스 직접 분석 결과)

| 위치 | 현재 코드 | 문제 |
|------|----------|------|
| 문서 메타 탭 | `st.code(json.dumps(meta))` | JSON 덤프를 그대로 노출 — 임원에게 raw JSON을 보여주는 것은 미완성 인상 |
| 품질 리포트 탭 | `st.code(json.dumps(report))` | 동일 문제. noise_rate=null 등 의미 없는 null 값 나열 |
| 청크 목록 탭 | `st.expander(f"chunk_id={chunk.get('chunk_id')}")` | chunk_id 숫자만 표시. 내용 미리보기 없음 |
| KPI 카드 4개 | `render_kpi_card(...)` | 아이콘 없음, 트렌드 없음, 색상 구분 없음 |
| 전략 비교 | 3열 숫자만 표시 | "조항 우선 64개 / 평균 200자" — 무엇이 더 좋은지 판단 기준 없음 |
| 청크 미리보기 | 12개 단순 expander | 청크 품질(길이, 유형, 의미그룹) 시각화 없음 |
| Run 인용 조회 | `st.text(...)` 나열 | 채택/미채택 시각 구분 없음 |
| `preview_chunks()` | 3가지 전략만 존재 | `parent_child` 전략 없어 신규 청킹 방식 미리보기 불가 |

---

## 작업 범위

| 파일 | 작업 내용 |
|------|----------|
| `ui/rag.py` | 전체 페이지 UI/UX 재설계 |
| `services/rag_chunk_lab_service.py` | `preview_chunks()`에 `parent_child` 전략 추가 |
| `ui/shared.py` | RAG 전용 공통 컴포넌트 3개 추가 |

---

## 디자인 방향

**콘셉트: "감사실 관제 대시보드"**

- 톤: 신뢰감 있는 다크 네이비 기반, 데이터 인텐시브
- 기준 팔레트: 기존 `shared.py` CSS 변수 완전 호환 유지 (`--mt-text-strong`, `mt-badge-*` 등)
- 신규 요소: 상태별 컬러 코딩, 아이콘 이모지 일관 사용, 진행 바, 인라인 미리보기 카드

**시연 시 핵심 인상 포인트:**
1. KPI 4개가 상태(정상/주의/오류)를 색으로 즉시 전달
2. 문서 메타를 Key-Value 그리드로 표시해 "정돈된 시스템" 인상
3. 청크 미리보기에서 `parent_child` 전략이 Parent/Child 구조로 시각화
4. 전략 비교 카드에 "추천" 뱃지와 품질 게이지 표시
5. Run 인용 조회에서 채택/미채택 청크가 색으로 구분

---

## 상세 구현 명세

---

### ① `services/rag_chunk_lab_service.py` — `parent_child` 전략 추가

**위치:** `preview_chunks()` 함수의 마지막 `# hybrid_policy` 분기 아래에 추가

```python
def preview_chunks(text: str, strategy: str) -> list[dict[str, Any]]:
    if strategy == "article_first":
        return [
            {"title": title, "content": body, "length": len(body), "strategy": strategy,
             "chunk_type": "parent"}
            for title, body in _split_article_sections(text)
            if body
        ]
    if strategy == "sliding_window":
        return [
            {"title": f"윈도우 {idx}", "content": chunk, "length": len(chunk),
             "strategy": strategy, "chunk_type": "leaf"}
            for idx, chunk in enumerate(_window_split(text, chunk_size=700, overlap=120), start=1)
        ]
    if strategy == "parent_child":
        # ── 신규: Parent-Child 계층 청킹 미리보기 ──────────────────────────
        # chunking_pipeline이 없을 때도 동작하도록 로컬 구현
        out: list[dict[str, Any]] = []
        PARENT_MIN = 200
        sections = _split_article_sections(text)
        used: set[int] = set()
        for i, (title, body) in enumerate(sections):
            if i in used:
                continue
            # 초단편 → 다음 조문과 병합
            if len(body) < PARENT_MIN and i + 1 < len(sections):
                next_title, next_body = sections[i + 1]
                merged_title = f"{title} ~ {next_title}"
                merged_body = body + "\n\n" + next_body
                used.add(i + 1)
                title, body = merged_title, merged_body
            used.add(i)
            # Parent 청크
            out.append({
                "title": f"[Parent] {title}",
                "content": body,
                "length": len(body),
                "strategy": strategy,
                "chunk_type": "parent",
                "article": title,
            })
            # Child 청크: 항목 기호(①②③) 기준 분할
            import re as _re
            item_pat = _re.compile(r"(?=[①②③④⑤⑥⑦⑧⑨⑩])")
            children = [c.strip() for c in item_pat.split(body) if c.strip()]
            if len(children) > 1:
                for c_idx, child_text in enumerate(children, start=1):
                    out.append({
                        "title": f"  └ [Child {c_idx}] {title}",
                        "content": child_text,
                        "length": len(child_text),
                        "strategy": strategy,
                        "chunk_type": "leaf",
                        "article": title,
                    })
        return out

    # hybrid_policy (기존 코드 유지)
    out: list[dict[str, Any]] = []
    for title, body in _split_article_sections(text):
        if len(body) <= 900:
            out.append({"title": title, "content": body, "length": len(body),
                        "strategy": strategy, "chunk_type": "parent"})
            continue
        for idx, chunk in enumerate(_window_split(body, chunk_size=650, overlap=100), start=1):
            out.append({"title": f"{title} · part {idx}", "content": chunk,
                        "length": len(chunk), "strategy": strategy, "chunk_type": "leaf"})
    return out
```

**주의:** `_split_article_sections()`이 현재 `list[tuple[str, str]]`를 반환하므로 호환성 유지.
`article_first`와 `sliding_window` 반환 dict에도 `chunk_type` 키를 하위 호환적으로 추가.

---

### ② `ui/shared.py` — RAG 전용 공통 컴포넌트 3개 추가

파일 하단 (기존 `render_empty_state()` 아래)에 추가:

```python
# ── RAG 라이브러리 전용 컴포넌트 ────────────────────────────────────────────

def render_rag_kpi_card(
    label: str,
    value: str,
    foot: str = "",
    status: str = "normal",   # "normal" | "warning" | "error" | "success"
    icon: str = "",
) -> None:
    """
    상태 색상이 있는 RAG KPI 카드.
    status에 따라 좌측 border 색과 배경 tint가 달라짐.
    """
    color_map = {
        "normal":  ("#2563eb", "#eff6ff"),
        "warning": ("#d97706", "#fffbeb"),
        "error":   ("#dc2626", "#fef2f2"),
        "success": ("#059669", "#ecfdf5"),
    }
    border_color, bg_tint = color_map.get(status, color_map["normal"])
    icon_html = f'<span style="font-size:1.3rem;margin-bottom:6px;display:block">{icon}</span>' if icon else ""
    st.markdown(f"""
    <div style="
        padding: 18px 20px;
        border-radius: 18px;
        border: 1px solid #e5e7eb;
        border-left: 4px solid {border_color};
        background: {bg_tint};
        box-shadow: 0 10px 24px rgba(15,23,42,0.05);
        min-height: 132px;
    ">
        {icon_html}
        <div class="mt-kpi-label">{label}</div>
        <div class="mt-kpi-value" style="color:{border_color}">{value}</div>
        <div class="mt-kpi-foot">{foot}</div>
    </div>
    """, unsafe_allow_html=True)


def render_rag_meta_grid(meta: dict) -> None:
    """
    문서 메타를 Key-Value 그리드로 렌더링.
    JSON 덤프 대신 정돈된 테이블 형태로 표시.
    """
    label_map = {
        "status": "처리 상태",
        "version": "버전",
        "effective_from": "시행 시작일",
        "effective_to": "시행 종료일",
        "lifecycle_status": "라이프사이클",
        "active_from": "활성 시작",
        "active_to": "활성 종료",
        "quality_gate_passed": "품질 게이트",
        "updated_at": "최종 수정",
    }
    status_icon_map = {
        "COMPLETED": "✅",
        "PROCESSING": "⏳",
        "FAILED": "❌",
        "ACTIVE": "🟢",
        "INACTIVE": "⚫",
    }
    rows_html = ""
    for key, display_label in label_map.items():
        raw = meta.get(key)
        if raw is None:
            display_val = '<span style="color:#94a3b8">—</span>'
        elif isinstance(raw, bool):
            display_val = "✅ 통과" if raw else "❌ 미통과"
        else:
            val_str = str(raw)
            icon = status_icon_map.get(val_str.upper(), "")
            display_val = f"{icon} {val_str}" if icon else val_str
        rows_html += f"""
        <div style="display:contents">
            <div style="color:#64748b;font-weight:700;font-size:0.82rem;padding:8px 0;
                        border-bottom:1px solid #f1f5f9">{display_label}</div>
            <div style="color:#0f172a;font-weight:500;font-size:0.85rem;padding:8px 0;
                        border-bottom:1px solid #f1f5f9">{display_val}</div>
        </div>
        """
    st.markdown(f"""
    <div style="display:grid;grid-template-columns:140px 1fr;gap:0 16px;
                background:#fff;border-radius:14px;padding:12px 16px;
                border:1px solid #e5e7eb;">
        {rows_html}
    </div>
    """, unsafe_allow_html=True)


def render_rag_quality_report(report: dict) -> None:
    """
    품질 리포트를 게이지 바 + 수치로 시각화.
    JSON 덤프 대신 직관적 지표 카드.
    """
    def _gauge(label: str, value: float | None, *, higher_is_bad: bool = True,
               threshold_warn: float = 0.1, threshold_err: float = 0.3,
               suffix: str = "%") -> str:
        if value is None:
            return f"""
            <div style="margin-bottom:14px">
                <div style="font-size:0.8rem;font-weight:700;color:#64748b;margin-bottom:4px">{label}</div>
                <div style="font-size:0.85rem;color:#94a3b8">데이터 없음</div>
            </div>"""
        pct = float(value) * 100 if suffix == "%" else float(value)
        if higher_is_bad:
            color = "#dc2626" if pct >= threshold_err * 100 else ("#d97706" if pct >= threshold_warn * 100 else "#059669")
        else:
            color = "#059669" if pct >= 80 else ("#d97706" if pct >= 50 else "#dc2626")
        bar_width = min(100, max(0, pct))
        return f"""
        <div style="margin-bottom:14px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
                <div style="font-size:0.8rem;font-weight:700;color:#64748b">{label}</div>
                <div style="font-size:0.9rem;font-weight:800;color:{color}">{pct:.1f}{suffix}</div>
            </div>
            <div style="background:#f1f5f9;border-radius:999px;height:7px;overflow:hidden">
                <div style="width:{bar_width}%;background:{color};height:100%;border-radius:999px;
                            transition:width 0.6s ease"></div>
            </div>
        </div>"""

    passed = report.get("quality_report_passed")
    input_c = report.get("input_chunks")
    final_c = report.get("final_chunks")
    missing = report.get("missing_required") or []

    gate_html = f"""
    <div style="display:flex;align-items:center;gap:10px;padding:12px 16px;
                border-radius:12px;background:{'#ecfdf5' if passed else '#fef2f2'};
                border:1px solid {'#a7f3d0' if passed else '#fecaca'};margin-bottom:16px">
        <span style="font-size:1.4rem">{'✅' if passed else '❌'}</span>
        <div>
            <div style="font-size:0.9rem;font-weight:800;color:{'#059669' if passed else '#dc2626'}">
                품질 게이트 {'통과' if passed else '미통과'}
            </div>
            <div style="font-size:0.78rem;color:#64748b">
                입력 {input_c or '?'}개 → 최종 {final_c or '?'}개 청크
            </div>
        </div>
    </div>"""

    gauges_html = (
        _gauge("노이즈율", report.get("noise_rate"), higher_is_bad=True,
               threshold_warn=0.05, threshold_err=0.15)
        + _gauge("중복율", report.get("duplicate_rate"), higher_is_bad=True,
                 threshold_warn=0.05, threshold_err=0.20)
        + _gauge("초단편 청크율", report.get("short_chunk_rate"), higher_is_bad=True,
                 threshold_warn=0.10, threshold_err=0.30)
        + _gauge("조항 커버리지", report.get("article_coverage"), higher_is_bad=False,
                 threshold_warn=50, threshold_err=30, suffix="%")
    )

    missing_html = ""
    if missing:
        items_html = "".join(f'<div style="color:#dc2626;font-size:0.8rem">• {m}</div>' for m in missing)
        missing_html = f"""
        <div style="padding:10px 14px;background:#fef2f2;border-radius:10px;
                    border:1px solid #fecaca;margin-top:10px">
            <div style="font-size:0.8rem;font-weight:700;color:#dc2626;margin-bottom:6px">
                ⚠ 누락 필수 항목
            </div>
            {items_html}
        </div>"""

    st.markdown(gate_html + gauges_html + missing_html, unsafe_allow_html=True)
```

---

### ③ `ui/rag.py` — 전체 페이지 UI/UX 재설계

아래 코드로 `render_rag_library_page()` 함수 **전체를 교체**한다.
기존 `render_kpi_card`, `render_panel_header`, `stylable_container` import 유지.
신규 import 추가: `render_rag_kpi_card`, `render_rag_meta_grid`, `render_rag_quality_report`

```python
from ui.shared import (
    stylable_container,
    render_empty_state,
    render_kpi_card,
    render_page_header,
    render_panel_header,
    render_rag_kpi_card,         # 신규
    render_rag_meta_grid,        # 신규
    render_rag_quality_report,   # 신규
    status_badge,
    fmt_dt_korea,
)


def render_rag_library_page() -> None:
    # ── 페이지 헤더 ────────────────────────────────────────────────────────
    render_page_header(
        "규정문서 라이브러리",
        "규정 문서·retrieval·청킹 실험을 한 화면에서 관리합니다. "
        "policy_rulebook_probe는 여기 인덱싱된 청크를 검색합니다.",
    )

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

                # -- 탭 3: 청크 목록 (인라인 미리보기 카드)
                with detail_tabs[2]:
                    chunks = detail.get("chunks") or []
                    if not chunks:
                        render_empty_state("저장된 청크가 없습니다.")
                    else:
                        # 청크 통계 인라인 표시
                        avg_len = sum(len(str(c.get("chunk_text") or "")) for c in chunks) / len(chunks)
                        st.markdown(
                            f"""<div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap">
                              <span class="mt-meta-pill">🧩 총 {len(chunks)}개</span>
                              <span class="mt-meta-pill">📏 평균 {avg_len:.0f}자</span>
                            </div>""",
                            unsafe_allow_html=True,
                        )
                        for chunk in chunks[:50]:
                            article = chunk.get("regulation_article") or "—"
                            parent_title = chunk.get("parent_title") or "제목 없음"
                            chunk_text = str(chunk.get("chunk_text") or "")
                            chunk_len = len(chunk_text)
                            preview = chunk_text[:60] + "..." if len(chunk_text) > 60 else chunk_text

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
                                                   padding:2px 8px;border-radius:999px;
                                                   font-size:0.72rem;font-weight:700">
                                        {q_label} {chunk_len}자
                                      </span>
                                      <span class="mt-meta-pill">
                                        page={chunk.get('page_no') or '—'} /
                                        index={chunk.get('chunk_index') or '—'}
                                      </span>
                                    </div>
                                    <div style="background:#f8fafc;border-radius:10px;
                                                padding:12px 14px;font-size:0.85rem;
                                                color:#0f172a;line-height:1.65;
                                                border:1px solid #e2e8f0">
                                        {chunk_text}
                                    </div>""",
                                    unsafe_allow_html=True,
                                )

    # ════════════════════════════════════════════════════════════════════════
    # TAB 2: 청킹 실험실
    # ════════════════════════════════════════════════════════════════════════
    with top_tabs[1]:
        render_panel_header(
            "청킹 실험실",
            "업로드한 규정 텍스트에 전략을 바꿔 적용하며 청킹 결과를 비교합니다.",
        )
        local_files = list_rulebook_files()
        upload = st.file_uploader("규정 TXT 업로드", type=["txt"])
        if upload is not None:
            save_uploaded_rulebook(upload.name, upload.getvalue())
            st.success(f"✅ 업로드 완료: {upload.name}")
            st.rerun()

        if not local_files:
            render_empty_state("업로드된 규정 파일이 없습니다.")
            return

        ctrl_left, ctrl_right = st.columns([0.35, 0.65])
        with ctrl_left:
            selected_file = st.selectbox(
                "대상 문서",
                options=local_files,
                format_func=lambda x: f"{x['name']} ({x['source']})",
            )
            strategy = st.selectbox(
                "청킹 전략",
                options=["parent_child", "hybrid_policy", "article_first", "sliding_window"],
                format_func=lambda x: {
                    "parent_child":    "🆕 Parent-Child 계층형 (권장)",
                    "hybrid_policy":   "하이브리드 정책형",
                    "article_first":   "조항 우선",
                    "sliding_window":  "슬라이딩 윈도우",
                }[x],
            )
            # 전략 설명
            strategy_desc = {
                "parent_child":   "조문 단위 Parent + 항목(①②③) 단위 Child 이중 구조. 초단편 조문 자동 병합. Dense 벡터 검색에 최적화.",
                "hybrid_policy":  "조항 경계를 먼저 지키고, 긴 조항만 내부 윈도우로 분할.",
                "article_first":  "조항 경계만 기준. 길이 무관 단순 분리.",
                "sliding_window": "고정 크기 윈도우로 오버랩 슬라이딩. 경계 무시.",
            }
            st.caption(strategy_desc[strategy])

        with ctrl_right:
            text = load_rulebook_text(selected_file["path"])
            chunks = preview_chunks(text, strategy)
            parent_count = sum(1 for c in chunks if c.get("chunk_type") == "parent")
            leaf_count = sum(1 for c in chunks if c.get("chunk_type") == "leaf")
            avg_len = sum(c["length"] for c in chunks) / len(chunks) if chunks else 0
            short_count = sum(1 for c in chunks if c["length"] < 200)

            # 선택 전략 지표
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("원문 길이", f"{len(text):,}자")
            m2.metric("총 청크 수", len(chunks),
                      delta=f"Parent {parent_count} + Leaf {leaf_count}" if parent_count else None)
            m3.metric("평균 길이", f"{avg_len:.0f}자")
            m4.metric("초단편(<200자)", short_count,
                      delta="개선 필요" if short_count > 5 else "양호",
                      delta_color="inverse")

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
            s_short = sum(1 for r in rows if r["length"] < 200)
            quality_score = max(0, 100 - s_short * 3)  # 단순 품질 점수
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
                    # 품질 게이지 바
                    st.markdown(
                        f"""<div style="margin-top:10px">
                          <div style="font-size:0.72rem;color:#94a3b8;margin-bottom:3px">
                            품질 점수 {quality_score}
                          </div>
                          <div style="background:#f1f5f9;border-radius:999px;height:5px">
                            <div style="width:{quality_score}%;background:{bar_color};
                                        height:100%;border-radius:999px"></div>
                          </div>
                        </div>""",
                        unsafe_allow_html=True,
                    )
                    st.caption(s_meta["desc"])

        st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)

        # ── 청크 미리보기 ────────────────────────────────────────────────
        st.markdown(
            '<div class="mt-panel-title" style="margin-bottom:8px">청크 미리보기</div>',
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
            with st.expander(
                f"{type_icon}  {idx}. {chunk['title'][:40]}  ·  {chunk_len}자",
                expanded=(idx == 1 and c_type == "parent"),
            ):
                st.markdown(
                    f"""<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
                      <span style="font-size:0.78rem;font-weight:700;color:{type_color}">
                        {c_type.upper()}
                      </span>
                      {len_tag}
                    </div>
                    <div style="background:#f8fafc;border-radius:10px;padding:12px 14px;
                                font-size:0.85rem;color:#0f172a;line-height:1.65;
                                border:1px solid #e2e8f0;white-space:pre-wrap">
                        {chunk['content']}
                    </div>""",
                    unsafe_allow_html=True,
                )

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
```

---

## 구현 완료 검증 체크리스트

```bash
# 1. 청킹 전략 추가 확인
python3 -c "
from services.rag_chunk_lab_service import preview_chunks, load_rulebook_text
from pathlib import Path
import glob
files = glob.glob('/path/to/규정집/*.txt')
if files:
    text = Path(files[0]).read_text(encoding='utf-8')
    chunks = preview_chunks(text, 'parent_child')
    parents = [c for c in chunks if c.get('chunk_type') == 'parent']
    leaves  = [c for c in chunks if c.get('chunk_type') == 'leaf']
    print(f'parent_child 전략: 총 {len(chunks)}개 (Parent {len(parents)} / Leaf {len(leaves)})')
    short = [c for c in chunks if c['length'] < 200]
    print(f'초단편 청크: {len(short)}개 (기존 40개에서 감소 여부 확인)')
else:
    print('규정 파일 없음 — 경로 확인 필요')
"

# 2. Streamlit UI 실행
streamlit run app.py --server.port 8502

# 3. 확인 항목
# ✅ KPI 4개에 아이콘 + 상태 색상(파란/초록/빨간) 표시
# ✅ 문서 메타 탭이 Key-Value 그리드로 표시 (JSON 덤프 아님)
# ✅ 품질 리포트 탭에 게이지 바 표시
# ✅ 청크 목록 탭에 "초단편/적정/장문" 색상 태그 표시
# ✅ 청킹 실험실 전략 드롭다운에 "Parent-Child 계층형(권장)" 옵션 추가
# ✅ 전략 비교 카드에 "추천" 뱃지와 품질 게이지 바 표시
# ✅ 청크 미리보기에서 Parent/Child 구조 아이콘 구분 표시
# ✅ Run 인용 조회에서 채택/후보 색상 카드 구분 표시
```

---

## 변경 요약

| 항목 | 변경 전 | 변경 후 |
|------|---------|---------|
| KPI 카드 | 텍스트+숫자만 | 아이콘 + 상태별 컬러 border |
| 문서 메타 | JSON 덤프 | Key-Value 그리드 + 아이콘 |
| 품질 리포트 | JSON 덤프 | 게이지 바 + 통과/미통과 배너 |
| 청크 목록 | expander+텍스트 | 길이 품질 색상 태그 + 스타일 카드 |
| 청킹 전략 수 | 3가지 | 4가지 (parent_child 추가) |
| 전략 비교 | 숫자 3열 | 추천 뱃지 + 품질 게이지 + 설명 |
| 청크 미리보기 | 단순 expander | Parent📦 / Child🔹 구조 시각화 |
| Run 인용 조회 | 텍스트 나열 | 채택(초록) / 후보(회색) 카드 구분 |





#추가ui작업
with top_tabs[1]:
    render_panel_header(
        "🔬 청킹 실험실",
        "규정 텍스트에 전략을 바꿔 적용하며 청킹 결과를 비교합니다. "
        "전략 선택 → 지표 확인 → 미리보기 → DB 재청킹 순으로 진행하세요."
    )

    local_files = list_rulebook_files()

    # ── Row 1: 컨트롤 3개 한 줄 (5-2 + A2) ─────────────────────────────────
    ctrl1, ctrl2, ctrl3 = st.columns([0.35, 0.35, 0.30])

    with ctrl1:
        selected_file = st.selectbox(
            "대상 문서",
            options=local_files,
            format_func=lambda x: f"{x['name']} ({x['source']})",
            index=0,
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
        return   # 이하 코드 실행 안 함

    text   = load_rulebook_text(selected_file["path"])
    chunks = preview_chunks(text, strategy)
    parent_chunks = [c for c in chunks if c.get("chunk_type") == "parent"] if strategy == "parent_child" else chunks

    # ── Row 2: 통계 지표 4개 한 줄 (5-2) ────────────────────────────────────
    s1, s2, s3, s4 = st.columns(4)

    total_chunks = len(chunks)
    avg_len = int(sum(c["length"] for c in chunks) / total_chunks) if total_chunks else 0

    # 초단편: parent_child면 parent만, 나머지는 전체
    check_chunks = parent_chunks if strategy == "parent_child" else chunks
    short_count  = sum(1 for c in check_chunks if c["length"] < 200)
    short_label  = f"초단편 ({('ROOT' if strategy == 'parent_child' else '전체')})"

    s1.metric("원문 길이",   f"{len(text):,}자")
    s2.metric("총 청크 수",  str(total_chunks),
              delta=f"Parent {len(parent_chunks)} + Leaf {total_chunks - len(parent_chunks)}"
              if strategy == "parent_child" else None)
    s3.metric("평균 길이",   f"{avg_len}자")

    # 초단편 수 — 양호/개선필요 배지 (5-4 툴팁 포함)
    is_good = short_count <= 5
    badge_text  = "양호" if is_good else "개선필요"
    badge_color = "#059669" if is_good else "#dc2626"
    badge_bg    = "#ecfdf5" if is_good else "#fef2f2"

    # 5-4: 배지 툴팁 설명 (HTML title 속성)
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
        st.markdown(
            f"<span title='{badge_title}' style='cursor:help;"
            f"background:{badge_bg};color:{badge_color};"
            f"font-size:0.75rem;font-weight:700;padding:3px 10px;"
            f"border-radius:20px;border:1px solid {badge_color}33;'>"
            f"{'✅ ' if is_good else '⚠️ '}{badge_text}</span>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

    # ── 전략 비교 (A3 + 5-3 + 5-4) ──────────────────────────────────────────
    st.markdown("#### 전략 비교")

    _STRATEGY_LABELS = {
        "parent_child":   ("🏆 Parent-Child 계층형", "parent_child"),
        "하이브리드 정책형": ("하이브리드 정책형",    "hybrid_policy"),
        "조항 우선":        ("조항 우선",              "article_first"),
        "슬라이딩 윈도우":  ("슬라이딩 윈도우",        "sliding_window"),
    }

    compare = {
        "🏆 Parent-Child 계층형": preview_chunks(text, "parent_child"),
        "하이브리드 정책형":       preview_chunks(text, "hybrid_policy"),
        "조항 우선":               preview_chunks(text, "article_first"),
        "슬라이딩 윈도우":         preview_chunks(text, "sliding_window"),
    }
    _STRAT_KEYS = {
        "🏆 Parent-Child 계층형": "parent_child",
        "하이브리드 정책형":       "hybrid_policy",
        "조항 우선":               "article_first",
        "슬라이딩 윈도우":         "sliding_window",
    }

    def _quality_score_and_detail(cks: list, strat_key: str) -> tuple[int, str]:
        """
        품질 점수 계산 + 5-3 툴팁 설명 문자열 반환.
        parent_child: ROOT(parent) 청크만 기준
        기타: 전체 기준
        """
        if not cks:
            return 0, "청크 없음"
        if strat_key == "parent_child":
            base = [c for c in cks if c.get("chunk_type") == "parent"]
        else:
            base = cks
        if not base:
            return 0, "parent 청크 없음"
        total_b = len(base)
        short_b = sum(1 for c in base if c["length"] < 200)
        penalty = short_b * 3
        score   = max(0, 100 - penalty)
        avg_b   = int(sum(c["length"] for c in base) / total_b)
        detail_str = (
            f"기준 청크: {total_b}개 ({'ARTICLE(Parent)만' if strat_key == 'parent_child' else '전체'})\n"
            f"200자 미만 단편: {short_b}개 × (-3점) = -{penalty}점\n"
            f"평균 길이: {avg_b}자\n"
            f"최종 점수: 100 - {penalty} = {score}점"
        )
        return score, detail_str

    compare_cols = st.columns(4)
    for col, (label, ck_list) in zip(compare_cols, compare.items()):
        strat_key = _STRAT_KEYS[label]
        q_score, q_detail = _quality_score_and_detail(ck_list, strat_key)
        is_recommended = strat_key == "parent_child"
        is_good_strat  = q_score >= 70

        border_color = "#2563eb" if is_recommended else "#e5e7eb"
        border_width = "2px"     if is_recommended else "1px"

        score_color = "#059669" if q_score >= 70 else "#d97706" if q_score >= 40 else "#dc2626"

        badge_t  = "양호"    if is_good_strat else "개선필요"
        badge_c  = "#059669" if is_good_strat else "#dc2626"
        badge_bg2= "#ecfdf5" if is_good_strat else "#fef2f2"

        # 5-4: 배지 툴팁
        if is_good_strat:
            b_tip = f"단편 {sum(1 for c in ([c for c in ck_list if c.get('chunk_type')=='parent'] if strat_key=='parent_child' else ck_list) if c['length']<200)}개 (기준: 5개 이하). 청킹 품질 양호, AI 검색 정확도 유리."
        else:
            b_tip = f"단편 초과. 200자 미만 ARTICLE이 많아 AI 검색 시 문맥 부족 우려. 재청킹 권장."

        with col:
            with stylable_container(
                key=f"rag_cmp_{strat_key}",
                css_styles=f"{{padding:14px 16px;border-radius:16px;"
                           f"border:{border_width} solid {border_color};"
                           f"background:rgba(255,255,255,0.98);"
                           f"box-shadow:0 8px 22px rgba(15,23,42,0.04);min-height:180px;}}"
            ):
                rec_label = "🏆 권장" if is_recommended else ""
                st.caption(f"{label} {rec_label}".strip())
                st.subheader(str(len(ck_list)))
                avg_l = f"{int(sum(c['length'] for c in ck_list)/len(ck_list)):.0f}" if ck_list else "0"
                st.caption(f"평균 {avg_l}자")

                # 5-3: 품질점수 + 마우스오버 툴팁
                st.markdown(
                    f"<div title='{q_detail}' style='cursor:help;margin-top:8px;'>"
                    f"<span style='font-size:11px;color:#6b7280;'>품질점수</span><br>"
                    f"<span style='font-size:24px;font-weight:800;color:{score_color};'>{q_score}점</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                # 5-4: 배지 + 툴팁
                st.markdown(
                    f"<span title='{b_tip}' style='cursor:help;"
                    f"background:{badge_bg2};color:{badge_c};"
                    f"font-size:0.72rem;font-weight:700;padding:2px 8px;"
                    f"border-radius:20px;'>"
                    f"{'✅' if is_good_strat else '⚠️'} {badge_t}</span>",
                    unsafe_allow_html=True,
                )

    # ── 청크 미리보기 ──────────────────────────────────────────────────────
    st.markdown("#### 청크 미리보기")
    # 범례
    if strategy == "parent_child":
        st.markdown(
            "<span style='background:#ef4444;color:#fff;font-size:0.72rem;"
            "padding:2px 8px;border-radius:4px;margin-right:6px;'>100자 미만</span>"
            "<span style='background:#22c55e;color:#fff;font-size:0.72rem;"
            "padding:2px 8px;border-radius:4px;margin-right:6px;'>200자 이상</span>"
            "<span style='font-size:0.72rem;color:#6b7280;'>(100~199자: 배지 없음)</span>",
            unsafe_allow_html=True,
        )
    for idx, chunk in enumerate(chunks[:20], start=1):
        ctype = chunk.get("chunk_type", "")
        icon  = "📦" if ctype == "parent" else ("🔹" if ctype == "child" else "")
        clen  = chunk["length"]
        len_badge = ""
        if clen < 100:
            len_badge = " 🔴"
        elif clen >= 200:
            len_badge = " 🟢"
        label = f"{icon} {chunk['title']} · {clen}자{len_badge}"
        with st.expander(label, expanded=(idx == 1)):
            if ctype:
                type_label = "ARTICLE (Parent)" if ctype == "parent" else "CLAUSE (Child)"
                clr = "#1d4ed8" if ctype == "parent" else "#6b7280"
                st.markdown(f"<span style='color:{clr};font-size:11px;font-weight:700;'>{type_label}</span>", unsafe_allow_html=True)
            st.write(chunk["content"])

    # ── DB 재청킹 — 전략비교 바로 아래, 미리보기 위 (5-1) ──────────────────
    st.markdown("---")
    with stylable_container(
        key="rag_rechunk_box",
        css_styles="""{
            background: #fffbeb;
            border: 1px solid #fde68a;
            border-radius: 14px;
            padding: 16px 18px;
            margin-bottom: 16px;
        }"""
    ):
        st.markdown(
            "<div style='font-weight:700;color:#92400e;font-size:0.9rem;margin-bottom:6px;'>"
            "⚡ DB 재청킹</div>"
            "<div style='font-size:0.82rem;color:#78350f;'>"
            "현재 선택된 규정집 문서로 지정된 문서(doc_id)에 각종 청킹 입력값(search_tsv)을 적용합니다. "
            "위 전략 비교에서 최적 전략을 확인한 뒤 실행하세요."
            "</div>",
            unsafe_allow_html=True,
        )
        doc_options = [{"label": f"doc_id={d['doc_id']} · {d['title']}", "value": d["doc_id"]} for d in items]
        sel_doc_rechunk = st.selectbox(
            "대상 문서 [doc_id]",
            options=[d["doc_id"] for d in items],
            format_func=lambda v: next((f"doc_id={d['doc_id']} · {d['title']}" for d in items if d["doc_id"] == v), str(v)),
            key="rechunk_doc_id",
        )
        if st.button("🔄 현재 규정집으로 재청킹 실행", type="primary", key="rechunk_btn"):
            st.info(f"doc_id={sel_doc_rechunk} 재청킹 요청 전송 중...")
            # 실제 재청킹 API 호출 로직 (기존 코드 유지)