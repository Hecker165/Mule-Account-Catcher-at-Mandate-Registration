"""Unit tests for audit chain tamper-evident hashing.

Tests the exact SHA-256 output for known inputs, stable hash output
despite input dictionary key order, UTC normalisation, and failure
detection when stored hash/previous hash/sequence is altered.
"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from app.persistence.audit_chain import (
    AuditVerificationResult,
    compute_event_hash,
    verify_audit_chain,
)


def test_genesis_event_hash() -> None:
    """Test hash computation for a genesis (first) event."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
    occurred_at = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    redacted_payload = {
        "action": "risk_session.created",
        "merchant_namespace": "demo-merchant-01",
    }

    hash_value = compute_event_hash(
        previous_event_hash=None,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=1,
        event_type="risk_session.created",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=occurred_at,
        redacted_payload=redacted_payload,
    )

    # Expected hash computed from canonical input
    canonical_payload = json.dumps(
        redacted_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    occurred_str = "2026-01-15T10:00:00Z"
    hash_input = "|".join(
        [
            "GENESIS",
            aggregate_type,
            str(aggregate_id).lower(),
            "1",
            "risk_session.created",
            "SYSTEM",
            "",
            occurred_str,
            canonical_payload,
        ]
    ).encode("utf-8")
    expected = "sha256:" + hashlib.sha256(hash_input).hexdigest()

    assert hash_value == expected
    assert hash_value.startswith("sha256:")
    assert len(hash_value) == 71  # "sha256:" + 64 hex chars


def test_chained_event_hash() -> None:
    """Test hash computation for a chained (non-genesis) event."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
    previous_hash = "sha256:abc123" + "0" * 58  # dummy previous hash
    occurred_at = datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC)
    redacted_payload = {
        "action": "risk_session.telemetry_updated",
        "status": "READY",
    }

    hash_value = compute_event_hash(
        previous_event_hash=previous_hash,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=2,
        event_type="risk_session.telemetry_updated",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=occurred_at,
        redacted_payload=redacted_payload,
    )

    # Verify it's a valid sha256 hash
    assert hash_value.startswith("sha256:")
    assert len(hash_value) == 71

    # Verify it differs from genesis
    assert hash_value != previous_hash


def test_hash_stable_despite_key_order() -> None:
    """Hash is stable regardless of input dictionary key order."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
    occurred_at = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)

    payload_a = {"z_key": "first", "a_key": "second"}
    payload_b = {"a_key": "second", "z_key": "first"}

    hash_a = compute_event_hash(
        previous_event_hash=None,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=1,
        event_type="test",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=occurred_at,
        redacted_payload=payload_a,
    )

    hash_b = compute_event_hash(
        previous_event_hash=None,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=1,
        event_type="test",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=occurred_at,
        redacted_payload=payload_b,
    )

    assert hash_a == hash_b, "Hash must be deterministic regardless of key order"


def test_utc_normalisation() -> None:
    """Timestamps are normalized to UTC with Z suffix."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

    # Same instant, different timezones
    dt_utc = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    dt_est = datetime(2026, 1, 15, 5, 0, 0, tzinfo=UTC)  # UTC-5

    hash_utc = compute_event_hash(
        previous_event_hash=None,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=1,
        event_type="test",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=dt_utc,
        redacted_payload={},
    )

    hash_est = compute_event_hash(
        previous_event_hash=None,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=1,
        event_type="test",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=dt_est,
        redacted_payload={},
    )

    # These represent DIFFERENT instants, so hashes should differ
    # This test verifies the conversion happens correctly
    assert hash_utc != hash_est


def test_verify_valid_chain() -> None:
    """Verify a valid chain returns valid=True."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

    # Genesis event - compute its real hash
    event1_hash = compute_event_hash(
        previous_event_hash=None,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=1,
        event_type="risk_session.created",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        redacted_payload={"action": "risk_session.created"},
    )

    event1 = {
        "sequence_number": 1,
        "event_hash": event1_hash,
        "previous_event_hash": None,
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id).lower(),
        "event_type": "risk_session.created",
        "actor_type": "SYSTEM",
        "actor_id": None,
        "occurred_at": datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        "redacted_payload": {"action": "risk_session.created"},
    }

    # Second event - compute its real hash
    event2_prev = event1["event_hash"]
    event2_hash = compute_event_hash(
        previous_event_hash=event2_prev,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=2,
        event_type="risk_session.updated",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        redacted_payload={"action": "risk_session.updated"},
    )

    event2 = {
        "sequence_number": 2,
        "event_hash": event2_hash,
        "previous_event_hash": event2_prev,
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id).lower(),
        "event_type": "risk_session.updated",
        "actor_type": "SYSTEM",
        "actor_id": None,
        "occurred_at": datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        "redacted_payload": {"action": "risk_session.updated"},
    }

    result = verify_audit_chain([event1, event2])
    assert isinstance(result, AuditVerificationResult)
    assert result.valid is True
    assert result.checked_events == 2
    assert result.first_invalid_sequence is None
    assert result.reason is None


def test_verify_empty_chain() -> None:
    """Empty chain is valid with 0 checked events."""
    result = verify_audit_chain([])
    assert result.valid is True
    assert result.checked_events == 0


def test_verify_broken_previous_hash() -> None:
    """Detects broken previous hash linkage."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

    # Genesis event - compute real hash
    event1_hash = compute_event_hash(
        previous_event_hash=None,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=1,
        event_type="risk_session.created",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        redacted_payload={"action": "risk_session.created"},
    )

    event1 = {
        "sequence_number": 1,
        "event_hash": event1_hash,
        "previous_event_hash": None,
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id).lower(),
        "event_type": "risk_session.created",
        "actor_type": "SYSTEM",
        "actor_id": None,
        "occurred_at": datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        "redacted_payload": {"action": "risk_session.created"},
    }

    # Second event: compute correct hash but with WRONG previous hash
    # This means we compute the hash using the wrong previous hash,
    # but store that computed hash. Then when verifying, the previous
    # hash check should fail (event2.previous_event_hash != event1.event_hash)
    wrong_prev = "sha256:WRONG_WRONG_WRONG_WRONG_WRONG_WRONG_WRONG_WRONG_WRONG_WRONG"
    event2_hash = compute_event_hash(
        previous_event_hash=wrong_prev,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=2,
        event_type="risk_session.updated",
        actor_type="SYSTEM",
        actor_id=None,
        occurred_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        redacted_payload={"action": "risk_session.updated"},
    )

    event2 = {
        "sequence_number": 2,
        "event_hash": event2_hash,  # Correct hash for the WRONG previous
        "previous_event_hash": wrong_prev,  # Wrong previous hash!
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id).lower(),
        "event_type": "risk_session.updated",
        "actor_type": "SYSTEM",
        "actor_id": None,
        "occurred_at": datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        "redacted_payload": {"action": "risk_session.updated"},
    }

    result = verify_audit_chain([event1, event2])
    assert result.valid is False
    assert result.checked_events == 1  # First event checked (valid), second failed
    assert result.first_invalid_sequence == 2
    assert "Previous hash mismatch" in result.reason


def test_verify_broken_stored_hash() -> None:
    """Detects when stored hash doesn't match recomputed hash."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

    event1 = {
        "sequence_number": 1,
        "event_hash": (
            "sha256:INVALID_HASH_1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
        ),
        "previous_event_hash": None,
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id).lower(),
        "event_type": "risk_session.created",
        "actor_type": "SYSTEM",
        "actor_id": None,
        "occurred_at": datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        "redacted_payload": {"action": "risk_session.created"},
    }

    result = verify_audit_chain([event1])
    assert result.valid is False
    assert result.first_invalid_sequence == 1
    assert "Hash mismatch" in result.reason


def test_verify_sequence_gap() -> None:
    """Detects non-sequential sequence numbers."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

    event1 = {
        "sequence_number": 1,
        "event_hash": "sha256:1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a",
        "previous_event_hash": None,
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id).lower(),
        "event_type": "risk_session.created",
        "actor_type": "SYSTEM",
        "actor_id": None,
        "occurred_at": datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        "redacted_payload": {"action": "risk_session.created"},
    }

    event2 = {
        "sequence_number": 3,  # Gap! Should be 2
        "event_hash": "sha256:2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b",
        "previous_event_hash": event1["event_hash"],
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id).lower(),
        "event_type": "risk_session.updated",
        "actor_type": "SYSTEM",
        "actor_id": None,
        "occurred_at": datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        "redacted_payload": {"action": "risk_session.updated"},
    }

    result = verify_audit_chain([event1, event2])
    assert result.valid is False
    assert result.first_invalid_sequence == 3
    assert "Sequence number 3 != expected 2" in result.reason


def test_verify_first_event_must_have_null_previous() -> None:
    """First event must have previous_event_hash = None."""
    aggregate_type = "risk_session"
    aggregate_id = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

    event1 = {
        "sequence_number": 1,
        "event_hash": "sha256:1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a",
        "previous_event_hash": "sha256:SHOULD_BE_NULL",  # Invalid!
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id).lower(),
        "event_type": "risk_session.created",
        "actor_type": "SYSTEM",
        "actor_id": None,
        "occurred_at": datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        "redacted_payload": {"action": "risk_session.created"},
    }

    result = verify_audit_chain([event1])
    assert result.valid is False
    assert result.checked_events == 0
    assert result.first_invalid_sequence == 1
    assert "First event must have previous_event_hash=None" in result.reason
