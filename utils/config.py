from __future__ import annotations

import os
from dataclasses import dataclass
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


settings = Settings()


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
