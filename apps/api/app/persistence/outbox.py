"""Outbox message ORM and repository for transactional outbox pattern.

A1 owns this. A6 will consume messages; A1 only makes them atomically durable and claimable.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import OutboxMessage as OutboxMessageORM
from app.persistence.types import (
    OutboxMessage,
    outbox_message_from_row,
    outbox_message_to_row,
)

__all__ = ["OutboxMessage", "OutboxRepository"]


class OutboxRepository:
    """Repository for outbox message operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def enqueue(
        self,
        aggregate_type: str,
        aggregate_id: UUID,
        message_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        available_at: datetime,
    ) -> UUID:
        """Create a new outbox message in the current transaction.

        The caller owns the transaction boundary.
        """
        row_data = outbox_message_to_row(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            message_type=message_type,
            payload=payload,
            idempotency_key=idempotency_key,
            available_at=available_at,
        )
        msg = OutboxMessageORM(**row_data)
        self.session.add(msg)
        await self.session.flush()
        return msg.outbox_message_id

    async def claim_batch(
        self,
        worker_id: str,
        limit: int,
        now: datetime,
    ) -> list[OutboxMessage]:
        """Atomically claim up to `limit` pending messages for processing.

        Uses FOR UPDATE SKIP LOCKED to ensure exclusive claim across workers.
        Updates status to PROCESSING, locked_at, locked_by, and increments attempt_count.

        Returns:
            List of claimed OutboxMessage instances (empty if none available).
        """
        # Select pending messages available now, ordered by available_at
        # Use FOR UPDATE SKIP LOCKED to claim exclusively
        stmt = (
            select(OutboxMessageORM)
            .where(
                OutboxMessageORM.status == "PENDING",
                OutboxMessageORM.available_at <= now,
            )
            .order_by(OutboxMessageORM.available_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        messages = result.scalars().all()

        if not messages:
            return []

        claimed: list[OutboxMessage] = []
        for msg in messages:
            msg.status = "PROCESSING"
            msg.locked_at = now
            msg.locked_by = worker_id
            msg.attempt_count += 1
            claimed.append(outbox_message_from_row(msg))

        await self.session.flush()
        return claimed

    async def mark_processed(self, outbox_message_id: UUID, processed_at: datetime) -> None:
        """Mark a message as successfully processed."""
        stmt = (
            update(OutboxMessageORM)
            .where(OutboxMessageORM.outbox_message_id == outbox_message_id)
            .values(
                status="PROCESSED",
                processed_at=processed_at,
            )
        )
        await self.session.execute(stmt)

    async def release_for_retry(
        self,
        outbox_message_id: UUID,
        available_at: datetime,
        safe_error_code: str,
    ) -> None:
        """Release a message back to PENDING for retry after a transient failure."""
        stmt = (
            update(OutboxMessageORM)
            .where(OutboxMessageORM.outbox_message_id == outbox_message_id)
            .values(
                status="PENDING",
                locked_at=None,
                locked_by=None,
                available_at=available_at,
                last_error_code=safe_error_code,
            )
        )
        await self.session.execute(stmt)

    async def mark_dead_letter(self, outbox_message_id: UUID, safe_error_code: str) -> None:
        """Mark a message as dead letter after permanent failure."""
        stmt = (
            update(OutboxMessageORM)
            .where(OutboxMessageORM.outbox_message_id == outbox_message_id)
            .values(
                status="DEAD_LETTER",
                last_error_code=safe_error_code,
            )
        )
        await self.session.execute(stmt)

    async def get(self, outbox_message_id: UUID) -> OutboxMessage | None:
        """Get an outbox message by ID."""
        stmt = select(OutboxMessageORM).where(
            OutboxMessageORM.outbox_message_id == outbox_message_id
        )
        result = await self.session.execute(stmt)
        msg = result.scalar_one_or_none()
        if msg is None:
            return None
        return outbox_message_from_row(msg)

    async def get_pending_count(self) -> int:
        """Get count of pending messages (for monitoring)."""
        from sqlalchemy import func

        stmt = select(func.count()).where(OutboxMessageORM.status == "PENDING")
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def get_dead_letter_count(self) -> int:
        """Get count of dead letter messages."""
        from sqlalchemy import func

        stmt = select(func.count()).where(OutboxMessageORM.status == "DEAD_LETTER")
        result = await self.session.execute(stmt)
        return result.scalar_one()
