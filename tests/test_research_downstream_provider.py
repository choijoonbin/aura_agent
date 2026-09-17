from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from dwp_agent.canonical_json import canonical_json_bytes
from dwp_agent.dwaion_workflow_contracts import (
    AttachmentCitation,
    ResearchDeliveryType,
    ResearchProgress,
    ResearchResult,
    ResearchRun,
    ResearchRunState,
)
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.governed_worker_runtime import (
    register_governed_worker_heartbeat,
    remove_governed_worker_heartbeat,
)
from dwp_agent.research_delivery_capabilities import research_delivery_capabilities
from dwp_agent.research_downstream_provider import (
    HttpResearchDownstreamProvider,
    ResearchDownstreamContext,
    ResearchDownstreamProviderError,
    normalize_research_downstream_parameters,
    research_downstream_capability,
    validate_research_downstream_observation_receipt,
)
from dwp_agent.main import app


def _configure(monkeypatch: pytest.MonkeyPatch, delivery_type: str) -> None:
    monkeypatch.setenv(
        f"DWP_RESEARCH_{delivery_type}_PROVIDER_URL",
        f"https://research-broker.internal/v1/{delivery_type.lower()}",
    )
    monkeypatch.setenv(
        f"DWP_RESEARCH_{delivery_type}_PROVIDER_TOKEN",
        "research-downstream-test-token-123456",
    )
    monkeypatch.setenv(
        "DWP_RESEARCH_DOWNSTREAM_ALLOWED_HOSTS", "research-broker.internal"
    )


def _context(delivery_type: ResearchDeliveryType) -> ResearchDownstreamContext:
    report = "# Verified research\n\nA receipt-backed report."
    evidence = "Verified evidence excerpt."
    run_id = uuid4()
    now = datetime.now(UTC)
    parameters: dict[str, object]
    if delivery_type == ResearchDeliveryType.HANDOFF:
        parameters = {
            "locale": "ko-KR",
            "approvalTarget": "finance-approvers",
            "requestTitle": "시장 조사 결과 검토",
            "requestReason": "검증된 조사 결과를 결재 요청으로 인계합니다.",
            "requestMetadata": {"priority": "NORMAL"},
        }
    else:
        parameters = {
            "locale": "ko-KR",
            "recipientIds": ["member-2"],
            "teamId": "strategy-team",
            "permission": "COMMENT",
            "expiresAt": (now + timedelta(days=7)).isoformat(),
        }
    return ResearchDownstreamContext(
        identity=PersonalDomainIdentity(
            tenant_id=71,
            user_id="member-1",
            correlation_id="research-provider-test",
            auth_session_id="session-1",
            roles=frozenset({"WORKSPACE_MEMBER"}),
            permissions=frozenset({"APP.DWAION_RESEARCH:VIEW"}),
        ),
        run=ResearchRun(
            run_id=run_id,
            plan_id=uuid4(),
            plan_revision=2,
            state=ResearchRunState.COMPLETED,
            version=4,
            progress=ResearchProgress(
                completed_steps=3,
                total_steps=3,
                discovered_sources=2,
                verified_citations=1,
            ),
            result=ResearchResult(
                report_markdown=report,
                result_sha256=hashlib.sha256(report.encode()).hexdigest(),
                citations=[
                    AttachmentCitation(
                        citation_id="source-1",
                        locator="page:1",
                        label="Source 1",
                        evidence=evidence,
                        content_sha256=hashlib.sha256(evidence.encode()).hexdigest(),
                    )
                ],
            ),
            receipt_id=uuid4(),
            started_at=now - timedelta(minutes=2),
            created_at=now - timedelta(minutes=3),
            updated_at=now,
            completed_at=now,
        ),
        delivery_id=uuid4(),
        delivery_type=delivery_type,
        parameters=parameters,
    )


def _parameter_receipt_binding(context: ResearchDownstreamContext) -> dict[str, object]:
    parameters = normalize_research_downstream_parameters(
        context.delivery_type, context.parameters
    )
    if context.delivery_type == ResearchDeliveryType.HANDOFF:
        effect = {
            "effectType": "HANDOFF",
            "approvalTarget": parameters["approvalTarget"],
            "requestTitle": parameters["requestTitle"],
            "requestReason": parameters["requestReason"],
            "requestMetadata": parameters.get("requestMetadata", {}),
        }
    else:
        effect = {
            "effectType": "SHARE",
            "recipientIds": parameters.get("recipientIds", []),
            "teamId": parameters.get("teamId"),
            "permission": parameters["permission"],
            "expiresAt": parameters["expiresAt"],
        }
    return {
        "parametersSha256": hashlib.sha256(
            canonical_json_bytes(parameters)
        ).hexdigest(),
        "effect": effect,
    }


def test_handoff_provider_sends_content_and_accepts_only_bound_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, "HANDOFF")
    context = _context(ResearchDeliveryType.HANDOFF)
    assert context.run.result is not None
    target_id = uuid4()
    receipt_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["authorization"] == (
            "Bearer research-downstream-test-token-123456"
        )
        assert request.headers["x-dwp-idempotency-key"] == str(context.delivery_id)
        assert request.headers["x-dwp-result-sha256"] == (
            context.run.result.result_sha256
        )
        assert body["parameters"]["approvalTarget"] == "finance-approvers"
        assert body["result"]["reportMarkdown"].startswith("# Verified research")
        assert body["result"]["citations"][0]["citationId"] == "source-1"
        return httpx.Response(
            200,
            json={
                "data": {
                    "deliveryId": str(context.delivery_id),
                    "runId": str(context.run.run_id),
                    "deliveryType": "HANDOFF",
                    "receiptId": str(receipt_id),
                    "providerReceiptId": "approval-request-receipt-71",
                    "targetId": str(target_id),
                    "targetPath": f"/approvals/requests/{target_id}",
                    "resultSha256": context.run.result.result_sha256,
                    **_parameter_receipt_binding(context),
                    "acceptedAt": datetime.now(UTC).isoformat(),
                }
            },
        )

    receipt = HttpResearchDownstreamProvider(
        ResearchDeliveryType.HANDOFF,
        transport=httpx.MockTransport(handler),
    ).deliver(context)

    assert receipt.receipt_id == receipt_id
    assert receipt.target_id == target_id
    assert research_downstream_capability(ResearchDeliveryType.HANDOFF).available


def test_share_provider_rejects_receipt_for_another_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, "SHARE")
    context = _context(ResearchDeliveryType.SHARE)
    target_id = uuid4()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "deliveryId": str(context.delivery_id),
                "runId": str(context.run.run_id),
                "deliveryType": "SHARE",
                "receiptId": str(uuid4()),
                "providerReceiptId": "share-receipt-71",
                "targetId": str(target_id),
                "targetPath": f"/collaboration/shares/{target_id}",
                "resultSha256": "f" * 64,
                **_parameter_receipt_binding(context),
                "acceptedAt": datetime.now(UTC).isoformat(),
            },
        )

    with pytest.raises(
        ResearchDownstreamProviderError,
        match="RESEARCH_SHARE_PROVIDER_BINDING_INVALID",
    ):
        HttpResearchDownstreamProvider(
            ResearchDeliveryType.SHARE,
            transport=httpx.MockTransport(handler),
        ).deliver(context)


@pytest.mark.parametrize(
    ("delivery_type", "effect_update"),
    (
        (ResearchDeliveryType.HANDOFF, {"approvalTarget": "other-approvers"}),
        (ResearchDeliveryType.SHARE, {"recipientIds": ["member-other"]}),
    ),
)
def test_downstream_receipt_binds_the_reviewed_effect_parameters(
    monkeypatch: pytest.MonkeyPatch,
    delivery_type: ResearchDeliveryType,
    effect_update: dict[str, object],
) -> None:
    _configure(monkeypatch, delivery_type.value)
    context = _context(delivery_type)
    assert context.run.result is not None
    target_id = uuid4()
    receipt_id = uuid4()
    binding = _parameter_receipt_binding(context)
    receipt = {
        "deliveryId": str(context.delivery_id), "runId": str(context.run.run_id),
        "deliveryType": delivery_type.value, "receiptId": str(receipt_id),
        "providerReceiptId": f"provider:{delivery_type.value.lower()}:receipt",
        "targetId": str(target_id),
        "targetPath": f"/research/{delivery_type.value.lower()}/{target_id}",
        "resultSha256": context.run.result.result_sha256,
        **binding,
        "effect": {**binding["effect"], **effect_update},
        "acceptedAt": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(ResearchDownstreamProviderError, match="BINDING_INVALID"):
        HttpResearchDownstreamProvider(
            delivery_type,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=receipt)),
        ).deliver(context)
    with pytest.raises(ValueError, match="binding"):
        validate_research_downstream_observation_receipt(
            delivery_id=context.delivery_id, run_id=context.run.run_id,
            delivery_type=delivery_type,
            result_sha256=context.run.result.result_sha256, receipt_id=receipt_id,
            receipt={"schemaVersion": 2, "targetType": delivery_type.value, **receipt},
            created_at=datetime.now(UTC), parameters=context.parameters,
        )


def test_downstream_receipt_rejects_substring_target_and_fabricated_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, "HANDOFF")
    context = _context(ResearchDeliveryType.HANDOFF)
    assert context.run.result is not None
    target_id = uuid4()
    receipt_id = uuid4()
    receipt = {
        "deliveryId": str(context.delivery_id), "runId": str(context.run.run_id),
        "deliveryType": "HANDOFF", "receiptId": str(receipt_id),
        "providerReceiptId": "approval-receipt-1", "targetId": str(target_id),
        "targetPath": f"/approvals/requests/prefix-{target_id}-suffix",
        "resultSha256": context.run.result.result_sha256,
        **_parameter_receipt_binding(context),
        "acceptedAt": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(ResearchDownstreamProviderError, match="BINDING_INVALID"):
        HttpResearchDownstreamProvider(
            ResearchDeliveryType.HANDOFF,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=receipt)),
        ).deliver(context)

    with pytest.raises(ValueError, match="schema"):
        validate_research_downstream_observation_receipt(
            delivery_id=context.delivery_id, run_id=context.run.run_id,
            delivery_type=ResearchDeliveryType.HANDOFF,
            result_sha256=context.run.result.result_sha256, receipt_id=receipt_id,
            receipt={"fabricated": True}, created_at=datetime.now(UTC),
            parameters=context.parameters,
        )


def test_generic_research_delivery_observation_endpoint_is_not_exposed() -> None:
    async def post() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/internal/v1/research/runs/{uuid4()}/deliveries/{uuid4()}/observations",
                json={
                    "commandId": str(uuid4()), "state": "COMPLETED",
                    "receiptId": str(uuid4()), "receipt": {"fabricated": True},
                },
            )

    assert asyncio.run(post()).status_code == 404


def test_unfenced_research_run_observation_endpoint_is_not_exposed() -> None:
    async def post() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/internal/v1/research/runs/{uuid4()}/observations",
                json={
                    "commandId": str(uuid4()),
                    "expectedVersion": 1,
                    "state": "COMPLETED",
                    "progress": {
                        "completedSteps": 1, "totalSteps": 1,
                        "discoveredSources": 1, "verifiedCitations": 1,
                    },
                    "result": {"fabricated": True},
                },
            )

    assert asyncio.run(post()).status_code == 404


def test_provider_timeout_is_retryable_failure_without_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, "HANDOFF")
    context = _context(ResearchDeliveryType.HANDOFF)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout", request=request)

    with pytest.raises(
        ResearchDownstreamProviderError,
        match="RESEARCH_HANDOFF_PROVIDER_UNREACHABLE",
    ):
        HttpResearchDownstreamProvider(
            ResearchDeliveryType.HANDOFF,
            transport=httpx.MockTransport(handler),
        ).deliver(context)


def test_capability_and_parameters_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DWP_RESEARCH_SHARE_PROVIDER_URL", raising=False)
    monkeypatch.delenv("DWP_RESEARCH_SHARE_PROVIDER_TOKEN", raising=False)
    capability = research_downstream_capability(ResearchDeliveryType.SHARE)
    assert capability.available is False
    assert capability.configured is False
    assert capability.reason_code == "RESEARCH_SHARE_PROVIDER_NOT_CONFIGURED"

    monkeypatch.setenv("DWP_GOVERNED_WORKERS_ENABLED", "true")
    register_governed_worker_heartbeat("RESEARCH_DELIVERY")
    try:
        identity = _context(ResearchDeliveryType.SHARE).identity
        combined = research_delivery_capabilities("", identity)
        assert combined.share.available is False
        assert combined.share.reason_code == "RESEARCH_SHARE_PROVIDER_NOT_CONFIGURED"
    finally:
        remove_governed_worker_heartbeat("RESEARCH_DELIVERY")

    with pytest.raises(ValueError, match="recipient or team"):
        normalize_research_downstream_parameters(
            ResearchDeliveryType.SHARE,
            {
                "locale": "en-US",
                "recipientIds": [],
                "permission": "VIEW",
                "expiresAt": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
    with pytest.raises(ValueError, match="Extra inputs"):
        normalize_research_downstream_parameters(
            ResearchDeliveryType.HANDOFF,
            {
                "locale": "en-US",
                "approvalTarget": "finance",
                "requestTitle": "Review",
                "requestReason": "Review the verified result before approval.",
                "unexpected": True,
            },
        )
