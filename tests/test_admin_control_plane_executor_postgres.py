from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from urllib.parse import quote, urlparse
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from dwp_agent.admin_control_plane_adapters import (
    AdminCommandAdapterResponse,
    AdminCommandExecutionTransient,
)
from dwp_agent.admin_control_plane_contracts import (
    CreateGovernedCommandRequest,
    GovernedCommandDecisionRequest,
    GovernedCommandRestartRequest,
    GovernedCommandState,
)
from dwp_agent.admin_control_plane_errors import (
    AdminControlPlaneConflict,
    AdminControlPlaneDenied,
)
from dwp_agent.admin_control_plane_executor import PostgresAdminControlCommandExecutor
from dwp_agent.admin_control_plane_snapshots import AdminControlPlaneSnapshots
from dwp_agent.admin_control_plane_store import AdminControlPlaneStore
from dwp_agent.ai_control_store import PostgresAIControlStore
from dwp_agent.ai_control_runtime import AIControlDenied
from dwp_agent.canonical_json import canonical_json_bytes
from dwp_agent.database_migrations import apply_migrations
from dwp_agent.governed_domain_core import GovernedPayloadCodec
from dwp_agent.transactional_outbox import PostgresTransactionalOutboxStore


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)


@pytest.fixture(scope="module")
def isolated_url() -> str:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Admin command executor tests require a dedicated test database.")
    schema = "test_admin_executor_" + uuid4().hex
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in DATABASE_URL else "?"
    url = DATABASE_URL + separator + "options=" + quote(f"-csearch_path={schema}")
    try:
        apply_migrations(url)
        yield url
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def _request(
    kind: str,
    target_type: str,
    target_id: str,
    *,
    expected_version: int,
    payload: dict[str, object],
    command_id=None,
) -> CreateGovernedCommandRequest:
    recovery = "Restore the last verified resource snapshot and preserve its audit receipt."
    return CreateGovernedCommandRequest.model_validate({
        "commandId": str(command_id or uuid4()),
        "kind": kind,
        "target": {"type": target_type, "id": target_id},
        "expectedVersion": expected_version,
        "reason": "Execute the reviewed DWAI-ON administrative change.",
        "ticketRef": "DWAION-ADMIN-VERIFY",
        "evidenceRefs": ["evidence:integration-test"],
        "impactAcknowledged": True,
        "preflight": {
            "changes": [{"field": "state", "before": "current", "after": "reviewed"}],
            "impactScopes": [target_type],
            "recoveryPlan": recovery,
            "recoveryPlanHash": hashlib.sha256(recovery.encode()).hexdigest(),
        },
        "payload": payload,
    })


def _approve(store: AdminControlPlaneStore, tenant: int, command) -> object:
    return store.decide(
        tenant_id=tenant,
        actor_user_id="checker-user",
        correlation_id=f"decision-{uuid4()}",
        command_id=command.command_id,
        request=GovernedCommandDecisionRequest(
            command_id=uuid4(),
            decision="APPROVE",
            expected_version=command.version,
            reason="Independent review verified the target, evidence, and recovery plan.",
            evidence_refs=["evidence:checker-review"],
        ),
    )


def _claim(outbox: PostgresTransactionalOutboxStore, tenant: int):
    lease = outbox.claim(
        tenant_id=tenant,
        topics=("ADMIN_CONTROL_COMMAND",),
        lease_seconds=300,
    )
    assert lease is not None
    return lease


def _execute_approved(
    store: AdminControlPlaneStore,
    outbox: PostgresTransactionalOutboxStore,
    executor: PostgresAdminControlCommandExecutor,
    tenant: int,
    request: CreateGovernedCommandRequest,
):
    command = store.create(
        tenant_id=tenant,
        actor_user_id="maker-user",
        correlation_id=f"command-{uuid4()}",
        auth_session_id=f"session-{uuid4()}",
        request=request,
    )
    if command.state.value == "AWAITING_APPROVAL":
        _approve(store, tenant, command)
    lease = _claim(outbox, tenant)
    assert executor.process(lease) == "SUCCEEDED"
    outbox.acknowledge(lease)
    return store.get(
        tenant_id=tenant,
        actor_user_id="maker-user",
        command_id=command.command_id,
    )


def _seed_resource(
    database_url: str,
    tenant: int,
    resource_type: str,
    resource_id: str,
    version: int,
    snapshot: dict[str, object],
    source_command_id,
) -> None:
    codec = GovernedPayloadCodec()
    envelope = codec.encrypt_json(
        snapshot,
        tenant_id=tenant,
        resource_type="admin-control-resource",
        resource_id=f"{resource_type}:{resource_id}",
        field="snapshot",
    )
    digest = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO ai_admin_control_resources (
                   tenant_id, resource_type, resource_id, resource_version,
                   snapshot_hash, snapshot_envelope, updated_by_command_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (tenant, resource_type, resource_id, version, digest, envelope, source_command_id),
        )


def test_internal_backlog_command_is_versioned_idempotent_and_rollback_deletes_creation(
    isolated_url: str,
) -> None:
    tenant = 800_000_000 + uuid4().int % 90_000_000
    store = AdminControlPlaneStore(isolated_url)
    outbox = PostgresTransactionalOutboxStore(isolated_url)
    executor = PostgresAdminControlCommandExecutor(isolated_url)
    request = _request(
        "BACKLOG_CREATE",
        "IMPROVEMENT_BACKLOG",
        f"backlog-{uuid4()}",
        expected_version=0,
        payload={
            "title": "Reduce evaluated response latency",
            "ownerTeam": "AI Platform",
            "priority": "P1",
            "metricEvidence": "metric:latency-p95",
            "problemCluster": "LATENCY",
            "targetValue": "p95 < 800ms",
            "linkedRelease": None,
            "state": "PROPOSED",
        },
    )
    created = store.create(
        tenant_id=tenant,
        actor_user_id="maker-user",
        correlation_id="create-backlog",
        auth_session_id="session-maker",
        request=request,
    )
    replay = store.create(
        tenant_id=tenant,
        actor_user_id="maker-user",
        correlation_id="create-backlog-replay",
        auth_session_id="session-maker",
        request=request,
    )
    assert replay.command_id == created.command_id
    changed = request.model_copy(update={"reason": "A different request bound to the same command ID."})
    with pytest.raises(AdminControlPlaneConflict, match="another request"):
        store.create(
            tenant_id=tenant,
            actor_user_id="maker-user",
            correlation_id="create-backlog-conflict",
            auth_session_id="session-maker",
            request=changed,
        )

    queued = _approve(store, tenant, created)
    assert outbox.claim(
        tenant_id=tenant + 1,
        topics=("ADMIN_CONTROL_COMMAND",),
        lease_seconds=300,
    ) is None
    lease = _claim(outbox, tenant)
    assert executor.process(lease) == "SUCCEEDED"
    assert executor.process(lease) == "SUCCEEDED"
    outbox.acknowledge(lease)
    completed = store.get(
        tenant_id=tenant,
        actor_user_id="maker-user",
        command_id=created.command_id,
    )
    assert completed.state.value == "SUCCEEDED"
    assert completed.receipt and completed.receipt.domain_receipt_ref
    outcomes = AdminControlPlaneSnapshots(isolated_url).outcomes(tenant, 30, None, None)
    assert [(item.item_id, item.version) for item in outcomes.backlog] == [
        (request.target.id, 1)
    ]

    rollback = store.rollback(
        tenant_id=tenant,
        actor_user_id="rollback-maker",
        correlation_id="rollback-backlog",
        command_id=created.command_id,
        request=GovernedCommandRestartRequest(
            command_id=uuid4(),
            expected_version=completed.version,
            reason="Remove the newly created backlog item using its verified receipt.",
            evidence_refs=["evidence:rollback-review"],
        ),
    )
    rolled_queued = _approve(store, tenant, rollback)
    assert rolled_queued.checker_user_id == "checker-user"
    rollback_lease = _claim(outbox, tenant)
    assert executor.process(rollback_lease) == "ROLLED_BACK"
    outbox.acknowledge(rollback_lease)
    rolled = store.get(
        tenant_id=tenant,
        actor_user_id="rollback-maker",
        command_id=created.command_id,
    )
    assert rolled.receipt and rolled.receipt.rollback_ref
    assert AdminControlPlaneSnapshots(isolated_url).outcomes(
        tenant, 30, None, None
    ).backlog == []
    with psycopg.connect(isolated_url) as connection:
        versions = connection.execute(
            """SELECT resource_version, execution_state
                 FROM ai_admin_control_resource_versions
                WHERE tenant_id = %s AND resource_type = 'IMPROVEMENT_BACKLOG'
                ORDER BY resource_version""",
            (tenant,),
        ).fetchall()
    assert versions == [(1, "SUCCEEDED"), (2, "ROLLED_BACK")]


def test_internal_emergency_stop_mutates_the_authoritative_ai_policy(
    isolated_url: str,
) -> None:
    tenant = 800_000_000 + uuid4().int % 90_000_000
    with psycopg.connect(isolated_url) as connection:
        connection.execute(
            """INSERT INTO ai_execution_policies (
                   tenant_id, allowed_model_routes, max_output_tokens_per_request,
                   budget_enforcement_mode, updated_by)
               VALUES (%s, %s::jsonb, 900, 'ALERT_ONLY', 'test-bootstrap')""",
            (tenant, '[{"provider":"OPENAI","model":"gpt-verified"}]'),
        )
    store = AdminControlPlaneStore(isolated_url)
    command = store.create(
        tenant_id=tenant,
        actor_user_id="maker-user",
        correlation_id="emergency-stop",
        auth_session_id="session-emergency",
        request=_request(
            "EMERGENCY_STOP",
            "AI_EXECUTION_POLICY",
            "ASK_RUNTIME",
            expected_version=1,
            payload={},
        ),
    )
    _approve(store, tenant, command)
    outbox = PostgresTransactionalOutboxStore(isolated_url)
    lease = _claim(outbox, tenant)
    assert PostgresAdminControlCommandExecutor(isolated_url).process(lease) == "SUCCEEDED"
    outbox.acknowledge(lease)
    policy = PostgresAIControlStore(isolated_url).policy(tenant_id=str(tenant))
    assert policy.emergency_disabled is True
    assert policy.policy_version == 2


def test_internal_draft_simulation_budget_incident_and_evidence_are_real_resources(
    isolated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DWP_AI_RUNTIME_CONTROL_ENFORCEMENT_ENABLED", raising=False)
    tenant = 800_000_000 + uuid4().int % 90_000_000
    with psycopg.connect(isolated_url) as connection:
        connection.execute(
            """INSERT INTO ai_execution_policies (
                   tenant_id, allowed_model_routes, max_output_tokens_per_request,
                   budget_enforcement_mode, updated_by)
               VALUES (%s, %s::jsonb, 900, 'ALERT_ONLY', 'test-bootstrap')""",
            (tenant, '[{"provider":"OPENAI","model":"gpt-verified"}]'),
        )
    store = AdminControlPlaneStore(isolated_url)
    outbox = PostgresTransactionalOutboxStore(isolated_url)
    executor = PostgresAdminControlCommandExecutor(isolated_url)
    draft = _execute_approved(
        store, outbox, executor, tenant,
        _request(
            "MODEL_ROUTING_DRAFT_SAVE", "ROUTING_POLICY", "ASK_RUNTIME",
            expected_version=1,
            payload={"policyId": "ASK_RUNTIME", "draft": {"primary": "OPENAI:gpt-verified"}},
        ),
    )
    simulation = _execute_approved(
        store, outbox, executor, tenant,
        _request(
            "MODEL_ROUTE_SIMULATE", "ROUTING_SIMULATOR", "tenant",
            expected_version=0,
            payload={
                "requesterRole": "KNOWLEDGE_WORKER",
                "agentId": "DWP_ASSISTANT",
                "dataClassification": "INTERNAL",
                "estimatedTokens": 512,
                "modality": "TEXT",
                "constraints": ["KR_REGION"],
            },
        ),
    )
    assert simulation.expected_version == 1

    now = "2026-09-17T06:00:00Z"
    _seed_resource(
        isolated_url, tenant, "AI_INCIDENT", "incident-1", 2,
        {"incidentId": "incident-1", "title": "Provider latency", "severity": "SEV2",
         "state": "RECOVERED", "affectedRunCount": 3, "affectedUserCount": 2,
         "scope": "ASK_RUNTIME", "ownerRef": "ai-platform", "correlationId": "corr-1",
         "openedAt": now, "updatedAt": now, "version": 2, "timeline": []},
        draft.command_id,
    )
    _seed_resource(
        isolated_url, tenant, "DRIFT_SIGNAL", "signal-1", 4,
        {"signalId": "signal-1", "label": "Quality drift", "severity": "WARNING",
         "currentValue": 0.7, "threshold": 0.8, "affectedScope": "ASK_RUNTIME",
         "detectedAt": now, "anonymizedSample": None, "feedbackEvidenceRef": None,
         "approvedRawAccess": False, "rollbackRecommendation": "Keep canary isolated."},
        draft.command_id,
    )
    budget = _execute_approved(
        store, outbox, executor, tenant,
        _request(
            "TOKEN_BUDGET_UPDATE", "TOKEN_BUDGET", "ASK_RUNTIME",
            expected_version=1, payload={"budgetTokens": 2000, "policyMode": "BLOCK"},
        ),
    )
    incident = _execute_approved(
        store, outbox, executor, tenant,
        _request(
            "INCIDENT_CLOSE", "AI_INCIDENT", "incident-1", expected_version=2,
            payload={"correlationId": "corr-1", "resolutionSummary": "Validation passed."},
        ),
    )
    evidence = _execute_approved(
        store, outbox, executor, tenant,
        _request(
            "DRIFT_EVIDENCE_ATTACH", "DRIFT_SIGNAL", "signal-1",
            expected_version=0, payload={"signalId": "signal-1"},
        ),
    )
    assert evidence.expected_version == 4
    snapshots = AdminControlPlaneSnapshots(isolated_url)
    outcomes = snapshots.outcomes(tenant, 30, None, None)
    token_budget = outcomes.token_budgets[0]
    assert token_budget.version == 2
    assert token_budget.budget_tokens == 2000
    assert token_budget.policy_mode == "BLOCK"
    assert token_budget.enforcement_activation_state.value == "DISABLED"
    assert outcomes.capability.status.value == "PARTIAL"
    assert outcomes.capability.reason and "do not currently block" in outcomes.capability.reason
    policy = PostgresAIControlStore(isolated_url).policy(tenant_id=str(tenant))
    assert policy.period_token_limit == 2000
    assert policy.budget_enforcement_mode.value == "ENFORCED"
    with pytest.raises(AIControlDenied, match="AI_TOKEN_BUDGET_HARD_LIMIT"):
        PostgresAIControlStore(isolated_url).reserve(
            tenant_id=str(tenant),
            run_id=str(uuid4()),
            attempt_generation=1,
            policy_version=policy.policy_version,
            requested_tokens=2001,
            now=datetime.now(UTC),
        )
    throttled = _execute_approved(
        store,
        outbox,
        executor,
        tenant,
        _request(
            "TOKEN_BUDGET_UPDATE",
            "TOKEN_BUDGET",
            "ASK_RUNTIME",
            expected_version=2,
            payload={"budgetTokens": 1500, "policyMode": "THROTTLE"},
        ),
    )
    throttled_policy = PostgresAIControlStore(isolated_url).policy(
        tenant_id=str(tenant)
    )
    assert throttled.receipt is not None
    assert throttled_policy.budget_enforcement_mode.value == "THROTTLED"
    with pytest.raises(AIControlDenied, match="AI_TOKEN_BUDGET_THROTTLED"):
        PostgresAIControlStore(isolated_url).reserve(
            tenant_id=str(tenant),
            run_id=str(uuid4()),
            attempt_generation=1,
            policy_version=throttled_policy.policy_version,
            requested_tokens=1501,
            now=datetime.now(UTC),
        )
    assert snapshots.incidents(tenant).incidents[0].state == "CLOSED"
    assert budget.receipt and incident.receipt and evidence.receipt
    with psycopg.connect(isolated_url) as connection:
        resource_types = {
            row[0] for row in connection.execute(
                "SELECT resource_type FROM ai_admin_control_resources WHERE tenant_id = %s",
                (tenant,),
            ).fetchall()
        }
    assert {"ROUTING_POLICY_DRAFT", "MODEL_ROUTE_SIMULATION", "DRIFT_EVIDENCE"} <= resource_types


def test_unconfigured_external_adapter_fails_truthfully_without_a_domain_mutation(
    isolated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DWP_ADMIN_CONTROL_AGENT_REGISTRY_URL", raising=False)
    monkeypatch.delenv("DWP_ADMIN_CONTROL_AGENT_REGISTRY_TOKEN", raising=False)
    tenant = 800_000_000 + uuid4().int % 90_000_000
    store = AdminControlPlaneStore(isolated_url)
    command = store.create(
        tenant_id=tenant,
        actor_user_id="maker-user",
        correlation_id="connector-create",
        auth_session_id="session-connector",
        request=_request(
            "AGENT_PROMOTE", "AGENT_REVISION", f"DWP_ASSISTANT:{uuid4()}",
            expected_version=7, payload={"rolloutPercent": 10},
        ),
    )
    _approve(store, tenant, command)
    outbox = PostgresTransactionalOutboxStore(isolated_url)
    lease = _claim(outbox, tenant)
    assert PostgresAdminControlCommandExecutor(isolated_url).process(lease) == "FAILED"
    outbox.acknowledge(lease)
    failed = store.get(
        tenant_id=tenant, actor_user_id="maker-user", command_id=command.command_id
    )
    assert failed.problem and failed.problem.code == "ADMIN_ADAPTER_NOT_CONFIGURED"
    with psycopg.connect(isolated_url) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM ai_admin_control_resources WHERE tenant_id = %s",
            (tenant,),
        ).fetchone()[0]
    assert count == 0


def test_evaluation_comparison_is_blocked_until_pii_review_passes(
    isolated_url: str,
) -> None:
    tenant = 800_000_000 + uuid4().int % 90_000_000
    dataset_id = f"dataset-{uuid4()}"
    store = AdminControlPlaneStore(isolated_url)
    outbox = PostgresTransactionalOutboxStore(isolated_url)
    executor = PostgresAdminControlCommandExecutor(isolated_url)
    source = _execute_approved(
        store,
        outbox,
        executor,
        tenant,
        _request(
            "BACKLOG_CREATE",
            "IMPROVEMENT_BACKLOG",
            f"source-{uuid4()}",
            expected_version=0,
            payload={
                "title": "Seed evaluation integration evidence",
                "ownerTeam": "AI Safety",
                "priority": "P2",
                "metricEvidence": "integration:test",
                "problemCluster": "EVALUATION",
                "targetValue": "PII gate enforced",
                "linkedRelease": None,
                "state": "PROPOSED",
            },
        ),
    )
    _seed_resource(
        isolated_url,
        tenant,
        "EVALUATION_DATASET",
        dataset_id,
        1,
        {
            "datasetId": dataset_id,
            "name": "Governed evaluation cases",
            "version": 1,
            "ownerRef": "ai-safety",
            "caseCount": 25,
            "piiState": "PENDING",
            "checksumSha256": "a" * 64,
            "updatedAt": datetime.now(UTC).isoformat(),
        },
        source.command_id,
    )
    comparison_request = _request(
        "EVALUATION_COMPARE",
        "EVALUATION_DATASET",
        dataset_id,
        expected_version=1,
        payload={
            "datasetId": dataset_id,
            "baseline": "baseline-v1",
            "candidate": "candidate-v2",
            "promptVersion": "prompt-v3",
            "policyVersion": "policy-v4",
            "toolVersion": "tools-v5",
            "evaluatorVersion": "evaluator-v6",
        },
    )
    with pytest.raises(AdminControlPlaneDenied, match="PII PASS"):
        store.create(
            tenant_id=tenant,
            actor_user_id="maker-user",
            correlation_id="comparison-before-pii",
            auth_session_id="session-maker",
            request=comparison_request,
        )

    pii_command = _execute_approved(
        store,
        outbox,
        executor,
        tenant,
        _request(
            "DATASET_PII_DECIDE",
            "EVALUATION_DATASET",
            dataset_id,
            expected_version=1,
            payload={
                "decision": "PASS",
                "evidenceRefs": ["evidence:pii-review"],
                "reviewerNote": "Redaction and retention evidence verified.",
            },
        ),
    )
    assert pii_command.receipt is not None
    accepted = store.create(
        tenant_id=tenant,
        actor_user_id="maker-user",
        correlation_id="comparison-after-pii",
        auth_session_id="session-maker",
        request=comparison_request.model_copy(
            update={"command_id": uuid4(), "expected_version": 2}
        ),
    )
    assert accepted.state.value == "AWAITING_APPROVAL"
    blocked = _execute_approved(
        store,
        outbox,
        executor,
        tenant,
        _request(
            "DATASET_PII_DECIDE",
            "EVALUATION_DATASET",
            dataset_id,
            expected_version=2,
            payload={
                "decision": "BLOCKED",
                "evidenceRefs": ["evidence:pii-revoked"],
                "reviewerNote": "New evidence invalidated the approved dataset.",
            },
        ),
    )
    assert blocked.receipt is not None
    _approve(store, tenant, accepted)
    comparison_lease = _claim(outbox, tenant)

    class NeverCalledEvaluationAdapter:
        def execute(self, **_: object):
            raise AssertionError("PII-blocked evaluation must not reach the provider.")

    comparison_executor = PostgresAdminControlCommandExecutor(
        isolated_url,
        adapters={"evaluation": NeverCalledEvaluationAdapter()},
    )
    assert comparison_executor.process(comparison_lease) == "FAILED"
    outbox.acknowledge(comparison_lease)
    failed = store.get(
        tenant_id=tenant,
        actor_user_id="maker-user",
        command_id=accepted.command_id,
    )
    assert failed.problem is not None
    assert failed.problem.code == "EVALUATION_DATASET_PII_NOT_APPROVED"


def test_evaluation_provider_call_holds_dataset_fence_against_pii_revocation(
    isolated_url: str,
) -> None:
    tenant = 800_000_000 + uuid4().int % 90_000_000
    dataset_id = f"dataset-{uuid4()}"
    store = AdminControlPlaneStore(isolated_url)
    outbox = PostgresTransactionalOutboxStore(isolated_url)
    internal_executor = PostgresAdminControlCommandExecutor(isolated_url)
    source = _execute_approved(
        store,
        outbox,
        internal_executor,
        tenant,
        _request(
            "BACKLOG_CREATE",
            "IMPROVEMENT_BACKLOG",
            f"source-{uuid4()}",
            expected_version=0,
            payload={
                "title": "Seed evaluation fence evidence",
                "ownerTeam": "AI Safety",
                "priority": "P1",
                "metricEvidence": "integration:pii-fence",
                "problemCluster": "EVALUATION",
                "targetValue": "PII decision cannot race provider execution",
                "linkedRelease": None,
                "state": "PROPOSED",
            },
        ),
    )
    approved_snapshot = {
        "datasetId": dataset_id,
        "name": "Approved evaluation cases",
        "version": 1,
        "ownerRef": "ai-safety",
        "caseCount": 25,
        "piiState": "PASS",
        "checksumSha256": "a" * 64,
        "updatedAt": datetime.now(UTC).isoformat(),
    }
    _seed_resource(
        isolated_url,
        tenant,
        "EVALUATION_DATASET",
        dataset_id,
        1,
        approved_snapshot,
        source.command_id,
    )
    command = store.create(
        tenant_id=tenant,
        actor_user_id="maker-user",
        correlation_id="evaluation-pii-fence",
        auth_session_id="session-maker",
        request=_request(
            "EVALUATION_RUN",
            "EVALUATION_DATASET",
            dataset_id,
            expected_version=1,
            payload={
                "datasetId": dataset_id,
                "datasetVersion": 1,
                "pinned": True,
            },
        ),
    )
    _approve(store, tenant, command)
    lease = _claim(outbox, tenant)

    blocked_snapshot = {
        **approved_snapshot,
        "version": 2,
        "piiState": "BLOCKED",
        "updatedAt": datetime.now(UTC).isoformat(),
    }
    codec = GovernedPayloadCodec()
    blocked_envelope = codec.encrypt_json(
        blocked_snapshot,
        tenant_id=tenant,
        resource_type="admin-control-resource",
        resource_id=f"EVALUATION_DATASET:{dataset_id}",
        field="snapshot",
    )
    blocked_digest = hashlib.sha256(canonical_json_bytes(blocked_snapshot)).hexdigest()

    class FencedEvaluationAdapter:
        update_was_fenced = False

        def execute(self, **kwargs: object) -> AdminCommandAdapterResponse:
            context = kwargs["context"]
            try:
                with psycopg.connect(isolated_url, autocommit=True) as connection:
                    connection.execute("SET lock_timeout = '250ms'")
                    connection.execute(
                        """UPDATE ai_admin_control_resources
                              SET resource_version = 2, snapshot_hash = %s,
                                  snapshot_envelope = %s, updated_at = CURRENT_TIMESTAMP
                            WHERE tenant_id = %s
                              AND resource_type = 'EVALUATION_DATASET'
                              AND resource_id = %s""",
                        (blocked_digest, blocked_envelope, tenant, dataset_id),
                    )
            except psycopg.errors.LockNotAvailable:
                self.update_was_fenced = True
            else:
                raise AssertionError(
                    "PII revocation must not commit while the provider call is in flight."
                )
            completed_at = datetime.now(UTC).isoformat()
            typed_result = {
                "runId": str(context.command_id),  # type: ignore[union-attr]
                "datasetId": dataset_id,
                "datasetVersion": 1,
                "pinned": True,
                "comparisonId": f"comparison:{context.command_id}",  # type: ignore[union-attr]
                "preservePinnedVersions": None,
                "state": "COMPLETED",
                "resultArtifactSha256": "b" * 64,
                "evidenceDigest": "c" * 64,
                "completedAt": completed_at,
            }
            result_snapshot = {
                "schemaVersion": 1,
                "commandId": str(context.command_id),  # type: ignore[union-attr]
                "attemptId": str(context.attempt_id),  # type: ignore[union-attr]
                "tenantId": context.tenant_id,  # type: ignore[union-attr]
                "correlationId": context.correlation_id,  # type: ignore[union-attr]
                "kind": context.kind.value,  # type: ignore[union-attr]
                "target": {
                    "type": context.target_type,  # type: ignore[union-attr]
                    "id": context.target_id,  # type: ignore[union-attr]
                },
                "expectedVersion": context.expected_target_version,  # type: ignore[union-attr]
                "requestPayloadSha256": hashlib.sha256(
                    canonical_json_bytes(context.payload)  # type: ignore[union-attr]
                ).hexdigest(),
                "resourceType": "EVALUATION_RUN",
                "resourceId": str(context.command_id),  # type: ignore[union-attr]
                "resultVersion": 1,
                "state": "COMPLETED",
                "outcome": "EVALUATION_COMPLETED",
                "providerReceiptId": f"evaluation:run:{context.command_id}",  # type: ignore[union-attr]
                "completedAt": completed_at,
                "evidenceRefs": ["evidence:pii-fence"],
                "result": typed_result,
                "resultSha256": hashlib.sha256(
                    canonical_json_bytes(typed_result)
                ).hexdigest(),
            }
            return AdminCommandAdapterResponse(
                command_id=context.command_id,  # type: ignore[union-attr]
                tenant_id=context.tenant_id,  # type: ignore[union-attr]
                correlation_id=context.correlation_id,  # type: ignore[union-attr]
                attempt_id=context.attempt_id,  # type: ignore[union-attr]
                kind=context.kind,  # type: ignore[union-attr]
                target={  # type: ignore[arg-type]
                    "type": context.target_type,  # type: ignore[union-attr]
                    "id": context.target_id,  # type: ignore[union-attr]
                },
                expected_version=context.expected_target_version,  # type: ignore[union-attr]
                state=GovernedCommandState.SUCCEEDED,
                result_summary="Evaluation run was accepted by the provider.",
                domain_receipt_ref=f"evaluation:run:{context.command_id}",  # type: ignore[union-attr]
                result_snapshot=result_snapshot,
                result_version=1,
                result_sha256=hashlib.sha256(
                    canonical_json_bytes(result_snapshot)
                ).hexdigest(),
            )

    adapter = FencedEvaluationAdapter()
    executor = PostgresAdminControlCommandExecutor(
        isolated_url,
        adapters={"evaluation": adapter},
    )
    assert executor.process(lease) == "SUCCEEDED"
    assert adapter.update_was_fenced is True
    outbox.acknowledge(lease)

    with psycopg.connect(isolated_url) as connection:
        updated = connection.execute(
            """UPDATE ai_admin_control_resources
                  SET resource_version = 2, snapshot_hash = %s,
                      snapshot_envelope = %s, updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = %s AND resource_type = 'EVALUATION_DATASET'
                  AND resource_id = %s
            RETURNING resource_version""",
            (blocked_digest, blocked_envelope, tenant, dataset_id),
        ).fetchone()
    assert updated == (2,)


class _TransientConnectorAdapter:
    def execute(self, **_: object):
        raise AdminCommandExecutionTransient("connector temporarily unavailable")


def test_transient_adapter_exhaustion_dead_letters_and_closes_the_command(
    isolated_url: str,
) -> None:
    tenant = 800_000_000 + uuid4().int % 90_000_000
    store = AdminControlPlaneStore(isolated_url)
    command = store.create(
        tenant_id=tenant,
        actor_user_id="maker-user",
        correlation_id="connector-retry",
        auth_session_id="session-retry",
        request=_request(
            "CONNECTOR_CREATE", "CONNECTOR", f"connector-{uuid4()}",
            expected_version=0, payload={"providerType": "EXTERNAL"},
        ),
    )
    _approve(store, tenant, command)
    outbox = PostgresTransactionalOutboxStore(isolated_url)
    executor = PostgresAdminControlCommandExecutor(
        isolated_url, adapters={"connector": _TransientConnectorAdapter()}
    )
    first = _claim(outbox, tenant)
    with pytest.raises(AdminCommandExecutionTransient):
        executor.process(first)
    assert outbox.retry(
        first, safe_error_code="CONNECTOR_TRANSIENT", retry_after_seconds=1,
        maximum_attempts=2,
    ) == "PENDING"
    with psycopg.connect(isolated_url) as connection:
        connection.execute(
            "UPDATE ai_transactional_outbox SET available_at = CURRENT_TIMESTAMP WHERE outbox_id = %s",
            (first.outbox_id,),
        )
    second = _claim(outbox, tenant)
    with pytest.raises(AdminCommandExecutionTransient):
        executor.process(second)
    assert outbox.retry(
        second, safe_error_code="CONNECTOR_TRANSIENT", retry_after_seconds=1,
        maximum_attempts=2,
    ) == "DEAD_LETTER"
    executor.fail_dead_letter(second, safe_error_code="ADMIN_EXECUTION_RETRY_EXHAUSTED")
    failed = store.get(
        tenant_id=tenant, actor_user_id="maker-user", command_id=command.command_id
    )
    assert failed.state.value == "FAILED"
    assert failed.problem and failed.problem.code == "ADMIN_EXECUTION_RETRY_EXHAUSTED"
    with psycopg.connect(isolated_url) as connection:
        state = connection.execute(
            "SELECT state FROM ai_transactional_outbox WHERE outbox_id = %s",
            (second.outbox_id,),
        ).fetchone()[0]
    assert state == "DEAD_LETTER"
