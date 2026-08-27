import json
import os
from datetime import datetime, timezone
from uuid import UUID

import pytest
from psycopg import connect

from dwp_agent.contracts import (
    AgentRegistryResolution,
    AnswerConfidence,
    AnswerFeedbackRequest,
    AskCitation,
    AskModelRoute,
    AskPolicyDecision,
    AskResponse,
    CitationSourceType,
    ConversationMessage,
    ConversationRole,
    ModelRouteState,
    PolicyOutcome,
    RegistryResolutionStatus,
    RegistryRiskTier,
    RiskTier,
)
from dwp_agent.envelope import PayloadEncryption, load_payload_encryption
from dwp_agent.evaluation_store import PostgresEvaluationStore
from dwp_agent.governance_contracts import (
    CreateEvaluationCaseRequest,
    CreateEvaluationSetRequest,
)
from dwp_agent.payload_contexts import (
    legacy_conversation_aad,
    legacy_evaluation_aad,
    legacy_message_aad,
    legacy_run_aad,
)
from dwp_agent.postgres_conversation_store import PostgresConversationStore
from dwp_agent.run_store import PostgresRunStore, RunStart, _apply_migrations


DATABASE_URL = os.getenv("DWP_AGENT_ENVELOPE_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_ENVELOPE_TEST_DATABASE_URL is not configured.",
)


def test_v13_writes_envelopes_and_reads_legacy_rows() -> None:
    _apply_migrations(DATABASE_URL)
    encryption = load_payload_encryption()

    _verify_v2_writes(encryption)
    _verify_legacy_reads(encryption)


def _verify_v2_writes(encryption: PayloadEncryption) -> None:
    run_id = "00000000-0000-0000-0000-000000001101"
    response = response_for(run_id=run_id, request_id="v2-request")
    run_store = PostgresRunStore(DATABASE_URL, encryption)
    lease = run_store.begin(
        RunStart(
            run_id=run_id,
            tenant_id="942",
            user_id="v2-user",
            request_id="v2-request",
            query_hash="a" * 64,
            agent_key="DWP_ASSISTANT",
            agent_revision=1,
            risk_tier="L1",
            policy_outcome="ALLOW",
            locale="ko-KR",
            correlation_id="v2-correlation",
        )
    )
    assert lease is not None

    with connect(DATABASE_URL) as connection:
        connection.execute(
            """INSERT INTO ai_conversation_retention_policies (tenant_id, retention_days)
               VALUES (942, 90) ON CONFLICT (tenant_id) DO NOTHING"""
        )
    conversation_store = PostgresConversationStore(DATABASE_URL, encryption)
    conversation_id = conversation_store.ensure(
        tenant_id="942",
        user_id="v2-user",
        conversation_id=None,
        locale="ko-KR",
        initial_query="V2 conversation",
    )
    user_message_id, assistant_message_id = conversation_store.append_exchange(
        tenant_id="942",
        user_id="v2-user",
        conversation_id=conversation_id,
        request_id="v2-request",
        query="V2 conversation",
        response=response,
        lease=lease,
    )
    response = response.model_copy(
        update={
            "conversation_id": conversation_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
        }
    )
    run_store.complete(response, lease=lease, tenant_id="942", user_id="v2-user")
    assert run_store.load("942", "v2-user", "v2-request", "a" * 64) == response
    assert len(
        conversation_store.get(
            tenant_id="942", user_id="v2-user", conversation_id=conversation_id
        ).messages
    ) == 2
    conversation_store.feedback(
        tenant_id="942",
        user_id="v2-user",
        run_id=UUID(run_id),
        request=AnswerFeedbackRequest(
            rating="UP", reason_codes=["GROUNDED"], comment="Useful answer"
        ),
    )

    evaluation_store = PostgresEvaluationStore(DATABASE_URL, encryption)
    evaluation_set = evaluation_store.create_set(
        tenant_id="942",
        actor_user_id="v2-user",
        correlation_id="v2-correlation",
        request=CreateEvaluationSetRequest(name="V2 evaluation"),
    )
    evaluation = evaluation_store.add_case(
        tenant_id="942",
        actor_user_id="v2-user",
        correlation_id="v2-correlation",
        evaluation_set_id=evaluation_set.summary.evaluation_set_id,
        request=CreateEvaluationCaseRequest(
            name="Grounded response",
            prompt="What is next?",
            expected_terms=["meeting"],
            source_scopes=[CitationSourceType.CALENDAR],
        ),
    )
    assert evaluation.cases[0].prompt == "What is next?"

    with connect(DATABASE_URL) as connection:
        envelope_count = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_agent_runs
                     WHERE run_id = %s AND response_envelope LIKE 'dwp2.%%'
                       AND response_nonce IS NULL AND response_key_version IS NULL)
                 + (SELECT COUNT(*) FROM ai_conversations
                     WHERE conversation_id = %s AND title_envelope LIKE 'dwp2.%%'
                       AND title_nonce IS NULL AND encryption_key_version IS NULL)
                 + (SELECT COUNT(*) FROM ai_conversation_messages
                     WHERE conversation_id = %s AND payload_envelope LIKE 'dwp2.%%'
                       AND payload_nonce IS NULL AND encryption_key_version IS NULL)
                 + (SELECT COUNT(*) FROM ai_answer_feedback
                     WHERE run_id = %s AND comment_envelope LIKE 'dwp2.%%'
                       AND comment_nonce IS NULL AND encryption_key_version IS NULL)
                 + (SELECT COUNT(*) FROM ai_evaluation_cases
                     WHERE evaluation_set_id = %s
                       AND prompt_envelope LIKE 'dwp2.%%'
                       AND expected_terms_envelope LIKE 'dwp2.%%'
                       AND prompt_nonce IS NULL AND encryption_key_version IS NULL)""",
            (
                UUID(run_id),
                conversation_id,
                conversation_id,
                UUID(run_id),
                evaluation_set.summary.evaluation_set_id,
            ),
        ).fetchone()[0]
    assert envelope_count == 6


def _verify_legacy_reads(encryption: PayloadEncryption) -> None:
    legacy = encryption.legacy_keyring
    run_id = "00000000-0000-0000-0000-000000001201"
    request_id = "legacy-request"
    response = response_for(run_id=run_id, request_id=request_id)
    version, nonce, ciphertext = legacy.encrypt_bytes(
        response.model_dump_json(by_alias=True).encode("utf-8"),
        legacy_run_aad("943", "legacy-user", request_id, run_id),
    )
    conversation_id = UUID("00000000-0000-0000-0000-000000001202")
    message_id = UUID("00000000-0000-0000-0000-000000001203")
    title_version, title_nonce, title_ciphertext = legacy.encrypt_bytes(
        b"Legacy conversation",
        legacy_conversation_aad("943", "legacy-user", conversation_id, "title"),
    )
    message = ConversationMessage(
        message_id=message_id,
        role=ConversationRole.USER,
        content="Legacy question",
        created_at=datetime.now(timezone.utc),
    )
    message_version, message_nonce, message_ciphertext = legacy.encrypt_bytes(
        message.model_dump_json(by_alias=True).encode("utf-8"),
        legacy_message_aad(message_id),
    )
    evaluation_set_id = UUID("00000000-0000-0000-0000-000000001204")
    evaluation_case_id = UUID("00000000-0000-0000-0000-000000001205")
    prompt_version, prompt_nonce, prompt_ciphertext = legacy.encrypt_bytes(
        b"Legacy prompt",
        legacy_evaluation_aad("943", evaluation_set_id, evaluation_case_id, "prompt"),
    )
    expected_version, expected_nonce, expected_ciphertext = legacy.encrypt_bytes(
        json.dumps(["legacy"]).encode("utf-8"),
        legacy_evaluation_aad("943", evaluation_set_id, evaluation_case_id, "expected"),
    )
    assert len(
        {
            version,
            title_version,
            message_version,
            prompt_version,
            expected_version,
        }
    ) == 1

    with connect(DATABASE_URL) as connection:
        connection.execute(
            """INSERT INTO ai_agent_runs (
                   run_id, tenant_id, user_id, request_id, query_hash, agent_key,
                   agent_revision, run_state, answer_state, risk_tier, policy_outcome,
                   status_code, locale, correlation_id, response_key_version,
                   response_nonce, response_ciphertext, completed_at, lease_generation)
               VALUES (%s, 943, 'legacy-user', %s, %s, 'DWP_ASSISTANT', 1,
                       'COMPLETED', 'COMPLETED', 'L1', 'ALLOW', 'ANSWER_GROUNDED',
                       'ko-KR', 'legacy-correlation', %s, %s, %s,
                       CURRENT_TIMESTAMP, 1)""",
            (UUID(run_id), request_id, "b" * 64, version, nonce, ciphertext),
        )
        connection.execute(
            """INSERT INTO ai_conversations (
                   conversation_id, tenant_id, user_id, locale, title_nonce,
                   title_ciphertext, encryption_key_version, message_count,
                   retention_until)
               VALUES (%s, 943, 'legacy-user', 'ko-KR', %s, %s, %s, 1,
                       CURRENT_TIMESTAMP + INTERVAL '90 days')""",
            (conversation_id, title_nonce, title_ciphertext, title_version),
        )
        connection.execute(
            """INSERT INTO ai_conversation_messages (
                   message_id, conversation_id, request_id, run_id, role,
                   payload_nonce, payload_ciphertext, encryption_key_version,
                   created_at, lease_generation)
               VALUES (%s, %s, %s, %s, 'USER', %s, %s, %s, %s, 1)""",
            (
                message_id,
                conversation_id,
                request_id,
                UUID(run_id),
                message_nonce,
                message_ciphertext,
                message_version,
                message.created_at,
            ),
        )
        connection.execute(
            """INSERT INTO ai_evaluation_sets (
                   evaluation_set_id, tenant_id, name, created_by, updated_by)
               VALUES (%s, 943, 'Legacy evaluation', 'legacy-user', 'legacy-user')""",
            (evaluation_set_id,),
        )
        connection.execute(
            """INSERT INTO ai_evaluation_cases (
                   evaluation_case_id, tenant_id, evaluation_set_id, name,
                   prompt_nonce, prompt_ciphertext, expected_terms_nonce,
                   expected_terms_ciphertext, encryption_key_version, source_scopes,
                   created_by)
               VALUES (%s, 943, %s, 'Legacy case', %s, %s, %s, %s, %s,
                       '[\"CALENDAR\"]'::jsonb, 'legacy-user')""",
            (
                evaluation_case_id,
                evaluation_set_id,
                prompt_nonce,
                prompt_ciphertext,
                expected_nonce,
                expected_ciphertext,
                prompt_version,
            ),
        )

    assert PostgresRunStore(DATABASE_URL, encryption).load(
        "943", "legacy-user", request_id, "b" * 64
    ) == response
    conversation = PostgresConversationStore(DATABASE_URL, encryption).get(
        tenant_id="943", user_id="legacy-user", conversation_id=conversation_id
    )
    assert conversation.summary.title == "Legacy conversation"
    assert conversation.messages == [message]
    evaluation = PostgresEvaluationStore(DATABASE_URL, encryption).detail(
        tenant_id="943", evaluation_set_id=evaluation_set_id
    )
    assert evaluation.cases[0].prompt == "Legacy prompt"
    assert evaluation.cases[0].expected_terms == ["legacy"]


def response_for(*, run_id: str, request_id: str) -> AskResponse:
    return AskResponse(
        run_id=run_id,
        audit_id="audit-1",
        request_id=request_id,
        correlation_id="correlation-1",
        state="COMPLETED",
        answer="Your next meeting starts at 15:00.",
        confidence=AnswerConfidence.HIGH,
        citations=[
            AskCitation(
                source_id="src-01",
                source_type=CitationSourceType.CALENDAR,
                title="Project review",
                source_system="DWP Calendar",
            )
        ],
        source_count=1,
        policy=AskPolicyDecision(
            outcome=PolicyOutcome.ALLOW,
            risk_tier=RiskTier.L1,
            code="ASK_READ_ALLOWED",
            explanation="Read-only query is permitted.",
            model_allowed=True,
        ),
        model_route=AskModelRoute(
            state=ModelRouteState.COMPLETED,
            provider="OPENAI",
            model="gpt-test",
        ),
        agent_registry=AgentRegistryResolution(
            entry_key="DWP_ASSISTANT",
            revision=1,
            artifact_version="test",
            risk_tier=RegistryRiskTier.MEDIUM,
            resolution=RegistryResolutionStatus.ACTIVE,
        ),
        status_code="ANSWER_GROUNDED",
        completed_at=datetime.now(timezone.utc),
    )
