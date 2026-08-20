from uuid import uuid4

import pytest

import dwp_agent.evaluation_runner as evaluation_runner_module
from dwp_agent.evaluation_runner import run_evaluation
from dwp_agent.policy import AskIdentity


class FailingEvaluationStore:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.failed_run_id = None

    def begin_run(self, **_: object):
        return self.run_id, object()

    def fail_run(self, *, evaluation_run_id, **_: object) -> None:
        self.failed_run_id = evaluation_run_id


def test_evaluation_failure_closes_running_state(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FailingEvaluationStore()
    monkeypatch.setattr(
        evaluation_runner_module,
        "_evaluate_cases",
        lambda **_: (_ for _ in ()).throw(RuntimeError("model route failed")),
    )
    identity = AskIdentity(
        tenant_id="1",
        user_id="7",
        roles=("DWAION_ADMIN",),
        permissions=("APP.ASK:VIEW", "ADMIN.DWAION_EVALUATION:EXECUTE"),
        correlation_id="corr-1",
    )

    with pytest.raises(RuntimeError, match="model route failed"):
        run_evaluation(
            store=store,
            tenant_id="1",
            actor_user_id="7",
            correlation_id="corr-1",
            evaluation_set_id=uuid4(),
            identity=identity,
        )

    assert store.failed_run_id == store.run_id
