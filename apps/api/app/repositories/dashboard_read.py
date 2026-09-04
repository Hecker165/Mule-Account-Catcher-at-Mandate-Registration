"""Dashboard read-only repository for redacted assessment projections.

Provides safe read access for dashboard UI without exposing sensitive hashes or payloads.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.common import Decision
from app.persistence.models import (
    ActionRequest as ActionRequestORM,
)
from app.persistence.models import (
    MandateWebhookEvent as MandateWebhookEventORM,
)
from app.persistence.models import (
    RiskAssessment as RiskAssessmentORM,
)
from app.persistence.models import (
    RuleEvaluation as RuleEvaluationORM,
)


@dataclass(frozen=True)
class DashboardAssessmentRow:
    """Redacted assessment row for dashboard display.

    Never exposes VPA hash, IP hash, device hash, customer hash, or payload JSON.
    """

    assessment_id: UUID
    decision: Decision
    score: int
    stage: str
    assessed_at: datetime
    token_id: str | None
    vpa_handle: str | None
    is_demo_event: bool
    action_status: str | None
    rule_reasons: list[str]


class DashboardReadRepository:
    """Read-only repository for dashboard projections."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_recent_assessments(
        self, limit: int, offset: int, decision: Decision | None = None
    ) -> list[DashboardAssessmentRow]:
        """List recent assessments with redacted fields for dashboard."""
        stmt = (
            select(
                RiskAssessmentORM.assessment_id,
                RiskAssessmentORM.decision,
                RiskAssessmentORM.score,
                RiskAssessmentORM.stage,
                RiskAssessmentORM.assessed_at,
                MandateWebhookEventORM.token_id,
                MandateWebhookEventORM.vpa_handle,
                MandateWebhookEventORM.is_demo_event,
                ActionRequestORM.status,
            )
            .select_from(RiskAssessmentORM)
            .outerjoin(
                MandateWebhookEventORM,
                RiskAssessmentORM.mandate_event_id == MandateWebhookEventORM.mandate_event_id,
            )
            .outerjoin(
                ActionRequestORM,
                RiskAssessmentORM.assessment_id == ActionRequestORM.assessment_id,
            )
            .order_by(RiskAssessmentORM.assessed_at.desc())
            .limit(limit)
            .offset(offset)
        )

        if decision is not None:
            stmt = stmt.where(RiskAssessmentORM.decision == decision.value)

        result = await self.session.execute(stmt)
        rows = result.all()

        assessments = []
        for row in rows:
            # Fetch rule reasons for this assessment
            eval_stmt = (
                select(RuleEvaluationORM.reason_text)
                .where(
                    RuleEvaluationORM.assessment_id == row.assessment_id,
                    RuleEvaluationORM.triggered,
                )
                .order_by(RuleEvaluationORM.position)
            )
            eval_result = await self.session.execute(eval_stmt)
            reasons = [r for (r,) in eval_result.all() if r]

            assessments.append(
                DashboardAssessmentRow(
                    assessment_id=row.assessment_id,
                    decision=Decision(row.decision),
                    score=row.score,
                    stage=row.stage,
                    assessed_at=row.assessed_at,
                    token_id=row.token_id,
                    vpa_handle=row.vpa_handle,
                    is_demo_event=(
                        bool(row.is_demo_event) if row.is_demo_event is not None else False
                    ),
                    action_status=row.status,
                    rule_reasons=reasons,
                )
            )

        return assessments
