from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from dwp_agent.context_broker import GroundedContext
from dwp_agent.main import app
from dwp_agent.proposal_analysis import (
    ProposalAnalysisService,
    set_proposal_analysis_service_for_tests,
)
from dwp_agent.proposal_analysis_commands import InMemoryProposalAnalysisCommandStore
from dwp_agent.proposal_analysis_control import (
    InMemoryProposalAnalysisControl,
    set_proposal_analysis_control_for_tests,
)
from dwp_agent.proposal_analysis_fingerprints import ProposalAnalysisFingerprints
from dwp_agent.proposal_privacy import (
    InMemoryProposalPrivacyService,
    set_proposal_privacy_service_for_tests,
)
from dwp_agent.proposal_store import InMemoryProposalStore, set_proposal_store_for_tests


SERVICE_TOKEN = "test-gateway-service-token"


class EmptyAnalysisBroker:
    def collect(self, *_args, **_kwargs) -> GroundedContext:
        return GroundedContext(
            sources=(),
            attempted_sources=("WORK_ITEM", "MAIL", "CALENDAR"),
            unavailable_sources=(),
        )


@pytest.fixture(autouse=True)
def configured_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    store = InMemoryProposalStore()
    fingerprints = ProposalAnalysisFingerprints.ephemeral()
    control = InMemoryProposalAnalysisControl(fingerprints)
    commands = InMemoryProposalAnalysisCommandStore(fingerprints)
    set_proposal_store_for_tests(store)
    set_proposal_analysis_control_for_tests(control)
    set_proposal_analysis_service_for_tests(
        ProposalAnalysisService(
            broker=EmptyAnalysisBroker(),
            fingerprints=fingerprints,
            control=control,
            commands=commands,
        )
    )
    set_proposal_privacy_service_for_tests(
        InMemoryProposalPrivacyService(store, commands)
    )
    yield store
    set_proposal_privacy_service_for_tests(None)
    set_proposal_analysis_service_for_tests(None)
    set_proposal_analysis_control_for_tests(None)
    set_proposal_store_for_tests(None)


async def request(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    user_id: str = "member-1",
    permissions: str = "APP.ASK:VIEW",
    identity_plane: str = "TENANT",
    auth_session_id: str = "proposal-session-1",
    accept_language: str = "en",
):
    transport = ASGITransport(app=app)
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-Tenant-ID": "42",
        "X-DWP-User-ID": user_id,
        "X-DWP-Identity-Plane": identity_plane,
        "X-DWP-Permissions": permissions,
        "X-Correlation-ID": f"proposal-{user_id}",
        "X-DWP-Auth-Session-ID": auth_session_id,
        "Accept-Language": accept_language,
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, json=body)


def proposal_body(*, target_user_id: str = "member-1", action: bool = True) -> dict:
    content = {
        "title": "프로젝트 위험 신호를 확인하세요",
        "summary": "마감 전 확인이 필요한 업무가 두 건 있습니다.",
        "rationale": "현재 마감일과 미완료 상태를 함께 분석했습니다.",
        "evidence": [
            {
                "sourceType": "WORK_ITEM",
                "referenceId": "work-100",
                "label": "고객 전환 계획 검토",
            }
        ],
        "actionInputs": (
            {"serviceCategory": "WORK_SUPPORT", "requestSummary": "위험 업무 지원 요청"}
            if action
            else {}
        ),
    }
    return {
        "commandId": str(uuid4()),
        "targetUserId": target_user_id,
        "sourceEventId": f"work-risk-{uuid4()}",
        "kind": "RISK",
        "priority": "HIGH",
        "agentKey": "DWP_ASSISTANT",
        "actionKey": "SERVICE.REQUEST.CREATE" if action else None,
        "content": content,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "changeReason": "미완료 업무 위험 신호를 사용자에게 제안합니다.",
    }


def create_proposal(body: dict):
    return asyncio.run(
        request(
            "POST",
            "/v1/admin/proposals",
            body=body,
            user_id="operator-1",
            permissions="ADMIN.DWAION_OPERATIONS:MANAGE",
        )
    )


def test_proposal_inbox_is_real_scoped_and_decisions_are_audited(
    configured_runtime: InMemoryProposalStore,
) -> None:
    created = create_proposal(proposal_body())
    proposal = created.json()["data"]

    inbox = asyncio.run(request("GET", "/v1/proposals"))
    other_inbox = asyncio.run(
        request("GET", "/v1/proposals", user_id="member-2")
    )
    accepted = asyncio.run(
        request(
            "POST",
            f"/v1/proposals/{proposal['proposalId']}/decisions",
            body={
                "commandId": str(uuid4()),
                "expectedRevision": 1,
                "decision": "ACCEPT",
            },
        )
    )
    handled = asyncio.run(request("GET", "/v1/proposals?view=HANDLED"))

    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    assert inbox.status_code == 200
    assert inbox.headers["Cache-Control"] == "no-store"
    assert inbox.json()["data"]["summary"] == {
        "active": 1,
        "highPriority": 1,
        "snoozed": 0,
        "handled": 0,
    }
    assert inbox.json()["data"]["items"][0]["content"]["title"].startswith("프로젝트")
    assert other_inbox.json()["data"]["items"] == []
    assert accepted.status_code == 200
    assert accepted.json()["data"]["proposal"]["state"] == "ACCEPTED"
    assert accepted.json()["data"]["actionReviewRequired"] is True
    assert handled.json()["data"]["items"][0]["revision"] == 2
    assert [event["eventType"] for event in configured_runtime.events] == [
        "CREATED",
        "ACCEPT",
    ]


def test_proposal_decision_is_idempotent_and_revision_guarded() -> None:
    proposal = create_proposal(proposal_body(action=False)).json()["data"]
    command_id = str(uuid4())
    decision = {
        "commandId": command_id,
        "expectedRevision": 1,
        "decision": "SNOOZE",
        "snoozeUntil": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
    }

    first = asyncio.run(
        request(
            "POST", f"/v1/proposals/{proposal['proposalId']}/decisions", body=decision
        )
    )
    replay = asyncio.run(
        request(
            "POST", f"/v1/proposals/{proposal['proposalId']}/decisions", body=decision
        )
    )
    replay_drift = asyncio.run(
        request(
            "POST",
            f"/v1/proposals/{proposal['proposalId']}/decisions",
            body={
                **decision,
                "snoozeUntil": (
                    datetime.now(timezone.utc) + timedelta(hours=4)
                ).isoformat(),
                "note": "같은 명령 키의 변경된 요청입니다.",
            },
        )
    )
    stale = asyncio.run(
        request(
            "POST",
            f"/v1/proposals/{proposal['proposalId']}/decisions",
            body={
                "commandId": str(uuid4()),
                "expectedRevision": 1,
                "decision": "DISMISS",
            },
        )
    )

    assert first.status_code == 200
    assert first.json()["data"]["proposal"]["state"] == "SNOOZED"
    assert replay.status_code == 200
    assert replay.json()["data"]["proposal"]["revision"] == 2
    assert replay_drift.status_code == 409
    assert stale.status_code == 409


@pytest.mark.parametrize(
    ("permissions", "identity_plane"),
    [
        ("APP.ASK:VIEW", "PROVIDER"),
        ("APP.WORK:VIEW", "TENANT"),
    ],
)
def test_proposal_inbox_fails_closed_for_wrong_plane_or_permission(
    permissions: str, identity_plane: str
) -> None:
    response = asyncio.run(
        request(
            "GET",
            "/v1/proposals",
            permissions=permissions,
            identity_plane=identity_plane,
        )
    )
    assert response.status_code == 403


def test_proposal_producer_validates_authority_action_and_cursor() -> None:
    denied = asyncio.run(
        request(
            "POST",
            "/v1/admin/proposals",
            body=proposal_body(),
            user_id="operator-1",
            permissions="APP.ASK:VIEW",
        )
    )
    invalid_body = proposal_body()
    invalid_body["actionKey"] = "UNKNOWN.ACTION"
    invalid = create_proposal(invalid_body)
    cursor = asyncio.run(request("GET", "/v1/proposals?cursor=not-a-cursor"))

    assert denied.status_code == 403
    assert invalid.status_code == 422
    assert cursor.status_code == 422


def test_proposal_source_event_is_idempotent_but_payload_drift_conflicts() -> None:
    body = proposal_body(action=False)
    first = create_proposal(body)
    replay = create_proposal({**body, "commandId": str(uuid4())})
    drift = {
        **body,
        "commandId": str(uuid4()),
        "content": {**body["content"], "summary": "변경된 요약"},
    }
    conflict = create_proposal(drift)
    command_drift = create_proposal(
        {**body, "changeReason": "동일 명령 키로 변경된 생성 사유를 제출합니다."}
    )

    assert replay.status_code == 201
    assert replay.json()["data"]["proposalId"] == first.json()["data"]["proposalId"]
    assert conflict.status_code == 409
    assert command_drift.status_code == 409


def test_analysis_api_replays_receipt_and_rejects_locale_drift() -> None:
    command_id = str(uuid4())
    first = asyncio.run(
        request(
            "POST",
            "/v1/proposals/analyze",
            body={"commandId": command_id},
            accept_language="en",
        )
    )
    replay = asyncio.run(
        request(
            "POST",
            "/v1/proposals/analyze",
            body={"commandId": command_id},
            accept_language="en",
        )
    )
    drift = asyncio.run(
        request(
            "POST",
            "/v1/proposals/analyze",
            body={"commandId": command_id},
            accept_language="ko",
        )
    )

    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json() == replay.json()
    assert drift.status_code == 409


def test_analysis_preference_and_clear_are_user_controlled_and_idempotent() -> None:
    preference = asyncio.run(request("GET", "/v1/proposals/preferences"))
    disabled = asyncio.run(
        request(
            "PUT",
            "/v1/proposals/preferences",
            body={
                "commandId": str(uuid4()),
                "expectedRevision": 0,
                "proactiveAnalysisEnabled": False,
            },
        )
    )
    blocked = asyncio.run(
        request(
            "POST",
            "/v1/proposals/analyze",
            body={"commandId": str(uuid4())},
        )
    )
    created = create_proposal(proposal_body(action=False))
    clear_command = str(uuid4())
    cleared = asyncio.run(
        request(
            "POST",
            "/v1/proposals/clear",
            body={"commandId": clear_command},
        )
    )
    replay = asyncio.run(
        request(
            "POST",
            "/v1/proposals/clear",
            body={"commandId": clear_command},
        )
    )

    assert preference.status_code == 200
    assert preference.json()["data"]["revision"] == 0
    assert disabled.status_code == 200
    assert disabled.json()["data"]["proactiveAnalysisEnabled"] is False
    assert blocked.status_code == 409
    assert created.status_code == 201
    assert cleared.status_code == 200
    assert cleared.json()["data"]["hiddenCount"] == 1
    assert replay.json() == cleared.json()
