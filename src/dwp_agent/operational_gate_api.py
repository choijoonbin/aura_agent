from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, FastAPI, Header, Request, status
from fastapi.responses import JSONResponse

from .governance_store import GovernanceStoreUnavailable
from .operational_gate_contracts import (
    BootstrapOperationalGatesRequest,
    ConfigureOperationalGateRequest,
    CreateOperationalGateEvidenceRequest,
    DecideOperationalGateRequest,
    GateEnvironment,
    OperationalGateDetailEnvelope,
    OperationalGateKey,
    OperationalGatePortfolioEnvelope,
    OperationalGateProblem,
    ValidateOperationalGateRequest,
)
from .operational_gate_store_provider import get_operational_gate_store
from .operational_gate_store_errors import (
    OperationalGateConflict,
    OperationalGateInvalidTransition,
    OperationalGateMissingEvidence,
    OperationalGateSeparationOfDutyViolation,
)
from .security import header_values, require_gateway_service


_PROBLEM_RESPONSES = {
    403: {"model": OperationalGateProblem, "description": "Insufficient gate permission"},
    404: {"model": OperationalGateProblem, "description": "Gate not found"},
    409: {"model": OperationalGateProblem, "description": "Gate workflow conflict"},
    503: {"model": OperationalGateProblem, "description": "Gate store unavailable"},
}

router = APIRouter(
    prefix="/v1/admin/gates",
    tags=["operational-gates"],
    dependencies=[Depends(require_gateway_service)],
    responses=_PROBLEM_RESPONSES,
)


class OperationalGateApiProblem(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        context: dict[str, str | list[str]] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.context = context or {}
        super().__init__(detail)


def install_operational_gate_problem_handler(app: FastAPI) -> None:
    @app.exception_handler(OperationalGateApiProblem)
    async def operational_gate_problem_handler(
        request: Request, error: OperationalGateApiProblem
    ) -> JSONResponse:
        correlation_id = request.headers.get("X-Correlation-ID", "unknown")
        problem = OperationalGateProblem(
            type=f"urn:dwp:problem:operational-gates:{error.code.lower()}",
            title=error.title,
            status=error.status_code,
            detail=error.detail,
            code=error.code,
            instance=request.url.path,
            correlation_id=correlation_id,
            context=error.context,
        )
        return JSONResponse(
            status_code=error.status_code,
            content=problem.model_dump(mode="json", by_alias=True),
            media_type="application/problem+json",
        )


def _headers(
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, str, str, str | None]:
    return tenant_id, user_id, correlation_id, permissions


def _require_permission(permissions: str | None, permission: str) -> None:
    authorities = set(header_values(permissions))
    if (
        "ADMIN.DWAION_GATES:MANAGE" not in authorities
        and f"ADMIN.DWAION_GATES:{permission}" not in authorities
    ):
        raise OperationalGateApiProblem(
            status_code=status.HTTP_403_FORBIDDEN,
            code="GATE_PERMISSION_DENIED",
            title="Operational gate permission required",
            detail=f"ADMIN.DWAION_GATES:{permission} permission is required.",
        )


@router.get("", response_model=OperationalGatePortfolioEnvelope, response_model_by_alias=True)
def list_operational_gates(
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    environment: GateEnvironment = GateEnvironment.PRODUCTION,
):
    tenant, user, _correlation, permissions = headers
    _require_permission(permissions, "VIEW")
    try:
        return OperationalGatePortfolioEnvelope(
            data=get_operational_gate_store().portfolio(
                tenant_id=tenant,
                actor_user_id=user,
                environment=environment,
            )
        )
    except GovernanceStoreUnavailable as error:
        _raise_domain_problem(error)


@router.post(
    "/bootstrap",
    response_model=OperationalGatePortfolioEnvelope,
    response_model_by_alias=True,
)
def bootstrap_operational_gates(
    request: BootstrapOperationalGatesRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    environment: GateEnvironment = GateEnvironment.PRODUCTION,
):
    tenant, user, correlation, permissions = headers
    _require_permission(permissions, "MANAGE")
    try:
        return OperationalGatePortfolioEnvelope(
            message="DWAI-ON operational gates initialized in a blocked state.",
            data=get_operational_gate_store().bootstrap(
                tenant_id=tenant,
                actor_user_id=user,
                correlation_id=correlation,
                environment=environment,
                request=request,
            ),
        )
    except OperationalGateConflict as error:
        _raise_domain_problem(error)
    except GovernanceStoreUnavailable as error:
        _raise_domain_problem(error)


@router.get(
    "/{gate_key}",
    response_model=OperationalGateDetailEnvelope,
    response_model_by_alias=True,
)
def get_operational_gate(
    gate_key: OperationalGateKey,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    environment: GateEnvironment = GateEnvironment.PRODUCTION,
):
    tenant, user, _correlation, permissions = headers
    _require_permission(permissions, "VIEW")
    return _detail(tenant, user, environment, gate_key)


@router.patch(
    "/{gate_key}",
    response_model=OperationalGateDetailEnvelope,
    response_model_by_alias=True,
)
def configure_operational_gate(
    gate_key: OperationalGateKey,
    request: ConfigureOperationalGateRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    environment: GateEnvironment = GateEnvironment.PRODUCTION,
):
    tenant, user, correlation, permissions = headers
    _require_permission(permissions, "UPDATE")
    try:
        data = get_operational_gate_store().configure(
            tenant_id=tenant,
            actor_user_id=user,
            correlation_id=correlation,
            environment=environment,
            gate_key=gate_key,
            request=request,
        )
        return OperationalGateDetailEnvelope(
            message="DWAI-ON operational gate configured.", data=data
        )
    except (KeyError, OperationalGateConflict, OperationalGateInvalidTransition) as error:
        _raise_domain_problem(error)
    except GovernanceStoreUnavailable as error:
        _raise_domain_problem(error)


@router.post(
    "/{gate_key}/evidence",
    response_model=OperationalGateDetailEnvelope,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
def add_operational_gate_evidence(
    gate_key: OperationalGateKey,
    request: CreateOperationalGateEvidenceRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    environment: GateEnvironment = GateEnvironment.PRODUCTION,
):
    tenant, user, correlation, permissions = headers
    _require_permission(permissions, "CREATE")
    try:
        data = get_operational_gate_store().add_evidence(
            tenant_id=tenant,
            actor_user_id=user,
            correlation_id=correlation,
            environment=environment,
            gate_key=gate_key,
            request=request,
        )
        return OperationalGateDetailEnvelope(
            message="DWAI-ON operational gate evidence added.", data=data
        )
    except (KeyError, OperationalGateConflict, OperationalGateInvalidTransition) as error:
        _raise_domain_problem(error)
    except GovernanceStoreUnavailable as error:
        _raise_domain_problem(error)


@router.post(
    "/{gate_key}/validation",
    response_model=OperationalGateDetailEnvelope,
    response_model_by_alias=True,
)
def validate_operational_gate(
    gate_key: OperationalGateKey,
    request: ValidateOperationalGateRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    environment: GateEnvironment = GateEnvironment.PRODUCTION,
):
    tenant, user, correlation, permissions = headers
    _require_permission(permissions, "UPDATE")
    try:
        data = get_operational_gate_store().validate(
            tenant_id=tenant,
            actor_user_id=user,
            correlation_id=correlation,
            environment=environment,
            gate_key=gate_key,
            request=request,
        )
        return OperationalGateDetailEnvelope(
            message="DWAI-ON operational gate validation recorded.", data=data
        )
    except (KeyError, OperationalGateConflict, OperationalGateInvalidTransition) as error:
        _raise_domain_problem(error)
    except GovernanceStoreUnavailable as error:
        _raise_domain_problem(error)


@router.post(
    "/{gate_key}/decision",
    response_model=OperationalGateDetailEnvelope,
    response_model_by_alias=True,
)
def decide_operational_gate(
    gate_key: OperationalGateKey,
    request: DecideOperationalGateRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    environment: GateEnvironment = GateEnvironment.PRODUCTION,
):
    tenant, user, correlation, permissions = headers
    _require_permission(permissions, "APPROVE")
    try:
        data = get_operational_gate_store().decide(
            tenant_id=tenant,
            actor_user_id=user,
            correlation_id=correlation,
            environment=environment,
            gate_key=gate_key,
            request=request,
        )
        return OperationalGateDetailEnvelope(
            message="DWAI-ON operational gate decision recorded.", data=data
        )
    except (
        OperationalGateConflict,
        OperationalGateInvalidTransition,
        OperationalGateSeparationOfDutyViolation,
        KeyError,
    ) as error:
        _raise_domain_problem(error)
    except GovernanceStoreUnavailable as error:
        _raise_domain_problem(error)


def _detail(
    tenant_id: str,
    actor_user_id: str,
    environment: GateEnvironment,
    gate_key: OperationalGateKey,
) -> OperationalGateDetailEnvelope:
    try:
        return OperationalGateDetailEnvelope(
            data=get_operational_gate_store().detail(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                environment=environment,
                gate_key=gate_key,
            )
        )
    except KeyError as error:
        raise OperationalGateApiProblem(
            status_code=status.HTTP_404_NOT_FOUND,
            code="GATE_NOT_FOUND",
            title="Operational gate not found",
            detail="The requested operational gate was not found.",
        ) from error
    except GovernanceStoreUnavailable as error:
        _raise_domain_problem(error)


def _raise_domain_problem(error: Exception) -> NoReturn:
    if isinstance(error, KeyError):
        raise OperationalGateApiProblem(
            status_code=status.HTTP_404_NOT_FOUND,
            code="GATE_NOT_FOUND",
            title="Operational gate not found",
            detail="Initialize the operational gate portfolio before changing a gate.",
        ) from error
    if isinstance(error, OperationalGateMissingEvidence):
        raise OperationalGateApiProblem(
            status_code=status.HTTP_409_CONFLICT,
            code="GATE_REQUIRED_EVIDENCE_MISSING",
            title="Required evidence is incomplete",
            detail=str(error),
            context={"missingEvidenceTypes": list(error.missing_evidence_types)},
        ) from error
    if isinstance(error, OperationalGateSeparationOfDutyViolation):
        raise OperationalGateApiProblem(
            status_code=status.HTTP_409_CONFLICT,
            code="GATE_SEPARATION_OF_DUTY",
            title="Independent approval is required",
            detail=str(error),
            context={"conflictingRole": error.conflicting_role},
        ) from error
    if isinstance(error, OperationalGateConflict):
        raise OperationalGateApiProblem(
            status_code=status.HTTP_409_CONFLICT,
            code="GATE_VERSION_CONFLICT",
            title="Operational gate changed",
            detail=str(error),
        ) from error
    if isinstance(error, OperationalGateInvalidTransition):
        raise OperationalGateApiProblem(
            status_code=status.HTTP_409_CONFLICT,
            code="GATE_INVALID_TRANSITION",
            title="Operational gate action is not available",
            detail=str(error),
        ) from error
    raise OperationalGateApiProblem(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="GATE_STORE_UNAVAILABLE",
        title="Operational gate service unavailable",
        detail=str(error),
    ) from error
