from __future__ import annotations

import hashlib
from uuid import UUID

from .context_broker import ContextBrokerUnavailable, WorkspaceContextBroker
from .contracts import CitationSourceType
from .dwaion_workflow_contracts import (
    AttachmentCitation,
    ExecuteResearchRunRequest,
    ResearchProgress,
    ResearchResult,
    ResearchRun,
    ResearchRunState,
    ResearchWorkerObservation,
)
from .model_gateway import (
    GroundingViolation,
    ModelCallFailed,
    ModelConfigurationRequired,
    ModelRefused,
    OpenAIResponsesGateway,
)
from .personal_domain_security import PersonalDomainIdentity
from .policy import AskIdentity
from .research_plan_store import get_research_plan_store
from .research_run_store import get_research_run_store
from .workspace_authorization import WorkspaceRequestAuthorization


class ResearchExecutor:
    def __init__(
        self,
        context_broker: WorkspaceContextBroker | None = None,
        model_gateway: OpenAIResponsesGateway | None = None,
    ) -> None:
        self.context_broker = context_broker or WorkspaceContextBroker()
        self.model_gateway = model_gateway or OpenAIResponsesGateway()

    def execute(
        self,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        request: ExecuteResearchRunRequest,
        workspace_authorization: WorkspaceRequestAuthorization,
    ) -> ResearchRun:
        run_store = get_research_run_store()
        run = run_store.get(identity, run_id)
        if run.version != request.expected_version:
            from .dwaion_workflow_errors import DwaionWorkflowConflict

            raise DwaionWorkflowConflict("The research run version has changed.")
        plan = get_research_plan_store().get(identity, run.plan_id)
        ask_identity = AskIdentity(
            tenant_id=str(identity.tenant_id),
            user_id=identity.user_id,
            roles=tuple(identity.roles),
            permissions=tuple(identity.permissions),
            correlation_id=identity.correlation_id,
        )
        scopes = tuple(
            CitationSourceType(policy.source_key)
            for policy in plan.definition.source_policies
            if policy.allowed and policy.source_key in _RESEARCH_SCOPES
        )
        try:
            context = self.context_broker.collect(
                plan.definition.question,
                identity=ask_identity,
                locale="ko",
                agent_key="DWP_ASSISTANT",
                source_scopes=scopes,
                workspace_authorization=workspace_authorization,
            )
            if not context.sources:
                return self._partial(identity, run, request, "RESEARCH_SOURCE_EVIDENCE_UNAVAILABLE")
            answer = self.model_gateway.generate(
                _prompt(plan.definition.goal, plan.definition.success_criteria, plan.definition.deliverable_types),
                context=context,
                locale="ko",
                run_id=str(run_id),
                safety_identifier=_safety_identifier(identity),
                max_output_tokens=min(plan.definition.budget.maximum_tokens, 4096),
            )
            if not answer.answer or not answer.cited_source_ids:
                return self._partial(identity, run, request, "RESEARCH_MODEL_ABSTAINED")
            by_id = {source.citation.source_id: source for source in context.sources}
            citations = [
                AttachmentCitation(
                    citation_id=source_id,
                    locator=by_id[source_id].citation.route or source_id,
                    label=by_id[source_id].citation.title,
                    content_sha256=hashlib.sha256(by_id[source_id].evidence.encode()).hexdigest(),
                    evidence=by_id[source_id].evidence,
                )
                for source_id in answer.cited_source_ids
            ]
            report = answer.answer.strip()
            result = ResearchResult(
                report_markdown=report,
                citations=citations,
                result_sha256=hashlib.sha256(report.encode()).hexdigest(),
            )
            progress = ResearchProgress(
                completed_steps=run.progress.total_steps,
                total_steps=run.progress.total_steps,
                discovered_sources=len(context.sources),
                verified_citations=len(citations),
                failed_sources=list(context.unavailable_sources),
            )
            return run_store.observe(
                identity,
                run_id,
                ResearchWorkerObservation(
                    command_id=request.command_id,
                    expected_version=request.expected_version,
                    state=ResearchRunState.COMPLETED,
                    progress=progress,
                    result=result,
                ),
            )
        except ModelConfigurationRequired:
            return self._partial(identity, run, request, "RESEARCH_MODEL_NOT_CONFIGURED")
        except ContextBrokerUnavailable:
            return self._partial(identity, run, request, "RESEARCH_CONTEXT_NOT_CONFIGURED")
        except (ModelCallFailed, ModelRefused, GroundingViolation):
            return self._partial(identity, run, request, "RESEARCH_MODEL_EXECUTION_FAILED")

    @staticmethod
    def _partial(
        identity: PersonalDomainIdentity,
        run: ResearchRun,
        request: ExecuteResearchRunRequest,
        code: str,
    ) -> ResearchRun:
        progress = run.progress.model_copy(
            update={
                "recovery_hint": "Review source access and provider configuration, then resume the run.",
            }
        )
        return get_research_run_store().observe(
            identity,
            run.run_id,
            ResearchWorkerObservation(
                command_id=request.command_id,
                expected_version=request.expected_version,
                state=ResearchRunState.PARTIAL,
                progress=progress,
                safe_error_code=code,
            ),
        )


def _prompt(goal: str, criteria: list[str], deliverables: list[str]) -> str:
    return (
        f"Research goal: {goal}\n"
        f"Success criteria: {'; '.join(criteria)}\n"
        f"Deliverables: {', '.join(deliverables)}\n"
        "Use only the supplied evidence and cite every material claim."
    )


def _safety_identifier(identity: PersonalDomainIdentity) -> str:
    return hashlib.sha256(
        f"dwaion-research:{identity.tenant_id}:{identity.user_id}".encode()
    ).hexdigest()


_RESEARCH_SCOPES = {
    CitationSourceType.WORK_ITEM.value,
    CitationSourceType.MAIL.value,
    CitationSourceType.CALENDAR.value,
    CitationSourceType.APPROVAL_TASK.value,
    CitationSourceType.APPROVAL_REQUEST.value,
    CitationSourceType.APPROVAL_FORM.value,
    CitationSourceType.APPROVAL_OPERATION.value,
}
