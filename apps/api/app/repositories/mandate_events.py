"""Mandate webhook event repository implementation.

Provides idempotent create_or_get for webhook events.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.mandate_event import MandateWebhookEvent
from app.persistence.models import MandateWebhookEvent as MandateWebhookEventORM
from app.persistence.types import mandate_event_from_row, mandate_event_to_row


class MandateEventRepository:
    """Repository for mandate webhook event persistence with idempotent create_or_get."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_or_get(self, event: MandateWebhookEvent) -> tuple[MandateWebhookEvent, bool]:
        """Create a new webhook event or return existing one.

        Uses a SAVEPOINT (nested transaction) so that a unique-conflict race
        only rolls back the conflicting insert — never the caller's enclosing
        transaction. Returns ``(event, created)``; no audit event is added in
        the duplicate branch.
        """
        row_data = mandate_event_to_row(event)
        orm_obj = MandateWebhookEventORM(**row_data)

        try:
            async with self.session.begin_nested():
                self.session.add(orm_obj)
                await self.session.flush()
            return mandate_event_from_row(orm_obj), True
        except IntegrityError:
            # Unique conflict on (provider, provider_event_id) or primary key:
            # query and return the existing row with created=False.
            stmt = select(MandateWebhookEventORM).where(
                MandateWebhookEventORM.provider == event.provider,
                MandateWebhookEventORM.provider_event_id == event.provider_event_id,
            )
            result = await self.session.execute(stmt)
            existing = result.scalar_one()
            return mandate_event_from_row(existing), False

    async def get(self, mandate_event_id: UUID) -> MandateWebhookEvent | None:
        """Get a webhook event by ID."""
        stmt = select(MandateWebhookEventORM).where(
            MandateWebhookEventORM.mandate_event_id == mandate_event_id
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None
        return mandate_event_from_row(orm_obj)

    async def attach_risk_session(
        self, mandate_event_id: UUID, risk_session_id: UUID
    ) -> MandateWebhookEvent:
        """Attach a risk session ID to an existing webhook event."""
        stmt = (
            update(MandateWebhookEventORM)
            .where(MandateWebhookEventORM.mandate_event_id == mandate_event_id)
            .values(risk_session_id=risk_session_id)
        )
        await self.session.execute(stmt)

        # Fetch and return updated event
        fetch_stmt = select(MandateWebhookEventORM).where(
            MandateWebhookEventORM.mandate_event_id == mandate_event_id
        )
        result = await self.session.execute(fetch_stmt)
        orm_obj = result.scalar_one()
        return mandate_event_from_row(orm_obj)
