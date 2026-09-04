"""Feature snapshot repository implementation."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.feature_snapshot import FeatureSnapshot
from app.persistence.models import FeatureSnapshot as FeatureSnapshotORM
from app.persistence.types import feature_snapshot_from_row, feature_snapshot_to_row


class FeatureSnapshotRepository:
    """Repository for feature snapshot persistence."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, snapshot: FeatureSnapshot) -> FeatureSnapshot:
        """Create a new feature snapshot."""
        row_data = feature_snapshot_to_row(snapshot)
        orm_obj = FeatureSnapshotORM(**row_data)
        self.session.add(orm_obj)
        await self.session.flush()
        return feature_snapshot_from_row(orm_obj)

    async def get(self, feature_snapshot_id: UUID) -> FeatureSnapshot | None:
        """Get a feature snapshot by ID."""
        stmt = select(FeatureSnapshotORM).where(
            FeatureSnapshotORM.feature_snapshot_id == feature_snapshot_id
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None
        return feature_snapshot_from_row(orm_obj)

    async def get_latest_for_event(
        self, mandate_event_id: UUID, feature_version: str
    ) -> FeatureSnapshot | None:
        """Get the latest feature snapshot for a mandate event and version."""
        stmt = select(FeatureSnapshotORM).where(
            FeatureSnapshotORM.mandate_event_id == mandate_event_id,
            FeatureSnapshotORM.feature_version == feature_version,
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None
        return feature_snapshot_from_row(orm_obj)
