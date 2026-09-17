from __future__ import annotations

import re
from uuid import NAMESPACE_URL, UUID, uuid5

from .artifact_contracts import (
    ArtifactDraftContent,
    ArtifactMetadata,
    ArtifactType,
    CreateArtifactRequest,
)
from .artifact_postgres_store import PostgresArtifactStore
from .dwaion_workflow_contracts import (
    ResearchDeliveryObservation,
    ResearchDeliveryState,
    ResearchDeliveryType,
    ResearchRunState,
)
from .governed_domain_core import GovernedDomainConflict
from .personal_domain_security import PersonalDomainIdentity
from .research_delivery_store import ResearchDeliveryStore
from .research_downstream_adapters import (
    HttpResearchDownstreamAdapter,
    ProposalResearchDownstreamAdapter,
    RoutineResearchDownstreamAdapter,
)
from .research_download_store import ResearchDownloadStore
from .research_plan_store import ResearchPlanStore
from .research_run_store import ResearchRunStore
from .transactional_outbox import OutboxLease


TOPICS = ("ai.research.delivery-requested.v1", "RESEARCH_DELIVERY")


class PostgresResearchDeliveryWorker:
    """Consumes durable research delivery intents for repository-backed targets."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        plan_store = ResearchPlanStore(database_url)
        self.run_store = ResearchRunStore(database_url, plan_store=plan_store)
        self.delivery_store = ResearchDeliveryStore(
            database_url, run_store=self.run_store
        )
        self.artifact_store = PostgresArtifactStore(database_url)
        self.download_store = ResearchDownloadStore(database_url)
        self.downstream_adapters = {
            ResearchDeliveryType.PROPOSAL: ProposalResearchDownstreamAdapter(database_url),
            ResearchDeliveryType.HANDOFF: HttpResearchDownstreamAdapter(
                ResearchDeliveryType.HANDOFF
            ),
            ResearchDeliveryType.SHARE: HttpResearchDownstreamAdapter(
                ResearchDeliveryType.SHARE
            ),
            ResearchDeliveryType.ROUTINE: RoutineResearchDownstreamAdapter(database_url),
        }

    def process(self, lease: OutboxLease) -> str:
        identity, run_id, delivery_id, delivery_type, parameters = self._intent(lease)
        delivery = self.delivery_store.get(identity, run_id, delivery_id)
        if delivery.delivery_type != delivery_type:
            raise ValueError("The research delivery type binding is invalid.")
        if delivery.state == ResearchDeliveryState.COMPLETED:
            return delivery.state.value
        if delivery.state not in {
            ResearchDeliveryState.QUEUED,
            ResearchDeliveryState.RUNNING,
            ResearchDeliveryState.PARTIAL,
        }:
            raise ValueError("The research delivery is not executable.")

        if delivery.state != ResearchDeliveryState.RUNNING:
            delivery = self.delivery_store.observe(
                identity,
                run_id,
                delivery_id,
                ResearchDeliveryObservation(
                    command_id=self._command(lease, "running"),
                    state=ResearchDeliveryState.RUNNING,
                ),
            )

        try:
            receipt_id, receipt = self._execute(
                identity, run_id, delivery_id, delivery_type, parameters
            )
        except GovernedDomainConflict as error:
            return self._partial(
                lease,
                identity,
                run_id,
                delivery_id,
                safe_error_code=_governed_error_code(error),
                recovery_hint=_governed_recovery_hint(error),
            )
        completed = self.delivery_store.observe(
            identity,
            run_id,
            delivery_id,
            ResearchDeliveryObservation(
                command_id=self._command(lease, "completed"),
                state=ResearchDeliveryState.COMPLETED,
                receipt_id=receipt_id,
                receipt=receipt,
            ),
        )
        return completed.state.value

    def mark_retry_exhausted(self, lease: OutboxLease) -> None:
        identity, run_id, delivery_id, _, _ = self._intent(lease)
        delivery = self.delivery_store.get(identity, run_id, delivery_id)
        if delivery.state in {
            ResearchDeliveryState.COMPLETED,
            ResearchDeliveryState.FAILED,
            ResearchDeliveryState.CANCELLED,
        }:
            return
        self.delivery_store.observe(
            identity,
            run_id,
            delivery_id,
            ResearchDeliveryObservation(
                command_id=self._command(lease, "retry-exhausted"),
                state=ResearchDeliveryState.FAILED,
                safe_error_code="RESEARCH_DELIVERY_RETRY_EXHAUSTED",
                recovery_hint=(
                    "Restore the governed delivery worker dependency and submit a new delivery request."
                ),
            ),
        )

    def _execute(
        self,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        delivery_id: UUID,
        delivery_type: ResearchDeliveryType,
        parameters: dict[str, object],
    ) -> tuple[UUID, dict[str, object]]:
        run = self.run_store.get(identity, run_id)
        if run.state != ResearchRunState.COMPLETED or run.result is None:
            raise ValueError("The completed research result is unavailable.")
        if delivery_type == ResearchDeliveryType.ARTIFACT:
            title = _artifact_title(run.result.report_markdown, run_id)
            body = _artifact_body(run.result.report_markdown, run.result.citations)
            artifact = self.artifact_store.create(
                identity,
                CreateArtifactRequest(
                    command_id=uuid5(
                        NAMESPACE_URL,
                        f"urn:dwp:research-delivery:{delivery_id}:artifact-create",
                    ),
                    expected_revision=0,
                    reason_code="RESEARCH_DELIVERY",
                    artifact_type=ArtifactType.DOCUMENT,
                    content=ArtifactDraftContent(title=title, body=body),
                    sources=[],
                    metadata=ArtifactMetadata(tags=["deep-research"]),
                ),
            )
            return artifact.artifact_id, {
                "schemaVersion": 1,
                "targetType": "ARTIFACT",
                "artifactId": str(artifact.artifact_id),
                "artifactRevision": artifact.revision,
                "draftRevision": artifact.draft_revision,
                "resultSha256": run.result.result_sha256,
                "targetPath": f"/dwaion/artifacts?artifact={artifact.artifact_id}",
            }
        if delivery_type == ResearchDeliveryType.EXPORT:
            exported = self.download_store.raw(identity, run_id)
            receipt_id = uuid5(
                NAMESPACE_URL,
                f"urn:dwp:research-delivery:{delivery_id}:raw-export",
            )
            return receipt_id, {
                "schemaVersion": 1,
                "targetType": "RAW_EXPORT",
                "runId": str(run_id),
                "resultSha256": run.result.result_sha256,
                "integrityFingerprint": exported.integrity_fingerprint,
                "downloadPath": f"/v1/research/runs/{run_id}/downloads/raw",
            }
        adapter = self.downstream_adapters.get(delivery_type)
        if adapter is not None:
            return adapter.deliver(identity, run, delivery_id, parameters)
        raise ValueError("The research delivery target has no configured governed adapter.")

    def _partial(
        self,
        lease: OutboxLease,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        delivery_id: UUID,
        *,
        safe_error_code: str,
        recovery_hint: str,
    ) -> str:
        partial = self.delivery_store.observe(
            identity,
            run_id,
            delivery_id,
            ResearchDeliveryObservation(
                command_id=self._command(lease, "partial"),
                state=ResearchDeliveryState.PARTIAL,
                safe_error_code=safe_error_code,
                recovery_hint=recovery_hint,
            ),
        )
        return partial.state.value

    @staticmethod
    def _command(lease: OutboxLease, phase: str) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"urn:dwp:research-delivery-worker:{lease.outbox_id}:{lease.generation}:{phase}",
        )

    @staticmethod
    def _intent(
        lease: OutboxLease,
    ) -> tuple[
        PersonalDomainIdentity,
        UUID,
        UUID,
        ResearchDeliveryType,
        dict[str, object],
    ]:
        if lease.topic not in TOPICS or lease.aggregate_type != "RESEARCH_DELIVERY":
            raise ValueError("The research delivery outbox binding is invalid.")
        try:
            delivery_id = UUID(str(lease.payload["deliveryId"]))
            run_id = UUID(str(lease.payload["runId"]))
            delivery_type = ResearchDeliveryType(str(lease.payload["deliveryType"]))
            parameters = lease.payload["parameters"]
            if not isinstance(parameters, dict):
                raise ValueError("The research delivery parameters are invalid.")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("The research delivery intent is invalid.") from error
        if lease.aggregate_id != str(delivery_id):
            raise ValueError("The research delivery aggregate binding is invalid.")
        identity = PersonalDomainIdentity(
            tenant_id=lease.tenant_id,
            user_id=lease.user_id,
            correlation_id=f"research-delivery:{lease.outbox_id}",
            auth_session_id=f"worker:{lease.outbox_id}",
            roles=frozenset({"WORKSPACE_MEMBER"}),
            permissions=frozenset(),
        )
        return identity, run_id, delivery_id, delivery_type, parameters


def _artifact_title(markdown: str, run_id: UUID) -> str:
    for line in markdown.splitlines():
        candidate = re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|>\s*)", "", line).strip()
        if candidate:
            return candidate[:200]
    return f"Deep research {run_id}"


def _artifact_body(markdown: str, citations: list[object]) -> str:
    citation_lines = ["", "## Evidence"]
    for citation in citations:
        citation_lines.append(
            f"- {citation.label} ({citation.locator}) — {citation.evidence}"
        )
    body = markdown.rstrip() + "\n" + "\n".join(citation_lines)
    if len(body) > 100_000:
        raise GovernedDomainConflict(
            "The research result exceeds the governed artifact content limit."
        )
    return body


def _governed_error_code(error: GovernedDomainConflict) -> str:
    message = str(error).casefold()
    if "retention" in message:
        return "ARTIFACT_RETENTION_POLICY_REQUIRED"
    if "content limit" in message:
        return "RESEARCH_ARTIFACT_CONTENT_LIMIT_EXCEEDED"
    return "RESEARCH_ARTIFACT_GOVERNANCE_CONFLICT"


def _governed_recovery_hint(error: GovernedDomainConflict) -> str:
    message = str(error).casefold()
    if "retention" in message:
        return "Configure an explicit ARTIFACT retention policy and submit a new delivery request."
    if "content limit" in message:
        return "Reduce the report size or use the governed raw export download."
    return "Review the governed artifact policy and submit a new delivery request."
