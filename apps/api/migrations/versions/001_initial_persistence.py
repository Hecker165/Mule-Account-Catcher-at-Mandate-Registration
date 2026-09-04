"""Create all A1 persistence tables with constraints and indexes.

Revision ID: 001_initial_persistence
Revises:
Create Date: 2026-01-15 10:00:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "001_initial_persistence"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── risk_sessions ────────────────────────────────────────────────────────
    op.create_table(
        "risk_sessions",
        sa.Column("risk_session_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1.0"),
        sa.Column("merchant_namespace", sa.String(64), nullable=False),
        sa.Column("checkout_order_ref", sa.String(128), nullable=True),
        sa.Column("customer_reference_hash", sa.String(76), nullable=True),
        sa.Column("ip_hash", sa.String(76), nullable=True),
        sa.Column("device_fingerprint_hash", sa.String(76), nullable=True),
        sa.Column("user_agent_hash", sa.String(76), nullable=True),
        sa.Column("flow_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flow_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mandate_intent", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_risk_sessions_merchant_namespace", "risk_sessions", ["merchant_namespace"])
    op.create_index("ix_risk_sessions_customer_reference_hash", "risk_sessions", ["customer_reference_hash"])
    op.create_index("ix_risk_sessions_ip_hash", "risk_sessions", ["ip_hash"])
    op.create_index("ix_risk_sessions_device_fingerprint_hash", "risk_sessions", ["device_fingerprint_hash"])
    op.create_index("ix_risk_sessions_status", "risk_sessions", ["status"])
    # Partial unique index on (merchant_namespace, checkout_order_ref) where order_ref IS NOT NULL
    op.execute(
        "CREATE UNIQUE INDEX uq_risk_sessions_namespace_order_ref "
        "ON risk_sessions (merchant_namespace, checkout_order_ref) "
        "WHERE checkout_order_ref IS NOT NULL"
    )
    # Check constraints
    op.create_check_constraint(
        "ck_risk_sessions_flow_time_order",
        "risk_sessions",
        "flow_completed_at IS NULL OR flow_started_at IS NULL OR flow_completed_at >= flow_started_at",
    )
    op.create_check_constraint(
        "ck_risk_sessions_status",
        "risk_sessions",
        "status IN ('CREATED', 'READY', 'CONSUMED', 'EXPIRED')",
    )

    # ── mandate_webhook_events ──────────────────────────────────────────────
    op.create_table(
        "mandate_webhook_events",
        sa.Column("mandate_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="razorpay"),
        sa.Column("provider_event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("token_id", sa.String(128), nullable=False),
        sa.Column(
            "risk_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("risk_sessions.risk_session_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("vpa_hash", sa.String(76), nullable=True),
        sa.Column("vpa_handle", sa.String(100), nullable=True),
        sa.Column("recurring_status", sa.String(64), nullable=True),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload_sha256", sa.String(71), nullable=False),
        sa.Column("is_demo_event", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mandate_webhook_events_event_type", "mandate_webhook_events", ["event_type"])
    op.create_index("ix_mandate_webhook_events_token_id", "mandate_webhook_events", ["token_id"])
    op.create_index("ix_mandate_webhook_events_risk_session_id", "mandate_webhook_events", ["risk_session_id"])
    op.create_index("ix_mandate_webhook_events_vpa_hash", "mandate_webhook_events", ["vpa_hash"])
    op.create_unique_constraint(
        "uq_mandate_webhook_events_provider_event",
        "mandate_webhook_events",
        ["provider", "provider_event_id"],
    )
    op.create_check_constraint(
        "ck_mandate_webhook_events_provider",
        "mandate_webhook_events",
        "provider = 'razorpay'",
    )
    op.create_check_constraint(
        "ck_mandate_webhook_events_event_type",
        "mandate_webhook_events",
        "event_type IN ('token.confirmed', 'token.rejected', 'token.cancelled')",
    )
    op.execute(
        "CREATE INDEX ix_mandate_events_received_at_desc "
        "ON mandate_webhook_events (received_at DESC)"
    )

    # ── feature_snapshots ───────────────────────────────────────────────────
    op.create_table(
        "feature_snapshots",
        sa.Column("feature_snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column(
            "mandate_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mandate_webhook_events.mandate_event_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "risk_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("risk_sessions.risk_session_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("feature_version", sa.String(64), nullable=False, server_default="rules-v1"),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        # Velocity
        sa.Column("ip_velocity_5m", sa.Integer, nullable=True),
        sa.Column("ip_velocity_1h", sa.Integer, nullable=True),
        sa.Column("device_velocity_5m", sa.Integer, nullable=True),
        sa.Column("device_velocity_1h", sa.Integer, nullable=True),
        sa.Column("customer_velocity_1h", sa.Integer, nullable=True),
        sa.Column("vpa_velocity_1h", sa.Integer, nullable=True),
        sa.Column("shared_demo_merchant_count_1h", sa.Integer, nullable=True),
        # Device/session
        sa.Column("is_new_device_for_customer", sa.Boolean, nullable=True),
        sa.Column("flow_duration_seconds", sa.Integer, nullable=True),
        sa.Column("user_agent_bot_suspected", sa.Boolean, nullable=True),
        # Network
        sa.Column("network_vpn_or_proxy", sa.Boolean, nullable=True),
        sa.Column("network_reputation_score", sa.Integer, nullable=True),
        # Risk flags
        sa.Column("npci_risk_flag", sa.Boolean, nullable=True),
        sa.Column("payer_bank_or_compliance_flag", sa.Boolean, nullable=True),
        # Mandate
        sa.Column("max_amount_paise", sa.Integer, nullable=True),
        sa.Column("mandate_frequency", sa.String(32), nullable=True),
        sa.Column("expiry_days_from_registration", sa.Integer, nullable=True),
        # Provenance
        sa.Column("sources", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("is_demo_simulation", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index("ix_feature_snapshots_mandate_event_id", "feature_snapshots", ["mandate_event_id"])
    op.create_index("ix_feature_snapshots_risk_session_id", "feature_snapshots", ["risk_session_id"])
    op.create_index("ix_feature_snapshots_calculated_at", "feature_snapshots", ["calculated_at"])
    op.create_index("ix_feature_snapshots_is_demo_simulation", "feature_snapshots", ["is_demo_simulation"])
    # Partial unique indexes
    op.execute(
        "CREATE UNIQUE INDEX uq_feature_snapshots_event_version "
        "ON feature_snapshots (mandate_event_id, feature_version) "
        "WHERE mandate_event_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_feature_snapshots_session_version "
        "ON feature_snapshots (risk_session_id, feature_version) "
        "WHERE risk_session_id IS NOT NULL"
    )
    # Check constraints
    op.create_check_constraint(
        "ck_feature_snapshots_require_event_or_session",
        "feature_snapshots",
        "mandate_event_id IS NOT NULL OR risk_session_id IS NOT NULL",
    )
    # Non-negative numeric fields
    for col in (
        "ip_velocity_5m",
        "ip_velocity_1h",
        "device_velocity_5m",
        "device_velocity_1h",
        "customer_velocity_1h",
        "vpa_velocity_1h",
        "shared_demo_merchant_count_1h",
        "max_amount_paise",
        "expiry_days_from_registration",
    ):
        op.create_check_constraint(
            f"ck_feature_snapshots_{col}",
            "feature_snapshots",
            f"{col} IS NULL OR {col} >= 0",
        )
    op.create_check_constraint(
        "ck_feature_snapshots_flow_duration",
        "feature_snapshots",
        "flow_duration_seconds IS NULL OR (flow_duration_seconds >= 0 AND flow_duration_seconds <= 86400)",
    )
    op.create_check_constraint(
        "ck_feature_snapshots_network_reputation",
        "feature_snapshots",
        "network_reputation_score IS NULL OR (network_reputation_score >= 0 AND network_reputation_score <= 100)",
    )
    # Demo counter requires demo flag
    op.create_check_constraint(
        "ck_feature_snapshots_demo_counter_requires_flag",
        "feature_snapshots",
        "shared_demo_merchant_count_1h IS NULL OR is_demo_simulation = true",
    )

    # ── risk_assessments ───────────────────────────────────────────────────
    op.create_table(
        "risk_assessments",
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column(
            "risk_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("risk_sessions.risk_session_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "mandate_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mandate_webhook_events.mandate_event_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "feature_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("feature_snapshots.feature_snapshot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("evaluation_latency_ms", sa.Integer, nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_risk_assessments_risk_session_id", "risk_assessments", ["risk_session_id"])
    op.create_index("ix_risk_assessments_mandate_event_id", "risk_assessments", ["mandate_event_id"])
    op.create_index("ix_risk_assessments_feature_snapshot_id", "risk_assessments", ["feature_snapshot_id"])
    op.create_index("ix_risk_assessments_stage", "risk_assessments", ["stage"])
    op.create_index("ix_risk_assessments_decision", "risk_assessments", ["decision"])
    op.execute(
        "CREATE INDEX ix_risk_assessments_assessed_at_desc "
        "ON risk_assessments (assessed_at DESC)"
    )
    # Partial unique indexes
    op.execute(
        "CREATE UNIQUE INDEX uq_risk_assessments_session_stage_engine "
        "ON risk_assessments (risk_session_id, stage, engine_version) "
        "WHERE risk_session_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_risk_assessments_event_stage_engine "
        "ON risk_assessments (mandate_event_id, stage, engine_version) "
        "WHERE mandate_event_id IS NOT NULL"
    )
    # Check constraints
    op.create_check_constraint(
        "ck_risk_assessments_score",
        "risk_assessments",
        "score BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "ck_risk_assessments_latency",
        "risk_assessments",
        "evaluation_latency_ms BETWEEN 0 AND 60000",
    )
    op.create_check_constraint(
        "ck_risk_assessments_precheck_requires_session",
        "risk_assessments",
        "stage <> 'PRECHECK' OR risk_session_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_risk_assessments_postconfirm_requires_event",
        "risk_assessments",
        "stage NOT IN ('POST_CONFIRMATION', 'REJECTION_AUDIT') OR mandate_event_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_risk_assessments_stage",
        "risk_assessments",
        "stage IN ('PRECHECK', 'POST_CONFIRMATION', 'REJECTION_AUDIT')",
    )
    op.create_check_constraint(
        "ck_risk_assessments_decision",
        "risk_assessments",
        "decision IN ('ALLOW', 'CHALLENGE', 'BLOCK')",
    )

    # ── rule_evaluations ───────────────────────────────────────────────────
    op.create_table(
        "rule_evaluations",
        sa.Column("rule_evaluation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "assessment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("risk_assessments.assessment_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("triggered", sa.Boolean, nullable=False),
        sa.Column("points", sa.Integer, nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("reason_text", sa.String(240), nullable=True),
        sa.Column("position", sa.Integer, nullable=False),
    )
    op.create_index("ix_rule_evaluations_assessment_id", "rule_evaluations", ["assessment_id"])
    op.create_unique_constraint(
        "uq_rule_evaluations_assessment_position",
        "rule_evaluations",
        ["assessment_id", "position"],
    )
    op.create_unique_constraint(
        "uq_rule_evaluations_assessment_rule",
        "rule_evaluations",
        ["assessment_id", "rule_id"],
    )
    op.create_check_constraint(
        "ck_rule_evaluations_points",
        "rule_evaluations",
        "points BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "ck_rule_evaluations_position",
        "rule_evaluations",
        "position >= 0",
    )

    # ── action_requests ────────────────────────────────────────────────────
    op.create_table(
        "action_requests",
        sa.Column("action_request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column(
            "assessment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("risk_assessments.assessment_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "mandate_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mandate_webhook_events.mandate_event_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("token_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_action_requests_assessment_id", "action_requests", ["assessment_id"])
    op.create_index("ix_action_requests_mandate_event_id", "action_requests", ["mandate_event_id"])
    op.create_index("ix_action_requests_status", "action_requests", ["status"])
    op.create_unique_constraint(
        "uq_action_requests_mandate_action",
        "action_requests",
        ["mandate_event_id", "action_type"],
    )
    op.execute(
        "CREATE INDEX ix_action_requests_status_requested_at "
        "ON action_requests (status, requested_at DESC)"
    )
    op.create_check_constraint(
        "ck_action_requests_action_type",
        "action_requests",
        "action_type = 'TOKEN_REVOKE'",
    )
    op.create_check_constraint(
        "ck_action_requests_status",
        "action_requests",
        "status IN ('QUEUED', 'IN_PROGRESS', 'SUCCEEDED', 'RETRYING', 'FAILED', 'NOT_REQUIRED')",
    )

    # ── action_attempts ────────────────────────────────────────────────────
    op.create_table(
        "action_attempts",
        sa.Column("action_attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column(
            "action_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("action_requests.action_request_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider_status_code", sa.SmallInteger, nullable=True),
        sa.Column("safe_error_code", sa.String(80), nullable=True),
        sa.Column("safe_error_message", sa.String(240), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_action_attempts_action_request_id", "action_attempts", ["action_request_id"])
    op.create_unique_constraint(
        "uq_action_attempts_request_number",
        "action_attempts",
        ["action_request_id", "attempt_number"],
    )
    op.create_check_constraint(
        "ck_action_attempts_attempt_number",
        "action_attempts",
        "attempt_number BETWEEN 1 AND 100",
    )
    op.create_check_constraint(
        "ck_action_attempts_provider_status",
        "action_attempts",
        "provider_status_code IS NULL OR (provider_status_code BETWEEN 100 AND 599)",
    )
    op.create_check_constraint(
        "ck_action_attempts_completed_after_attempted",
        "action_attempts",
        "completed_at IS NULL OR completed_at >= attempted_at",
    )
    op.create_check_constraint(
        "ck_action_attempts_status",
        "action_attempts",
        "status IN ('QUEUED', 'IN_PROGRESS', 'SUCCEEDED', 'RETRYING', 'FAILED', 'NOT_REQUIRED')",
    )

    # ── outbox_messages ────────────────────────────────────────────────────
    op.create_table(
        "outbox_messages",
        sa.Column("outbox_message_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("aggregate_type", sa.String(32), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_outbox_messages_status", "outbox_messages", ["status"])
    op.create_index("ix_outbox_messages_available_at", "outbox_messages", ["available_at"])
    op.create_index("ix_outbox_claimable", "outbox_messages", ["status", "available_at"])
    op.create_check_constraint(
        "ck_outbox_messages_status",
        "outbox_messages",
        "status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'DEAD_LETTER')",
    )
    op.create_check_constraint(
        "ck_outbox_messages_attempt_count",
        "outbox_messages",
        "attempt_count >= 0",
    )

    # ── audit_events ───────────────────────────────────────────────────────
    op.create_table(
        "audit_events",
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("aggregate_type", sa.String(32), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redacted_payload", postgresql.JSONB, nullable=False),
        sa.Column("previous_event_hash", sa.String(71), nullable=True),
        sa.Column("event_hash", sa.String(71), nullable=False),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_audit_events_aggregate_sequence",
        "audit_events",
        ["aggregate_type", "aggregate_id", "sequence_number"],
    )
    op.create_unique_constraint(
        "uq_audit_events_event_hash",
        "audit_events",
        ["event_hash"],
    )
    op.execute(
        "CREATE INDEX ix_audit_events_aggregate_tail "
        "ON audit_events (aggregate_type, aggregate_id, sequence_number DESC)"
    )
    op.create_check_constraint(
        "ck_audit_events_sequence",
        "audit_events",
        "sequence_number >= 1",
    )
    op.create_check_constraint(
        "ck_audit_events_actor_type",
        "audit_events",
        "actor_type IN ('SYSTEM', 'WEBHOOK', 'WORKER', 'OPERATOR', 'DEMO')",
    )


def downgrade() -> None:
    # Drop in reverse foreign-key dependency order
    op.drop_table("audit_events")
    op.drop_table("outbox_messages")
    op.drop_table("action_attempts")
    op.drop_table("action_requests")
    op.drop_table("rule_evaluations")
    op.drop_table("risk_assessments")
    op.drop_table("feature_snapshots")
    op.drop_table("mandate_webhook_events")
    op.drop_table("risk_sessions")