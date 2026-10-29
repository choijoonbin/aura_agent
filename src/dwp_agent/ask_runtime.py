from __future__ import annotations
import json
from typing import Callable
from uuid import uuid4
from .audit import record_ask_failure, record_ask_run
from .ai_control_runtime import AIRuntimeControl
from .ask_response_guard import selected_response_guard
from .ask_runtime_response import build_ask_response as _response
from .context_broker import WorkspaceContextBroker
from .conversation_store import (
    ConversationStore,
    ConversationTurn,
    get_conversation_store,
)
from .conversation_continuation import bind_conversation_request
from .contracts import (
    AgentRegistryResolution,
    AskModelRoute,
    AskPolicyDecision,
    AskRequest,
    AskResponse,
    AskState,
    ModelRouteState,
    PolicyOutcome,
    RegistryResolutionStatus,
)
from .grounded_answer_runtime import grounded_answer
from .model_gateway import OpenAIResponsesGateway
from .policy import AskIdentity, SafetyControls, evaluate_ask_policy
from .personal_memory_runtime import PersonalMemoryRuntime
from .registry import resolve_agent
from .run_store import RunInProgress, RunLease, RunStart, RunStore, get_run_store, privacy_hash
from .run_observability import RunStageKey
from .workspace_authorization import WorkspaceRequestAuthorization
class AskRuntime:
    def __init__(
        self,
        *,
        context_broker: WorkspaceContextBroker | None = None,
        model_gateway: OpenAIResponsesGateway | None = None,
        run_store: RunStore | None = None,
        conversation_store: ConversationStore | None = None,
        personalization_runtime: PersonalMemoryRuntime | None = None,
        ai_runtime_control: AIRuntimeControl | None = None,
    ) -> None:
        self.context_broker = context_broker or WorkspaceContextBroker()
        self.model_gateway = model_gateway or OpenAIResponsesGateway()
        self.run_store = run_store or get_run_store()
        self.conversation_store = conversation_store or get_conversation_store(self.run_store)
        self.personalization_runtime = personalization_runtime or PersonalMemoryRuntime()
        self.ai_runtime_control = ai_runtime_control
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
        request = bind_conversation_request(self.conversation_store, request, identity)
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
            return selected_response_guard(
                self.context_broker, request, identity, existing, workspace_authorization
            )

        run_id = str(uuid4())
        audit_id = str(uuid4())
        registry = resolve_agent(
            request.agent_key,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            correlation_id=identity.correlation_id,
        )
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
            audit_id=audit_id,
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
                return selected_response_guard(
                    self.context_broker, request, identity, replay, workspace_authorization
                )
            raise RunInProgress("The Ask request is already running.")
        run_id = lease.run_id
        def tracked_progress(stage: str) -> None:
            self.run_store.advance_stage(
                lease,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                request_id=request.request_id,
                stage=RunStageKey(stage),
            )
            _progress(on_progress, stage)

        provider_request_hash: str | None = None
        model_safe_error_code: str | None = None
        try:
            if (
                (registry.entry_key == "DWP_ASSISTANT" or (
                    registry.entry_key == "DWP_APPROVAL_EXPERT"
                    and request.page_context is not None
                    and request.page_context.selected_work is not None
                ))
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
                response = _response(
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
                response = _response(
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
                (
                    response,
                    provider_request_hash,
                    model_safe_error_code,
                ) = self._grounded_answer(
                    request=request,
                    identity=identity,
                    run_id=run_id,
                    lease=lease,
                    audit_id=audit_id,
                    registry=registry,
                    policy=policy,
                    conversation_history=conversation_history,
                    on_progress=tracked_progress,
                    workspace_authorization=workspace_authorization,
                )

            response = selected_response_guard(
                self.context_broker,
                request,
                identity,
                response,
                workspace_authorization,
                on_recheck=lambda observations: self.run_store.record_source_health(
                    lease,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    request_id=request.request_id,
                    observations=observations,
                ),
            )
            tracked_progress("PERSISTING")
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
                safe_error_code=model_safe_error_code,
            )
            record_ask_run(
                response,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                roles=list(identity.roles),
                model_safe_error_code=model_safe_error_code,
                lease_generation=lease.generation,
            )
            _progress(on_progress, "COMPLETED")
            return response
        except Exception:
            if not self.run_store.is_completed(
                lease, tenant_id=identity.tenant_id, user_id=identity.user_id,
                request_id=request.request_id,
            ):
                self.run_store.fail(lease, "ASK_RUNTIME_FAILED")
                record_ask_failure(
                    run_id=run_id, audit_id=audit_id, tenant_id=identity.tenant_id,
                    user_id=identity.user_id, correlation_id=identity.correlation_id,
                    agent_key=registry.entry_key, agent_revision=registry.revision,
                    risk_tier=str(policy.risk_tier), lease_generation=lease.generation,
                    roles=list(identity.roles),
                )
            raise

    def _grounded_answer(
        self,
        *,
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
        return grounded_answer(
            context_broker=self.context_broker,
            model_gateway=self.model_gateway,
            run_store=self.run_store,
            personalization_runtime=self.personalization_runtime,
            ai_runtime_control=self.ai_runtime_control,
            request=request,
            identity=identity,
            run_id=run_id,
            lease=lease,
            audit_id=audit_id,
            registry=registry,
            policy=policy,
            conversation_history=conversation_history,
            on_progress=on_progress,
            workspace_authorization=workspace_authorization,
        )

def _progress(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)
