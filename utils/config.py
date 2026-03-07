from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from langfuse.langchain import CallbackHandler

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "local")
    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "8010"))
    streamlit_port: int = int(os.getenv("STREAMLIT_PORT", "8502"))

    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://dwp_user:dwp_password@localhost:5432/dwp_aura",
    )

    default_tenant_id: int = int(os.getenv("DEFAULT_TENANT_ID", "1"))
    default_user_id: int = int(os.getenv("DEFAULT_USER_ID", "1"))

    aura_platform_path: str = os.getenv(
        "AURA_PLATFORM_PATH",
        "/Users/joonbinchoi/Work/dwp/aura-platform",
    )
    dwp_backend_path: str = os.getenv(
        "DWP_BACKEND_PATH",
        "/Users/joonbinchoi/Work/dwp/dwp-backend",
    )
    dwp_frontend_path: str = os.getenv(
        "DWP_FRONTEND_PATH",
        "/Users/joonbinchoi/Work/dwp/dwp-frontend",
    )
    api_base_url: str = os.getenv("API_BASE_URL", "http://localhost:8010")

    agent_runtime_mode: str = os.getenv("AGENT_RUNTIME_MODE", "langgraph")
    enable_multi_agent: bool = os.getenv("ENABLE_MULTI_AGENT", "true").lower() == "true"
    enable_langgraph_if_available: bool = os.getenv("ENABLE_LANGGRAPH_IF_AVAILABLE", "true").lower() == "true"
    enable_legacy_aura_specialist: bool = os.getenv("ENABLE_LEGACY_AURA_SPECIALIST", "false").lower() == "true"

    # Reasoning note LLM label (shown in UI when note_source is "llm")
    reasoning_llm_label: str = os.getenv("REASONING_LLM_LABEL", "LLM")
    enable_reasoning_live_llm: bool = os.getenv("ENABLE_REASONING_LIVE_LLM", "true").lower() == "true"
    reasoning_llm_model: str = os.getenv("REASONING_LLM_MODEL", "gpt-5")
    reasoning_stream_max_chars: int = int(os.getenv("REASONING_STREAM_MAX_CHARS", "5000"))
    reasoning_stream_max_sentences: int = int(os.getenv("REASONING_STREAM_MAX_SENTENCES", "10"))
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    openai_base_url: str | None = os.getenv("OPENAI_BASE_URL") or None
    openai_api_version: str = os.getenv("OPENAI_API_VERSION", "2024-12-01-preview")
    openai_embedding_model: str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    openai_embedding_dim: int = int(os.getenv("OPENAI_EMBEDDING_DIM", "3072"))
    rag_embedding_column: str = os.getenv("RAG_EMBEDDING_COLUMN", "embedding_az")
    rag_embedding_cast_type: str = os.getenv("RAG_EMBEDDING_CAST_TYPE", "halfvec")
    openai_embedding_batch_size: int = int(os.getenv("OPENAI_EMBEDDING_BATCH_SIZE", "64"))
    openai_embedding_max_retries: int = int(os.getenv("OPENAI_EMBEDDING_MAX_RETRIES", "3"))

    # Score engine weights
    score_policy_weight: float = float(os.getenv("SCORE_POLICY_WEIGHT", "0.6"))
    score_evidence_weight: float = float(os.getenv("SCORE_EVIDENCE_WEIGHT", "0.4"))
    score_compound_multiplier_max: float = float(os.getenv("SCORE_COMPOUND_MULTIPLIER_MAX", "1.5"))
    score_amount_multiplier_max: float = float(os.getenv("SCORE_AMOUNT_MULTIPLIER_MAX", "1.3"))

    # Langfuse (observability, optional)
    langfuse_enabled: bool = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
    langfuse_public_key: str | None = os.getenv("LANGFUSE_PUBLIC_KEY") or None
    langfuse_secret_key: str | None = os.getenv("LANGFUSE_SECRET_KEY") or None
    langfuse_host: str | None = os.getenv("LANGFUSE_HOST") or None

    # MCC 위험 분류 (단일 소스: screener.py, agent_tools.py에서 참조)
    mcc_high_risk: str = os.getenv("MCC_HIGH_RISK", "5813,7993,7994,5912,7992,5999")
    mcc_leisure: str = os.getenv("MCC_LEISURE", "7996,7997,7941,7011")
    mcc_medium_risk: str = os.getenv("MCC_MEDIUM_RISK", "5812,5811,5814,4722")
    mcc_source: str = os.getenv("MCC_SOURCE", "env")  # env | json | db
    mcc_json_path: str | None = os.getenv("MCC_JSON_PATH") or None
    mcc_db_table: str = os.getenv("MCC_DB_TABLE", "dwp_aura.mcc_risk_policy")

    enable_llm_planner: bool = os.getenv("ENABLE_LLM_PLANNER", "true").lower() == "true"
    enable_parallel_tool_execution: bool = os.getenv("ENABLE_PARALLEL_TOOL_EXECUTION", "true").lower() == "true"
    checkpointer_backend: str = os.getenv("CHECKPOINTER_BACKEND", "memory")

    # RAG: Dense 검색 HyDE (가설 문서 임베딩), Rerank LLM fallback
    enable_hyde_query: bool = os.getenv("ENABLE_HYDE_QUERY", "false").lower() == "true"
    enable_llm_rerank_fallback: bool = os.getenv("ENABLE_LLM_RERANK_FALLBACK", "true").lower() == "true"


settings = Settings()


def _mcc_set(raw: str) -> frozenset[str]:
    return frozenset(s.strip() for s in (raw or "").split(",") if s.strip())


def _parse_mcc_json(path: str) -> dict[str, frozenset[str]]:
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    high = payload.get("high_risk") or payload.get("highRisk") or []
    leisure = payload.get("leisure") or []
    medium = payload.get("medium_risk") or payload.get("mediumRisk") or []
    return {
        "high_risk": frozenset(str(v).strip() for v in high if str(v).strip()),
        "leisure": frozenset(str(v).strip() for v in leisure if str(v).strip()),
        "medium_risk": frozenset(str(v).strip() for v in medium if str(v).strip()),
    }


def _parse_mcc_db(table_name: str) -> dict[str, frozenset[str]]:
    # 기대 스키마: (mcc_code text, risk_level text)  risk_level ∈ HIGH/LEISURE/MEDIUM
    from sqlalchemy import create_engine, text

    engine = create_engine(settings.database_url, future=True)
    high: set[str] = set()
    leisure: set[str] = set()
    medium: set[str] = set()
    sql = text(f"SELECT mcc_code, risk_level FROM {table_name} WHERE mcc_code IS NOT NULL")
    with engine.connect() as conn:
        rows = conn.execute(sql).all()
    for row in rows:
        code = str(row[0] or "").strip()
        level = str(row[1] or "").strip().upper()
        if not code:
            continue
        if level == "HIGH":
            high.add(code)
        elif level == "LEISURE":
            leisure.add(code)
        elif level == "MEDIUM":
            medium.add(code)
    return {
        "high_risk": frozenset(high),
        "leisure": frozenset(leisure),
        "medium_risk": frozenset(medium),
    }


@lru_cache(maxsize=1)
def get_mcc_sets() -> dict[str, frozenset[str]]:
    env_sets = {
        "high_risk": _mcc_set(settings.mcc_high_risk),
        "leisure": _mcc_set(settings.mcc_leisure),
        "medium_risk": _mcc_set(settings.mcc_medium_risk),
    }

    source = (settings.mcc_source or "env").strip().lower()
    try:
        if source == "json" and settings.mcc_json_path:
            loaded = _parse_mcc_json(settings.mcc_json_path)
            if loaded["high_risk"] or loaded["leisure"] or loaded["medium_risk"]:
                return loaded
        if source == "db":
            loaded = _parse_mcc_db(settings.mcc_db_table)
            if loaded["high_risk"] or loaded["leisure"] or loaded["medium_risk"]:
                return loaded
    except Exception:
        # 운영 안전성: 외부 소스 실패 시 env 기본값으로 즉시 fallback
        return env_sets

    return env_sets


def refresh_mcc_sets() -> None:
    get_mcc_sets.cache_clear()


# 하위 호환용 상수 (기존 import 경로 유지)
MCC_HIGH_RISK: frozenset[str] = get_mcc_sets()["high_risk"]
MCC_LEISURE: frozenset[str] = get_mcc_sets()["leisure"]
MCC_MEDIUM_RISK: frozenset[str] = get_mcc_sets()["medium_risk"]


def get_langfuse_handler(session_id: str | None = None) -> "CallbackHandler | None":
    """
    Langfuse CallbackHandler를 반환합니다.
    - LANGFUSE_ENABLED=true 이고 PUBLIC/SECRET/HOST 가 설정된 경우에만 반환.
    - session_id(예: run_id)를 넘기면 Langfuse 대시보드에서 해당 실행을 세션별로 조회 가능.
    """
    if not settings.langfuse_enabled:
        return None
    if not (settings.langfuse_public_key and settings.langfuse_secret_key and settings.langfuse_host):
        return None
    try:
        from langfuse.langchain import CallbackHandler
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
        if session_id:
            os.environ["LANGFUSE_SESSION_ID"] = session_id
        return CallbackHandler(public_key=settings.langfuse_public_key)
    except Exception:
        return None


def ensure_source_paths() -> None:
    """PoC에서 레퍼런스 소스 경로 존재 여부를 점검한다."""
    required = [
        Path(settings.aura_platform_path),
        Path(settings.dwp_backend_path),
        Path(settings.dwp_frontend_path),
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise RuntimeError(f"Reference source path not found: {missing}")
