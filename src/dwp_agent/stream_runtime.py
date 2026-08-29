from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, Queue
from time import monotonic
from typing import TypeVar

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from .ask_runtime import AskRuntime
from .contracts import AskEnvelope, AskRequest
from .policy import AskIdentity, SafetyControls
from .workspace_authorization import WorkspaceRequestAuthorization


T = TypeVar("T")


class AskStreamPool:
    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max_concurrency
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="dwaion-ask-stream",
        )

    def try_submit(self, work: Callable[[], T]) -> Future[T] | None:
        if not self._slots.acquire(blocking=False):
            return None

        def guarded() -> T:
            try:
                return work()
            finally:
                self._slots.release()

        try:
            return self._executor.submit(guarded)
        except RuntimeError:
            self._slots.release()
            return None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


_POOL: AskStreamPool | None = None
_POOL_LOCK = threading.Lock()


def get_ask_stream_pool() -> AskStreamPool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = AskStreamPool(_bounded_int("DWP_AGENT_STREAM_MAX_CONCURRENCY", 8, 1, 64))
        return _POOL


def ask_stream_timeout_seconds() -> float:
    return _bounded_float("DWP_AGENT_STREAM_TIMEOUT_SECONDS", 40.0, 5.0, 40.0)


def stream_ask_response(
    *,
    request: AskRequest,
    identity: AskIdentity,
    runtime: AskRuntime,
    safety_controls: SafetyControls,
    encode_event: Callable[[str, dict[str, object]], str],
    error_code: Callable[[Exception], str],
    workspace_authorization: WorkspaceRequestAuthorization | None = None,
) -> StreamingResponse:
    events: Queue[tuple[str, dict[str, object] | None]] = Queue(maxsize=32)

    def worker() -> None:
        try:
            response = runtime.answer(
                request,
                identity=identity,
                on_progress=lambda stage: events.put(("progress", {"stage": stage})),
                safety_controls=safety_controls,
                workspace_authorization=workspace_authorization,
            )
            envelope = AskEnvelope(data=response)
            events.put(("result", envelope.model_dump(mode="json", by_alias=True)))
        except Exception as error:  # Stream headers are committed after this task starts.
            events.put(("error", {"code": error_code(error)}))
        finally:
            events.put(("done", None))

    future = get_ask_stream_pool().try_submit(worker)
    if future is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="DWAI-ON stream capacity is temporarily exhausted.",
            headers={"Retry-After": "1"},
        )

    def stream():
        deadline = monotonic() + ask_stream_timeout_seconds()
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    yield encode_event("error", {"code": "ASK_STREAM_TIMEOUT"})
                    break
                try:
                    event, payload = events.get(timeout=remaining)
                except Empty:
                    yield encode_event("error", {"code": "ASK_STREAM_TIMEOUT"})
                    break
                if event == "done":
                    break
                yield encode_event(event, payload or {})
        finally:
            future.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


def shutdown_ask_stream_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.shutdown()
            _POOL = None


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(os.getenv(name, str(default)))))
    except ValueError:
        return default
