"""Pre-check orchestrator for A2 session capture.

Implements the transactional pre-check flow with explicit error types
for the route layer to map to HTTP status codes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.contracts.common import AuditActorType, RiskSessionStatus
from app.contracts.risk_assessment import RiskAssessment
from app.repositories import (
    AuditRepository,
    RiskAssessmentRepository,
    RiskSessionRepository,
)
from app.services.precheck_provider import (
    PrecheckEvaluator,
    PrecheckEvaluatorUnavailable,
)


class RiskSessionNotFound(RuntimeError):
    """Raised when a risk session does not exist."""

    def __init__(self, risk_session_id: UUID) -> None:
        self.risk_session_id = risk_session_id
        super().__init__(f"risk session {risk_session_id} not found")


class RiskSessionNotReady(RuntimeError):
    """Raised when a session is not in READY status for pre-check."""

    def __init__(self, risk_session_id: UUID, current_status: RiskSessionStatus) -> None:
        self.risk_session_id = risk_session_id
        self.current_status = current_status
        msg = f"risk session {risk_session_id} is {current_status.value}, expected READY"
        super().__init__(msg)


class RiskSessionInconsistent(RuntimeError):
    """Raised when a consumed session lacks a pre-check assessment."""

    def __init__(self, risk_session_id: UUID) -> None:
        self.risk_session_id = risk_session_id
        msg = f"risk session {risk_session_id} is CONSUMED but lacks pre-check assessment"
        super().__init__(msg)


class PrecheckUnavailable(RuntimeError):
    """Raised when the evaluator is not installed."""

    def __init__(self) -> None:
        super().__init__("precheck evaluator is unavailable")


class InvalidPrecheckAssessment(RuntimeError):
    """Raised when the evaluator returns an invalid assessment."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"precheck evaluator returned an invalid assessment: {reason}")


@dataclass(frozen=True)
class PrecheckResult:
    """Result of a pre-check operation."""

    assessment: RiskAssessment
    is_replay: bool


class PrecheckOrchestrator:
    """Orchestrates the pre-check flow with A1 repositories and evaluator."""

    def __init__(
        self,
        session_factory: Callable[..., Any],
        risk_session_repo: type[RiskSessionRepository],
        risk_assessment_repo: type[RiskAssessmentRepository],
        audit_repo: type[AuditRepository],
        evaluator: PrecheckEvaluator,
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._risk_session_repo = risk_session_repo
        self._risk_assessment_repo = risk_assessment_repo
        self._audit_repo = audit_repo
        self._evaluator = evaluator
        self._clock = clock

    async def assess(self, risk_session_id: UUID) -> PrecheckResult:
        """Execute the pre-check flow for a risk session."""
        async with self._session_factory() as session:
            session_repo = self._risk_session_repo(session)
            assessment_repo = self._risk_assessment_repo(session)
            audit_repo = self._audit_repo(session)

            # Load session
            risk_session = await session_repo.get(risk_session_id)
            if risk_session is None:
                raise RiskSessionNotFound(risk_session_id)

            # Handle CONSUMED session - return existing pre-check
            if risk_session.status == RiskSessionStatus.CONSUMED:
                existing = await assessment_repo.get_precheck_for_session(risk_session_id)
                if existing is None:
                    raise RiskSessionInconsistent(risk_session_id)
                return PrecheckResult(assessment=existing, is_replay=True)

            # Must be READY
            if risk_session.status != RiskSessionStatus.READY:
                raise RiskSessionNotReady(risk_session_id, risk_session.status)

            # Invoke evaluator
            try:
                assessment = await self._evaluator.assess(session, risk_session)
            except PrecheckEvaluatorUnavailable:
                raise PrecheckUnavailable() from None

            # Validate assessment invariants
            if assessment.stage.value != "PRECHECK":
                raise InvalidPrecheckAssessment("stage must be PRECHECK")
            if assessment.risk_session_id != risk_session_id:
                raise InvalidPrecheckAssessment("risk_session_id mismatch")
            if assessment.mandate_event_id is not None:
                raise InvalidPrecheckAssessment("mandate_event_id must be None for PRECHECK")

            # NOTE: per A2 spec §4.3, the evaluator (A5 PrecheckEvaluationService)
            # is responsible for producing AND persisting the FeatureSnapshot and
            # RiskAssessment in this transaction. A2 only validates the returned
            # assessment, consumes the session and appends the audit event.

            # Mark session CONSUMED
            now = self._clock()
            await session_repo.update_status(risk_session_id, RiskSessionStatus.CONSUMED, now)

            # Audit event: risk_session.precheck_completed
            await audit_repo.append(
                aggregate_type="risk_session",
                aggregate_id=risk_session_id,
                event_type="risk_session.precheck_completed",
                actor_type=AuditActorType.SYSTEM,
                actor_id="precheck_api",
                occurred_at=now,
                redacted_payload={
                    "assessment_id": str(assessment.assessment_id),
                    "decision": assessment.decision.value,
                    "score": assessment.score,
                    "engine_version": assessment.engine_version,
                    "evaluation_latency_ms": assessment.evaluation_latency_ms,
                },
            )

            await session.commit()

            return PrecheckResult(assessment=assessment, is_replay=False)
