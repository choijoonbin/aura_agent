import asyncio
from threading import Event

from dwp_agent.contracts import AskRequest
from dwp_agent.policy import AskIdentity, SafetyControls
from dwp_agent.stream_runtime import (
    AskStreamPool,
    shutdown_ask_stream_pool,
    stream_ask_response,
)
from dwp_agent.workspace_authorization import WorkspaceRequestAuthorization


def test_stream_pool_rejects_work_when_all_bounded_slots_are_active() -> None:
    started = Event()
    release = Event()
    pool = AskStreamPool(max_concurrency=1)

    def blocking_work() -> str:
        started.set()
        release.wait(timeout=2)
        return "done"

    first = pool.try_submit(blocking_work)
    assert first is not None
    assert started.wait(timeout=1)
    assert pool.try_submit(lambda: "overflow") is None

    release.set()
    assert first.result(timeout=1) == "done"
    pool.shutdown()


def test_stream_worker_receives_ephemeral_workspace_authorization() -> None:
    captured: list[WorkspaceRequestAuthorization | None] = []

    class FailingRuntime:
        def answer(self, _request, **kwargs):
            captured.append(kwargs.get("workspace_authorization"))
            raise RuntimeError("stop after capture")

    authorization = WorkspaceRequestAuthorization(
        cookie_header="DWP_SESSION=stream-secret"
    )
    response = stream_ask_response(
        request=AskRequest(request_id="stream-auth", query="Show my calendar"),
        identity=AskIdentity(
            tenant_id="1",
            user_id="7",
            roles=("WORKSPACE_MEMBER",),
            permissions=("APP.ASK:VIEW", "APP.CALENDAR:VIEW"),
            correlation_id="stream-auth",
        ),
        runtime=FailingRuntime(),  # type: ignore[arg-type]
        safety_controls=SafetyControls(),
        encode_event=lambda event, payload: f"{event}:{payload}\n",
        error_code=lambda _error: "EXPECTED_FAILURE",
        workspace_authorization=authorization,
    )

    async def consume() -> None:
        async for _chunk in response.body_iterator:
            pass

    try:
        asyncio.run(consume())
    finally:
        shutdown_ask_stream_pool()

    assert captured == [authorization]
    assert "stream-secret" not in repr(captured[0])
