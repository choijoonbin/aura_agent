from __future__ import annotations

from .contracts import AskPageContext, AskRequest, ConversationDetail
from .conversation_store import ConversationNotFound


def verify_conversation_agent(detail: ConversationDetail, agent_key: str | None) -> None:
    assistants = [message for message in detail.messages if message.role == "ASSISTANT"]
    if agent_key and (not assistants or any(message.agent_key != agent_key for message in assistants)):
        raise ConversationNotFound("Conversation does not match the selected agent.")


def bind_conversation_request(store, request: AskRequest, identity) -> AskRequest:
    if request.conversation_id is None:
        return request
    detail = store.get(tenant_id=identity.tenant_id, user_id=identity.user_id,
                       conversation_id=request.conversation_id)
    verify_conversation_agent(detail, request.agent_key)
    assistants = [message for message in detail.messages if message.role == "ASSISTANT"]
    selection = assistants[-1].selected_work if assistants else None
    supplied = request.page_context.selected_work if request.page_context else None
    if supplied is not None and supplied != selection:
        raise ConversationNotFound("Conversation does not match the selected work scope.")
    if selection is None:
        return request
    page = AskPageContext(
        route="/work/queue", app_key="APP.WORK", surface="selected-work-assist",
        entity_type=selection.source_system, entity_ref=str(selection.source_reference),
        selected_work=selection,
    )
    scopes = [selection.source_system] if selection.source_system.startswith("APPROVAL_") else ["WORK_ITEM"]
    return AskRequest.model_validate({
        **request.model_dump(), "page_context": page, "source_scopes": scopes,
    })
