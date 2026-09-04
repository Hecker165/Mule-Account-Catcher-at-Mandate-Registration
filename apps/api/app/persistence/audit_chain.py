"""Tamper-evident audit chain implementation for A1 persistence.

Implements the canonical SHA-256 hash algorithm for audit events as specified
in A1_PERSISTENCE_AND_AUDIT.md Section 7.1.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class AuditVerificationResult:
    """Result of verifying an audit chain."""

    valid: bool
    checked_events: int
    first_invalid_sequence: int | None = None
    reason: str | None = None


def compute_event_hash(
    previous_event_hash: str | None,
    aggregate_type: str,
    aggregate_id: UUID,
    sequence_number: int,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
    occurred_at: datetime,
    redacted_payload: dict[str, Any],
) -> str:
    """Compute the canonical SHA-256 hash for an audit event.

    This is the exact algorithm specified in A1 spec Section 7.1.
    Used by both write and verify paths.

    Args:
        previous_event_hash: Hash of the previous event in the chain, or None for
            genesis
        aggregate_type: Type of aggregate (risk_session, mandate_event,
            risk_assessment, action_request)
        aggregate_id: UUID of the aggregate
        sequence_number: Sequence number in the chain (1-based)
        event_type: Type of event (e.g., risk_assessment.created)
        actor_type: Actor type (SYSTEM, WEBHOOK, WORKER, OPERATOR, DEMO)
        actor_id: Optional actor identifier
        occurred_at: When the event occurred (timezone-aware UTC)
        redacted_payload: Redacted payload as dict

    Returns:
        Hash string in format "sha256:<64 hex chars>"
    """
    # Canonical JSON serialization
    canonical_payload = json.dumps(
        redacted_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    # Normalize timestamp to UTC with Z suffix
    utc_time = occurred_at.astimezone(UTC)
    occurred_str = utc_time.isoformat().replace("+00:00", "Z")

    # Build hash input exactly as specified
    hash_input = "|".join(
        [
            previous_event_hash or "GENESIS",
            aggregate_type,
            str(aggregate_id).lower(),
            str(sequence_number),
            event_type,
            actor_type,
            actor_id or "",
            occurred_str,
            canonical_payload,
        ]
    ).encode("utf-8")

    return "sha256:" + hashlib.sha256(hash_input).hexdigest()


def compute_next_hash(
    last_event: dict[str, Any] | None,
    aggregate_type: str,
    aggregate_id: UUID,
    sequence_number: int,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
    occurred_at: datetime,
    redacted_payload: dict[str, Any],
) -> str:
    """Compute hash for the next event in a chain.

    Args:
        last_event: The last event in the chain (dict with previous_event_hash, event_hash, etc.)
                    or None if this is the first event.
        Other args: Same as compute_event_hash

    Returns:
        The computed event_hash for the new event.
    """
    previous_hash = None
    if last_event is not None:
        previous_hash = last_event.get("event_hash")

    return compute_event_hash(
        previous_event_hash=previous_hash,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        sequence_number=sequence_number,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        occurred_at=occurred_at,
        redacted_payload=redacted_payload,
    )


def verify_audit_chain(events: list[dict[str, Any]]) -> AuditVerificationResult:
    """Verify an audit event chain for integrity.

    Args:
        events: List of audit event dicts, ordered by sequence_number ASC.
            Each dict must have: sequence_number, event_hash, previous_event_hash,
            aggregate_type, aggregate_id, event_type, actor_type, actor_id,
            occurred_at, redacted_payload

    Returns:
        AuditVerificationResult with validation outcome.
    """
    if not events:
        return AuditVerificationResult(valid=True, checked_events=0)

    # Check sequence starts at 1 and increments by 1
    for i, event in enumerate(events):
        expected_seq = i + 1
        if event["sequence_number"] != expected_seq:
            return AuditVerificationResult(
                valid=False,
                checked_events=i,
                first_invalid_sequence=event["sequence_number"],
                reason=f"Sequence number {event['sequence_number']} != expected {expected_seq}",
            )

    # Check first event has no previous hash
    if events[0]["previous_event_hash"] is not None:
        return AuditVerificationResult(
            valid=False,
            checked_events=0,
            first_invalid_sequence=1,
            reason="First event must have previous_event_hash=None",
        )

    # Verify each event's hash and chain linkage
    for i, event in enumerate(events):
        # Recompute the hash
        computed_hash = compute_event_hash(
            previous_event_hash=event["previous_event_hash"],
            aggregate_type=event["aggregate_type"],
            aggregate_id=UUID(event["aggregate_id"]),
            sequence_number=event["sequence_number"],
            event_type=event["event_type"],
            actor_type=event["actor_type"],
            actor_id=event["actor_id"],
            occurred_at=event["occurred_at"],
            redacted_payload=event["redacted_payload"],
        )

        # Check stored hash matches computed hash
        if event["event_hash"] != computed_hash:
            return AuditVerificationResult(
                valid=False,
                checked_events=i,
                first_invalid_sequence=event["sequence_number"],
                reason=(
                    f"Hash mismatch at sequence {event['sequence_number']}: "
                    f"stored={event['event_hash']}, computed={computed_hash}"
                ),
            )

        # Check previous hash linkage (for events after the first)
        if i > 0:
            prev_hash = events[i - 1]["event_hash"]
            if event["previous_event_hash"] != prev_hash:
                return AuditVerificationResult(
                    valid=False,
                    checked_events=i,
                    first_invalid_sequence=event["sequence_number"],
                    reason=(
                        f"Previous hash mismatch at sequence "
                        f"{event['sequence_number']}: expected {prev_hash}, "
                        f"got {event['previous_event_hash']}"
                    ),
                )

    return AuditVerificationResult(
        valid=True,
        checked_events=len(events),
        first_invalid_sequence=None,
        reason=None,
    )


# Advisory lock key for concurrent append protection. The PostgreSQL session
# hashes this string with hashtextextended(...) inside pg_advisory_xact_lock,
# which is deterministic across processes (unlike Python's hash()).
def audit_lock_key_string(aggregate_type: str, aggregate_id: UUID) -> str:
    """Return the canonical advisory-lock key for an aggregate audit chain.

    Spec-exact format: ``audit:{aggregate_type}:{aggregate_id-lowercase}``.
    """
    return f"audit:{aggregate_type}:{str(aggregate_id).lower()}"
