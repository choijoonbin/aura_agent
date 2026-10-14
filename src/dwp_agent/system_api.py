from __future__ import annotations

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import Field

from .contract_model import ContractModel
from .database_health import probe_database_status
from .operational_health import operational_health_snapshot
from .operations_api import require_dwaion_admin_view
from .security import require_gateway_service


class ModelProviderOperationalHealth(ContractModel):
    status: Literal[
        "AVAILABLE",
        "CONFIGURED",
        "INVALID_CONFIGURATION",
        "NOT_CONFIGURED",
        "UNAVAILABLE",
    ]
    provider: str = Field(min_length=1, max_length=40)
    configured: bool
    last_observed_at: str | None = None
    safe_error_code: str | None = Field(default=None, max_length=120)


class SourceOperationalHealth(ContractModel):
    status: Literal["SUCCESS", "UNAVAILABLE", "NOT_CONFIGURED"]
    last_observed_at: str


class SourcesOperationalHealth(ContractModel):
    status: Literal["UNKNOWN", "HEALTHY", "DEGRADED"]
    window_seconds: int = Field(ge=30, le=3_600)
    recent: dict[str, SourceOperationalHealth]


class OperationalHealthComponents(ContractModel):
    database: str
    model_provider: ModelProviderOperationalHealth
    sources: SourcesOperationalHealth


class OperationalHealthEnvelope(ContractModel):
    status: Literal["ok", "unavailable"]
    service: str
    version: str
    degraded: bool
    degradation_reasons: list[str]
    components: OperationalHealthComponents


class OperationalHealthError(ContractModel):
    detail: str


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

    @router.get(
        "/operational-health",
        tags=["system"],
        response_model=OperationalHealthEnvelope,
        response_model_by_alias=True,
        response_model_exclude_none=True,
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "model": OperationalHealthEnvelope | OperationalHealthError,
                "description": "Runtime dependency unavailable",
            }
        },
        dependencies=[
            Depends(require_gateway_service),
            Depends(require_dwaion_admin_view),
        ],
    )
    def operational_health(
        response: Response,
        tenant_id: Annotated[
            str,
            Header(alias="X-DWP-Tenant-ID", min_length=1, max_length=64),
        ],
    ) -> dict[str, object]:
        database = probe_database_status()
        ready = _runtime_ready(database)
        operational = operational_health_snapshot(tenant_id=tenant_id)
        degraded = not ready or bool(operational["degraded"])
        reasons = (
            ([f"DATABASE_{database}"] if not ready else [])
            + list(operational["degradationReasons"])
        )
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if ready else "unavailable",
            "service": service_name,
            "version": service_version,
            "degraded": degraded,
            "degradationReasons": reasons,
            "components": {
                "database": database,
                "modelProvider": operational["modelProvider"],
                "sources": operational["sources"],
            },
        }

    return router
