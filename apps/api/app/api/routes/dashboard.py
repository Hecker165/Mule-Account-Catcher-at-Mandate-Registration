"""Read-only operator dashboard API: redacted projections, audit chains, verification.

All routes are GET-only, set ``Cache-Control: no-store`` and read through A1
repositories. Responses are built field-by-field from explicitly listed
fields; repository rows are never forwarded wholesale.

The per-row latency enrichment is a bounded N+1 (at most 100 primary-key
reads against the local database): acceptable for a demo dashboard and kept
inside A1 interfaces. Reasons are mapped from the fetched ``RiskAssessment``
contract's triggered evaluations (rule_id, reason_code, reason_text, points),
which is the authoritative per-rule source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from app.contracts.common import ActionStatus, AuditActorType, Decision, DecisionStage
from app.repositories import AuditRepository, DashboardReadRepository, RiskAssessmentRepository

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_NO_STORE = "no-store"


class DashboardReasonItem(BaseModel):
    rule_id: str
    reason_code: str | None = None
    reason_text: str | None = None
    points: int = 0


class DashboardAssessmentItem(BaseModel):
    assessment_id: UUID
    decision: Decision
    score: int
    stage: DecisionStage
    assessed_at: datetime
    evaluation_latency_ms: int
    token_id: str | None = None
    vpa_handle: str | None = None
    is_demo_event: bool = False
    action_status: ActionStatus | None = None
    reasons: list[DashboardReasonItem] = Field(default_factory=list)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "assessment_id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f80",
                    "decision": "BLOCK",
                    "score": 80,
                    "stage": "POST_CONFIRMATION",
                    "assessed_at": "2026-01-15T10:02:03Z",
                    "evaluation_latency_ms": 12,
                    "token_id": "token_demo123",
                    "vpa_handle": "upi",
                    "is_demo_event": False,
                    "action_status": "SUCCEEDED",
                    "reasons": [
                        {
                            "rule_id": "velocity_device_burst",
                            "reason_code": "DEVICE_VELOCITY_BURST",
                            "reason_text": "Device made 7 registrations in 5 minutes.",
                            "points": 25,
                        }
                    ],
                }
            ]
        }
    }


class DashboardAssessmentsResponse(BaseModel):
    items: list[DashboardAssessmentItem]
    limit: int
    offset: int


class DashboardAuditItem(BaseModel):
    audit_event_id: UUID
    sequence_number: int
    event_type: str
    actor_type: AuditActorType
    actor_id: str | None = None
    occurred_at: datetime
    redacted_payload: dict[str, Any]


class AuditVerificationResponse(BaseModel):
    valid: bool
    checked_events: int
    first_invalid_sequence: int | None = None
    reason: str | None = None


AggregateType = Literal["risk_session", "mandate_event", "risk_assessment", "action_request"]


@router.get("/assessments", response_model=DashboardAssessmentsResponse)
async def list_assessments(
    request: Request,
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    decision: Decision | None = None,
) -> DashboardAssessmentsResponse:
    """Return the newest redacted assessment projections, newest first."""
    response.headers["Cache-Control"] = _NO_STORE
    async with request.app.state.session_factory() as session:
        rows = await DashboardReadRepository(session).list_recent_assessments(
            limit=limit, offset=offset, decision=decision
        )
        assessments = RiskAssessmentRepository(session)
        items: list[DashboardAssessmentItem] = []
        for row in rows:
            contract = await assessments.get(row.assessment_id)
            latency = contract.evaluation_latency_ms if contract is not None else 0
            reasons = [
                DashboardReasonItem(
                    rule_id=evaluation.rule_id,
                    reason_code=evaluation.reason_code,
                    reason_text=evaluation.reason_text,
                    points=evaluation.points,
                )
                for evaluation in (contract.rule_evaluations if contract is not None else [])
                if evaluation.triggered
            ]
            items.append(
                DashboardAssessmentItem(
                    assessment_id=row.assessment_id,
                    decision=row.decision,
                    score=row.score,
                    stage=DecisionStage(row.stage),
                    assessed_at=row.assessed_at,
                    evaluation_latency_ms=latency,
                    token_id=row.token_id,
                    vpa_handle=row.vpa_handle,
                    is_demo_event=row.is_demo_event,
                    action_status=ActionStatus(row.action_status)
                    if row.action_status is not None
                    else None,
                    reasons=reasons,
                )
            )
        return DashboardAssessmentsResponse(items=items, limit=limit, offset=offset)


@router.get("/audit-events", response_model=list[DashboardAuditItem])
async def list_audit_events(
    request: Request,
    response: Response,
    aggregate_type: AggregateType,
    aggregate_id: UUID,
    limit: int = Query(100, ge=1, le=500),
) -> list[DashboardAuditItem]:
    """Return one aggregate's audit chain, newest first (A1 payloads as-is)."""
    response.headers["Cache-Control"] = _NO_STORE
    async with request.app.state.session_factory() as session:
        events = await AuditRepository(session).list_for_aggregate(aggregate_type, aggregate_id)
        newest_first = list(reversed(events))[:limit]
        return [
            DashboardAuditItem(
                audit_event_id=event.audit_event_id,
                sequence_number=event.sequence_number,
                event_type=event.event_type,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                occurred_at=event.occurred_at,
                redacted_payload=event.redacted_payload,
            )
            for event in newest_first
        ]


@router.get("/audit-events/verify", response_model=AuditVerificationResponse)
async def verify_audit_events(
    request: Request,
    response: Response,
    aggregate_type: AggregateType,
    aggregate_id: UUID,
    limit: int = Query(100, ge=1, le=500),
) -> AuditVerificationResponse:
    """Verify one aggregate's full audit chain (missing aggregates verify valid/zero)."""
    del limit
    response.headers["Cache-Control"] = _NO_STORE
    async with request.app.state.session_factory() as session:
        result = await AuditRepository(session).verify_aggregate(aggregate_type, aggregate_id)
        return AuditVerificationResponse(
            valid=result.valid,
            checked_events=result.checked_events,
            first_invalid_sequence=result.first_invalid_sequence,
            reason=result.reason,
        )
