"""Demo read-only lookups over A1's ORM models.

New file in the repositories package (A9, A6 precedent): it modifies no
A1-owned file. Constructor takes ``AsyncSession``; ``select``-only reads,
never writes, never commits.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.action import ActionAttempt, ActionRequest
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.persistence.models import ActionAttempt as ActionAttemptORM
from app.persistence.models import ActionRequest as ActionRequestORM
from app.persistence.models import FeatureSnapshot as FeatureSnapshotORM
from app.persistence.models import MandateWebhookEvent as MandateWebhookEventORM
from app.persistence.types import (
    action_attempt_from_row,
    action_request_from_row,
    feature_snapshot_from_row,
    mandate_event_from_row,
)


class DemoReadRepository:
    """Read-only action/event/snapshot lookups the demo scenarios need."""

    def __init__(self, db_session: AsyncSession) -> None:
        self.session = db_session

    async def get_action_request_for_mandate_event(
        self, mandate_event_id: UUID
    ) -> ActionRequest | None:
        """Return the revoke request queued for one mandate event, if any."""
        stmt = (
            select(ActionRequestORM)
            .where(ActionRequestORM.mandate_event_id == mandate_event_id)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        return action_request_from_row(orm_obj) if orm_obj is not None else None

    async def count_action_requests_for_mandate_event(self, mandate_event_id: UUID) -> int:
        """Count revoke requests queued for one mandate event."""
        stmt = select(func.count()).where(ActionRequestORM.mandate_event_id == mandate_event_id)
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def count_events_for_provider_event(self, provider: str, provider_event_id: str) -> int:
        """Count stored events for one provider event id (idempotency proof)."""
        stmt = select(func.count()).where(
            MandateWebhookEventORM.provider == provider,
            MandateWebhookEventORM.provider_event_id == provider_event_id,
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def get_event_for_provider_event(
        self, provider: str, provider_event_id: str
    ) -> MandateWebhookEvent | None:
        """Return the stored event for one provider event id, if any."""
        stmt = select(MandateWebhookEventORM).where(
            MandateWebhookEventORM.provider == provider,
            MandateWebhookEventORM.provider_event_id == provider_event_id,
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        return mandate_event_from_row(orm_obj) if orm_obj is not None else None

    async def list_attempts_for_request(self, action_request_id: UUID) -> list[ActionAttempt]:
        """List attempts for one request ordered by attempt number."""
        stmt = (
            select(ActionAttemptORM)
            .where(ActionAttemptORM.action_request_id == action_request_id)
            .order_by(ActionAttemptORM.attempt_number)
        )
        result = await self.session.execute(stmt)
        return [action_attempt_from_row(orm) for orm in result.scalars().all()]

    async def get_latest_snapshot_for_session(
        self, risk_session_id: UUID
    ) -> FeatureSnapshot | None:
        """Return the newest feature snapshot for one risk session, if any."""
        stmt = (
            select(FeatureSnapshotORM)
            .where(FeatureSnapshotORM.risk_session_id == risk_session_id)
            .order_by(FeatureSnapshotORM.calculated_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        return feature_snapshot_from_row(orm_obj) if orm_obj is not None else None
