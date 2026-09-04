"""PostgreSQL integration tests for A1 persistence layer.

Requires TEST_POSTGRES_URL environment variable pointing to a PostgreSQL 16 database.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.action import ActionRequest
from app.contracts.common import (
    ActionStatus,
    AuditActorType,
    Decision,
    DecisionStage,
    MandateEventType,
    RiskSessionStatus,
)
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.contracts.risk_session import MandateIntent, RiskSession
from app.persistence.models import OutboxMessage as OutboxMessageORM
from app.repositories import (
    ActionRepository,
    AuditRepository,
    DashboardReadRepository,
    FeatureSnapshotRepository,
    MandateEventRepository,
    RiskAssessmentRepository,
    RiskSessionRepository,
)

# ── Fixtures / factories ────────────────────────────────────────────────────


def make_risk_session(
    namespace: str = "demo-merchant-01",
    order_ref: str | None = "order_demo_001",
) -> RiskSession:
    """Build a full RiskSession contract with fixed, fake hash values."""
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    return RiskSession(
        schema_version="1.0",
        risk_session_id=uuid4(),
        merchant_namespace=namespace,
        checkout_order_ref=order_ref,
        customer_reference_hash="hmac-sha256:" + "a" * 64,
        ip_hash="hmac-sha256:" + "b" * 64,
        device_fingerprint_hash="hmac-sha256:" + "c" * 64,
        user_agent_hash="hmac-sha256:" + "d" * 64,
        flow_started_at=now,
        flow_completed_at=None,
        mandate_intent=MandateIntent(
            max_amount_paise=500000,
            frequency="monthly",
            expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
        ),
        status=RiskSessionStatus.CREATED,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def sample_mandate_event() -> MandateWebhookEvent:
    return MandateWebhookEvent(
        schema_version="1.0",
        mandate_event_id=uuid4(),
        provider="razorpay",
        provider_event_id="evt_demo_confirmed_001",
        event_type=MandateEventType.TOKEN_CONFIRMED,
        token_id="token_demo123",
        risk_session_id=None,
        vpa_hash="hmac-sha256:" + "a" * 64,
        vpa_handle="upi",
        recurring_status="confirmed",
        failure_reason=None,
        provider_created_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 15, 10, 0, 1, tzinfo=UTC),
        raw_payload_sha256="sha256:" + "b" * 64,
        is_demo_event=True,
    )


async def create_mandate_event(session: AsyncSession) -> MandateWebhookEvent:
    """Persist one mandate event and return its contract."""
    repo = MandateEventRepository(session)
    event = MandateWebhookEvent(
        schema_version="1.0",
        mandate_event_id=uuid4(),
        provider="razorpay",
        provider_event_id=f"evt_{uuid4().hex[:12]}",
        event_type=MandateEventType.TOKEN_CONFIRMED,
        token_id="token_demo123",
        risk_session_id=None,
        vpa_hash="hmac-sha256:" + "a" * 64,
        vpa_handle="upi",
        recurring_status="confirmed",
        failure_reason=None,
        provider_created_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 15, 10, 0, 1, tzinfo=UTC),
        raw_payload_sha256="sha256:" + "b" * 64,
        is_demo_event=True,
    )
    created, was_created = await repo.create_or_get(event)
    assert was_created is True
    return created


async def create_feature_snapshot(
    session: AsyncSession,
    mandate_event_id: UUID,
    is_demo: bool = False,
) -> FeatureSnapshot:
    """Persist one feature snapshot referencing a real mandate event."""
    repo = FeatureSnapshotRepository(session)
    snapshot = FeatureSnapshot(
        schema_version="1.0",
        feature_snapshot_id=uuid4(),
        mandate_event_id=mandate_event_id,
        risk_session_id=None,
        feature_version="rules-v1",
        calculated_at=datetime(2026, 1, 15, 10, 0, 2, tzinfo=UTC),
        is_demo_simulation=is_demo,
        sources={},
    )
    return await repo.create(snapshot)


async def create_assessment(
    session: AsyncSession,
    feature_snapshot_id: UUID,
    mandate_event_id: UUID,
) -> RiskAssessment:
    """Persist one risk assessment referencing real parent rows."""
    repo = RiskAssessmentRepository(session)
    assessment = RiskAssessment(
        schema_version="1.0",
        assessment_id=uuid4(),
        risk_session_id=None,
        mandate_event_id=mandate_event_id,
        feature_snapshot_id=feature_snapshot_id,
        stage=DecisionStage.POST_CONFIRMATION,
        score=75,
        decision=Decision.CHALLENGE,
        engine_version="rules-v1",
        rule_evaluations=[
            RuleEvaluation(
                rule_id="velocity_ip_5m",
                triggered=True,
                points=30,
                reason_code="VEL_IP_5M_HIGH",
                reason_text="IP velocity in 5m window exceeds threshold",
            ),
            RuleEvaluation(
                rule_id="new_device",
                triggered=True,
                points=25,
                reason_code="NEW_DEVICE",
                reason_text="Device not previously seen for this customer",
            ),
            RuleEvaluation(
                rule_id="vpn_proxy",
                triggered=True,
                points=20,
                reason_code="VPN_DETECTED",
                reason_text="VPN or proxy detected",
            ),
        ],
        evaluation_latency_ms=5,
        assessed_at=datetime(2026, 1, 15, 10, 0, 3, tzinfo=UTC),
    )
    return await repo.create(assessment)


# ── Required Integration Tests ─────────────────────────────────────────────


async def test_migration_creates_all_expected_tables_and_constraints(session: AsyncSession) -> None:
    """Verify migration creates all expected tables and constraints."""
    # Get table names from information_schema
    result = await session.execute(
        text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
    )
    tables = [row[0] for row in result.fetchall()]

    expected_tables = {
        "risk_sessions",
        "mandate_webhook_events",
        "feature_snapshots",
        "risk_assessments",
        "rule_evaluations",
        "action_requests",
        "action_attempts",
        "outbox_messages",
        "audit_events",
        "alembic_version",  # Alembic version table
    }

    for table in expected_tables:
        assert table in tables, f"Table {table} not created by migration"

    # Check key constraints exist
    result = await session.execute(
        text("""
            SELECT constraint_name, table_name FROM information_schema.table_constraints
            WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'CHECK', 'FOREIGN KEY')
            AND table_schema = 'public'
        """)
    )
    constraints = {(row[0], row[1]) for row in result.fetchall()}

    # Verify critical constraints
    assert ("pk_risk_sessions", "risk_sessions") in constraints
    assert ("uq_mandate_webhook_events_provider_event", "mandate_webhook_events") in constraints
    assert ("uq_audit_events_aggregate_sequence", "audit_events") in constraints
    assert ("uq_audit_events_event_hash", "audit_events") in constraints
    assert ("uq_action_requests_mandate_action", "action_requests") in constraints


async def test_risk_session_round_trip_preserves_contract_without_raw_fields(
    session: AsyncSession,
) -> None:
    """Risk session round trip preserves contract without raw fields."""
    repo = RiskSessionRepository(session)

    # Create
    created = await repo.create(make_risk_session())
    assert created.risk_session_id is not None
    assert created.merchant_namespace == "demo-merchant-01"
    assert created.status == RiskSessionStatus.CREATED

    # Get by ID
    fetched = await repo.get(created.risk_session_id)
    assert fetched is not None
    assert fetched.risk_session_id == created.risk_session_id
    assert fetched.merchant_namespace == created.merchant_namespace
    assert fetched.checkout_order_ref == created.checkout_order_ref

    # Verify NO raw fields in contract
    assert not hasattr(fetched, "customer_reference")
    assert not hasattr(fetched, "device_fingerprint")
    assert fetched.customer_reference_hash is not None  # Hash is present
    assert fetched.device_fingerprint_hash is not None


async def test_duplicate_checkout_order_ref_is_rejected_per_merchant_namespace(
    session: AsyncSession,
) -> None:
    """Duplicate checkout_order_ref is rejected per merchant_namespace."""
    repo = RiskSessionRepository(session)

    # Create first session
    await repo.create(make_risk_session())

    # Create second session with same merchant_namespace and checkout_order_ref
    duplicate = make_risk_session(
        namespace="demo-merchant-01",
        order_ref="order_demo_001",  # Same!
    )

    with pytest.raises(Exception) as exc_info:
        await repo.create(duplicate)

    # Should be integrity error from unique constraint
    assert (
        "uq_risk_sessions_namespace_order_ref" in str(exc_info.value)
        or "unique" in str(exc_info.value).lower()
    )


async def test_same_order_ref_is_allowed_in_different_namespaces(
    session: AsyncSession,
) -> None:
    """Same checkout_order_ref is allowed in different merchant namespaces."""
    repo = RiskSessionRepository(session)

    # Create in first namespace
    await repo.create(make_risk_session())

    # Create in second namespace with same order ref
    created = await repo.create(
        make_risk_session(
            namespace="other-merchant-02",
            order_ref="order_demo_001",  # Same order ref
        )
    )
    assert created.risk_session_id is not None
    assert created.merchant_namespace == "other-merchant-02"


async def test_webhook_create_or_get_is_idempotent_and_returns_original(
    session: AsyncSession, sample_mandate_event: MandateWebhookEvent
) -> None:
    """Webhook create_or_get is idempotent and returns original on duplicate."""
    repo = MandateEventRepository(session)

    # First insert
    event1, created1 = await repo.create_or_get(sample_mandate_event)
    assert created1 is True
    assert event1.mandate_event_id == sample_mandate_event.mandate_event_id

    # Second insert with same provider_event_id but newly generated mandate_event_id
    duplicate = sample_mandate_event.model_copy(update={"mandate_event_id": uuid4()})
    event2, created2 = await repo.create_or_get(duplicate)
    assert created2 is False
    assert event2.mandate_event_id == sample_mandate_event.mandate_event_id
    assert event2.provider_event_id == sample_mandate_event.provider_event_id


async def test_feature_snapshot_requires_event_or_session_at_database_boundary(
    session: AsyncSession,
) -> None:
    """Feature snapshot requires at least mandate_event_id or risk_session_id at DB boundary.

    The Pydantic contract already rejects both-None input, so this test
    bypasses the contract with a raw SQL insert to prove the CHECK constraint
    exists at the database boundary.
    """
    event = await create_mandate_event(session)

    with pytest.raises(Exception) as exc_info:
        await session.execute(
            text(
                """
                INSERT INTO feature_snapshots (
                    feature_snapshot_id, schema_version, mandate_event_id,
                    risk_session_id, feature_version, calculated_at,
                    sources, is_demo_simulation
                ) VALUES (
                    :fid, '1.0', NULL,
                    NULL, 'rules-v1', :ts,
                    '{}'::jsonb, false
                )
                """
            ),
            {"fid": str(uuid4()), "ts": datetime(2026, 1, 15, 10, 0, 2, tzinfo=UTC)},
        )

    assert (
        "ck_feature_snapshots_require_event_or_session" in str(exc_info.value)
        or "check constraint" in str(exc_info.value).lower()
    )
    assert event.mandate_event_id is not None  # parent row exists but is not referenced


async def test_feature_snapshot_demo_counter_requires_demo_flag_at_database_boundary(
    session: AsyncSession,
) -> None:
    """Feature snapshot demo counter requires is_demo_simulation=true at DB boundary.

    The Pydantic contract already rejects this combination, so the database
    CHECK constraint is proven with a raw SQL insert.
    """
    event = await create_mandate_event(session)

    with pytest.raises(Exception) as exc_info:
        await session.execute(
            text(
                """
                INSERT INTO feature_snapshots (
                    feature_snapshot_id, schema_version, mandate_event_id,
                    risk_session_id, feature_version, calculated_at,
                    shared_demo_merchant_count_1h, sources, is_demo_simulation
                ) VALUES (
                    :fid, '1.0', :eid,
                    NULL, 'rules-v1', :ts,
                    3, '{}'::jsonb, false
                )
                """
            ),
            {
                "fid": str(uuid4()),
                "eid": str(event.mandate_event_id),
                "ts": datetime(2026, 1, 15, 10, 0, 2, tzinfo=UTC),
            },
        )

    assert (
        "ck_feature_snapshots_demo_counter_requires_flag" in str(exc_info.value)
        or "check constraint" in str(exc_info.value).lower()
    )


async def test_risk_assessment_persists_ordered_rule_evaluations(
    session: AsyncSession,
) -> None:
    """Risk assessment persists ordered rule evaluations."""
    event = await create_mandate_event(session)
    snapshot = await create_feature_snapshot(session, event.mandate_event_id)

    created = await create_assessment(session, snapshot.feature_snapshot_id, event.mandate_event_id)
    assert created.assessment_id is not None
    assert len(created.rule_evaluations) == 3

    # Verify order preserved
    fetched_repo = RiskAssessmentRepository(session)
    fetched = await fetched_repo.get(created.assessment_id)
    assert fetched is not None
    assert len(fetched.rule_evaluations) == 3
    assert fetched.rule_evaluations[0].rule_id == "velocity_ip_5m"
    assert fetched.rule_evaluations[1].rule_id == "new_device"
    assert fetched.rule_evaluations[2].rule_id == "vpn_proxy"


async def test_action_request_and_outbox_commit_atomically(
    session: AsyncSession,
) -> None:
    """Action request and outbox message commit atomically in same transaction."""
    event = await create_mandate_event(session)
    snapshot = await create_feature_snapshot(session, event.mandate_event_id)
    assessment = await create_assessment(
        session, snapshot.feature_snapshot_id, event.mandate_event_id
    )

    repo = ActionRepository(session)

    action_request = ActionRequest(
        schema_version="1.0",
        action_request_id=uuid4(),
        assessment_id=assessment.assessment_id,
        mandate_event_id=event.mandate_event_id,
        action_type="TOKEN_REVOKE",
        token_id="token_demo456",
        idempotency_key="idem-key-000000000001",
        status=ActionStatus.QUEUED,
        requested_at=datetime(2026, 1, 15, 10, 0, 4, tzinfo=UTC),
    )

    outbox_payload = {
        "action_request_id": str(action_request.action_request_id),
        "token_id": action_request.token_id,
        "type": "token_revoke_requested",
    }

    created = await repo.create_request(action_request, outbox_payload)
    assert created.action_request_id == action_request.action_request_id

    # Verify outbox message was created (outbox aggregate_id == action_request_id)
    outbox_stmt = select(OutboxMessageORM).where(
        OutboxMessageORM.aggregate_id == created.action_request_id
    )
    outbox_result = await session.execute(outbox_stmt)
    outbox_orm = outbox_result.scalar_one()
    assert outbox_orm.aggregate_type == "action_request"
    assert outbox_orm.message_type == "token_revoke_requested"
    assert outbox_orm.status == "PENDING"


async def test_action_request_duplicate_idempotency_key_is_rejected(
    session: AsyncSession,
) -> None:
    """Action request duplicate idempotency key is rejected."""
    event1 = await create_mandate_event(session)
    snapshot1 = await create_feature_snapshot(session, event1.mandate_event_id)
    assessment1 = await create_assessment(
        session, snapshot1.feature_snapshot_id, event1.mandate_event_id
    )

    event2 = await create_mandate_event(session)
    snapshot2 = await create_feature_snapshot(session, event2.mandate_event_id)
    assessment2 = await create_assessment(
        session, snapshot2.feature_snapshot_id, event2.mandate_event_id
    )

    repo = ActionRepository(session)

    action1 = ActionRequest(
        schema_version="1.0",
        action_request_id=uuid4(),
        assessment_id=assessment1.assessment_id,
        mandate_event_id=event1.mandate_event_id,
        action_type="TOKEN_REVOKE",
        token_id="token_demo456",
        idempotency_key="same-idem-key-000000001",
        status=ActionStatus.QUEUED,
        requested_at=datetime(2026, 1, 15, 10, 0, 4, tzinfo=UTC),
    )

    await repo.create_request(action1)

    # Second request with same idempotency_key but a different mandate event
    action2 = ActionRequest(
        schema_version="1.0",
        action_request_id=uuid4(),
        assessment_id=assessment2.assessment_id,
        mandate_event_id=event2.mandate_event_id,
        action_type="TOKEN_REVOKE",
        token_id="token_demo789",
        idempotency_key="same-idem-key-000000001",  # Duplicate!
        status=ActionStatus.QUEUED,
        requested_at=datetime(2026, 1, 15, 10, 0, 4, tzinfo=UTC),
    )

    with pytest.raises(Exception) as exc_info:
        await repo.create_request(action2)

    assert (
        "idempotency_key" in str(exc_info.value).lower() or "unique" in str(exc_info.value).lower()
    )


async def test_outbox_claim_batch_is_exclusive_between_two_sessions(
    session: AsyncSession,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Outbox claim_batch is exclusive between two sessions (simulated)."""
    from app.persistence.outbox import OutboxRepository

    # Create a test message
    outbox = OutboxRepository(session)
    await outbox.enqueue(
        aggregate_type="action_request",
        aggregate_id=uuid4(),
        message_type="token_revoke_requested",
        payload={"test": "data"},
        idempotency_key="test_idem_001",
        available_at=datetime.now(UTC),
    )
    await session.commit()

    # Claim from two independent sessions; only one worker may receive the row
    async with test_session_factory() as session1:
        outbox1 = OutboxRepository(session1)
        claimed1 = await outbox1.claim_batch("worker_1", 10, datetime.now(UTC))
        await session1.commit()

    async with test_session_factory() as session2:
        outbox2 = OutboxRepository(session2)
        claimed2 = await outbox2.claim_batch("worker_2", 10, datetime.now(UTC))
        await session2.commit()

    # Only one worker should get the message
    total_claimed = len(claimed1) + len(claimed2)
    assert total_claimed == 1, f"Expected 1 message claimed, got {total_claimed}"


async def test_outbox_release_retry_and_dead_letter_transitions(session: AsyncSession) -> None:
    """Outbox release_for_retry and mark_dead_letter transitions work."""
    from app.persistence.outbox import OutboxRepository

    outbox = OutboxRepository(session)
    await outbox.enqueue(
        aggregate_type="action_request",
        aggregate_id=uuid4(),
        message_type="token_revoke_requested",
        payload={"test": "data"},
        idempotency_key="test_idem_002",
        available_at=datetime.now(UTC),
    )
    await session.commit()

    # Claim the message
    claimed = await outbox.claim_batch("worker_1", 10, datetime.now(UTC))
    assert len(claimed) == 1
    claimed_msg = claimed[0]
    assert claimed_msg["status"] == "PROCESSING"

    # Release for retry
    await outbox.release_for_retry(
        claimed_msg["outbox_message_id"],
        datetime.now(UTC),
        "TRANSIENT_ERROR",
    )
    await session.commit()

    # Verify it's back to PENDING
    refreshed = await outbox.get(claimed_msg["outbox_message_id"])
    assert refreshed["status"] == "PENDING"
    assert refreshed["last_error_code"] == "TRANSIENT_ERROR"

    # Claim again and mark dead letter
    claimed2 = await outbox.claim_batch("worker_1", 10, datetime.now(UTC))
    assert len(claimed2) == 1

    await outbox.mark_dead_letter(claimed2[0]["outbox_message_id"], "PERMANENT_ERROR")
    await session.commit()

    refreshed2 = await outbox.get(claimed2[0]["outbox_message_id"])
    assert refreshed2["status"] == "DEAD_LETTER"
    assert refreshed2["last_error_code"] == "PERMANENT_ERROR"


async def test_audit_append_builds_and_verifies_chain(session: AsyncSession) -> None:
    """Audit append builds and verifies chain."""
    repo = AuditRepository(session)

    aggregate_type = "risk_session"
    aggregate_id = uuid4()

    # Append first event
    event1 = await repo.append(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type="risk_session.created",
        actor_type=AuditActorType.SYSTEM,
        actor_id=None,
        occurred_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        redacted_payload={"action": "risk_session.created"},
    )

    assert event1.sequence_number == 1
    assert event1.previous_event_hash is None
    assert event1.event_hash.startswith("sha256:")

    # Append second event
    event2 = await repo.append(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type="risk_session.updated",
        actor_type=AuditActorType.SYSTEM,
        actor_id=None,
        occurred_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        redacted_payload={"action": "risk_session.updated"},
    )

    assert event2.sequence_number == 2
    assert event2.previous_event_hash == event1.event_hash

    # Verify chain
    result = await repo.verify_aggregate(aggregate_type, aggregate_id)
    assert result.valid is True
    assert result.checked_events == 2


async def test_audit_chain_detects_direct_database_tampering(session: AsyncSession) -> None:
    """Audit chain detects direct database tampering."""
    repo = AuditRepository(session)

    aggregate_type = "risk_session"
    aggregate_id = uuid4()

    # Create valid chain
    await repo.append(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type="risk_session.created",
        actor_type=AuditActorType.SYSTEM,
        actor_id=None,
        occurred_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        redacted_payload={"action": "risk_session.created"},
    )
    await session.commit()

    # Tamper directly with SQL - change redacted_payload
    await session.execute(
        text("""
            UPDATE audit_events
            SET redacted_payload = '{"tampered": true}'
            WHERE aggregate_type = :atype AND aggregate_id = :aid
        """),
        {"atype": aggregate_type, "aid": str(aggregate_id)},
    )
    await session.commit()

    # Verification should fail
    result = await repo.verify_aggregate(aggregate_type, aggregate_id)
    assert result.valid is False
    assert result.first_invalid_sequence == 1
    assert "Hash mismatch" in result.reason


async def test_concurrent_audit_appends_are_linear_and_valid(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent audit appends to same aggregate produce ordered, valid chain."""
    import asyncio

    aggregate_type = "risk_session"
    aggregate_id = uuid4()

    async def append_event(event_type: str, delay: float = 0) -> None:
        await asyncio.sleep(delay)
        async with test_session_factory() as s:
            repo = AuditRepository(s)
            await repo.append(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                actor_type=AuditActorType.SYSTEM,
                actor_id=None,
                occurred_at=datetime.now(UTC),
                redacted_payload={"event": event_type},
            )
            await s.commit()

    # Simulate concurrent appends
    await asyncio.gather(
        append_event("event_a", 0),
        append_event("event_b", 0.01),
        append_event("event_c", 0.02),
    )

    # Verify chain is valid and sequential
    async with test_session_factory() as verify_session:
        verify_repo = AuditRepository(verify_session)
        result = await verify_repo.verify_aggregate(aggregate_type, aggregate_id)
        assert result.valid is True
        assert result.checked_events == 3

        # Check sequence is 1, 2, 3
        events = await verify_repo.list_for_aggregate(aggregate_type, aggregate_id)
        assert [e.sequence_number for e in events] == [1, 2, 3]


async def test_dashboard_projection_is_redacted_and_ordered(session: AsyncSession) -> None:
    """Dashboard projection is redacted and ordered by assessed_at DESC."""
    event1 = await create_mandate_event(session)
    snapshot1 = await create_feature_snapshot(session, event1.mandate_event_id)
    assessment1 = await create_assessment(
        session, snapshot1.feature_snapshot_id, event1.mandate_event_id
    )

    event2 = await create_mandate_event(session)
    snapshot2 = await create_feature_snapshot(session, event2.mandate_event_id)
    repo_ra = RiskAssessmentRepository(session)
    assessment2 = await repo_ra.create(
        RiskAssessment(
            schema_version="1.0",
            assessment_id=uuid4(),
            risk_session_id=None,
            mandate_event_id=event2.mandate_event_id,
            feature_snapshot_id=snapshot2.feature_snapshot_id,
            stage=DecisionStage.POST_CONFIRMATION,
            score=20,
            decision=Decision.ALLOW,
            engine_version="rules-v1",
            rule_evaluations=[
                RuleEvaluation(
                    rule_id="clean_user",
                    triggered=False,
                    points=0,
                    reason_code=None,
                    reason_text=None,
                )
            ],
            evaluation_latency_ms=3,
            assessed_at=datetime(2026, 1, 15, 10, 10, 0, tzinfo=UTC),
        )
    )

    repo = DashboardReadRepository(session)

    results = await repo.list_recent_assessments(limit=10, offset=0)
    assert len(results) >= 2
    # Verify ordering: most recent assessed_at first
    assert results[0].assessment_id == assessment2.assessment_id
    assert results[1].assessment_id == assessment1.assessment_id

    # Verify field values on projection
    row = results[1]
    assert row.decision == Decision.CHALLENGE
    assert row.score == 75
    assert row.token_id == "token_demo123"
    assert row.vpa_handle == "upi"
    assert row.is_demo_event is True
    assert len(row.rule_reasons) == 3
    assert "IP velocity in 5m window exceeds threshold" in row.rule_reasons

    # Verify sensitive hashes are NEVER present on DashboardAssessmentRow
    assert not hasattr(row, "vpa_hash")
    assert not hasattr(row, "ip_hash")
    assert not hasattr(row, "device_fingerprint_hash")
    assert not hasattr(row, "customer_reference_hash")
    assert not hasattr(row, "raw_payload_sha256")

    # Verify filter by decision
    filtered = await repo.list_recent_assessments(limit=10, offset=0, decision=Decision.ALLOW)
    assert len(filtered) == 1
    assert filtered[0].assessment_id == assessment2.assessment_id


# ── Additional Integration Tests ───────────────────────────────────────────


async def test_risk_session_update_telemetry_preserves_existing_hashes(
    session: AsyncSession,
) -> None:
    """update_telemetry preserves existing hash values when None provided."""
    repo = RiskSessionRepository(session)

    created = await repo.create(make_risk_session())
    original_ip_hash = created.ip_hash
    original_device_hash = created.device_fingerprint_hash

    # Update with only flow_completed_at and status
    updated = await repo.update_telemetry(
        risk_session_id=created.risk_session_id,
        customer_reference_hash=None,  # None = retain
        ip_hash=None,  # None = retain
        device_fingerprint_hash=None,  # None = retain
        user_agent_hash=None,  # None = retain
        flow_completed_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        status=RiskSessionStatus.READY,
        updated_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
    )

    assert updated.ip_hash == original_ip_hash
    assert updated.device_fingerprint_hash == original_device_hash
    assert updated.flow_completed_at == datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC)
    assert updated.status == RiskSessionStatus.READY


async def test_mandate_event_attach_risk_session(
    session: AsyncSession, sample_mandate_event: MandateWebhookEvent
) -> None:
    """Mandate event can be linked to risk session after creation."""
    repo = MandateEventRepository(session)

    # Create event without risk_session_id
    event, _ = await repo.create_or_get(sample_mandate_event)
    assert event.risk_session_id is None

    # Create a risk session
    rs_repo = RiskSessionRepository(session)
    rs = await rs_repo.create(
        make_risk_session(
            namespace="demo-merchant-01",
            order_ref="order_demo_002",
        )
    )

    # Attach
    attached = await repo.attach_risk_session(event.mandate_event_id, rs.risk_session_id)
    assert attached.risk_session_id == rs.risk_session_id


async def test_feature_snapshot_partial_unique_indexes(
    session: AsyncSession,
) -> None:
    """Feature snapshot partial unique indexes work correctly."""
    event = await create_mandate_event(session)
    repo = FeatureSnapshotRepository(session)

    mandate_event_id = event.mandate_event_id
    fs1 = FeatureSnapshot(
        schema_version="1.0",
        feature_snapshot_id=uuid4(),
        mandate_event_id=mandate_event_id,
        risk_session_id=None,
        feature_version="rules-v1",
        calculated_at=datetime(2026, 1, 15, 10, 0, 2, tzinfo=UTC),
        is_demo_simulation=False,
        sources={},
    )
    await repo.create(fs1)

    # Duplicate with same mandate_event_id and feature_version should fail
    fs2 = FeatureSnapshot(
        schema_version="1.0",
        feature_snapshot_id=uuid4(),
        mandate_event_id=mandate_event_id,
        risk_session_id=None,
        feature_version="rules-v1",  # Same!
        calculated_at=datetime(2026, 1, 15, 10, 1, 0, tzinfo=UTC),
        is_demo_simulation=False,
        sources={},
    )

    with pytest.raises(Exception) as exc_info:
        await repo.create(fs2)

    assert (
        "uq_feature_snapshots_event_version" in str(exc_info.value)
        or "unique" in str(exc_info.value).lower()
    )
