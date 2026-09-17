from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from dwp_agent.ai_control_contracts import MeasurementFreshness
from dwp_agent.ai_control_runtime import AIControlConflict, AIControlDenied
from dwp_agent.ask_warning_contracts import AskRuntimeWarning
from dwp_agent.contracts import (
    AgentRegistryResolution,
    AskModelRoute,
    AskPolicyDecision,
    AskResponse,
    ModelRouteState,
    PolicyOutcome,
    RegistryResolutionStatus,
    RegistryRiskTier,
    RiskTier,
)
from dwp_agent.ai_control_store import PostgresAIControlStore
from dwp_agent.database_migrations import apply_migrations
from dwp_agent.envelope import load_payload_encryption
from dwp_agent.run_store import PostgresRunStore
from dwp_agent.run_store_types import RunStart


@pytest.mark.integration
def test_missing_model_usage_remains_charged_until_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    schema = "test_ai_control_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in database_url else "?"
    isolated_url = database_url + separator + "options=" + quote(f"-csearch_path={schema}")
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)
    run_id = str(uuid4())
    now = datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc)
    try:
        apply_migrations(isolated_url)
        with psycopg.connect(isolated_url) as connection:
            connection.execute(
                """INSERT INTO ai_execution_policies (
                       tenant_id, allowed_model_routes, allowed_knowledge_sources,
                       max_output_tokens_per_request, budget_enforcement_mode,
                       period_token_limit, updated_by)
                   VALUES (%s, %s::jsonb, %s::jsonb, 256, 'ENFORCED', 10000, 'test')""",
                (
                    int(tenant_id),
                    '[{"provider":"OPENAI","model":"gpt-test"}]',
                    '["WORK_ITEM"]',
                ),
            )
        store = PostgresAIControlStore(isolated_url)
        reservation = store.reserve(
            tenant_id=tenant_id,
            run_id=run_id,
            attempt_generation=2,
            policy_version=1,
            requested_tokens=900,
            now=now,
        )
        with pytest.raises(AIControlConflict, match="not found for this tenant"):
            store.settle(
                tenant_id=str(int(tenant_id) + 1),
                reservation_id=reservation.reservation_id,
                run_id=run_id,
                attempt_generation=2,
                provider="OPENAI",
                model="gpt-test",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                usage_observed=True,
                now=now,
            )
        store.settle(
            tenant_id=tenant_id,
            reservation_id=reservation.reservation_id,
            run_id=run_id,
            attempt_generation=2,
            provider="OPENAI",
            model="gpt-test",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            usage_observed=False,
            now=now,
        )

        observed = store.usage(tenant_id=tenant_id, now=now)
        overview = store.overview(tenant_id=tenant_id, now=now)
        with pytest.raises(AIControlConflict, match="already admitted"):
            store.reserve(
                tenant_id=tenant_id,
                run_id=run_id,
                attempt_generation=2,
                policy_version=1,
                requested_tokens=900,
                now=now,
            )
        with psycopg.connect(isolated_url) as connection:
            row = connection.execute(
                """SELECT state, reserved_tokens, attempt_generation
                     FROM ai_runtime_usage_reservations
                    WHERE tenant_id = %s AND run_id = %s""",
                (int(tenant_id), run_id),
            ).fetchone()

        assert row == ("MEASUREMENT_MISSING", 900, 2)
        assert observed.reserved_tokens == 900
        assert observed.unmeasured_reserved_tokens == 900
        assert observed.measurement_freshness == MeasurementFreshness.UNAVAILABLE
        assert "MODEL_USAGE_MEASUREMENT_MISSING" in overview.warnings
        assert overview.tool_enforcement_state == "NOT_CONNECTED"

        expired = store.reserve(
            tenant_id=tenant_id,
            run_id=str(uuid4()),
            attempt_generation=3,
            policy_version=1,
            requested_tokens=600,
            now=now,
        )
        with psycopg.connect(isolated_url) as connection:
            connection.execute(
                """UPDATE ai_runtime_usage_reservations SET expires_at = %s
                    WHERE reservation_id = %s""",
                (now, expired.reservation_id),
            )
        after_expiry = store.usage(tenant_id=tenant_id, now=now + timedelta(seconds=1))
        assert after_expiry.reserved_tokens == 1_500
        assert after_expiry.unmeasured_reserved_tokens == 1_500

        race_tenant_id = str(int(tenant_id) + 2)
        race_run_id = str(uuid4())
        with psycopg.connect(isolated_url) as connection:
            connection.execute(
                """INSERT INTO ai_execution_policies (
                       tenant_id, allowed_model_routes, allowed_knowledge_sources,
                       max_output_tokens_per_request, budget_enforcement_mode,
                       period_token_limit, updated_by)
                   VALUES (%s, %s::jsonb, %s::jsonb, 256, 'ENFORCED', 1000, 'test')""",
                (
                    int(race_tenant_id),
                    '[{"provider":"OPENAI","model":"gpt-test"}]',
                    '["WORK_ITEM"]',
                ),
            )
        race_reservation = store.reserve(
            tenant_id=race_tenant_id,
            run_id=race_run_id,
            attempt_generation=1,
            policy_version=1,
            requested_tokens=900,
            now=now,
        )
        admission_entered = threading.Event()
        continue_admission = threading.Event()
        settlement_started = threading.Event()
        settlement_done = threading.Event()
        admission_errors: list[Exception] = []
        settlement_errors: list[Exception] = []

        class PausingAdmissionStore(PostgresAIControlStore):
            def _usage(self, connection, tenant: int, observed_at: datetime):
                if tenant == int(race_tenant_id):
                    admission_entered.set()
                    if not continue_admission.wait(timeout=5):
                        raise RuntimeError("Timed out waiting to continue budget admission.")
                return super()._usage(connection, tenant, observed_at)

        pausing_store = PausingAdmissionStore(isolated_url)

        def admit_concurrently() -> None:
            try:
                pausing_store.reserve(
                    tenant_id=race_tenant_id,
                    run_id=str(uuid4()),
                    attempt_generation=2,
                    policy_version=1,
                    requested_tokens=200,
                    now=now,
                )
            except Exception as error:  # captured for assertion in the test thread
                admission_errors.append(error)

        def settle_concurrently() -> None:
            settlement_started.set()
            try:
                store.settle(
                    tenant_id=race_tenant_id,
                    reservation_id=race_reservation.reservation_id,
                    run_id=race_run_id,
                    attempt_generation=1,
                    provider="OPENAI",
                    model="gpt-test",
                    input_tokens=700,
                    output_tokens=200,
                    total_tokens=900,
                    usage_observed=True,
                    now=now + timedelta(seconds=1),
                )
            except Exception as error:  # captured for assertion in the test thread
                settlement_errors.append(error)
            finally:
                settlement_done.set()

        admission_thread = threading.Thread(target=admit_concurrently)
        settlement_thread = threading.Thread(target=settle_concurrently)
        admission_thread.start()
        assert admission_entered.wait(timeout=5)
        settlement_thread.start()
        assert settlement_started.wait(timeout=5)
        assert not settlement_done.wait(timeout=0.2)
        continue_admission.set()
        admission_thread.join(timeout=5)
        settlement_thread.join(timeout=5)

        assert not admission_thread.is_alive()
        assert not settlement_thread.is_alive()
        assert settlement_errors == []
        assert len(admission_errors) == 1
        assert isinstance(admission_errors[0], AIControlDenied)
        assert admission_errors[0].code == "AI_TOKEN_BUDGET_HARD_LIMIT"
        race_usage = store.usage(tenant_id=race_tenant_id, now=now + timedelta(seconds=1))
        assert race_usage.measured_total_tokens == 900
        assert race_usage.reserved_tokens == 0

        monkeypatch.setenv("DWP_ENVIRONMENT", "local")
        monkeypatch.setenv("DWP_AGENT_KEY_PROVIDER", "local-inline")
        monkeypatch.setenv(
            "DWP_AGENT_DATA_KEY", "ZHdwLWxvY2FsLWFnZW50LWRhdGEta2V5LTMyYnl0ZSE="
        )
        monkeypatch.setenv("DWP_AGENT_DATA_KEY_VERSION", "ai-warning-test-v1")
        warning_run_id = str(uuid4())
        warning_request_id = "ai-control-warning-replay"
        query_hash = "a" * 64
        run_store = PostgresRunStore(isolated_url, load_payload_encryption())
        lease = run_store.begin(RunStart(
            run_id=warning_run_id,
            tenant_id=race_tenant_id,
            user_id="warning-user",
            request_id=warning_request_id,
            query_hash=query_hash,
            agent_key="DWP_ASSISTANT",
            agent_revision=1,
            risk_tier="L1",
            policy_outcome="ALLOW",
            locale="en",
            correlation_id="warning-correlation",
        ))
        assert lease is not None
        response = AskResponse(
            run_id=warning_run_id,
            audit_id="warning-audit",
            request_id=warning_request_id,
            correlation_id="warning-correlation",
            state="ABSTAINED",
            source_count=0,
            policy=AskPolicyDecision(
                outcome=PolicyOutcome.DENY,
                risk_tier=RiskTier.L1,
                code="ASK_POLICY_DENIED",
                explanation="A persisted warning response used by the integration test.",
                model_allowed=False,
            ),
            model_route=AskModelRoute(state=ModelRouteState.NOT_INVOKED),
            agent_registry=AgentRegistryResolution(
                entry_key="DWP_ASSISTANT",
                revision=1,
                artifact_version="ai-warning-test",
                risk_tier=RegistryRiskTier.MEDIUM,
                resolution=RegistryResolutionStatus.ACTIVE,
            ),
            status_code="ASK_POLICY_DENIED",
            completed_at=now,
            warnings=[AskRuntimeWarning.MODEL_USAGE_MEASUREMENT_MISSING],
        )
        run_store.complete(
            response,
            lease=lease,
            tenant_id=race_tenant_id,
            user_id="warning-user",
        )
        replay = run_store.load(
            race_tenant_id, "warning-user", warning_request_id, query_hash
        )
        with psycopg.connect(isolated_url) as connection:
            envelope = connection.execute(
                "SELECT response_envelope FROM ai_agent_runs WHERE run_id = %s",
                (warning_run_id,),
            ).fetchone()[0]
        assert envelope.startswith("dwp2.")
        assert "MODEL_USAGE_MEASUREMENT_MISSING" not in envelope
        assert replay is not None
        assert replay.warnings == [AskRuntimeWarning.MODEL_USAGE_MEASUREMENT_MISSING]
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
