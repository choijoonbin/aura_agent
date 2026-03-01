from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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


settings = Settings()


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
