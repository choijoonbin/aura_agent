"""
Phase E: 실행 상태·이벤트·최종 결과 분리.
- Event log: get_timeline(run_id) — 스트림 이벤트 시퀀스 (orchestration + AGENT_EVENT).
- Final result: get_result(run_id) — run 종료 시 한 번 저장되는 결과 (completed/failed/hitl_required).
- Latest: latest_run_of_case(case_id) — 해당 케이스의 최신 run_id.
- History: list_runs_of_case(case_id) — 해당 케이스의 run_id 목록 (과거 포함).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class RunContext:
    case_id: str
    run_id: str
    queue: asyncio.Queue


class StreamRuntime:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._case_runs: dict[str, str] = {}
        self._runs_by_case: dict[str, list[str]] = {}
        self._results_by_run: dict[str, dict[str, Any]] = {}
        self._timeline_by_run: dict[str, list[dict[str, Any]]] = {}
        self._hitl_requests_by_run: dict[str, dict[str, Any]] = {}
        self._hitl_responses_by_run: dict[str, dict[str, Any]] = {}
        self._hitl_drafts_by_run: dict[str, dict[str, Any]] = {}
        self._run_lineage: dict[str, dict[str, Any]] = {}

    def create_run(self, case_id: str, run_id: str, *, parent_run_id: str | None = None, mode: str = "primary") -> RunContext:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[run_id] = q
        self._case_runs[case_id] = run_id
        self._runs_by_case.setdefault(case_id, []).append(run_id)
        self._timeline_by_run[run_id] = []
        self._run_lineage[run_id] = {
            "case_id": case_id,
            "parent_run_id": parent_run_id,
            "mode": mode,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return RunContext(case_id=case_id, run_id=run_id, queue=q)

    def get_queue(self, run_id: str) -> asyncio.Queue | None:
        return self._queues.get(run_id)

    def latest_run_of_case(self, case_id: str) -> str | None:
        return self._case_runs.get(case_id)

    def list_runs_of_case(self, case_id: str) -> list[str]:
        return list(self._runs_by_case.get(case_id, []))

    def get_result(self, run_id: str) -> dict[str, Any] | None:
        return self._results_by_run.get(run_id)

    def get_timeline(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._timeline_by_run.get(run_id, []))

    def get_hitl_request(self, run_id: str) -> dict[str, Any] | None:
        return self._hitl_requests_by_run.get(run_id)

    def set_hitl_request(self, run_id: str, payload: dict[str, Any]) -> None:
        self._hitl_requests_by_run[run_id] = payload

    def set_hitl_response(self, run_id: str, payload: dict[str, Any]) -> None:
        self._hitl_responses_by_run[run_id] = payload

    def get_hitl_response(self, run_id: str) -> dict[str, Any] | None:
        return self._hitl_responses_by_run.get(run_id)

    def set_hitl_draft(self, run_id: str, payload: dict[str, Any]) -> None:
        self._hitl_drafts_by_run[run_id] = payload

    def get_hitl_draft(self, run_id: str) -> dict[str, Any] | None:
        return self._hitl_drafts_by_run.get(run_id)

    def get_lineage(self, run_id: str) -> dict[str, Any] | None:
        return self._run_lineage.get(run_id)

    async def publish(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        q = self._queues.get(run_id)
        if q is None:
            return
        event = {
            "event_type": event_type,
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        self._timeline_by_run.setdefault(run_id, []).append(event)
        if event_type == "AGENT_EVENT" and payload.get("event_type") == "HITL_REQUESTED":
            self.set_hitl_request(run_id, payload.get("metadata") or {})
        await q.put((event_type, payload))

    def set_result(self, run_id: str, result_payload: dict[str, Any]) -> None:
        self._results_by_run[run_id] = result_payload
        hitl_request = (((result_payload.get("result") or {}).get("hitl_request")) if isinstance(result_payload, dict) else None)
        if hitl_request:
            self.set_hitl_request(run_id, hitl_request)

    async def close(self, run_id: str) -> None:
        q = self._queues.get(run_id)
        if q is None:
            return
        await q.put(("done", {"runId": run_id}))


runtime = StreamRuntime()
