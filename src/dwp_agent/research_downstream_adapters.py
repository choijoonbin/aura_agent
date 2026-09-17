from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from .dwaion_workflow_contracts import ResearchDeliveryType, ResearchRun
from .personal_domain_security import PersonalDomainIdentity
from .personal_routine_contracts import (
    CreateRoutineRequest,
    RoutineBudget,
    RoutineCadence,
    RoutineDefinition,
    RoutineSource,
)
from .personal_routine_postgres_store import PostgresPersonalRoutineStore
from .postgres_proposal_store import PostgresProposalStore
from .proposal_contracts import (
    CreateAgentProposalRequest,
    ProposalContent,
    ProposalEvidence,
    ProposalKind,
    ProposalPriority,
)
from .research_downstream_provider import (
    HttpResearchDownstreamProvider,
    ResearchDownstreamContext,
)


class ResearchDownstreamAdapter(Protocol):
    def deliver(
        self,
        identity: PersonalDomainIdentity,
        run: ResearchRun,
        delivery_id: UUID,
        parameters: dict[str, object],
    ) -> tuple[UUID, dict[str, object]]: ...


class ProposalResearchDownstreamAdapter:
    def __init__(self, database_url: str) -> None:
        self.store = PostgresProposalStore(database_url)

    def deliver(
        self,
        identity: PersonalDomainIdentity,
        run: ResearchRun,
        delivery_id: UUID,
        parameters: dict[str, object],
    ) -> tuple[UUID, dict[str, object]]:
        if run.result is None:
            raise ValueError("The research result is unavailable.")
        title = _title(run.result.report_markdown, run.run_id)
        summary = _summary(run.result.report_markdown)
        proposal = self.store.create(
            tenant_id=str(identity.tenant_id),
            actor_user_id=identity.user_id,
            correlation_id=identity.correlation_id,
            request=CreateAgentProposalRequest(
                command_id=uuid5(
                    NAMESPACE_URL,
                    f"urn:dwp:research-delivery:{delivery_id}:proposal-create",
                ),
                target_user_id=identity.user_id,
                source_event_id=f"research-delivery:{delivery_id}",
                kind=ProposalKind.INSIGHT,
                priority=ProposalPriority.MEDIUM,
                agent_key="DWAI_ON.DEEP_RESEARCH",
                content=ProposalContent(
                    title=title,
                    summary=summary,
                    rationale=(
                        "Created from a completed citation-backed Deep Research run "
                        f"and immutable delivery request {delivery_id}."
                    ),
                    action_inputs={
                        "researchRunId": str(run.run_id),
                        "researchPlanId": str(run.plan_id),
                    },
                    evidence=[
                        ProposalEvidence(
                            source_type="DEEP_RESEARCH",
                            reference_id=citation.citation_id,
                            label=citation.label,
                            occurred_at=run.completed_at,
                            route=f"/dwaion/deep-research?run={run.run_id}",
                        )
                        for citation in run.result.citations[:20]
                    ],
                ),
                expires_at=datetime.now(UTC) + timedelta(days=30),
                change_reason=(
                    "Deliver the completed Deep Research result into the governed proposal inbox."
                ),
            ),
        )
        return proposal.proposal_id, {
            "schemaVersion": 1,
            "targetType": "PROPOSAL",
            "proposalId": str(proposal.proposal_id),
            "proposalRevision": proposal.revision,
            "resultSha256": run.result.result_sha256,
            "targetPath": f"/dwaion/proposals?proposal={proposal.proposal_id}",
        }


class HttpResearchDownstreamAdapter:
    def __init__(
        self,
        target_type: ResearchDeliveryType,
        *,
        provider: HttpResearchDownstreamProvider | None = None,
    ) -> None:
        if target_type not in {
            ResearchDeliveryType.HANDOFF,
            ResearchDeliveryType.SHARE,
        }:
            raise ValueError("The downstream target type is unsupported.")
        self.target_type = target_type
        self.provider = provider or HttpResearchDownstreamProvider(target_type)

    def deliver(
        self,
        identity: PersonalDomainIdentity,
        run: ResearchRun,
        delivery_id: UUID,
        parameters: dict[str, object],
    ) -> tuple[UUID, dict[str, object]]:
        if run.result is None:
            raise ValueError("The research result is unavailable.")
        receipt = self.provider.deliver(
            ResearchDownstreamContext(
                identity=identity,
                run=run,
                delivery_id=delivery_id,
                delivery_type=self.target_type,
                parameters=parameters,
            )
        )
        value = receipt.model_dump(mode="json", by_alias=True)
        return receipt.receipt_id, {
            "schemaVersion": 2,
            "targetType": self.target_type.value,
            **value,
        }


class RoutineResearchDownstreamAdapter:
    """Creates an inactive, consent-gated monthly routine from completed research."""

    def __init__(self, database_url: str) -> None:
        self.store = PostgresPersonalRoutineStore(database_url)

    def deliver(
        self,
        identity: PersonalDomainIdentity,
        run: ResearchRun,
        delivery_id: UUID,
        parameters: dict[str, object],
    ) -> tuple[UUID, dict[str, object]]:
        if run.result is None:
            raise ValueError("The research result is unavailable.")
        worker_identity = PersonalDomainIdentity(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            correlation_id=identity.correlation_id,
            auth_session_id=identity.auth_session_id,
            roles=identity.roles,
            permissions=identity.permissions | frozenset({"APP.WORK:VIEW"}),
        )
        routine = self.store.create(
            worker_identity,
            CreateRoutineRequest(
                command_id=uuid5(
                    NAMESPACE_URL,
                    f"urn:dwp:research-delivery:{delivery_id}:routine-create",
                ),
                expected_revision=0,
                reason_code="RESEARCH_DELIVERY",
                definition=RoutineDefinition(
                    name=_title(run.result.report_markdown, run.run_id)[:80],
                    objective=(
                        "Refresh this citation-backed research result every month. "
                        + _summary(run.result.report_markdown)
                    )[:500],
                    cadence=RoutineCadence.MONTHLY,
                    month_day=1,
                    local_time="09:00",
                    time_zone="Asia/Seoul",
                    locale="ko-KR",
                    sources=[RoutineSource.WORK_ITEM],
                    budget=RoutineBudget(maximum_runs_per_month=12),
                ),
            ),
        )
        return routine.routine_id, {
            "schemaVersion": 1,
            "targetType": "ROUTINE",
            "routineId": str(routine.routine_id),
            "routineRevision": routine.revision,
            "lifecycleState": routine.lifecycle_state.value,
            "consentState": routine.consent_state.value,
            "resultSha256": run.result.result_sha256,
            "targetPath": f"/dwaion/routines?routine={routine.routine_id}",
        }


def _title(markdown: str, run_id: UUID) -> str:
    for line in markdown.splitlines():
        candidate = line.lstrip("#-*+> ").strip()
        if candidate:
            return candidate[:240]
    return f"Deep Research insight {run_id}"


def _summary(markdown: str) -> str:
    candidate = " ".join(line.strip("#-*+> ") for line in markdown.splitlines())
    normalized = " ".join(candidate.split())
    return normalized[:1_000] or "Completed citation-backed Deep Research result."
