"""Action request and attempt repository implementation.

Provides persistence for action requests with outbox integration and attempt tracking.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ActionStatus
from app.contracts.action import ActionAttempt, ActionRequest
from app.persistence.models import ActionAttempt as ActionAttemptORM
from app.persistence.models import ActionRequest as ActionRequestORM
from app.persistence.outbox import OutboxRepository
from app.persistence.types import (
    action_attempt_from_row,
    action_attempt_to_row,
    action_request_from_row,
    action_request_to_row,
)


class ActionRepository:
    """Repository for action request and attempt persistence."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.outbox = OutboxRepository(session)

    async def create_request(
        self,
        request: ActionRequest,
        outbox_payload: dict[str, Any] | None = None,
    ) -> ActionRequest:
        """Create an action request, optionally enqueueing an outbox message atomically."""
        row_data = action_request_to_row(request)
        orm_obj = ActionRequestORM(**row_data)
        self.session.add(orm_obj)
        await self.session.flush()

        if outbox_payload is not None:
            await self.outbox.enqueue(
                aggregate_type="action_request",
                aggregate_id=request.action_request_id,
                message_type="token_revoke_requested",
                payload=outbox_payload,
                idempotency_key=request.idempotency_key,
                available_at=request.requested_at,
            )

        return action_request_from_row(orm_obj)

    async def get_request(self, action_request_id: UUID) -> ActionRequest | None:
        """Get an action request by ID."""
        stmt = select(ActionRequestORM).where(
            ActionRequestORM.action_request_id == action_request_id
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None
        return action_request_from_row(orm_obj)

    async def create_attempt(self, attempt: ActionAttempt) -> ActionAttempt:
        """Create an action attempt record."""
        row_data = action_attempt_to_row(attempt)
        orm_obj = ActionAttemptORM(**row_data)
        self.session.add(orm_obj)
        await self.session.flush()
        return action_attempt_from_row(orm_obj)

    async def update_request_status(
        self, action_request_id: UUID, status: ActionStatus
    ) -> ActionRequest:
        """Update the status of an action request."""
        stmt = (
            update(ActionRequestORM)
            .where(ActionRequestORM.action_request_id == action_request_id)
            .values(status=status.value if isinstance(status, ActionStatus) else status)
        )
        await self.session.execute(stmt)

        fetch_stmt = select(ActionRequestORM).where(
            ActionRequestORM.action_request_id == action_request_id
        )
        result = await self.session.execute(fetch_stmt)
        orm_obj = result.scalar_one()
        return action_request_from_row(orm_obj)
