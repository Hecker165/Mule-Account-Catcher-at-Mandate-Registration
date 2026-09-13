"""A2 FastAPI dependencies for session capture and pre-check."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import Request

from app.core.settings import get_settings
from app.repositories import AuditRepository, RiskAssessmentRepository, RiskSessionRepository
from app.services.precheck_orchestrator import PrecheckOrchestrator
from app.services.precheck_provider import (
    PrecheckEvaluator,
    get_precheck_evaluator,
)
from app.services.privacy_hashing import HmacPseudonymizer
from app.services.session_capture import SessionCaptureService

__all__ = [
    "PrecheckEvaluator",
    "get_precheck_evaluator",
    "get_precheck_orchestrator",
    "get_session_capture_service",
]


def get_session_capture_service(request: Request) -> SessionCaptureService:
    """Build a SessionCaptureService from app state and settings."""
    settings = get_settings()
    return SessionCaptureService(
        session_factory=request.app.state.session_factory,
        risk_session_repo=RiskSessionRepository,
        audit_repo=AuditRepository,
        pseudonymizer=HmacPseudonymizer(settings.hmac_pepper),
        clock=lambda: datetime.now(UTC),
        uuid_factory=uuid.uuid4,
    )


def get_precheck_orchestrator(request: Request) -> PrecheckOrchestrator:
    """Build a PrecheckOrchestrator with the evaluator from app state."""
    return PrecheckOrchestrator(
        session_factory=request.app.state.session_factory,
        risk_session_repo=RiskSessionRepository,
        risk_assessment_repo=RiskAssessmentRepository,
        audit_repo=AuditRepository,
        evaluator=get_precheck_evaluator(request),
        clock=lambda: datetime.now(UTC),
    )
