"""A6 read-only attempt queries over A1's ORM models.

New file in the repositories package; no A1-owned file is modified.
Constructor takes ``AsyncSession``; reads only, never commits, never writes.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import ActionAttempt as ActionAttemptORM


class ActionAttemptReadRepository:
    """Read-only queries used to derive the next attempt number."""

    def __init__(self, db_session: AsyncSession) -> None:
        self.session = db_session

    async def count_attempts(self, action_request_id: UUID) -> int:
        """Count existing attempts for one action request."""
        stmt = select(func.count()).where(ActionAttemptORM.action_request_id == action_request_id)
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def latest_attempt_number(self, action_request_id: UUID) -> int | None:
        """Highest attempt number recorded, or None when there are no attempts."""
        stmt = select(func.max(ActionAttemptORM.attempt_number)).where(
            ActionAttemptORM.action_request_id == action_request_id
        )
        result = await self.session.execute(stmt)
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None
