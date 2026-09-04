"""Contract-to-ORM conversion helpers for A1 persistence.

Explicit conversion functions for each A0 contract to/from ORM models.
Uses only JSON-safe model_dump(mode="json") values for JSONB columns.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.contracts import FeatureSource
from app.contracts.action import ActionAttempt, ActionRequest
from app.contracts.audit import AuditEvent
from app.contracts.common import (
    ActionStatus,
    ActionType,
    AuditActorType,
    Decision,
    DecisionStage,
    MandateEventType,
    RiskSessionStatus,
)
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.contracts.risk_session import (
    MandateIntent,
    RiskSession,
)
from app.persistence.models import (
    ActionAttempt as ActionAttemptORM,
)
from app.persistence.models import (
    ActionRequest as ActionRequestORM,
)
from app.persistence.models import (
    AuditEvent as AuditEventORM,
)
from app.persistence.models import (
    FeatureSnapshot as FeatureSnapshotORM,
)
from app.persistence.models import (
    MandateWebhookEvent as MandateWebhookEventORM,
)
from app.persistence.models import (
    OutboxMessage as OutboxMessageORM,
)
from app.persistence.models import (
    RiskAssessment as RiskAssessmentORM,
)
from app.persistence.models import (
    RiskSession as RiskSessionORM,
)
from app.persistence.models import (
    RuleEvaluation as RuleEvaluationORM,
)

# ──────────────────────────────────────────────────────────────────────────────
# RiskSession conversions
# ──────────────────────────────────────────────────────────────────────────────


def risk_session_to_row(session: RiskSession) -> dict[str, Any]:
    """Convert RiskSession contract to ORM row dict."""
    return {
        "risk_session_id": session.risk_session_id,
        "schema_version": session.schema_version,
        "merchant_namespace": session.merchant_namespace,
        "checkout_order_ref": session.checkout_order_ref,
        "customer_reference_hash": session.customer_reference_hash,
        "ip_hash": session.ip_hash,
        "device_fingerprint_hash": session.device_fingerprint_hash,
        "user_agent_hash": session.user_agent_hash,
        "flow_started_at": session.flow_started_at,
        "flow_completed_at": session.flow_completed_at,
        "mandate_intent": _mandate_intent_to_json(session.mandate_intent),
        "status": session.status.value
        if isinstance(session.status, RiskSessionStatus)
        else session.status,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def risk_session_from_row(row: RiskSessionORM) -> RiskSession:
    """Convert ORM row to RiskSession contract."""
    return RiskSession(
        schema_version=row.schema_version,
        risk_session_id=row.risk_session_id,
        merchant_namespace=row.merchant_namespace,
        checkout_order_ref=row.checkout_order_ref,
        customer_reference_hash=row.customer_reference_hash,
        ip_hash=row.ip_hash,
        device_fingerprint_hash=row.device_fingerprint_hash,
        user_agent_hash=row.user_agent_hash,
        flow_started_at=row.flow_started_at,
        flow_completed_at=row.flow_completed_at,
        mandate_intent=_mandate_intent_from_json(row.mandate_intent),
        status=RiskSessionStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _mandate_intent_to_json(intent: MandateIntent) -> dict[str, Any]:
    """Convert MandateIntent to JSON-serializable dict."""
    return intent.model_dump(mode="json", exclude_none=True)


def _mandate_intent_from_json(data: dict[str, Any]) -> MandateIntent:
    """Convert JSON dict to MandateIntent."""
    return MandateIntent.model_validate(data)


# ──────────────────────────────────────────────────────────────────────────────
# MandateWebhookEvent conversions
# ──────────────────────────────────────────────────────────────────────────────


def mandate_event_to_row(event: MandateWebhookEvent) -> dict[str, Any]:
    """Convert MandateWebhookEvent contract to ORM row dict."""
    return {
        "mandate_event_id": event.mandate_event_id,
        "schema_version": event.schema_version,
        "provider": event.provider,
        "provider_event_id": event.provider_event_id,
        "event_type": event.event_type.value
        if isinstance(event.event_type, MandateEventType)
        else event.event_type,
        "token_id": event.token_id,
        "risk_session_id": event.risk_session_id,
        "vpa_hash": event.vpa_hash,
        "vpa_handle": event.vpa_handle,
        "recurring_status": event.recurring_status,
        "failure_reason": event.failure_reason,
        "provider_created_at": event.provider_created_at,
        "received_at": event.received_at,
        "raw_payload_sha256": event.raw_payload_sha256,
        "is_demo_event": event.is_demo_event,
    }


def mandate_event_from_row(row: MandateWebhookEventORM) -> MandateWebhookEvent:
    """Convert ORM row to MandateWebhookEvent contract."""
    return MandateWebhookEvent(
        schema_version=row.schema_version,
        mandate_event_id=row.mandate_event_id,
        provider=row.provider,
        provider_event_id=row.provider_event_id,
        event_type=MandateEventType(row.event_type),
        token_id=row.token_id,
        risk_session_id=row.risk_session_id,
        vpa_hash=row.vpa_hash,
        vpa_handle=row.vpa_handle,
        recurring_status=row.recurring_status,
        failure_reason=row.failure_reason,
        provider_created_at=row.provider_created_at,
        received_at=row.received_at,
        raw_payload_sha256=row.raw_payload_sha256,
        is_demo_event=row.is_demo_event,
    )


# ──────────────────────────────────────────────────────────────────────────────
# FeatureSnapshot conversions
# ──────────────────────────────────────────────────────────────────────────────


def feature_snapshot_to_row(snapshot: FeatureSnapshot) -> dict[str, Any]:
    """Convert FeatureSnapshot contract to ORM row dict."""
    return {
        "feature_snapshot_id": snapshot.feature_snapshot_id,
        "schema_version": snapshot.schema_version,
        "mandate_event_id": snapshot.mandate_event_id,
        "risk_session_id": snapshot.risk_session_id,
        "feature_version": snapshot.feature_version,
        "calculated_at": snapshot.calculated_at,
        "ip_velocity_5m": snapshot.ip_velocity_5m,
        "ip_velocity_1h": snapshot.ip_velocity_1h,
        "device_velocity_5m": snapshot.device_velocity_5m,
        "device_velocity_1h": snapshot.device_velocity_1h,
        "customer_velocity_1h": snapshot.customer_velocity_1h,
        "vpa_velocity_1h": snapshot.vpa_velocity_1h,
        "shared_demo_merchant_count_1h": snapshot.shared_demo_merchant_count_1h,
        "is_new_device_for_customer": snapshot.is_new_device_for_customer,
        "flow_duration_seconds": snapshot.flow_duration_seconds,
        "user_agent_bot_suspected": snapshot.user_agent_bot_suspected,
        "network_vpn_or_proxy": snapshot.network_vpn_or_proxy,
        "network_reputation_score": snapshot.network_reputation_score,
        "npci_risk_flag": snapshot.npci_risk_flag,
        "payer_bank_or_compliance_flag": snapshot.payer_bank_or_compliance_flag,
        "max_amount_paise": snapshot.max_amount_paise,
        "mandate_frequency": snapshot.mandate_frequency,
        "expiry_days_from_registration": snapshot.expiry_days_from_registration,
        "sources": _sources_to_json(snapshot.sources),
        "is_demo_simulation": snapshot.is_demo_simulation,
    }


def feature_snapshot_from_row(row: FeatureSnapshotORM) -> FeatureSnapshot:
    """Convert ORM row to FeatureSnapshot contract."""
    return FeatureSnapshot(
        schema_version=row.schema_version,
        feature_snapshot_id=row.feature_snapshot_id,
        mandate_event_id=row.mandate_event_id,
        risk_session_id=row.risk_session_id,
        feature_version=row.feature_version,
        calculated_at=row.calculated_at,
        ip_velocity_5m=row.ip_velocity_5m,
        ip_velocity_1h=row.ip_velocity_1h,
        device_velocity_5m=row.device_velocity_5m,
        device_velocity_1h=row.device_velocity_1h,
        customer_velocity_1h=row.customer_velocity_1h,
        vpa_velocity_1h=row.vpa_velocity_1h,
        shared_demo_merchant_count_1h=row.shared_demo_merchant_count_1h,
        is_new_device_for_customer=row.is_new_device_for_customer,
        flow_duration_seconds=row.flow_duration_seconds,
        user_agent_bot_suspected=row.user_agent_bot_suspected,
        network_vpn_or_proxy=row.network_vpn_or_proxy,
        network_reputation_score=row.network_reputation_score,
        npci_risk_flag=row.npci_risk_flag,
        payer_bank_or_compliance_flag=row.payer_bank_or_compliance_flag,
        max_amount_paise=row.max_amount_paise,
        mandate_frequency=row.mandate_frequency,
        expiry_days_from_registration=row.expiry_days_from_registration,
        sources=_sources_from_json(row.sources),
        is_demo_simulation=row.is_demo_simulation,
    )


def _sources_to_json(sources: dict[str, FeatureSource]) -> dict[str, Any]:
    """Convert sources dict to JSON-serializable dict."""
    return {k: v.model_dump(mode="json", exclude_none=True) for k, v in sources.items()}


def _sources_from_json(data: dict[str, Any]) -> dict[str, FeatureSource]:
    """Convert JSON dict to sources dict."""
    return {k: FeatureSource.model_validate(v) for k, v in data.items()}


# ──────────────────────────────────────────────────────────────────────────────
# RiskAssessment conversions
# ──────────────────────────────────────────────────────────────────────────────


def risk_assessment_to_row(assessment: RiskAssessment) -> dict[str, Any]:
    """Convert RiskAssessment contract to ORM row dict."""
    return {
        "assessment_id": assessment.assessment_id,
        "schema_version": assessment.schema_version,
        "risk_session_id": assessment.risk_session_id,
        "mandate_event_id": assessment.mandate_event_id,
        "feature_snapshot_id": assessment.feature_snapshot_id,
        "stage": assessment.stage.value
        if isinstance(assessment.stage, DecisionStage)
        else assessment.stage,
        "score": assessment.score,
        "decision": assessment.decision.value
        if isinstance(assessment.decision, Decision)
        else assessment.decision,
        "engine_version": assessment.engine_version,
        "evaluation_latency_ms": assessment.evaluation_latency_ms,
        "assessed_at": assessment.assessed_at,
    }


def risk_assessment_from_row(
    row: RiskAssessmentORM, rule_evaluations: Sequence[RuleEvaluationORM] | None = None
) -> RiskAssessment:
    """Convert ORM row to RiskAssessment contract."""
    evaluations = []
    if rule_evaluations:
        evaluations = [rule_evaluation_from_row(re) for re in rule_evaluations]

    return RiskAssessment(
        schema_version=row.schema_version,
        assessment_id=row.assessment_id,
        risk_session_id=row.risk_session_id,
        mandate_event_id=row.mandate_event_id,
        feature_snapshot_id=row.feature_snapshot_id,
        stage=DecisionStage(row.stage),
        score=row.score,
        decision=Decision(row.decision),
        engine_version=row.engine_version,
        rule_evaluations=evaluations,
        evaluation_latency_ms=row.evaluation_latency_ms,
        assessed_at=row.assessed_at,
    )


def rule_evaluation_to_row(
    evaluation: RuleEvaluation, assessment_id: UUID, position: int
) -> dict[str, Any]:
    """Convert RuleEvaluation contract to ORM row dict."""
    return {
        "assessment_id": assessment_id,
        "rule_id": evaluation.rule_id,
        "triggered": evaluation.triggered,
        "points": evaluation.points,
        "reason_code": evaluation.reason_code,
        "reason_text": evaluation.reason_text,
        "position": position,
    }


def rule_evaluation_from_row(row: RuleEvaluationORM) -> RuleEvaluation:
    """Convert ORM row to RuleEvaluation contract."""
    return RuleEvaluation(
        rule_id=row.rule_id,
        triggered=row.triggered,
        points=row.points,
        reason_code=row.reason_code,
        reason_text=row.reason_text,
    )


# ──────────────────────────────────────────────────────────────────────────────
# ActionRequest / ActionAttempt conversions
# ──────────────────────────────────────────────────────────────────────────────


def action_request_to_row(request: ActionRequest) -> dict[str, Any]:
    """Convert ActionRequest contract to ORM row dict."""
    return {
        "action_request_id": request.action_request_id,
        "schema_version": request.schema_version,
        "assessment_id": request.assessment_id,
        "mandate_event_id": request.mandate_event_id,
        "action_type": request.action_type.value
        if isinstance(request.action_type, ActionType)
        else request.action_type,
        "token_id": request.token_id,
        "idempotency_key": request.idempotency_key,
        "status": request.status.value
        if isinstance(request.status, ActionStatus)
        else request.status,
        "requested_at": request.requested_at,
    }


def action_request_from_row(row: ActionRequestORM) -> ActionRequest:
    """Convert ORM row to ActionRequest contract."""
    return ActionRequest(
        schema_version=row.schema_version,
        action_request_id=row.action_request_id,
        assessment_id=row.assessment_id,
        mandate_event_id=row.mandate_event_id,
        action_type=ActionType(row.action_type),
        token_id=row.token_id,
        idempotency_key=row.idempotency_key,
        status=ActionStatus(row.status),
        requested_at=row.requested_at,
    )


def action_attempt_to_row(attempt: ActionAttempt) -> dict[str, Any]:
    """Convert ActionAttempt contract to ORM row dict."""
    return {
        "action_attempt_id": attempt.action_attempt_id,
        "schema_version": attempt.schema_version,
        "action_request_id": attempt.action_request_id,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status.value
        if isinstance(attempt.status, ActionStatus)
        else attempt.status,
        "provider_status_code": attempt.provider_status_code,
        "safe_error_code": attempt.safe_error_code,
        "safe_error_message": attempt.safe_error_message,
        "attempted_at": attempt.attempted_at,
        "completed_at": attempt.completed_at,
    }


def action_attempt_from_row(row: ActionAttemptORM) -> ActionAttempt:
    """Convert ORM row to ActionAttempt contract."""
    return ActionAttempt(
        schema_version=row.schema_version,
        action_attempt_id=row.action_attempt_id,
        action_request_id=row.action_request_id,
        attempt_number=row.attempt_number,
        status=ActionStatus(row.status),
        provider_status_code=row.provider_status_code,
        safe_error_code=row.safe_error_code,
        safe_error_message=row.safe_error_message,
        attempted_at=row.attempted_at,
        completed_at=row.completed_at,
    )


# ──────────────────────────────────────────────────────────────────────────────
# AuditEvent conversions
# ──────────────────────────────────────────────────────────────────────────────


def audit_event_to_row(event: AuditEvent) -> dict[str, Any]:
    """Convert AuditEvent contract to ORM row dict."""
    return {
        "audit_event_id": event.audit_event_id,
        "schema_version": event.schema_version,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "sequence_number": event.sequence_number,
        "event_type": event.event_type,
        "actor_type": event.actor_type.value
        if isinstance(event.actor_type, AuditActorType)
        else event.actor_type,
        "actor_id": event.actor_id,
        "occurred_at": event.occurred_at,
        "redacted_payload": event.redacted_payload,
        "previous_event_hash": event.previous_event_hash,
        "event_hash": event.event_hash,
    }


def audit_event_from_row(row: AuditEventORM) -> AuditEvent:
    """Convert ORM row to AuditEvent contract."""
    return AuditEvent(
        schema_version=row.schema_version,
        audit_event_id=row.audit_event_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        sequence_number=row.sequence_number,
        event_type=row.event_type,
        actor_type=AuditActorType(row.actor_type),
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
        redacted_payload=row.redacted_payload,
        previous_event_hash=row.previous_event_hash,
        event_hash=row.event_hash,
    )


# ──────────────────────────────────────────────────────────────────────────────
# OutboxMessage conversions
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutboxMessage:
    """Internal immutable dataclass representing an outbox message.

    ORM model objects must not cross package boundaries.
    Supports both attribute access and key subscription for compatibility.
    """

    outbox_message_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    message_type: str
    payload: dict[str, Any]
    idempotency_key: str
    status: str
    attempt_count: int
    available_at: datetime
    locked_at: datetime | None = None
    locked_by: str | None = None
    processed_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime | None = None

    def __getitem__(self, item: str) -> Any:
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


def outbox_message_to_row(
    aggregate_type: str,
    aggregate_id: UUID,
    message_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    available_at: datetime,
) -> dict[str, Any]:
    """Create ORM row dict for OutboxMessage."""
    return {
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "message_type": message_type,
        "payload": payload,
        "idempotency_key": idempotency_key,
        "status": "PENDING",
        "attempt_count": 0,
        "available_at": available_at,
    }


def outbox_message_from_row(row: OutboxMessageORM) -> OutboxMessage:
    """Convert ORM row to OutboxMessage internal dataclass."""
    return OutboxMessage(
        outbox_message_id=row.outbox_message_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        message_type=row.message_type,
        payload=row.payload,
        idempotency_key=row.idempotency_key,
        status=row.status,
        attempt_count=row.attempt_count,
        available_at=row.available_at,
        locked_at=row.locked_at,
        locked_by=row.locked_by,
        processed_at=row.processed_at,
        last_error_code=row.last_error_code,
        created_at=row.created_at,
    )
