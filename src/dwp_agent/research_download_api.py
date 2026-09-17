from __future__ import annotations

import json
from typing import Annotated, Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .personal_domain_security import (
    PersonalDomainIdentity,
    personal_domain_dependencies,
    require_personal_domain_identity,
)
from .research_download_contracts import ResearchRawDownload, ResearchReceiptDownload
from .research_download_store import get_research_download_store


router = APIRouter(dependencies=personal_domain_dependencies, tags=["research"])
T = TypeVar("T")


@router.get(
    "/v1/research/runs/{run_id}/downloads/raw",
    response_model=ResearchRawDownload,
)
def download_research_raw(
    run_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ResearchRawDownload:
    _access(identity)
    _download_headers(response, f"research-{run_id}-raw.json")
    return _run(lambda: get_research_download_store().raw(identity, run_id))


@router.get(
    "/v1/research/runs/{run_id}/downloads/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}},
)
def download_research_pdf(
    run_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
) -> Response:
    _access(identity)
    content = _run(lambda: get_research_download_store().pdf(identity, run_id))
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="research-{run_id}-report.pdf"',
        },
    )


@router.get(
    "/v1/research/runs/{run_id}/downloads/receipt",
    response_model=ResearchReceiptDownload,
)
def download_research_receipt(
    run_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ResearchReceiptDownload:
    _access(identity)
    _download_headers(response, f"research-{run_id}-receipt.json")
    return _run(lambda: get_research_download_store().receipt(identity, run_id))


@router.get(
    "/v1/research/runs/{run_id}/downloads/audit",
    response_class=Response,
    responses={200: {"content": {"application/x-ndjson": {"schema": {"type": "string"}}}}},
)
def download_research_audit(
    run_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
) -> Response:
    _access(identity)
    events = _run(lambda: get_research_download_store().audit(identity, run_id))
    content = "\n".join(
        json.dumps(event.model_dump(mode="json", by_alias=True), ensure_ascii=False)
        for event in events
    ) + "\n"
    return Response(
        content=content,
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="research-{run_id}-audit.jsonl"',
        },
    )


def _access(identity: PersonalDomainIdentity) -> None:
    identity.require("APP.ASK:VIEW", "APP.DWAION_RESEARCH:VIEW")


def _download_headers(response: Response, filename: str) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except DwaionWorkflowNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DwaionWorkflowConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DwaionWorkflowUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
