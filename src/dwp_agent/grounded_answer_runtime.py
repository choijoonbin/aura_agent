from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .ai_control_runtime import AIRuntimeControl, AIRuntimePlan
from .ask_response_guard import (
    model_provider_label,
    safe_status_code,
    safety_identifier,
)
from .ask_runtime_response import build_ask_response
from .attachment_context import AttachmentContextUnavailable, collect_with_ready_attachments
from .context_broker import ContextBrokerUnavailable, WorkspaceContextBroker
from .contracts import (
    AgentRegistryResolution,
    AskCitation,
    AskModelRoute,
    AskPolicyDecision,
    AskRequest,
    AskResponse,
    AskState,
    ModelRouteState,
)
from .conversation_store import ConversationTurn
from .grounded_fallback import grounded_evidence_fallback
from .grounded_response_status import grounded_status_for_provider
from .model_error_codes import safe_model_error_code
from .model_gateway import (
    GroundingViolation,
    ModelCallFailed,
    ModelConfigurationRequired,
    ModelRefused,
    OpenAIResponsesGateway,
    input_token_reservation_ceiling,
)
from .operational_health import OPERATIONAL_HEALTH
from .personal_memory_runtime import PersonalMemoryRuntime
from .policy import AskIdentity
from .run_store import RunLease, RunStore
from .run_observability import RunSourceHealthStatus, SourceHealthObservation
from .workspace_authorization import WorkspaceRequestAuthorization


def grounded_answer(
    *,
    context_broker: WorkspaceContextBroker,
    model_gateway: OpenAIResponsesGateway,
    run_store: RunStore,
    personalization_runtime: PersonalMemoryRuntime,
    ai_runtime_control: AIRuntimeControl | None,
    request: AskRequest,
    identity: AskIdentity,
    run_id: str,
    lease: RunLease,
    audit_id: str,
    registry: AgentRegistryResolution,
    policy: AskPolicyDecision,
    conversation_history: tuple[ConversationTurn, ...],
    on_progress: Callable[[str], None] | None,
    workspace_authorization: WorkspaceRequestAuthorization | None,
) -> tuple[AskResponse, str | None, str | None]:
    runtime_plan: AIRuntimePlan | None = None
    if ai_runtime_control is not None:
        runtime_plan = ai_runtime_control.preflight(
            tenant_id=identity.tenant_id,
            provider=model_provider_label(model_gateway),
            model=model_gateway.model.strip(),
            knowledge_sources=tuple(scope.value for scope in request.source_scopes),
            now=datetime.now(timezone.utc),
            region=getattr(model_gateway, "provider_region", None),
        )
    _progress(on_progress, "RETRIEVING")
    try:
        context = collect_with_ready_attachments(
            context_broker,
            request,
            identity=identity,
            locale=request.locale,
            agent_key=registry.entry_key,
            workspace_authorization=workspace_authorization,
        )
    except ContextBrokerUnavailable:
        _record_source_health(
            run_store=run_store,
            lease=lease,
            request=request,
            identity=identity,
            observations=tuple(
                SourceHealthObservation(
                    source_type=scope.value,
                    status=RunSourceHealthStatus.NOT_CONFIGURED,
                    observed_at=datetime.now(timezone.utc),
                    operational=scope.value != "ATTACHMENT",
                )
                for scope in request.source_scopes
            ),
        )
        return (
            build_ask_response(
                request=request,
                identity=identity,
                run_id=run_id,
                audit_id=audit_id,
                registry=registry,
                policy=policy,
                state=AskState.CONFIGURATION_REQUIRED,
                status_code="CONTEXT_BROKER_CONFIGURATION_REQUIRED",
                model_route=AskModelRoute(state=ModelRouteState.NOT_INVOKED),
                warnings=runtime_plan.warnings if runtime_plan else (),
            ),
            None,
            None,
        )
    except AttachmentContextUnavailable as error:
        _record_source_health(
            run_store=run_store,
            lease=lease,
            request=request,
            identity=identity,
            observations=error.source_health + (
                SourceHealthObservation(
                    source_type="ATTACHMENT",
                    status=RunSourceHealthStatus.UNAVAILABLE,
                    observed_at=datetime.now(timezone.utc),
                    operational=False,
                ),
            ),
        )
        return (
            build_ask_response(
                request=request,
                identity=identity,
                run_id=run_id,
                audit_id=audit_id,
                registry=registry,
                policy=policy,
                state=AskState.ABSTAINED,
                status_code="ATTACHMENT_NOT_READY",
                model_route=AskModelRoute(state=ModelRouteState.NOT_INVOKED),
                warnings=runtime_plan.warnings if runtime_plan else (),
            ),
            None,
            None,
        )
    _record_source_health(
        run_store=run_store,
        lease=lease,
        request=request,
        identity=identity,
        observations=context.source_health,
    )
    if not context.sources:
        status_code = context.status_code or (
            "CONTEXT_SOURCE_UNAVAILABLE"
            if context.attempted_sources and context.unavailable_sources
            else "NO_GROUNDED_SOURCE"
        )
        return (
            build_ask_response(
                request=request,
                identity=identity,
                run_id=run_id,
                audit_id=audit_id,
                registry=registry,
                policy=policy,
                state=AskState.ABSTAINED,
                status_code=status_code,
                model_route=AskModelRoute(state=ModelRouteState.NOT_INVOKED),
                warnings=runtime_plan.warnings if runtime_plan else (),
            ),
            None,
            None,
        )
    _progress(on_progress, "REASONING")
    personalization = personalization_runtime.resolve(
        identity, agent_key=registry.entry_key
    )
    fallback_used = False
    try:
        input_token_ceiling = input_token_reservation_ceiling(
            request.query,
            context=context,
            locale=request.locale,
            conversation_history=conversation_history,
            page_context=request.page_context,
            agent_key=registry.entry_key,
            personal_preferences=personalization.preferences,
        )

        def call_model(output_limit: int | None):
            return model_gateway.generate(
                request.query,
                context=context,
                locale=request.locale,
                run_id=run_id,
                safety_identifier=safety_identifier(identity),
                conversation_history=conversation_history,
                page_context=request.page_context,
                agent_key=registry.entry_key,
                personal_preferences=personalization.preferences,
                max_output_tokens=output_limit,
                allow_automatic_retry=ai_runtime_control is None,
            )

        model_answer = (
            ai_runtime_control.invoke_model(
                plan=runtime_plan,
                run_id=run_id,
                attempt_generation=lease.generation,
                input_token_ceiling=input_token_ceiling,
                call=call_model,
                now=datetime.now(timezone.utc),
            )
            if ai_runtime_control is not None and runtime_plan is not None
            else call_model(None)
        )
    except ModelConfigurationRequired as error:
        if error.safe_error_code == "MODEL_ROUTE_CONFIGURATION_INVALID":
            OPERATIONAL_HEALTH.record_model_invalid_configuration(
                tenant_id=identity.tenant_id,
                provider=model_provider_label(model_gateway)
            )
        elif error.safe_error_code == "MODEL_ROUTE_CONFIGURATION_REQUIRED":
            OPERATIONAL_HEALTH.record_model_not_configured(
                tenant_id=identity.tenant_id,
                provider=model_provider_label(model_gateway)
            )
        return (
            build_ask_response(
                request=request,
                identity=identity,
                run_id=run_id,
                audit_id=audit_id,
                registry=registry,
                policy=policy,
                state=AskState.CONFIGURATION_REQUIRED,
                status_code="MODEL_ROUTE_CONFIGURATION_REQUIRED",
                source_count=len(context.sources),
                model_route=AskModelRoute(
                    state=ModelRouteState.CONFIGURATION_REQUIRED,
                    provider=model_provider_label(model_gateway),
                    model=model_gateway.model.strip() or None,
                ),
                personalization=personalization.evidence(model_applied=False),
                warnings=runtime_plan.warnings if runtime_plan else (),
            ),
            None,
            error.safe_error_code,
        )
    except ModelRefused:
        OPERATIONAL_HEALTH.record_model_available(
            tenant_id=identity.tenant_id,
            provider=model_provider_label(model_gateway)
        )
        return (
            build_ask_response(
                request=request,
                identity=identity,
                run_id=run_id,
                audit_id=audit_id,
                registry=registry,
                policy=policy,
                state=AskState.ABSTAINED,
                status_code="MODEL_REFUSED",
                source_count=len(context.sources),
                model_route=AskModelRoute(
                    state=ModelRouteState.REFUSED,
                    provider=model_provider_label(model_gateway),
                    model=model_gateway.model.strip() or None,
                ),
                personalization=personalization.evidence(model_applied=False),
                warnings=runtime_plan.warnings if runtime_plan else (),
            ),
            None,
            None,
        )
    except ModelCallFailed as error:
        model_safe_error_code = safe_model_error_code(error)
        OPERATIONAL_HEALTH.record_model_unavailable(
            tenant_id=identity.tenant_id,
            provider=model_provider_label(model_gateway),
            safe_error_code=model_safe_error_code,
        )
        model_answer = grounded_evidence_fallback(context, request.locale)
        fallback_used = True
    except GroundingViolation as error:
        OPERATIONAL_HEALTH.record_model_unavailable(
            tenant_id=identity.tenant_id,
            provider=model_provider_label(model_gateway),
            safe_error_code="MODEL_GROUNDING_CONTRACT_INVALID",
        )
        return (
            build_ask_response(
                request=request,
                identity=identity,
                run_id=run_id,
                audit_id=audit_id,
                registry=registry,
                policy=policy,
                state=AskState.ABSTAINED,
                status_code=safe_status_code(error),
                source_count=len(context.sources),
                model_route=AskModelRoute(
                    state=ModelRouteState.REFUSED,
                    provider=model_provider_label(model_gateway),
                    model=model_gateway.model.strip() or None,
                ),
                personalization=personalization.evidence(model_applied=False),
                warnings=runtime_plan.warnings if runtime_plan else (),
            ),
            None,
            None,
        )
    else:
        model_safe_error_code = None
        OPERATIONAL_HEALTH.record_model_available(
            tenant_id=identity.tenant_id,
            provider=model_provider_label(model_gateway)
        )
    _progress(on_progress, "VERIFYING")
    citations_by_id = {
        source.citation.source_id: source.citation for source in context.sources
    }
    citations: list[AskCitation] = [
        citations_by_id[source_id] for source_id in model_answer.cited_source_ids
    ]
    if model_answer.answer is None:
        state = AskState.ABSTAINED
        status_code = "EVIDENCE_INSUFFICIENT"
        citations = []
    else:
        state = AskState.COMPLETED
        status_code = grounded_status_for_provider(
            "DWP_GROUNDED_FALLBACK" if fallback_used else model_answer.provider
        )
    return (
        build_ask_response(
            request=request,
            identity=identity,
            run_id=run_id,
            audit_id=audit_id,
            registry=registry,
            policy=policy,
            state=state,
            status_code=status_code,
            answer=model_answer.answer,
            confidence=model_answer.confidence,
            citations=citations,
            source_count=len(context.sources),
            model_route=AskModelRoute(
                state=ModelRouteState.COMPLETED,
                provider=model_answer.provider,
                model=model_answer.model,
                input_tokens=model_answer.input_tokens,
                output_tokens=model_answer.output_tokens,
                total_tokens=model_answer.total_tokens,
                latency_ms=model_answer.latency_ms,
            ),
            personalization=personalization.evidence(model_applied=not fallback_used),
            warnings=runtime_plan.warnings if runtime_plan else (),
        ),
        model_answer.provider_request_hash,
        model_safe_error_code,
    )


def _progress(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _record_source_health(
    *,
    run_store: RunStore,
    lease: RunLease,
    request: AskRequest,
    identity: AskIdentity,
    observations: tuple[SourceHealthObservation, ...],
) -> None:
    run_store.record_source_health(
        lease,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        request_id=request.request_id,
        observations=observations,
    )
    OPERATIONAL_HEALTH.record_sources(
        tenant_id=identity.tenant_id,
        observations=observations,
    )
