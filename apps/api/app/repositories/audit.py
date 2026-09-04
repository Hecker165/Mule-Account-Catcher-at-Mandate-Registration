"""Audit repository implementation with tamper-evident chain.

Provides append-only audit event persistence with advisory locking for concurrency
and chain verification capabilities.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.audit import AuditEvent
from app.contracts.common import AuditActorType
from app.persistence.audit_chain import (
    AuditVerificationResult,
    audit_lock_key_string,
    compute_next_hash,
    verify_audit_chain,
)
from app.persistence.models import AuditEvent as AuditEventORM
from app.persistence.types import audit_event_from_row


class AuditRepository:
    """Repository for audit event persistence with chain integrity."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def append(
        self,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        occurred_at: datetime,
        redacted_payload: dict[str, Any],
    ) -> AuditEvent:
        """Append an audit event to the chain with advisory lock for concurrency.

        Acquires a PostgreSQL transaction-scoped advisory lock to ensure
        correct linear chain when concurrent appends occur.
        """
        lock_key = audit_lock_key_string(aggregate_type, aggregate_id)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

        # Get the latest event in this aggregate's chain
        stmt = (
            select(AuditEventORM)
            .where(
                AuditEventORM.aggregate_type == aggregate_type,
                AuditEventORM.aggregate_id == aggregate_id,
            )
            .order_by(AuditEventORM.sequence_number.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        last_event = result.scalar_one_or_none()

        sequence_number = 1
        if last_event is not None:
            sequence_number = last_event.sequence_number + 1

        event_hash = compute_next_hash(
            last_event=(
                {
                    "event_hash": last_event.event_hash,
                    "previous_event_hash": last_event.previous_event_hash,
                }
                if last_event
                else None
            ),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            sequence_number=sequence_number,
            event_type=event_type,
            actor_type=actor_type.value if isinstance(actor_type, AuditActorType) else actor_type,
            actor_id=actor_id,
            occurred_at=occurred_at,
            redacted_payload=redacted_payload,
        )

        previous_event_hash = last_event.event_hash if last_event else None

        row_data = {
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "sequence_number": sequence_number,
            "event_type": event_type,
            "actor_type": actor_type.value
            if isinstance(actor_type, AuditActorType)
            else actor_type,
            "actor_id": actor_id,
            "occurred_at": occurred_at,
            "redacted_payload": redacted_payload,
            "previous_event_hash": previous_event_hash,
            "event_hash": event_hash,
            "schema_version": "1.0",
        }

        orm_obj = AuditEventORM(**row_data)
        self.session.add(orm_obj)
        await self.session.flush()
        return audit_event_from_row(orm_obj)

    async def list_for_aggregate(self, aggregate_type: str, aggregate_id: UUID) -> list[AuditEvent]:
        """List all audit events for an aggregate, ordered by sequence."""
        stmt = (
            select(AuditEventORM)
            .where(
                AuditEventORM.aggregate_type == aggregate_type,
                AuditEventORM.aggregate_id == aggregate_id,
            )
            .order_by(AuditEventORM.sequence_number)
        )
        result = await self.session.execute(stmt)
        orms = result.scalars().all()
        return [audit_event_from_row(orm) for orm in orms]

    async def verify_aggregate(
        self, aggregate_type: str, aggregate_id: UUID
    ) -> AuditVerificationResult:
        """Verify the integrity of an audit chain for an aggregate."""
        stmt = (
            select(AuditEventORM)
            .where(
                AuditEventORM.aggregate_type == aggregate_type,
                AuditEventORM.aggregate_id == aggregate_id,
            )
            .order_by(AuditEventORM.sequence_number)
        )
        result = await self.session.execute(stmt)
        orms = result.scalars().all()

        if not orms:
            return AuditVerificationResult(valid=True, checked_events=0)

        # Convert to dicts for verification
        events = []
        for orm in orms:
            events.append(
                {
                    "sequence_number": orm.sequence_number,
                    "event_hash": orm.event_hash,
                    "previous_event_hash": orm.previous_event_hash,
                    "aggregate_type": orm.aggregate_type,
                    "aggregate_id": str(orm.aggregate_id),
                    "event_type": orm.event_type,
                    "actor_type": orm.actor_type,
                    "actor_id": orm.actor_id,
                    "occurred_at": orm.occurred_at,
                    "redacted_payload": orm.redacted_payload,
                }
            )

        return verify_audit_chain(events)
