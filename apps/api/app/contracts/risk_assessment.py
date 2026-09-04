"""Risk assessment contract.

A0 owns the shape. A5 owns scoring logic and threshold enforcement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.contracts.common import (
    ContractModel,
    Decision,
    DecisionStage,
    ensure_utc,
)


class RuleEvaluation(ContractModel):
    """Result of evaluating a single rule against a feature snapshot."""

    rule_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
    triggered: bool
    points: Annotated[int, Field(ge=0, le=100)]
    reason_code: Annotated[str | None, Field(min_length=1, max_length=80)] = None
    reason_text: Annotated[str | None, Field(min_length=1, max_length=240)] = None

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "rule_id": "ip_velocity_5m_high",
                "triggered": True,
                "points": 25,
                "reason_code": "IP_VELOCITY_5M",
                "reason_text": "IP address seen in 15 registrations in the last 5 minutes.",
            }
        ]
    }


class RiskAssessment(ContractModel):
    """Immutable risk assessment produced by the rule engine."""

    schema_version: str = "1.0"
    assessment_id: Annotated[UUID, Field(default_factory=uuid4)]
    risk_session_id: UUID | None = None
    mandate_event_id: UUID | None = None
    feature_snapshot_id: UUID
    stage: DecisionStage
    score: Annotated[int, Field(ge=0, le=100)]
    decision: Decision
    engine_version: str = "rules-v1"
    rule_evaluations: Annotated[list[RuleEvaluation], Field(min_length=1)]
    evaluation_latency_ms: Annotated[int, Field(ge=0, le=60_000)]
    assessed_at: datetime

    @field_validator("assessed_at", mode="before")
    @classmethod
    def _validate_assessed_at_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    @model_validator(mode="after")
    def _stage_id_requirements(self) -> RiskAssessment:
        if self.stage == DecisionStage.PRECHECK and self.risk_session_id is None:
            raise ValueError("risk_session_id is required for PRECHECK assessments.")
        if (
            self.stage
            in (
                DecisionStage.POST_CONFIRMATION,
                DecisionStage.REJECTION_AUDIT,
            )
            and self.mandate_event_id is None
        ):
            raise ValueError(f"mandate_event_id is required for {self.stage.value} assessments.")
        return self

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "schema_version": "1.0",
                "assessment_id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f80",
                "risk_session_id": None,
                "mandate_event_id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
                "feature_snapshot_id": "c3d4e5f6-a7b8-4c9d-0e1f-2a3b4c5d6e7f",
                "stage": "POST_CONFIRMATION",
                "score": 85,
                "decision": "BLOCK",
                "engine_version": "rules-v1",
                "rule_evaluations": [
                    {
                        "rule_id": "ip_velocity_5m_high",
                        "triggered": True,
                        "points": 25,
                        "reason_code": "IP_VELOCITY_5M",
                        "reason_text": "IP address seen in 15 registrations in 5 minutes.",
                    }
                ],
                "evaluation_latency_ms": 12,
                "assessed_at": "2026-01-15T10:02:03Z",
            }
        ]
    }
