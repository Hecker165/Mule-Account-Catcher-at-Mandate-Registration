"""A5 evaluation services: extract, score, persist and audit via A1.

Both services compose A4's ``FeatureExtractor`` with the rule engine and
persist snapshot + assessment + audit (+ revoke outbox) through A1 in the
caller's transaction. They never commit; the caller (A2/A3 route) owns the
transaction.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.action import ActionRequest
from app.contracts.common import (
    ActionStatus,
    ActionType,
    AuditActorType,
    Decision,
    DecisionStage,
    MandateEventType,
)
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_assessment import RiskAssessment
from app.contracts.risk_session import RiskSession
from app.domain.rules.engine import RuleEngine
from app.repositories import (
    ActionRepository,
    AuditRepository,
    FeatureSnapshotRepository,
    RiskAssessmentRepository,
    RiskSessionRepository,
)
from app.services.features.extractor import FeatureExtractor


class EvaluationInvariantError(RuntimeError):
    """The engine/extractor produced an assessment violating A0 stage invariants."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"evaluation invariant violated: {reason}")


class MandateEvaluationService:
    """Implements the A3 ``MandateEventEvaluator`` protocol."""

    def __init__(
        self,
        extractor: FeatureExtractor,
        engine: RuleEngine,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
        snapshot_repository: Callable[[AsyncSession], FeatureSnapshotRepository] = (
            FeatureSnapshotRepository
        ),
        assessment_repository: Callable[[AsyncSession], RiskAssessmentRepository] = (
            RiskAssessmentRepository
        ),
        action_repository: Callable[[AsyncSession], ActionRepository] = ActionRepository,
        audit_repository: Callable[[AsyncSession], AuditRepository] = AuditRepository,
        session_repository: Callable[[AsyncSession], RiskSessionRepository] = (
            RiskSessionRepository
        ),
    ) -> None:
        self._extractor = extractor
        self._engine = engine
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._snapshots = snapshot_repository
        self._assessments = assessment_repository
        self._actions = action_repository
        self._audits = audit_repository
        self._sessions = session_repository

    async def evaluate(self, db_session: AsyncSession, event: MandateWebhookEvent) -> None:
        """Extract, score, persist and audit one stored webhook event."""
        start = time.perf_counter()
        risk_session: RiskSession | None = None
        if event.risk_session_id is not None:
            risk_session = await self._sessions(db_session).get(event.risk_session_id)
        snapshot = await self._extractor.extract_for_event(event, risk_session, now=self._clock())

        stage = (
            DecisionStage.POST_CONFIRMATION
            if event.event_type == MandateEventType.TOKEN_CONFIRMED
            else DecisionStage.REJECTION_AUDIT
        )
        result = self._engine.evaluate(snapshot, stage)
        latency_ms = max(0, min(60_000, int((time.perf_counter() - start) * 1000)))
        assessment = RiskAssessment(
            schema_version="1.0",
            assessment_id=self._uuid_factory(),
            risk_session_id=event.risk_session_id,
            mandate_event_id=event.mandate_event_id,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            stage=stage,
            score=result.score,
            decision=result.decision,
            engine_version=self._engine.engine_version,
            rule_evaluations=list(result.rule_evaluations),
            evaluation_latency_ms=latency_ms,
            assessed_at=self._clock(),
        )
        _validate_assessment(assessment)
        await self._snapshots(db_session).create(snapshot)
        await self._assessments(db_session).create(assessment)
        await self._audits(db_session).append(
            aggregate_type="risk_assessment",
            aggregate_id=assessment.assessment_id,
            event_type="risk_assessment.completed",
            actor_type=AuditActorType.SYSTEM,
            actor_id="risk_engine",
            occurred_at=self._clock(),
            redacted_payload={
                "assessment_id": str(assessment.assessment_id),
                "stage": assessment.stage.value,
                "decision": assessment.decision.value,
                "score": assessment.score,
                "engine_version": assessment.engine_version,
                "evaluation_latency_ms": assessment.evaluation_latency_ms,
                "triggered_rule_count": sum(
                    1 for evaluation in result.rule_evaluations if evaluation.triggered
                ),
            },
        )
        if stage == DecisionStage.POST_CONFIRMATION and result.decision == Decision.BLOCK:
            await self._queue_revoke(db_session, event, assessment)

    async def _queue_revoke(
        self, db_session: AsyncSession, event: MandateWebhookEvent, assessment: RiskAssessment
    ) -> None:
        request = ActionRequest(
            schema_version="1.0",
            action_request_id=self._uuid_factory(),
            assessment_id=assessment.assessment_id,
            mandate_event_id=event.mandate_event_id,
            action_type=ActionType.TOKEN_REVOKE,
            token_id=event.token_id,
            idempotency_key=f"revoke-{event.mandate_event_id}",
            status=ActionStatus.QUEUED,
            requested_at=self._clock(),
        )
        outbox_payload = {
            "action_request_id": str(request.action_request_id),
            "mandate_event_id": str(event.mandate_event_id),
            "token_id": event.token_id,
            "idempotency_key": request.idempotency_key,
            "action_type": "TOKEN_REVOKE",
            "requested_at": request.requested_at.isoformat().replace("+00:00", "Z"),
        }
        await self._actions(db_session).create_request(request, outbox_payload)
        await self._audits(db_session).append(
            aggregate_type="action_request",
            aggregate_id=request.action_request_id,
            event_type="action_request.created",
            actor_type=AuditActorType.SYSTEM,
            actor_id="risk_engine",
            occurred_at=self._clock(),
            redacted_payload={
                "action_type": "TOKEN_REVOKE",
                "token_id": event.token_id,
                "idempotency_key": request.idempotency_key,
                "assessment_id": str(assessment.assessment_id),
            },
        )


class PrecheckEvaluationService:
    """Implements the A2 ``PrecheckEvaluator`` protocol."""

    def __init__(
        self,
        extractor: FeatureExtractor,
        engine: RuleEngine,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
        snapshot_repository: Callable[[AsyncSession], FeatureSnapshotRepository] = (
            FeatureSnapshotRepository
        ),
        assessment_repository: Callable[[AsyncSession], RiskAssessmentRepository] = (
            RiskAssessmentRepository
        ),
        audit_repository: Callable[[AsyncSession], AuditRepository] = AuditRepository,
    ) -> None:
        self._extractor = extractor
        self._engine = engine
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._snapshots = snapshot_repository
        self._assessments = assessment_repository
        self._audits = audit_repository

    async def assess(self, db_session: AsyncSession, risk_session: RiskSession) -> RiskAssessment:
        """Extract, score, persist and audit one READY risk session."""
        start = time.perf_counter()
        snapshot = await self._extractor.extract_for_session(risk_session, now=self._clock())
        result = self._engine.evaluate(snapshot, DecisionStage.PRECHECK)
        latency_ms = max(0, min(60_000, int((time.perf_counter() - start) * 1000)))
        assessment = RiskAssessment(
            schema_version="1.0",
            assessment_id=self._uuid_factory(),
            risk_session_id=risk_session.risk_session_id,
            mandate_event_id=None,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            stage=DecisionStage.PRECHECK,
            score=result.score,
            decision=result.decision,
            engine_version=self._engine.engine_version,
            rule_evaluations=list(result.rule_evaluations),
            evaluation_latency_ms=latency_ms,
            assessed_at=self._clock(),
        )
        _validate_assessment(assessment)
        await self._snapshots(db_session).create(snapshot)
        await self._assessments(db_session).create(assessment)
        await self._audits(db_session).append(
            aggregate_type="risk_assessment",
            aggregate_id=assessment.assessment_id,
            event_type="risk_assessment.completed",
            actor_type=AuditActorType.SYSTEM,
            actor_id="risk_engine",
            occurred_at=self._clock(),
            redacted_payload={
                "assessment_id": str(assessment.assessment_id),
                "stage": assessment.stage.value,
                "decision": assessment.decision.value,
                "score": assessment.score,
                "engine_version": assessment.engine_version,
                "evaluation_latency_ms": assessment.evaluation_latency_ms,
                "triggered_rule_count": sum(
                    1 for evaluation in result.rule_evaluations if evaluation.triggered
                ),
            },
        )
        return assessment


def _validate_assessment(assessment: RiskAssessment) -> None:
    if assessment.stage == DecisionStage.PRECHECK:
        if assessment.risk_session_id is None:
            raise EvaluationInvariantError("PRECHECK requires risk_session_id")
        if assessment.decision == Decision.BLOCK:
            raise EvaluationInvariantError("BLOCK is illegal at PRECHECK")
    else:
        if assessment.mandate_event_id is None:
            raise EvaluationInvariantError(f"{assessment.stage.value} requires mandate_event_id")
        if assessment.decision == Decision.CHALLENGE:
            raise EvaluationInvariantError(f"CHALLENGE is illegal at {assessment.stage.value}")
