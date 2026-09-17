from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Callable
from uuid import UUID

from .context_broker import ContextBrokerUnavailable, WorkspaceContextBroker
from .contracts import CitationSourceType
from .dwaion_workflow_contracts import (
    AttachmentCitation,
    ExecuteResearchRunRequest,
    ResearchProgress,
    ResearchPlan,
    ResearchResult,
    ResearchRun,
    ResearchRunState,
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
from .research_run_runtime import ResearchRuntimeControls
from .research_run_store import get_research_run_store
from .workspace_authorization import WorkspaceRequestAuthorization


@dataclass(frozen=True)
class ResearchExecutionOutcome:
    state: ResearchRunState
    progress: ResearchProgress
    result: ResearchResult | None = None
    safe_error_code: str | None = None


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
        return get_research_run_store().request_execution(
            identity, run_id, request, workspace_authorization
        )

    def perform(
        self,
        identity: PersonalDomainIdentity,
        run: ResearchRun,
        plan: ResearchPlan,
        controls: ResearchRuntimeControls,
        workspace_authorization: WorkspaceRequestAuthorization,
        checkpoint: Callable[[str], ResearchRuntimeControls],
    ) -> ResearchExecutionOutcome:
        ask_identity = AskIdentity(
            tenant_id=str(identity.tenant_id),
            user_id=identity.user_id,
            roles=tuple(identity.roles),
            permissions=tuple(identity.permissions),
            correlation_id=identity.correlation_id,
        )
        controls = checkpoint("BEFORE_CONTEXT")
        scopes = tuple(
            CitationSourceType(policy.source_key)
            for policy in plan.definition.source_policies
            if policy.allowed
            and policy.source_key in _RESEARCH_SCOPES
            and policy.source_key not in controls.excluded_source_keys
        )
        requested_source_keys = {scope.value for scope in scopes}
        try:
            context = self.context_broker.collect(
                plan.definition.question,
                identity=ask_identity,
                locale="ko",
                agent_key="DWP_ASSISTANT",
                source_scopes=scopes,
                workspace_authorization=workspace_authorization,
            )
            checkpoint("AFTER_CONTEXT")
            context = replace(
                context,
                sources=context.sources[: plan.definition.budget.maximum_sources],
            )
            unavailable = requested_source_keys & set(context.unavailable_sources)
            if plan.definition.require_all_allowed_sources and unavailable:
                return self._partial(run, "RESEARCH_REQUIRED_SOURCE_UNAVAILABLE")
            if not context.sources:
                return self._partial(run, "RESEARCH_SOURCE_EVIDENCE_UNAVAILABLE")
            answer = self.model_gateway.generate(
                _prompt(plan.definition.goal, plan.definition.success_criteria, plan.definition.deliverable_types),
                context=context,
                locale="ko",
                run_id=str(run.run_id),
                safety_identifier=_safety_identifier(identity),
                max_output_tokens=min(plan.definition.budget.maximum_tokens, 4096),
            )
            checkpoint("AFTER_MODEL")
            if not answer.answer or not answer.cited_source_ids:
                return self._partial(run, "RESEARCH_MODEL_ABSTAINED")
            by_id = {source.citation.source_id: source for source in context.sources}
            if any(source_id not in by_id for source_id in answer.cited_source_ids):
                return self._partial(run, "RESEARCH_MODEL_GROUNDING_INVALID")
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
            return ResearchExecutionOutcome(
                state=ResearchRunState.COMPLETED,
                progress=progress,
                result=result,
            )
        except ModelConfigurationRequired:
            return self._partial(run, "RESEARCH_MODEL_NOT_CONFIGURED")
        except ContextBrokerUnavailable:
            return self._partial(run, "RESEARCH_CONTEXT_NOT_CONFIGURED")
        except (ModelCallFailed, ModelRefused, GroundingViolation):
            return self._partial(run, "RESEARCH_MODEL_EXECUTION_FAILED")

    @staticmethod
    def _partial(
        run: ResearchRun,
        code: str,
    ) -> ResearchExecutionOutcome:
        progress = run.progress.model_copy(
            update={
                "recovery_hint": "Review source access and provider configuration, then resume the run.",
            }
        )
        return ResearchExecutionOutcome(
            state=ResearchRunState.PARTIAL,
            progress=progress,
            safe_error_code=code,
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
