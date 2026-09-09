from __future__ import annotations

import base64
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier, Lock
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent import ask_runtime as ask_runtime_module
from dwp_agent.ask_runtime import AskRuntime
from dwp_agent.context_broker import GroundedContext
from dwp_agent.conversation_store import ConversationNotFound, ConversationRetentionLocked
from dwp_agent.contracts import (
    AgentRegistryResolution,
    AskModelRoute,
    AskPolicyDecision,
    AskRequest,
    AskResponse,
    ModelRouteState,
    PolicyOutcome,
    RegistryResolutionStatus,
    RegistryRiskTier,
    RiskTier,
)
from dwp_agent.policy import AskIdentity
from dwp_agent.postgres_conversation_store import PostgresConversationStore
from dwp_agent.run_store import (
    PostgresRunStore,
    RequestIdConflict,
    RunInProgress,
    RunStart,
    RunStoreUnavailable,
    _apply_migrations,
)


class LeaseTestEncryption:
    def encrypt_bytes(self, *_args, **_kwargs) -> str:
        return "dwp2.lease-test-envelope"

    def decrypt_bytes(self, *_args, **_kwargs):
        raise AssertionError("Expired lease lookup must not decrypt payloads.")


class ReversibleLeaseTestEncryption:
    prefix = "dwp2.lease-test."

    def encrypt_bytes(self, payload: bytes, *_args, **_kwargs) -> str:
        return self.prefix + base64.urlsafe_b64encode(payload).decode("ascii")

    def decrypt_bytes(self, *, envelope: str | None, **_kwargs) -> bytes:
        assert envelope is not None and envelope.startswith(self.prefix)
        return base64.urlsafe_b64decode(envelope.removeprefix(self.prefix))


class CoordinatedPostgresRunStore(PostgresRunStore):
    def __init__(self, database_url: str, encryption: ReversibleLeaseTestEncryption) -> None:
        super().__init__(database_url, encryption)  # type: ignore[arg-type]
        self._initial_load_barrier = Barrier(2)
        self._load_count = 0
        self._load_lock = Lock()

    def load(self, *args, **kwargs):
        result = super().load(*args, **kwargs)
        with self._load_lock:
            wait_for_competitor = self._load_count < 2
            self._load_count += 1
        if wait_for_competitor:
            self._initial_load_barrier.wait(timeout=5)
        return result


class EmptyContextBroker:
    def collect(self, *_args, **_kwargs) -> GroundedContext:
        return GroundedContext(sources=(), attempted_sources=(), unavailable_sources=())


@pytest.mark.integration
def test_expired_ask_lease_is_reclaimed_without_replacing_the_run_identity() -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(800_000_000 + uuid4().int % 100_000_000)
    first = RunStart(
        run_id=str(uuid4()),
        tenant_id=tenant_id,
        user_id="lease-user",
        request_id="lease-request",
        query_hash="a" * 64,
        agent_key="DWP_ASSISTANT",
        agent_revision=1,
        risk_tier="L1",
        policy_outcome="ALLOW",
        locale="ko-KR",
        correlation_id="lease-first",
    )
    retry = replace(first, run_id=str(uuid4()), correlation_id="lease-retry")
    store = PostgresRunStore(database_url, LeaseTestEncryption())  # type: ignore[arg-type]

    try:
        first_lease = store.begin(first)
        assert first_lease is not None
        assert (first_lease.run_id, first_lease.generation) == (first.run_id, 1)
        with pytest.raises(RunInProgress):
            store.load(tenant_id, first.user_id, first.request_id, first.query_hash)
        assert store.begin(retry) is None

        with connect(database_url) as connection:
            connection.execute(
                """UPDATE ai_agent_runs
                      SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                    WHERE run_id = %s""",
                (first.run_id,),
            )

        assert store.load(tenant_id, first.user_id, first.request_id, first.query_hash) is None
        claim_barrier = Barrier(3)

        def reclaim_expired_lease():
            claim_barrier.wait()
            return store.begin(retry)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = [executor.submit(reclaim_expired_lease) for _ in range(2)]
            claim_barrier.wait()
            claim_results = [claim.result() for claim in claims]

        assert sum(claim is not None for claim in claim_results) == 1
        retry_lease = next(claim for claim in claim_results if claim is not None)
        assert retry_lease is not None
        assert retry_lease.run_id == first.run_id
        assert retry_lease.generation == first_lease.generation + 1
        with pytest.raises(RequestIdConflict):
            store.begin(replace(retry, query_hash="b" * 64))

        store.fail(first_lease, "STALE_WORKER_FAILED")
        stale_response = _abstained_response(first.run_id, first.request_id)
        with pytest.raises(RunStoreUnavailable, match="could not be completed"):
            store.complete(
                stale_response,
                lease=first_lease,
                tenant_id=tenant_id,
                user_id=first.user_id,
            )

        with connect(database_url) as connection:
            row = connection.execute(
                """SELECT COUNT(*), MIN(run_id::text), MIN(correlation_id),
                          MIN(run_state), MIN(lease_generation), MIN(safe_error_code)
                     FROM ai_agent_runs
                    WHERE tenant_id = %s AND user_id = %s AND request_id = %s""",
                (int(tenant_id), first.user_id, first.request_id),
            ).fetchone()
        assert row == (1, first.run_id, "lease-retry", "RUNNING", 2, None)

        store.complete(
            stale_response,
            lease=retry_lease,
            tenant_id=tenant_id,
            user_id=first.user_id,
        )
        with connect(database_url) as connection:
            completed = connection.execute(
                """SELECT run_state, lease_generation, lease_expires_at,
                          response_envelope
                     FROM ai_agent_runs
                    WHERE run_id = %s""",
                (first.run_id,),
            ).fetchone()
        assert completed == ("COMPLETED", 2, None, "dwp2.lease-test-envelope")
    finally:
        _delete_tenant_test_data(database_url, tenant_id)


@pytest.mark.integration
def test_conversation_visibility_is_fenced_by_the_completed_lease_generation() -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(800_000_000 + uuid4().int % 100_000_000)
    user_id = "lease-conversation-user"
    request_id = "lease-conversation-request"
    first = _run_start(tenant_id, user_id, request_id)
    retry = replace(first, run_id=str(uuid4()), correlation_id="lease-conversation-retry")
    encryption = ReversibleLeaseTestEncryption()
    run_store = PostgresRunStore(database_url, encryption)  # type: ignore[arg-type]
    conversation_store = PostgresConversationStore(database_url, encryption)  # type: ignore[arg-type]

    try:
        _insert_retention_policy(database_url, tenant_id)
        first_lease = run_store.begin(first)
        assert first_lease is not None
        conversation_id = conversation_store.ensure(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=None,
            locale="ko-KR",
            initial_query="리스 가시성",
        )
        stale_response = _abstained_response(first_lease.run_id, request_id)
        stale_pair = conversation_store.append_exchange(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            request_id=request_id,
            query="리스 가시성",
            response=stale_response,
            lease=first_lease,
        )
        assert len(stale_pair) == 2
        _assert_conversation_state(
            conversation_store, tenant_id, user_id, conversation_id, count=0
        )

        _expire_run(database_url, first_lease.run_id)
        retry_lease = run_store.begin(retry)
        assert retry_lease is not None
        assert retry_lease.run_id == first_lease.run_id
        assert retry_lease.generation == first_lease.generation + 1
        _assert_conversation_state(
            conversation_store, tenant_id, user_id, conversation_id, count=0
        )
        with pytest.raises(RunStoreUnavailable, match="could not be completed"):
            run_store.complete(
                stale_response,
                lease=first_lease,
                tenant_id=tenant_id,
                user_id=user_id,
            )

        active_response = _abstained_response(retry_lease.run_id, request_id)
        active_pair = conversation_store.append_exchange(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            request_id=request_id,
            query="리스 가시성",
            response=active_response,
            lease=retry_lease,
        )
        active_response = active_response.model_copy(
            update={
                "conversation_id": conversation_id,
                "user_message_id": active_pair[0],
                "assistant_message_id": active_pair[1],
            }
        )
        _assert_conversation_state(
            conversation_store, tenant_id, user_id, conversation_id, count=0
        )
        run_store.complete(
            active_response,
            lease=retry_lease,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        detail = _assert_conversation_state(
            conversation_store, tenant_id, user_id, conversation_id, count=2
        )
        assert [message.content for message in detail.messages] == [
            "리스 가시성",
            "ASK_POLICY_DENIED",
        ]
        assert detail.summary.agent_key == "DWP_ASSISTANT"
        assert detail.summary.source_systems == []
        assert detail.summary.evidence_count == 0
        assert detail.summary.summary_excerpt == "ASK_POLICY_DENIED"
        assert detail.summary.last_answer_status == "ASK_POLICY_DENIED"
        assert detail.summary.retention_until is not None
        assert detail.summary.legal_hold is False
        listed = conversation_store.list(tenant_id=tenant_id, user_id=user_id)
        assert listed == [detail.summary]
        with connect(database_url) as connection:
            connection.execute(
                """UPDATE ai_conversation_retention_policies
                      SET legal_hold = TRUE
                    WHERE tenant_id = %s""",
                (int(tenant_id),),
            )
        assert conversation_store.list(
            tenant_id=tenant_id, user_id=user_id
        )[0].legal_hold is True
        with pytest.raises(ConversationRetentionLocked):
            conversation_store.delete(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        assert run_store.load(tenant_id, user_id, request_id, first.query_hash) == active_response
        with connect(database_url) as connection:
            row = connection.execute(
                """SELECT COUNT(*), MIN(lease_generation), MAX(lease_generation),
                          COUNT(DISTINCT run_id)
                     FROM ai_conversation_messages
                    WHERE conversation_id = %s""",
                (conversation_id,),
            ).fetchone()
        assert row == (2, retry_lease.generation, retry_lease.generation, 1)
    finally:
        _delete_tenant_test_data(database_url, tenant_id)


@pytest.mark.integration
def test_concurrent_begin_loser_does_not_create_an_orphan_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(800_000_000 + uuid4().int % 100_000_000)
    user_id = "lease-orphan-user"
    request_id = "lease-orphan-request"
    encryption = ReversibleLeaseTestEncryption()
    run_store = CoordinatedPostgresRunStore(database_url, encryption)
    conversation_store = PostgresConversationStore(database_url, encryption)  # type: ignore[arg-type]
    runtime = AskRuntime(
        context_broker=EmptyContextBroker(),  # type: ignore[arg-type]
        run_store=run_store,
        conversation_store=conversation_store,
    )
    request = AskRequest(
        request_id=request_id,
        query="What needs my attention?",
        locale="en",
    )
    identity = AskIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=("WORKSPACE_MEMBER",),
        permissions=("APP.ASK:VIEW",),
        correlation_id="lease-orphan-correlation",
    )
    monkeypatch.setenv("DWP_AGENT_PRIVACY_HASH_SECRET", "lease-test-privacy-secret")
    monkeypatch.setattr(
        ask_runtime_module,
        "resolve_agent",
        lambda *_args, **_kwargs: _active_agent(),
    )

    try:
        _insert_retention_policy(database_url, tenant_id)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(runtime.answer, request, identity=identity) for _ in range(2)]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except RunInProgress:
                    pass

        assert outcomes
        assert all(outcome == outcomes[0] for outcome in outcomes)
        with connect(database_url) as connection:
            conversation_count = connection.execute(
                """SELECT COUNT(*) FROM ai_conversations
                    WHERE tenant_id = %s AND user_id = %s""",
                (int(tenant_id), user_id),
            ).fetchone()[0]
        assert conversation_count == 1
    finally:
        _delete_tenant_test_data(database_url, tenant_id)


def _integration_database_url() -> str:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Ask lease integration tests require a dedicated test database.")
    return database_url


def _run_start(tenant_id: str, user_id: str, request_id: str) -> RunStart:
    return RunStart(
        run_id=str(uuid4()),
        tenant_id=tenant_id,
        user_id=user_id,
        request_id=request_id,
        query_hash="a" * 64,
        agent_key="DWP_ASSISTANT",
        agent_revision=1,
        risk_tier="L1",
        policy_outcome="ALLOW",
        locale="ko-KR",
        correlation_id="lease-conversation-first",
    )


def _active_agent() -> AgentRegistryResolution:
    return AgentRegistryResolution(
        entry_key="DWP_ASSISTANT",
        revision=1,
        artifact_version="lease-test",
        risk_tier=RegistryRiskTier.MEDIUM,
        resolution=RegistryResolutionStatus.ACTIVE,
    )


def _insert_retention_policy(database_url: str, tenant_id: str) -> None:
    with connect(database_url) as connection:
        connection.execute(
            """INSERT INTO ai_conversation_retention_policies (tenant_id, retention_days)
               VALUES (%s, 90)
               ON CONFLICT (tenant_id) DO UPDATE SET retention_days = EXCLUDED.retention_days""",
            (int(tenant_id),),
        )


def _expire_run(database_url: str, run_id: str) -> None:
    with connect(database_url) as connection:
        connection.execute(
            """UPDATE ai_agent_runs
                  SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                WHERE run_id = %s""",
            (run_id,),
        )


def _assert_conversation_state(
    store: PostgresConversationStore,
    tenant_id: str,
    user_id: str,
    conversation_id,
    *,
    count: int,
):
    if count == 0:
        assert store.list(tenant_id=tenant_id, user_id=user_id) == []
        with pytest.raises(ConversationNotFound):
            store.get(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        with connect(store.database_url) as connection:
            stored_count = connection.execute(
                """SELECT message_count FROM ai_conversations
                    WHERE conversation_id = %s""",
                (conversation_id,),
            ).fetchone()[0]
        assert stored_count == 0
        return None
    detail = store.get(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    assert detail.summary.message_count == count
    assert len(detail.messages) == count
    return detail


def _delete_tenant_test_data(database_url: str, tenant_id: str) -> None:
    with connect(database_url) as connection:
        connection.execute(
            "DELETE FROM ai_conversations WHERE tenant_id = %s", (int(tenant_id),)
        )
        connection.execute(
            "DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant_id),)
        )
        connection.execute(
            "DELETE FROM ai_conversation_retention_policies WHERE tenant_id = %s",
            (int(tenant_id),),
        )


def _abstained_response(run_id: str, request_id: str) -> AskResponse:
    return AskResponse(
        run_id=run_id,
        audit_id="lease-audit",
        request_id=request_id,
        correlation_id="lease-response",
        state="ABSTAINED",
        source_count=0,
        policy=AskPolicyDecision(
            outcome=PolicyOutcome.DENY,
            risk_tier=RiskTier.L1,
            code="ASK_POLICY_DENIED",
            explanation="The request was denied for a lease test.",
            model_allowed=False,
        ),
        model_route=AskModelRoute(state=ModelRouteState.NOT_INVOKED),
        agent_registry=_active_agent(),
        status_code="ASK_POLICY_DENIED",
        completed_at=datetime.now(timezone.utc),
    )
