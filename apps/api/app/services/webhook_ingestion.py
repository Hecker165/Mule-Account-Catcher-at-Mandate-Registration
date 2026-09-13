"""Idempotent webhook ingestion: correlation, A1 create_or_get and receipt audit."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.contracts.common import AuditActorType
from app.contracts.mandate_event import MandateWebhookEvent
from app.repositories.audit import AuditRepository
from app.repositories.mandate_events import MandateEventRepository
from app.repositories.risk_sessions import RiskSessionRepository
from app.services.mandate_evaluator_port import (
    MandateEvaluatorUnavailable,
    MandateEventEvaluator,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    mandate_event_id: UUID
    is_duplicate: bool
    correlation_status: str  # "correlated" | "unresolved" | "unavailable"
    evaluation_status: str  # "completed" | "not_configured" | "skipped_duplicate"


class WebhookIngestionService:
    """Correlates, persists and audits a parsed webhook event in one transaction.

    The caller opens the transaction; this service never commits.
    """

    def __init__(
        self,
        mandate_events: MandateEventRepository,
        audit: AuditRepository,
        risk_sessions: RiskSessionRepository,
        evaluator: MandateEventEvaluator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._mandate_events = mandate_events
        self._audit = audit
        self._risk_sessions = risk_sessions
        self._evaluator = evaluator
        self._clock = clock

    async def ingest(
        self,
        event: MandateWebhookEvent,
        notes_risk_session_id: UUID | None,
        notes_merchant_namespace: str | None,
        notes_checkout_order_ref: str | None,
    ) -> IngestResult:
        """Ingest one parsed event; duplicate deliveries short-circuit."""
        risk_session_id, correlation_status = await self._correlate(
            notes_risk_session_id, notes_merchant_namespace, notes_checkout_order_ref
        )
        if risk_session_id is not None:
            event = event.model_copy(update={"risk_session_id": risk_session_id})

        stored, created = await self._mandate_events.create_or_get(event)
        if not created:
            return IngestResult(
                mandate_event_id=stored.mandate_event_id,
                is_duplicate=True,
                correlation_status="unavailable",
                evaluation_status="skipped_duplicate",
            )

        evaluation_status = await self._evaluate(stored)

        await self._audit.append(
            aggregate_type="mandate_event",
            aggregate_id=stored.mandate_event_id,
            event_type="mandate_event.received",
            actor_type=AuditActorType.WEBHOOK,
            actor_id="razorpay_webhook",
            occurred_at=self._clock(),
            redacted_payload={
                "event_type": stored.event_type.value,
                "provider": stored.provider,
                "token_id": stored.token_id,
                "correlation_status": correlation_status,
                "risk_session_id": (
                    str(stored.risk_session_id) if stored.risk_session_id is not None else None
                ),
                "is_demo_event": stored.is_demo_event,
                "vpa_handle": stored.vpa_handle,
                "failure_reason_present": stored.failure_reason is not None,
                "evaluation_status": evaluation_status,
            },
        )

        logger.info(
            "webhook ingested mandate_event_id=%s correlation=%s evaluation=%s",
            stored.mandate_event_id,
            correlation_status,
            evaluation_status,
        )
        return IngestResult(
            mandate_event_id=stored.mandate_event_id,
            is_duplicate=False,
            correlation_status=correlation_status,
            evaluation_status=evaluation_status,
        )

    async def _correlate(
        self,
        notes_risk_session_id: UUID | None,
        notes_merchant_namespace: str | None,
        notes_checkout_order_ref: str | None,
    ) -> tuple[UUID | None, str]:
        if notes_risk_session_id is not None:
            session = await self._risk_sessions.get(notes_risk_session_id)
            if session is not None:
                return session.risk_session_id, "correlated"
            return None, "unresolved"
        if notes_merchant_namespace and notes_checkout_order_ref:
            session = await self._risk_sessions.get_by_order_ref(
                notes_merchant_namespace, notes_checkout_order_ref
            )
            if session is not None:
                return session.risk_session_id, "correlated"
        return None, "unavailable"

    async def _evaluate(self, stored: MandateWebhookEvent) -> str:
        try:
            await self._evaluator.evaluate(self._mandate_events.session, stored)
        except MandateEvaluatorUnavailable:
            return "not_configured"
        return "completed"
