from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from .audit import record_ask_run
from .context_broker import ContextBrokerUnavailable, WorkspaceContextBroker
from .conversation_store import (
    ConversationStore,
    ConversationTurn,
    get_conversation_store,
)
from .contracts import (
    AgentRegistryResolution,
    AnswerConfidence,
    AskCitation,
    AskModelRoute,
    AskPolicyDecision,
    AskRequest,
    AskResponse,
    AskState,
    ModelRouteState,
    PolicyOutcome,
    RegistryResolutionStatus,
)
from .model_gateway import (
    GroundingViolation,
    ModelCallFailed,
    ModelConfigurationRequired,
    ModelRefused,
    OpenAIResponsesGateway,
)
from .policy import AskIdentity, SafetyControls, evaluate_ask_policy
from .registry import resolve_agent
from .run_store import RunInProgress, RunStart, RunStore, get_run_store, privacy_hash
from .workspace_authorization import WorkspaceRequestAuthorization


class AskRuntime:
    def __init__(
        self,
        *,
        context_broker: WorkspaceContextBroker | None = None,
        model_gateway: OpenAIResponsesGateway | None = None,
        run_store: RunStore | None = None,
        conversation_store: ConversationStore | None = None,
    ) -> None:
        self.context_broker = context_broker or WorkspaceContextBroker()
        self.model_gateway = model_gateway or OpenAIResponsesGateway()
        self.run_store = run_store or get_run_store()
        self.conversation_store = conversation_store or get_conversation_store(self.run_store)

    def answer(
        self,
        request: AskRequest,
        *,
        identity: AskIdentity,
        on_progress: Callable[[str], None] | None = None,
        safety_controls: SafetyControls | None = None,
        workspace_authorization: WorkspaceRequestAuthorization | None = None,
    ) -> AskResponse:
        _progress(on_progress, "AUTHORIZING")
        query_hash = privacy_hash(
            json.dumps(
                request.model_dump(mode="json", by_alias=True, exclude={"request_id"}),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        existing = self.run_store.load(
            identity.tenant_id,
            identity.user_id,
            request.request_id,
            query_hash,
        )
        if existing is not None:
            _progress(on_progress, "COMPLETED")
            return existing

        run_id = str(uuid4())
        audit_id = str(uuid4())
        registry = self._resolve_registry(request, identity)
        policy = evaluate_ask_policy(
            request.query,
            identity,
            agent_key=registry.entry_key,
            safety_controls=safety_controls,
        )
        conversation_id = None
        conversation_history: tuple[ConversationTurn, ...] = ()
        started = RunStart(
            run_id=run_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            request_id=request.request_id,
            query_hash=query_hash,
            agent_key=registry.entry_key,
            agent_revision=registry.revision,
            risk_tier=policy.risk_tier,
            policy_outcome=policy.outcome,
            locale=request.locale,
            correlation_id=identity.correlation_id,
        )
        lease = self.run_store.begin(started)
        if lease is None:
            replay = self.run_store.load(
                identity.tenant_id,
                identity.user_id,
                request.request_id,
                query_hash,
            )
            if replay is not None:
                return replay
            raise RunInProgress("The Ask request is already running.")
        run_id = lease.run_id

        provider_request_hash: str | None = None
        try:
            if (
                registry.entry_key == "DWP_ASSISTANT"
                and "APP.ASK:VIEW"
                in {permission.upper() for permission in identity.permissions}
            ):
                conversation_id = self.conversation_store.ensure(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    conversation_id=request.conversation_id,
                    locale=request.locale,
                    initial_query=request.query,
                )
                conversation_history = self.conversation_store.recent_history(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    conversation_id=conversation_id,
                )
            if registry.resolution != RegistryResolutionStatus.ACTIVE:
                response = self._response(
                    request=request,
                    identity=identity,
                    run_id=run_id,
                    audit_id=audit_id,
                    registry=registry,
                    policy=policy,
                    state=AskState.CONFIGURATION_REQUIRED,
                    status_code="AGENT_REGISTRY_CONFIGURATION_REQUIRED",
                    model_route=AskModelRoute(
                        state=ModelRouteState.CONFIGURATION_REQUIRED,
                    ),
                )
            elif policy.outcome != PolicyOutcome.ALLOW or not policy.model_allowed:
                response = self._response(
                    request=request,
                    identity=identity,
                    run_id=run_id,
                    audit_id=audit_id,
                    registry=registry,
                    policy=policy,
                    state=AskState.ABSTAINED,
                    status_code=policy.code,
                    model_route=AskModelRoute(state=ModelRouteState.NOT_INVOKED),
                )
            else:
                response, provider_request_hash = self._grounded_answer(
                    request=request,
                    identity=identity,
                    run_id=run_id,
                    audit_id=audit_id,
                    registry=registry,
                    policy=policy,
                    conversation_history=conversation_history,
                    on_progress=on_progress,
                    workspace_authorization=workspace_authorization,
                )

            _progress(on_progress, "PERSISTING")
            if conversation_id is not None:
                user_message_id, assistant_message_id = self.conversation_store.append_exchange(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    conversation_id=conversation_id,
                    request_id=request.request_id,
                    query=request.query,
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
            self.run_store.complete(
                response,
                lease=lease,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                provider_request_hash=provider_request_hash,
            )
            record_ask_run(
                response,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                roles=list(identity.roles),
            )
            _progress(on_progress, "COMPLETED")
            return response
        except Exception:
            self.run_store.fail(lease, "ASK_RUNTIME_FAILED")
            raise

    def _grounded_answer(
        self,
        *,
        request: AskRequest,
        identity: AskIdentity,
        run_id: str,
        audit_id: str,
        registry: AgentRegistryResolution,
        policy: AskPolicyDecision,
        conversation_history: tuple[ConversationTurn, ...],
        on_progress: Callable[[str], None] | None,
        workspace_authorization: WorkspaceRequestAuthorization | None,
    ) -> tuple[AskResponse, str | None]:
        _progress(on_progress, "RETRIEVING")
        try:
            context = self.context_broker.collect(
                request.query,
                identity=identity,
                locale=request.locale,
                agent_key=registry.entry_key,
                source_scopes=request.source_scopes,
                page_context=request.page_context,
                workspace_authorization=workspace_authorization,
            )
        except ContextBrokerUnavailable:
            return (
                self._response(
                    request=request,
                    identity=identity,
                    run_id=run_id,
                    audit_id=audit_id,
                    registry=registry,
                    policy=policy,
                    state=AskState.CONFIGURATION_REQUIRED,
                    status_code="CONTEXT_BROKER_CONFIGURATION_REQUIRED",
                    model_route=AskModelRoute(state=ModelRouteState.NOT_INVOKED),
                ),
                None,
            )

        if not context.sources:
            status_code = (
                "CONTEXT_SOURCE_UNAVAILABLE"
                if context.attempted_sources and context.unavailable_sources
                else "NO_GROUNDED_SOURCE"
            )
            return (
                self._response(
                    request=request,
                    identity=identity,
                    run_id=run_id,
                    audit_id=audit_id,
                    registry=registry,
                    policy=policy,
                    state=AskState.ABSTAINED,
                    status_code=status_code,
                    model_route=AskModelRoute(state=ModelRouteState.NOT_INVOKED),
                ),
                None,
            )

        _progress(on_progress, "REASONING")
        try:
            model_answer = self.model_gateway.generate(
                request.query,
                context=context,
                locale=request.locale,
                run_id=run_id,
                safety_identifier=_safety_identifier(identity),
                conversation_history=conversation_history,
                page_context=request.page_context,
                agent_key=registry.entry_key,
            )
        except ModelConfigurationRequired:
            return (
                self._response(
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
                        provider=_model_provider_label(self.model_gateway),
                        model=self.model_gateway.model.strip() or None,
                    ),
                ),
                None,
            )
        except ModelRefused:
            return (
                self._response(
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
                        provider=_model_provider_label(self.model_gateway),
                        model=self.model_gateway.model.strip() or None,
                    ),
                ),
                None,
            )
        except (GroundingViolation, ModelCallFailed) as error:
            return (
                self._response(
                    request=request,
                    identity=identity,
                    run_id=run_id,
                    audit_id=audit_id,
                    registry=registry,
                    policy=policy,
                    state=AskState.ABSTAINED,
                    status_code=_safe_status_code(error),
                    source_count=len(context.sources),
                    model_route=AskModelRoute(
                        state=ModelRouteState.REFUSED,
                        provider=_model_provider_label(self.model_gateway),
                        model=self.model_gateway.model.strip() or None,
                    ),
                ),
                None,
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
            status_code = "ANSWER_GROUNDED"

        return (
            self._response(
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
            ),
            model_answer.provider_request_hash,
        )

    def _resolve_registry(
        self,
        request: AskRequest,
        identity: AskIdentity,
    ) -> AgentRegistryResolution:
        return resolve_agent(
            request.agent_key,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            correlation_id=identity.correlation_id,
        )

    def _response(
        self,
        *,
        request: AskRequest,
        identity: AskIdentity,
        run_id: str,
        audit_id: str,
        registry: AgentRegistryResolution,
        policy: AskPolicyDecision,
        state: AskState,
        status_code: str,
        model_route: AskModelRoute,
        answer: str | None = None,
        confidence: AnswerConfidence | None = None,
        citations: list[AskCitation] | None = None,
        source_count: int = 0,
    ) -> AskResponse:
        return AskResponse(
            run_id=run_id,
            audit_id=audit_id,
            request_id=request.request_id,
            correlation_id=identity.correlation_id,
            state=state,
            answer=answer,
            confidence=confidence,
            citations=citations or [],
            source_count=source_count,
            policy=policy,
            model_route=model_route,
            agent_registry=registry,
            status_code=status_code,
            completed_at=datetime.now(timezone.utc),
        )


def _safety_identifier(identity: AskIdentity) -> str:
    secret = os.getenv("DWP_AGENT_SAFETY_SECRET", "").strip()
    if not secret:
        secret = os.getenv("DWP_AGENT_PRIVACY_HASH_SECRET", "").strip()
    if not secret:
        raise ModelConfigurationRequired("Agent safety identifier secret is required.")
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{identity.tenant_id}:{identity.user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"dwp_{digest[:48]}"


def _model_provider_label(model_gateway: object) -> str:
    provider = getattr(model_gateway, "provider_label", "OPENAI")
    return str(provider).strip().upper()[:40] or "OPENAI"


def _safe_status_code(error: Exception) -> str:
    value = str(error).strip().upper() or type(error).__name__.upper()
    safe = "".join(character if character.isalnum() or character in "_.-" else "_" for character in value)
    return safe[:120]


def _progress(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)
