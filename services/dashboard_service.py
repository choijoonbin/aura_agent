from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.config import settings


@dataclass(frozen=True)
class DashboardSnapshot:
    run_id: str
    case_id: str | None
    voucher_key: str | None
    occurred_at: datetime | None
    tool_call_success_rate: float | None
    tool_call_total: int
    tool_call_ok: int
    hitl_requested: bool
    resume_success: bool | None
    citation_coverage: float | None
    fallback_usage_rate: float | None
    event_count: int
    lineage_mode: str | None
    parent_run_id: str | None
    severity: str | None
    score: float | None
    reasoning_summary: str | None
    quality_signals: list[str]
    source_mode: str = "snapshot"


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "y", "yes"}
    return bool(value)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_quality_signals(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith('['):
            try:
                import json
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed if str(v).strip()]
            except Exception:
                pass
        return [v.strip() for v in value.split(',') if v.strip()]
    try:
        return [str(v) for v in list(value) if str(v).strip()]
    except Exception:
        return []


def _fetch_snapshot_rows(db: Session, *, since: datetime, limit: int) -> list[dict[str, Any]]:
    sql = text(
        """
        select
            a.occurred_at,
            a.resource_id as run_id,
            a.metadata_json,
            car.severity,
            car.score,
            car.reasoning_summary,
            car.analysis_quality_signals,
            car.grounding_coverage_ratio,
            completed.metadata_json->'result'->>'severity' as completed_severity,
            completed.metadata_json->'result'->>'score' as completed_score,
            completed.metadata_json->'result'->>'reasonText' as completed_reason_text,
            completed.metadata_json->'result'->'verification_summary'->>'coverage_ratio' as completed_verification_coverage,
            completed.metadata_json->'result'->>'quality_signals_json' as completed_quality_signals_json,
            completed.metadata_json->'result'->>'qualitySignalsJson' as completed_quality_signals_json_camel,
            completed.metadata_json->'result'->>'quality_signals' as completed_quality_signals_text,
            completed.metadata_json->'result'->>'analysis_quality_signals' as completed_analysis_quality_signals_text,
            screening.metadata_json->'payload'->'metadata'->>'severity' as screening_severity
        from dwp_aura.agent_activity_log a
        left join dwp_aura.case_analysis_result car
          on car.run_id::text = a.resource_id
        left join lateral (
            select a2.metadata_json
            from dwp_aura.agent_activity_log a2
            where a2.tenant_id = a.tenant_id
              and a2.resource_type = a.resource_type
              and a2.resource_id = a.resource_id
              and a2.event_type = 'RUN_COMPLETED'
            order by a2.occurred_at desc
            limit 1
        ) completed on true
        left join lateral (
            select a3.metadata_json
            from dwp_aura.agent_activity_log a3
            where a3.tenant_id = a.tenant_id
              and a3.resource_type = a.resource_type
              and a3.resource_id = a.resource_id
              and a3.event_type = 'AGENT_EVENT'
              and upper(coalesce(a3.metadata_json->'payload'->>'event_type','')) = 'SCREENING_RESULT'
            order by a3.occurred_at desc
            limit 1
        ) screening on true
        where a.tenant_id = :tenant_id
          and a.resource_type = 'analysis_run'
          and a.event_type = 'RUN_DIAGNOSTICS_SNAPSHOT'
          and a.occurred_at >= :since
        order by a.occurred_at desc
        limit :limit
        """
    )
    return list(
        db.execute(
            sql,
            {
                "tenant_id": settings.default_tenant_id,
                "since": since,
                "limit": limit,
            },
        ).mappings().all()
    )


def _fetch_fallback_rows(db: Session, *, since: datetime, limit: int) -> list[dict[str, Any]]:
    sql = text(
        """
        select
            a.resource_id as run_id,
            max(a.occurred_at) as occurred_at,
            max(a.metadata_json->>'case_id') as case_id,
            max(a.metadata_json->>'voucher_key') as voucher_key,
            count(*) filter (where a.event_type = 'AGENT_EVENT') as event_count,
            count(*) filter (
              where a.event_type = 'AGENT_EVENT'
                and upper(coalesce(a.metadata_json->'payload'->>'event_type','')) = 'TOOL_CALL'
            ) as tool_call_total,
            count(*) filter (
              where a.event_type = 'AGENT_EVENT'
                and upper(coalesce(a.metadata_json->'payload'->>'event_type','')) = 'TOOL_RESULT'
            ) as tool_call_ok,
            bool_or(a.event_type = 'HITL_REQUESTED') as hitl_requested,
            null::boolean as resume_success,
            count(*) filter (
              where a.event_type = 'AGENT_EVENT'
                and lower(coalesce(a.metadata_json->'payload'->'metadata'->>'note_source','')) = 'fallback'
            ) as fallback_note_count,
            count(*) filter (
              where a.event_type = 'AGENT_EVENT'
                and lower(coalesce(a.metadata_json->'payload'->'metadata'->>'note_source','')) in ('fallback','llm')
            ) as note_count,
            max(coalesce(a.metadata_json->>'parent_run_id','')) as parent_run_id,
            max(coalesce(a.metadata_json->'diagnostics'->>'lineage_mode','')) as lineage_mode,
            car.severity,
            car.score,
            car.reasoning_summary,
            car.grounding_coverage_ratio,
            car.analysis_quality_signals,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->>'severity' end) as completed_severity,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->>'reasonText' end) as completed_reason_text,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->>'score' end) as completed_score,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->'score_breakdown'->>'grounding_score' end) as completed_grounding_score,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->'score_breakdown'->>'final_score' end) as completed_final_score,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->'verification_summary'->>'coverage_ratio' end) as completed_verification_coverage,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->>'quality_signals_json' end) as completed_quality_signals_json,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->>'qualitySignalsJson' end) as completed_quality_signals_json_camel,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->>'quality_signals' end) as completed_quality_signals_text,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->>'analysis_quality_signals' end) as completed_analysis_quality_signals_text,
            max(case when a.event_type = 'RUN_COMPLETED' then a.metadata_json->'result'->>'analysis_quality_signals' end) as completed_analysis_quality_signals_json
        from dwp_aura.agent_activity_log a
        left join dwp_aura.case_analysis_result car
          on car.run_id::text = a.resource_id
        where a.tenant_id = :tenant_id
          and a.resource_type = 'analysis_run'
          and a.occurred_at >= :since
        group by
          a.resource_id,
          car.severity,
          car.score,
          car.reasoning_summary,
          car.grounding_coverage_ratio,
          car.analysis_quality_signals
        order by max(a.occurred_at) desc
        limit :limit
        """
    )
    return list(
        db.execute(
            sql,
            {
                "tenant_id": settings.default_tenant_id,
                "since": since,
                "limit": limit,
            },
        ).mappings().all()
    )


def fetch_dashboard_snapshots(
    db: Session,
    *,
    days: int = 30,
    limit: int = 400,
) -> list[DashboardSnapshot]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = _fetch_snapshot_rows(db, since=since, limit=limit)
    source_mode = "snapshot"
    if not rows:
        rows = _fetch_fallback_rows(db, since=since, limit=limit)
        source_mode = "derived"

    out: list[DashboardSnapshot] = []
    for row in rows:
        metadata = dict(row.get("metadata_json") or {})
        diag = dict(metadata.get("diagnostics") or {})
        severity = None
        score = None
        reasoning_summary = None
        quality_signals: list[str] = []
        fallback_rate = None

        if source_mode == "snapshot":
            tool_total = _to_int(diag.get("tool_call_total"))
            tool_ok = _to_int(diag.get("tool_call_ok"))
            fallback_rate = _to_float(diag.get("fallback_usage_rate"))
            citation_coverage = _to_float(diag.get("citation_coverage"))
            if citation_coverage is None:
                citation_coverage = _to_float(row.get("grounding_coverage_ratio"))
            hitl_requested = bool(diag.get("hitl_requested"))
            resume_success = _to_bool(diag.get("resume_success"))
            event_count = _to_int(diag.get("event_count"))
            lineage_mode = diag.get("lineage_mode")
            parent_run_id = diag.get("parent_run_id")
            case_id = metadata.get("case_id")
            voucher_key = metadata.get("voucher_key")
            tool_success_rate = _to_float(diag.get("tool_call_success_rate"))
            severity = row.get("severity") or row.get("completed_severity") or row.get("screening_severity")
            score = _to_float(row.get("score"))
            if score is None:
                score = _to_float(row.get("completed_score"))
            reasoning_summary = row.get("reasoning_summary") or row.get("completed_reason_text")
            quality_signals = _parse_quality_signals(row.get("analysis_quality_signals"))
            if not quality_signals:
                for key in ["completed_quality_signals_json", "completed_quality_signals_json_camel", "completed_quality_signals_text", "completed_analysis_quality_signals_text"]:
                    quality_signals = _parse_quality_signals(row.get(key))
                    if quality_signals:
                        break
            if citation_coverage is None:
                citation_coverage = _to_float(row.get("completed_verification_coverage"))
        else:
            tool_total = _to_int(row.get("tool_call_total"))
            tool_ok = _to_int(row.get("tool_call_ok"))
            note_count = _to_int(row.get("note_count"))
            fallback_note_count = _to_int(row.get("fallback_note_count"))
            fallback_rate = (fallback_note_count / note_count) if note_count else None
            citation_coverage = _to_float(row.get("grounding_coverage_ratio"))
            hitl_requested = bool(row.get("hitl_requested"))
            resume_success = _to_bool(row.get("resume_success"))
            event_count = _to_int(row.get("event_count"))
            lineage_mode = (row.get("lineage_mode") or "").strip() or ("RESUMED" if row.get("parent_run_id") else "ROOT")
            parent_run_id = (row.get("parent_run_id") or "").strip() or None
            case_id = row.get("case_id")
            voucher_key = row.get("voucher_key")
            tool_success_rate = (tool_ok / tool_total) if tool_total else None
            if citation_coverage is None:
                citation_coverage = _to_float(row.get("completed_verification_coverage"))
            if citation_coverage is None:
                citation_coverage = _to_float(row.get("completed_grounding_score"))
            severity = row.get("severity") or row.get("completed_severity") or row.get("screening_severity")
            score = _to_float(row.get("score"))
            if score is None:
                score = _to_float(row.get("completed_score"))
            if score is None:
                score = _to_float(row.get("completed_final_score"))
            reasoning_summary = row.get("reasoning_summary") or row.get("completed_reason_text")
            quality_signals = _parse_quality_signals(row.get("analysis_quality_signals"))
            if not quality_signals:
                for key in ["completed_quality_signals_json", "completed_quality_signals_json_camel", "completed_quality_signals_text", "completed_analysis_quality_signals_text", "completed_analysis_quality_signals_json"]:
                    quality_signals = _parse_quality_signals(row.get(key))
                    if quality_signals:
                        break

        out.append(
            DashboardSnapshot(
                run_id=str(row["run_id"]),
                case_id=case_id,
                voucher_key=voucher_key,
                occurred_at=row["occurred_at"],
                tool_call_success_rate=tool_success_rate,
                tool_call_total=tool_total,
                tool_call_ok=tool_ok,
                hitl_requested=hitl_requested,
                resume_success=resume_success,
                citation_coverage=citation_coverage,
                fallback_usage_rate=fallback_rate,
                event_count=event_count,
                lineage_mode=lineage_mode,
                parent_run_id=parent_run_id,
                severity=severity,
                score=score,
                reasoning_summary=reasoning_summary,
                quality_signals=quality_signals,
                source_mode=source_mode,
            )
        )
    return out


def snapshots_to_frame(snapshots: list[DashboardSnapshot]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for s in snapshots:
        rows.append(
            {
                "run_id": s.run_id,
                "case_id": s.case_id,
                "voucher_key": s.voucher_key,
                "occurred_at": s.occurred_at,
                "date": s.occurred_at.date().isoformat() if s.occurred_at else None,
                "tool_call_success_rate": s.tool_call_success_rate,
                "tool_call_total": s.tool_call_total,
                "tool_call_ok": s.tool_call_ok,
                "hitl_requested": 1 if s.hitl_requested else 0,
                "resume_success": 1 if s.resume_success else 0,
                "resume_success_raw": s.resume_success,
                "citation_coverage": s.citation_coverage,
                "fallback_usage_rate": s.fallback_usage_rate,
                "event_count": s.event_count,
                "lineage_mode": s.lineage_mode,
                "parent_run_id": s.parent_run_id,
                "severity": s.severity,
                "score": s.score,
                "reasoning_summary": s.reasoning_summary,
                "quality_signals": ", ".join(s.quality_signals),
                "source_mode": s.source_mode,
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "run_id",
                "case_id",
                "voucher_key",
                "occurred_at",
                "date",
                "tool_call_success_rate",
                "tool_call_total",
                "tool_call_ok",
                "hitl_requested",
                "resume_success",
                "resume_success_raw",
                "citation_coverage",
                "fallback_usage_rate",
                "event_count",
                "lineage_mode",
                "parent_run_id",
                "severity",
                "score",
                "reasoning_summary",
                "quality_signals",
                "source_mode",
            ]
        )
    return pd.DataFrame(rows)


def build_dashboard_overview(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "total_runs": 0,
            "unique_cases": 0,
            "avg_citation_coverage": None,
            "avg_tool_success_rate": None,
            "hitl_rate": None,
            "resume_success_rate": None,
            "fallback_rate": None,
            "avg_score": None,
            "source_mode": None,
        }

    hitl_series = frame["hitl_requested"]
    resumed = frame[frame["resume_success_raw"].notna()]
    source_mode = frame["source_mode"].iloc[0] if "source_mode" in frame.columns and not frame.empty else None
    return {
        "total_runs": int(len(frame)),
        "unique_cases": int(frame["case_id"].fillna(frame["voucher_key"]).nunique()),
        "avg_citation_coverage": float(frame["citation_coverage"].dropna().mean()) if frame["citation_coverage"].notna().any() else None,
        "avg_tool_success_rate": float(frame["tool_call_success_rate"].dropna().mean()) if frame["tool_call_success_rate"].notna().any() else None,
        "hitl_rate": float(hitl_series.mean()) if len(hitl_series) else None,
        "resume_success_rate": float(resumed["resume_success"].mean()) if not resumed.empty else None,
        "fallback_rate": float(frame["fallback_usage_rate"].dropna().mean()) if frame["fallback_usage_rate"].notna().any() else None,
        "avg_score": float(frame["score"].dropna().mean()) if frame["score"].notna().any() else None,
        "source_mode": source_mode,
    }


def build_trend_frames(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if frame.empty:
        empty = pd.DataFrame(columns=["date", "citation_coverage", "tool_success_rate", "fallback_rate", "run_count", "hitl_rate"])
        return empty, empty
    grouped = (
        frame.groupby("date", dropna=True)
        .agg(
            run_count=("run_id", "count"),
            citation_coverage=("citation_coverage", "mean"),
            tool_success_rate=("tool_call_success_rate", "mean"),
            fallback_rate=("fallback_usage_rate", "mean"),
            hitl_rate=("hitl_requested", "mean"),
        )
        .reset_index()
        .sort_values("date")
    )
    quality = grouped[["date", "citation_coverage", "tool_success_rate", "fallback_rate"]].copy()
    volume = grouped[["date", "run_count", "hitl_rate"]].copy()
    return quality, volume


def build_recent_runs_frame(frame: pd.DataFrame, *, limit: int = 12) -> pd.DataFrame:
    if frame.empty:
        return frame
    cols = [
        "occurred_at",
        "voucher_key",
        "run_id",
        "severity",
        "score",
        "citation_coverage",
        "tool_call_success_rate",
        "hitl_requested",
        "resume_success_raw",
        "fallback_usage_rate",
        "quality_signals",
        "source_mode",
    ]
    recent = frame.sort_values("occurred_at", ascending=False).head(limit).copy()
    return recent[cols]


def build_case_compare_frame(frame: pd.DataFrame, voucher_key: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    comp = frame[frame["voucher_key"] == voucher_key].copy()
    if comp.empty:
        return comp
    cols = [
        "occurred_at",
        "run_id",
        "severity",
        "score",
        "citation_coverage",
        "tool_call_success_rate",
        "hitl_requested",
        "resume_success_raw",
        "fallback_usage_rate",
        "event_count",
        "quality_signals",
        "source_mode",
    ]
    return comp.sort_values("occurred_at", ascending=False)[cols]


def build_quality_signal_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["signal", "count"])
    counts: dict[str, int] = {}
    for text_value in frame["quality_signals"].dropna().tolist():
        for token in [t.strip() for t in str(text_value).split(",") if t.strip()]:
            counts[token] = counts.get(token, 0) + 1
    rows = [{"signal": k, "count": v} for k, v in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    return pd.DataFrame(rows)
