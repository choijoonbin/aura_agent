from __future__ import annotations

import os

from fastapi import APIRouter, Response, status

from .database_health import probe_database_status


def _runtime_ready(state: str) -> bool:
    return state == "READY" or (
        os.getenv("DWP_ENVIRONMENT", "local").strip().lower() == "local"
        and state == "DISABLED"
    )


def build_system_router(*, service_name: str, service_version: str) -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": service_name,
            "version": service_version,
            "docs": "/docs",
        }

    @router.get("/livez", tags=["system"])
    def livez() -> dict[str, str]:
        return {
            "status": "alive",
            "service": service_name,
            "version": service_version,
        }

    @router.get("/readyz", tags=["system"])
    def readyz(response: Response) -> dict[str, str | dict[str, str]]:
        database = probe_database_status()
        ready = _runtime_ready(database)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if ready else "not_ready",
            "service": service_name,
            "version": service_version,
            "components": {"database": database},
        }

    @router.get("/health", tags=["system"])
    def health(response: Response) -> dict[str, str | dict[str, str]]:
        database = probe_database_status()
        ready = _runtime_ready(database)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if ready else "unavailable",
            "service": service_name,
            "version": service_version,
            "components": {"database": database},
        }

    return router
