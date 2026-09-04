"""Migration test - verifies alembic upgrade creates expected schema."""

from sqlalchemy import text


async def test_migration_creates_expected_tables(clean_session):
    """Test that migration creates all expected tables with correct names."""
    result = await clean_session.execute(
        text("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
        AND table_name NOT IN ('alembic_version')
        ORDER BY table_name
    """)
    )
    tables = {row[0] for row in result}

    expected = {
        "action_attempts",
        "action_requests",
        "audit_events",
        "feature_snapshots",
        "mandate_webhook_events",
        "outbox_messages",
        "risk_assessments",
        "risk_sessions",
        "rule_evaluations",
    }

    assert tables == expected, f"Tables mismatch. Expected: {expected}, Got: {tables}"


async def test_migration_creates_expected_columns(clean_session):
    """Test that key tables have expected columns."""
    result = await clean_session.execute(
        text("""
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
        AND table_name IN (
            'risk_sessions', 'mandate_webhook_events', 'feature_snapshots',
            'risk_assessments', 'action_requests', 'audit_events'
        )
        ORDER BY table_name, ordinal_position
    """)
    )
    columns = {}
    for row in result:
        table = row[0]
        if table not in columns:
            columns[table] = []
        columns[table].append(row[1])

    # Verify critical columns exist
    assert "risk_session_id" in columns["risk_sessions"]
    assert "customer_reference_hash" in columns["risk_sessions"]
    assert "ip_hash" in columns["risk_sessions"]
    assert "mandate_intent" in columns["risk_sessions"]

    assert "mandate_event_id" in columns["mandate_webhook_events"]
    assert "provider_event_id" in columns["mandate_webhook_events"]
    assert "vpa_hash" in columns["mandate_webhook_events"]
    assert "raw_payload_sha256" in columns["mandate_webhook_events"]

    assert "feature_snapshot_id" in columns["feature_snapshots"]
    assert "mandate_event_id" in columns["feature_snapshots"]
    assert "risk_session_id" in columns["feature_snapshots"]
    assert "sources" in columns["feature_snapshots"]

    assert "assessment_id" in columns["risk_assessments"]
    assert "feature_snapshot_id" in columns["risk_assessments"]
    assert "rule_evaluations" not in columns["risk_assessments"]  # Separate table

    assert "action_request_id" in columns["action_requests"]
    assert "idempotency_key" in columns["action_requests"]

    assert "audit_event_id" in columns["audit_events"]
    assert "aggregate_type" in columns["audit_events"]
    assert "sequence_number" in columns["audit_events"]
    assert "previous_event_hash" in columns["audit_events"]
    assert "event_hash" in columns["audit_events"]
    assert "redacted_payload" in columns["audit_events"]


async def test_migration_creates_expected_indexes(clean_session):
    """Test that expected indexes exist."""
    result = await clean_session.execute(
        text("""
        SELECT indexname, tablename FROM pg_indexes
        WHERE schemaname = 'public'
        AND indexname NOT LIKE 'pg_%'
        ORDER BY tablename, indexname
    """)
    )
    indexes = {row[0]: row[1] for row in result}

    expected_indexes = {
        "ix_risk_sessions_merchant_namespace": "risk_sessions",
        "ix_risk_sessions_customer_reference_hash": "risk_sessions",
        "ix_mandate_events_received_at_desc": "mandate_webhook_events",
        "ix_feature_snapshots_calculated_at": "feature_snapshots",
        "ix_risk_assessments_assessed_at_desc": "risk_assessments",
        "ix_action_requests_status_requested_at": "action_requests",
        "ix_outbox_claimable": "outbox_messages",
        "ix_audit_events_aggregate_tail": "audit_events",
    }

    for idx_name, expected_table in expected_indexes.items():
        assert idx_name in indexes, f"Missing index: {idx_name}"
        assert indexes[idx_name] == expected_table, f"Index {idx_name} on wrong table"


async def test_migration_creates_expected_foreign_keys(clean_session):
    """Test that expected foreign keys exist."""
    result = await clean_session.execute(
        text("""
        SELECT
            tc.table_name,
            kcu.column_name,
            ccu.table_name AS foreign_table_name,
            ccu.column_name AS foreign_column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
        AND tc.table_schema = 'public'
        ORDER BY tc.table_name, kcu.column_name
    """)
    )
    fks = {}
    for row in result:
        table = row[0]
        if table not in fks:
            fks[table] = []
        fks[table].append((row[1], row[2], row[3]))

    # Verify key foreign keys
    assert "mandate_webhook_events" in fks
    mandate_fks = set(fks["mandate_webhook_events"])
    assert ("risk_session_id", "risk_sessions", "risk_session_id") in mandate_fks

    assert "feature_snapshots" in fks
    feature_fks = set(fks["feature_snapshots"])
    assert ("mandate_event_id", "mandate_webhook_events", "mandate_event_id") in feature_fks
    assert ("risk_session_id", "risk_sessions", "risk_session_id") in feature_fks

    assert "risk_assessments" in fks
    assessment_fks = set(fks["risk_assessments"])
    assert ("feature_snapshot_id", "feature_snapshots", "feature_snapshot_id") in assessment_fks
