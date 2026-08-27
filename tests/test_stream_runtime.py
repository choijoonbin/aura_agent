from threading import Event

from dwp_agent.stream_runtime import AskStreamPool


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
