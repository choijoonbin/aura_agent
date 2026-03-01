from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.config import settings


def list_agents(db: Session) -> list[dict[str, Any]]:
    sql = text(
        """
        select
            am.agent_id,
            am.agent_key,
            am.name,
            am.domain,
            am.model_name,
            am.temperature,
            am.max_tokens,
            am.is_active,
            am.tenant_id,
            am.created_at,
            am.updated_at
        from dwp_aura.agent_master am
        where am.tenant_id = :tenant_id
          and am.is_active = true
        order by am.is_active desc, am.agent_id asc
        """
    )
    rows = db.execute(sql, {"tenant_id": settings.default_tenant_id}).mappings().all()
    return [dict(row) for row in rows]


def get_agent_detail(db: Session, agent_id: int) -> dict[str, Any] | None:
    agent_sql = text(
        """
        select
            am.agent_id,
            am.agent_key,
            am.name,
            am.domain,
            am.model_name,
            am.temperature,
            am.max_tokens,
            am.is_active,
            am.tenant_id,
            am.created_at,
            am.updated_at
        from dwp_aura.agent_master am
        where am.tenant_id = :tenant_id
          and am.agent_id = :agent_id
        """
    )
    agent = db.execute(
        agent_sql,
        {"tenant_id": settings.default_tenant_id, "agent_id": agent_id},
    ).mappings().first()
    if not agent:
        return None

    prompt_sql = text(
        """
        select
            prompt_id,
            system_instruction,
            version,
            is_current,
            created_at
        from dwp_aura.agent_prompt_history
        where agent_id = :agent_id
        order by is_current desc, created_at desc
        """
    )
    prompts = [dict(row) for row in db.execute(prompt_sql, {"agent_id": agent_id}).mappings().all()]

    tools_sql = text(
        """
        select
            ati.tool_id,
            ati.tool_name,
            ati.description,
            ati.schema_json
        from dwp_aura.agent_tool_mapping atm
        join dwp_aura.agent_tool_inventory ati on ati.tool_id = atm.tool_id
        where atm.agent_id = :agent_id
        order by ati.tool_id asc
        """
    )
    tools = [dict(row) for row in db.execute(tools_sql, {"agent_id": agent_id}).mappings().all()]

    docs_sql = text(
        """
        select
            rd.doc_id,
            rd.title,
            rd.status,
            rd.doc_type,
            rd.source_type,
            rd.version,
            rd.quality_gate_passed
        from dwp_aura.agent_document_mapping adm
        join dwp_aura.rag_document rd on rd.doc_id = adm.doc_id and rd.tenant_id = adm.tenant_id
        where adm.agent_id = :agent_id
        order by rd.doc_id asc
        """
    )
    documents = [dict(row) for row in db.execute(docs_sql, {"agent_id": agent_id}).mappings().all()]

    current_prompt = next((prompt for prompt in prompts if prompt.get("is_current")), prompts[0] if prompts else None)
    return {
        **dict(agent),
        "current_prompt": current_prompt,
        "prompt_history": prompts,
        "tools": tools,
        "documents": documents,
    }
