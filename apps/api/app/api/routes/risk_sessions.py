"""A2 risk session API routes.

Endpoints for session creation, telemetry recording, and pre-check.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from app.api.dependencies import get_precheck_orchestrator, get_session_capture_service
from app.contracts.risk_assessment import RiskAssessment
from app.contracts.risk_session import (
    RiskSession,
    RiskSessionCreateRequest,
    RiskSessionTelemetryUpdateRequest,
)
from app.services.precheck_orchestrator import (
    InvalidPrecheckAssessment,
    PrecheckResult,
    PrecheckUnavailable,
    RiskSessionInconsistent,
    RiskSessionNotFound,
    RiskSessionNotReady,
)
from app.services.session_capture import CreateSessionResult

router = APIRouter(prefix="/risk-sessions", tags=["risk-sessions"])


@router.post(
    "",
    response_model=RiskSession,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "description": "Idempotent replay",
            "headers": {"X-Idempotent-Replay": {"schema": {"type": "string"}}},
        },
    },
)
async def create_risk_session(
    request: Request,
    response: Response,
    body: RiskSessionCreateRequest,
    x_forwarded_for: str | None = Header(None),
    user_agent: str | None = Header(None),
) -> RiskSession:
    """Create a new risk session or return existing one on idempotent replay."""
    service = get_session_capture_service(request)
    # Use direct peer IP, not X-Forwarded-For
    peer_ip = request.client.host if request.client else None

    result: CreateSessionResult = await service.create(
        request=body,
        peer_ip=peer_ip,
        request_user_agent=user_agent,
    )

    if result.is_replay:
        response.status_code = status.HTTP_200_OK
        response.headers["X-Idempotent-Replay"] = "true"

    response.headers["Cache-Control"] = "no-store"
    return result.session


@router.patch(
    "/{risk_session_id}/telemetry",
    response_model=RiskSession,
    responses={
        404: {"description": "Risk session not found"},
        409: {"description": "Risk session is already consumed"},
    },
)
async def record_telemetry(
    request: Request,
    response: Response,
    risk_session_id: UUID,
    body: RiskSessionTelemetryUpdateRequest,
    x_forwarded_for: str | None = Header(None),
    user_agent: str | None = Header(None),
) -> RiskSession:
    """Record telemetry and transition session to READY."""
    service = get_session_capture_service(request)
    peer_ip = request.client.host if request.client else None

    try:
        updated = await service.record_telemetry(
            risk_session_id=risk_session_id,
            request=body,
            peer_ip=peer_ip,
            request_user_agent=user_agent,
        )
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="risk session not found",
                headers={"Cache-Control": "no-store"},
            ) from None
        if "already" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="risk session is already consumed",
                headers={"Cache-Control": "no-store"},
            ) from None
        raise

    response.headers["Cache-Control"] = "no-store"
    return updated


@router.post(
    "/{risk_session_id}/precheck",
    response_model=RiskAssessment,
    responses={
        404: {"description": "Risk session not found"},
        409: {"description": "Risk session telemetry is not ready / inconsistent state"},
        503: {"description": "Precheck evaluator is unavailable"},
    },
)
async def request_precheck(
    request: Request,
    response: Response,
    risk_session_id: UUID,
) -> RiskAssessment:
    """Request a pre-registration risk assessment."""
    orchestrator = get_precheck_orchestrator(request)
    try:
        result: PrecheckResult = await orchestrator.assess(risk_session_id)
    except RiskSessionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="risk session not found",
            headers={"Cache-Control": "no-store"},
        ) from None
    except RiskSessionNotReady:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="risk session telemetry is not ready",
            headers={"Cache-Control": "no-store"},
        ) from None
    except RiskSessionInconsistent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="risk session has inconsistent consumed state",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PrecheckUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="precheck evaluator is unavailable",
            headers={"Cache-Control": "no-store"},
        ) from None
    except InvalidPrecheckAssessment:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="precheck evaluator returned an invalid assessment",
            headers={"Cache-Control": "no-store"},
        ) from None

    if result.is_replay:
        response.headers["X-Idempotent-Replay"] = "true"

    response.headers["Cache-Control"] = "no-store"
    return result.assessment
