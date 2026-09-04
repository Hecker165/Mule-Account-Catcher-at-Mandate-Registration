"""Risk assessment repository implementation.

Provides persistence for risk assessments with ordered rule evaluations.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.common import DecisionStage
from app.contracts.risk_assessment import RiskAssessment
from app.persistence.models import RiskAssessment as RiskAssessmentORM
from app.persistence.models import RuleEvaluation as RuleEvaluationORM
from app.persistence.types import (
    risk_assessment_from_row,
    risk_assessment_to_row,
    rule_evaluation_to_row,
)


class RiskAssessmentRepository:
    """Repository for risk assessment persistence."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, assessment: RiskAssessment) -> RiskAssessment:
        """Create a new risk assessment with its rule evaluations."""
        # Insert assessment
        row_data = risk_assessment_to_row(assessment)
        orm_obj = RiskAssessmentORM(**row_data)
        self.session.add(orm_obj)
        await self.session.flush()

        # Insert rule evaluations with position
        for position, evaluation in enumerate(assessment.rule_evaluations):
            eval_row = rule_evaluation_to_row(evaluation, assessment.assessment_id, position)
            eval_orm = RuleEvaluationORM(**eval_row)
            self.session.add(eval_orm)

        await self.session.flush()
        return assessment

    async def get(self, assessment_id: UUID) -> RiskAssessment | None:
        """Get a risk assessment by ID with its rule evaluations."""
        stmt = select(RiskAssessmentORM).where(RiskAssessmentORM.assessment_id == assessment_id)
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None

        # Fetch rule evaluations
        eval_stmt = (
            select(RuleEvaluationORM)
            .where(RuleEvaluationORM.assessment_id == assessment_id)
            .order_by(RuleEvaluationORM.position)
        )
        eval_result = await self.session.execute(eval_stmt)
        eval_orms = eval_result.scalars().all()

        return risk_assessment_from_row(orm_obj, eval_orms)

    async def get_precheck_for_session(self, risk_session_id: UUID) -> RiskAssessment | None:
        """Get the precheck assessment for a risk session."""
        stmt = select(RiskAssessmentORM).where(
            RiskAssessmentORM.risk_session_id == risk_session_id,
            RiskAssessmentORM.stage == DecisionStage.PRECHECK.value,
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None

        eval_stmt = (
            select(RuleEvaluationORM)
            .where(RuleEvaluationORM.assessment_id == orm_obj.assessment_id)
            .order_by(RuleEvaluationORM.position)
        )
        eval_result = await self.session.execute(eval_stmt)
        eval_orms = eval_result.scalars().all()

        return risk_assessment_from_row(orm_obj, eval_orms)

    async def get_for_event(
        self, mandate_event_id: UUID, stage: DecisionStage, engine_version: str
    ) -> RiskAssessment | None:
        """Get assessment for a mandate event, stage, and engine version."""
        stmt = select(RiskAssessmentORM).where(
            RiskAssessmentORM.mandate_event_id == mandate_event_id,
            RiskAssessmentORM.stage == stage.value,
            RiskAssessmentORM.engine_version == engine_version,
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None

        eval_stmt = (
            select(RuleEvaluationORM)
            .where(RuleEvaluationORM.assessment_id == orm_obj.assessment_id)
            .order_by(RuleEvaluationORM.position)
        )
        eval_result = await self.session.execute(eval_stmt)
        eval_orms = eval_result.scalars().all()

        return risk_assessment_from_row(orm_obj, eval_orms)
