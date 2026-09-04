"""SQLAlchemy ORM models for A1 persistence.

All models map to the A0 contract shapes without storing raw sensitive data.
Typed ``Mapped[]`` annotations keep ``mypy --strict`` clean.
"""

import enum
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.base import Base


class RiskSessionStatusEnum(enum.StrEnum):
    CREATED = "CREATED"
    READY = "READY"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


class DecisionEnum(enum.StrEnum):
    ALLOW = "ALLOW"
    CHALLENGE = "CHALLENGE"
    BLOCK = "BLOCK"


class DecisionStageEnum(enum.StrEnum):
    PRECHECK = "PRECHECK"
    POST_CONFIRMATION = "POST_CONFIRMATION"
    REJECTION_AUDIT = "REJECTION_AUDIT"


class MandateEventTypeEnum(enum.StrEnum):
    TOKEN_CONFIRMED = "token.confirmed"
    TOKEN_REJECTED = "token.rejected"
    TOKEN_CANCELLED = "token.cancelled"


class ActionTypeEnum(enum.StrEnum):
    TOKEN_REVOKE = "TOKEN_REVOKE"


class ActionStatusEnum(enum.StrEnum):
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    NOT_REQUIRED = "NOT_REQUIRED"


class OutboxStatusEnum(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    DEAD_LETTER = "DEAD_LETTER"


class AuditActorTypeEnum(enum.StrEnum):
    SYSTEM = "SYSTEM"
    WEBHOOK = "WEBHOOK"
    WORKER = "WORKER"
    OPERATOR = "OPERATOR"
    DEMO = "DEMO"


class AvailabilityEnum(enum.StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DEMO_SIMULATED = "DEMO_SIMULATED"


def utc_now() -> datetime:
    """Return current UTC datetime with timezone."""
    return datetime.now(UTC)


# ──────────────────────────────────────────────────────────────────────────────
# risk_sessions
# ──────────────────────────────────────────────────────────────────────────────


class RiskSession(Base):
    __tablename__ = "risk_sessions"

    risk_session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    merchant_namespace: Mapped[str] = mapped_column(String(64), index=True)
    checkout_order_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    customer_reference_hash: Mapped[str | None] = mapped_column(String(76), index=True)
    ip_hash: Mapped[str | None] = mapped_column(String(76), index=True)
    device_fingerprint_hash: Mapped[str | None] = mapped_column(String(76), index=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(76), nullable=True)
    flow_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flow_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mandate_intent: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Relationships
    mandate_events: Mapped[list["MandateWebhookEvent"]] = relationship(
        back_populates="risk_session"
    )
    feature_snapshots: Mapped[list["FeatureSnapshot"]] = relationship(back_populates="risk_session")
    risk_assessments: Mapped[list["RiskAssessment"]] = relationship(back_populates="risk_session")

    __table_args__ = (
        # Partial unique index on (merchant_namespace, checkout_order_ref)
        # where order_ref is not null
        Index(
            "uq_risk_sessions_namespace_order_ref",
            "merchant_namespace",
            "checkout_order_ref",
            unique=True,
            postgresql_where=text("checkout_order_ref IS NOT NULL"),
        ),
        # Check constraint: flow_completed_at >= flow_started_at
        CheckConstraint(
            "flow_completed_at IS NULL OR flow_started_at IS NULL OR "
            "flow_completed_at >= flow_started_at",
            name="ck_risk_sessions_flow_time_order",
        ),
        # Check constraint: status must be one of the valid values
        CheckConstraint(
            "status IN ('CREATED', 'READY', 'CONSUMED', 'EXPIRED')",
            name="ck_risk_sessions_status",
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# mandate_webhook_events
# ──────────────────────────────────────────────────────────────────────────────


class MandateWebhookEvent(Base):
    __tablename__ = "mandate_webhook_events"

    mandate_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32), default="razorpay")
    provider_event_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    token_id: Mapped[str] = mapped_column(String(128), index=True)
    risk_session_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("risk_sessions.risk_session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    vpa_hash: Mapped[str | None] = mapped_column(String(76), index=True)
    vpa_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recurring_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_payload_sha256: Mapped[str] = mapped_column(String(71))
    is_demo_event: Mapped[bool] = mapped_column(Boolean, default=False)
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    risk_session: Mapped["RiskSession"] = relationship(back_populates="mandate_events")
    feature_snapshots: Mapped[list["FeatureSnapshot"]] = relationship(
        back_populates="mandate_event"
    )
    risk_assessments: Mapped[list["RiskAssessment"]] = relationship(back_populates="mandate_event")
    action_requests: Mapped[list["ActionRequest"]] = relationship(back_populates="mandate_event")

    __table_args__ = (
        # Unique constraint on (provider, provider_event_id) for idempotency
        UniqueConstraint(
            "provider", "provider_event_id", name="uq_mandate_webhook_events_provider_event"
        ),
        # Check constraint: provider must be 'razorpay' for v1
        CheckConstraint(
            "provider = 'razorpay'",
            name="ck_mandate_webhook_events_provider",
        ),
        # Check constraint: event_type must be valid
        CheckConstraint(
            "event_type IN ('token.confirmed', 'token.rejected', 'token.cancelled')",
            name="ck_mandate_webhook_events_event_type",
        ),
        # Index for received_at DESC ordering
        Index(
            "ix_mandate_events_received_at_desc",
            "received_at",
            postgresql_using="btree",
            postgresql_ops={"received_at": "DESC"},
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# feature_snapshots
# ──────────────────────────────────────────────────────────────────────────────


class FeatureSnapshot(Base):
    __tablename__ = "feature_snapshots"

    feature_snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(16))
    mandate_event_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("mandate_webhook_events.mandate_event_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    risk_session_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("risk_sessions.risk_session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    feature_version: Mapped[str] = mapped_column(String(64), default="rules-v1")
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Velocity features
    ip_velocity_5m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip_velocity_1h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_velocity_5m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_velocity_1h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_velocity_1h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vpa_velocity_1h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shared_demo_merchant_count_1h: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Device/session features
    is_new_device_for_customer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flow_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_agent_bot_suspected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Network features
    network_vpn_or_proxy: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    network_reputation_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Risk flags
    npci_risk_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    payer_bank_or_compliance_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Mandate features
    max_amount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mandate_frequency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expiry_days_from_registration: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Provenance
    sources: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_demo_simulation: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Relationships
    mandate_event: Mapped["MandateWebhookEvent"] = relationship(back_populates="feature_snapshots")
    risk_session: Mapped["RiskSession"] = relationship(back_populates="feature_snapshots")
    risk_assessments: Mapped[list["RiskAssessment"]] = relationship(
        back_populates="feature_snapshot"
    )

    __table_args__ = (
        # Check: at least one of mandate_event_id or risk_session_id
        CheckConstraint(
            "mandate_event_id IS NOT NULL OR risk_session_id IS NOT NULL",
            name="ck_feature_snapshots_require_event_or_session",
        ),
        # Check: non-negative numeric fields
        CheckConstraint(
            "ip_velocity_5m IS NULL OR ip_velocity_5m >= 0",
            name="ck_feature_snapshots_ip_velocity_5m",
        ),
        CheckConstraint(
            "ip_velocity_1h IS NULL OR ip_velocity_1h >= 0",
            name="ck_feature_snapshots_ip_velocity_1h",
        ),
        CheckConstraint(
            "device_velocity_5m IS NULL OR device_velocity_5m >= 0",
            name="ck_feature_snapshots_device_velocity_5m",
        ),
        CheckConstraint(
            "device_velocity_1h IS NULL OR device_velocity_1h >= 0",
            name="ck_feature_snapshots_device_velocity_1h",
        ),
        CheckConstraint(
            "customer_velocity_1h IS NULL OR customer_velocity_1h >= 0",
            name="ck_feature_snapshots_customer_velocity_1h",
        ),
        CheckConstraint(
            "vpa_velocity_1h IS NULL OR vpa_velocity_1h >= 0",
            name="ck_feature_snapshots_vpa_velocity_1h",
        ),
        CheckConstraint(
            "shared_demo_merchant_count_1h IS NULL OR shared_demo_merchant_count_1h >= 0",
            name="ck_feature_snapshots_shared_demo_merchant",
        ),
        CheckConstraint(
            "flow_duration_seconds IS NULL OR "
            "(flow_duration_seconds >= 0 AND flow_duration_seconds <= 86400)",
            name="ck_feature_snapshots_flow_duration",
        ),
        CheckConstraint(
            "network_reputation_score IS NULL OR "
            "(network_reputation_score >= 0 AND network_reputation_score <= 100)",
            name="ck_feature_snapshots_network_reputation",
        ),
        CheckConstraint(
            "max_amount_paise IS NULL OR max_amount_paise >= 0",
            name="ck_feature_snapshots_max_amount",
        ),
        CheckConstraint(
            "expiry_days_from_registration IS NULL OR expiry_days_from_registration >= 0",
            name="ck_feature_snapshots_expiry_days",
        ),
        # Check: shared_demo_merchant_count_1h requires is_demo_simulation
        CheckConstraint(
            "shared_demo_merchant_count_1h IS NULL OR is_demo_simulation = true",
            name="ck_feature_snapshots_demo_counter_requires_flag",
        ),
        # Partial unique indexes
        Index(
            "uq_feature_snapshots_event_version",
            "mandate_event_id",
            "feature_version",
            unique=True,
            postgresql_where=text("mandate_event_id IS NOT NULL"),
        ),
        Index(
            "uq_feature_snapshots_session_version",
            "risk_session_id",
            "feature_version",
            unique=True,
            postgresql_where=text("risk_session_id IS NOT NULL"),
        ),
        # Index for dashboard queries
        Index("ix_feature_snapshots_calculated_at", "calculated_at"),
        Index("ix_feature_snapshots_is_demo_simulation", "is_demo_simulation"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# risk_assessments
# ──────────────────────────────────────────────────────────────────────────────


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"

    assessment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(16))
    risk_session_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("risk_sessions.risk_session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    mandate_event_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("mandate_webhook_events.mandate_event_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    feature_snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("feature_snapshots.feature_snapshot_id", ondelete="RESTRICT"),
        index=True,
    )
    stage: Mapped[str] = mapped_column(String(32), index=True)
    score: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(16), index=True)
    engine_version: Mapped[str] = mapped_column(String(64))
    evaluation_latency_ms: Mapped[int] = mapped_column(Integer)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    risk_session: Mapped["RiskSession"] = relationship(back_populates="risk_assessments")
    mandate_event: Mapped["MandateWebhookEvent"] = relationship(back_populates="risk_assessments")
    feature_snapshot: Mapped["FeatureSnapshot"] = relationship(back_populates="risk_assessments")
    rule_evaluations: Mapped[list["RuleEvaluation"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )
    action_requests: Mapped[list["ActionRequest"]] = relationship(back_populates="assessment")

    __table_args__ = (
        # Check: score between 0 and 100
        CheckConstraint("score BETWEEN 0 AND 100", name="ck_risk_assessments_score"),
        # Check: evaluation_latency_ms range
        CheckConstraint(
            "evaluation_latency_ms BETWEEN 0 AND 60000", name="ck_risk_assessments_latency"
        ),
        # Check: stage and ID requirements
        CheckConstraint(
            "stage <> 'PRECHECK' OR risk_session_id IS NOT NULL",
            name="ck_risk_assessments_precheck_requires_session",
        ),
        CheckConstraint(
            "stage NOT IN ('POST_CONFIRMATION', 'REJECTION_AUDIT') OR mandate_event_id IS NOT NULL",
            name="ck_risk_assessments_postconfirm_requires_event",
        ),
        # Check: valid stage
        CheckConstraint(
            "stage IN ('PRECHECK', 'POST_CONFIRMATION', 'REJECTION_AUDIT')",
            name="ck_risk_assessments_stage",
        ),
        # Check: valid decision
        CheckConstraint(
            "decision IN ('ALLOW', 'CHALLENGE', 'BLOCK')",
            name="ck_risk_assessments_decision",
        ),
        # Partial unique indexes
        Index(
            "uq_risk_assessments_session_stage_engine",
            "risk_session_id",
            "stage",
            "engine_version",
            unique=True,
            postgresql_where=text("risk_session_id IS NOT NULL"),
        ),
        Index(
            "uq_risk_assessments_event_stage_engine",
            "mandate_event_id",
            "stage",
            "engine_version",
            unique=True,
            postgresql_where=text("mandate_event_id IS NOT NULL"),
        ),
        # Index for dashboard queries
        Index(
            "ix_risk_assessments_assessed_at_desc",
            "assessed_at",
            postgresql_using="btree",
            postgresql_ops={"assessed_at": "DESC"},
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# rule_evaluations
# ──────────────────────────────────────────────────────────────────────────────


class RuleEvaluation(Base):
    __tablename__ = "rule_evaluations"

    rule_evaluation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    assessment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("risk_assessments.assessment_id", ondelete="CASCADE"),
        index=True,
    )
    rule_id: Mapped[str] = mapped_column(String(64))
    triggered: Mapped[bool] = mapped_column(Boolean)
    points: Mapped[int] = mapped_column(Integer)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason_text: Mapped[str | None] = mapped_column(String(240), nullable=True)
    position: Mapped[int] = mapped_column(Integer)

    # Relationships
    assessment: Mapped["RiskAssessment"] = relationship(back_populates="rule_evaluations")

    __table_args__ = (
        # Check: points between 0 and 100
        CheckConstraint("points BETWEEN 0 AND 100", name="ck_rule_evaluations_points"),
        # Check: position >= 0
        CheckConstraint("position >= 0", name="ck_rule_evaluations_position"),
        # Unique: (assessment_id, position)
        UniqueConstraint(
            "assessment_id", "position", name="uq_rule_evaluations_assessment_position"
        ),
        # Unique: (assessment_id, rule_id)
        UniqueConstraint("assessment_id", "rule_id", name="uq_rule_evaluations_assessment_rule"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# action_requests
# ──────────────────────────────────────────────────────────────────────────────


class ActionRequest(Base):
    __tablename__ = "action_requests"

    action_request_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(16))
    assessment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("risk_assessments.assessment_id", ondelete="RESTRICT"),
        index=True,
    )
    mandate_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("mandate_webhook_events.mandate_event_id", ondelete="RESTRICT"),
        index=True,
    )
    action_type: Mapped[str] = mapped_column(String(32))
    token_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    assessment: Mapped["RiskAssessment"] = relationship(back_populates="action_requests")
    mandate_event: Mapped["MandateWebhookEvent"] = relationship(back_populates="action_requests")
    attempts: Mapped[list["ActionAttempt"]] = relationship(
        back_populates="action_request", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Check: action_type valid
        CheckConstraint("action_type = 'TOKEN_REVOKE'", name="ck_action_requests_action_type"),
        # Check: status valid
        CheckConstraint(
            "status IN ('QUEUED', 'IN_PROGRESS', 'SUCCEEDED', 'RETRYING', "
            "'FAILED', 'NOT_REQUIRED')",
            name="ck_action_requests_status",
        ),
        # Unique: one revoke request per mandate_event_id + action_type
        UniqueConstraint(
            "mandate_event_id", "action_type", name="uq_action_requests_mandate_action"
        ),
        # Index for worker queries
        Index(
            "ix_action_requests_status_requested_at",
            "status",
            "requested_at",
            postgresql_using="btree",
            postgresql_ops={"requested_at": "DESC"},
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# action_attempts
# ──────────────────────────────────────────────────────────────────────────────


class ActionAttempt(Base):
    __tablename__ = "action_attempts"

    action_attempt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(16))
    action_request_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("action_requests.action_request_id", ondelete="CASCADE"),
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    provider_status_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(String(240), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    action_request: Mapped["ActionRequest"] = relationship(back_populates="attempts")

    __table_args__ = (
        # Check: attempt_number between 1 and 100
        CheckConstraint(
            "attempt_number BETWEEN 1 AND 100", name="ck_action_attempts_attempt_number"
        ),
        # Check: provider_status_code range
        CheckConstraint(
            "provider_status_code IS NULL OR (provider_status_code BETWEEN 100 AND 599)",
            name="ck_action_attempts_provider_status",
        ),
        # Check: completed_at >= attempted_at
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= attempted_at",
            name="ck_action_attempts_completed_after_attempted",
        ),
        # Check: status valid
        CheckConstraint(
            "status IN ('QUEUED', 'IN_PROGRESS', 'SUCCEEDED', 'RETRYING', "
            "'FAILED', 'NOT_REQUIRED')",
            name="ck_action_attempts_status",
        ),
        # Unique: (action_request_id, attempt_number)
        UniqueConstraint(
            "action_request_id", "attempt_number", name="uq_action_attempts_request_number"
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# outbox_messages
# ──────────────────────────────────────────────────────────────────────────────


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    outbox_message_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    message_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Check: valid status
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'DEAD_LETTER')",
            name="ck_outbox_messages_status",
        ),
        # Check: attempt_count >= 0
        CheckConstraint("attempt_count >= 0", name="ck_outbox_messages_attempt_count"),
        # Index for claim_batch query
        Index("ix_outbox_claimable", "status", "available_at"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# audit_events
# ──────────────────────────────────────────────────────────────────────────────


class AuditEvent(Base):
    __tablename__ = "audit_events"

    audit_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(16))
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    sequence_number: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    previous_event_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(71), unique=True)
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # Check: sequence_number >= 1
        CheckConstraint("sequence_number >= 1", name="ck_audit_events_sequence"),
        # Check: valid actor_type
        CheckConstraint(
            "actor_type IN ('SYSTEM', 'WEBHOOK', 'WORKER', 'OPERATOR', 'DEMO')",
            name="ck_audit_events_actor_type",
        ),
        # Unique: (aggregate_type, aggregate_id, sequence_number)
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "sequence_number",
            name="uq_audit_events_aggregate_sequence",
        ),
        # Unique: event_hash
        UniqueConstraint("event_hash", name="uq_audit_events_event_hash"),
        # Index for tail lookup (most recent first)
        Index(
            "ix_audit_events_aggregate_tail",
            "aggregate_type",
            "aggregate_id",
            "sequence_number",
            postgresql_using="btree",
            postgresql_ops={"sequence_number": "DESC"},
        ),
    )
