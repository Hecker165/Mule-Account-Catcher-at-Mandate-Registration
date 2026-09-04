"""Canonical API and event contracts. A0 owns this package.

All public models are re-exported here in a stable, alphabetised ``__all__``.
Future agents import from ``app.contracts`` — not from submodules directly.
"""

from app.contracts.action import ActionAttempt, ActionRequest
from app.contracts.audit import AuditEvent
from app.contracts.common import (
    ActionStatus,
    ActionType,
    AuditActorType,
    Availability,
    ContractModel,
    Decision,
    DecisionStage,
    FeatureSource,
    HashValue,
    MandateEventType,
    RiskSessionStatus,
    Sha256Hash,
    ensure_utc,
)
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.contracts.risk_session import (
    MandateIntent,
    RiskSession,
    RiskSessionCreateRequest,
    RiskSessionTelemetryUpdateRequest,
)

__all__ = [
    "ActionAttempt",
    "ActionRequest",
    "ActionStatus",
    "ActionType",
    "AuditActorType",
    "AuditEvent",
    "Availability",
    "ContractModel",
    "Decision",
    "DecisionStage",
    "FeatureSnapshot",
    "FeatureSource",
    "HashValue",
    "MandateEventType",
    "MandateIntent",
    "MandateWebhookEvent",
    "RiskAssessment",
    "RiskSession",
    "RiskSessionCreateRequest",
    "RiskSessionStatus",
    "RiskSessionTelemetryUpdateRequest",
    "RuleEvaluation",
    "Sha256Hash",
    "ensure_utc",
]
