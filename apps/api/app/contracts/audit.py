"""Audit event contract.

A0 owns the shape. A1 determines how hashes are generated and persisted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.contracts.common import (
    AuditActorType,
    ContractModel,
    Sha256Hash,
    ensure_utc,
)


class AuditEvent(ContractModel):
    """Immutable, hash-chained audit event.

    The first event in an aggregate has ``previous_event_hash=None``;
    subsequent events require it. A1 determines hash generation.
    """

    schema_version: str = "1.0"
    audit_event_id: Annotated[UUID, Field(default_factory=uuid4)]
    aggregate_type: Literal[
        "risk_session",
        "mandate_event",
        "risk_assessment",
        "action_request",
    ]
    aggregate_id: UUID
    sequence_number: Annotated[int, Field(ge=1)]
    event_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.]{2,100}$")]
    actor_type: AuditActorType
    actor_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    occurred_at: datetime
    redacted_payload: dict[str, object]
    previous_event_hash: Sha256Hash | None = None
    event_hash: Sha256Hash

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _validate_occurred_at_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    @model_validator(mode="after")
    def _chain_consistency(self) -> AuditEvent:
        if self.sequence_number == 1 and self.previous_event_hash is not None:
            raise ValueError(
                "The first event in an aggregate (sequence_number=1) must have "
                "previous_event_hash=None."
            )
        if self.sequence_number > 1 and self.previous_event_hash is None:
            raise ValueError(
                "Events after the first (sequence_number > 1) must have a previous_event_hash."
            )
        return self

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "schema_version": "1.0",
                "audit_event_id": "a7b8c9d0-e1f2-4a3b-4c5d-6e7f80910213",
                "aggregate_type": "risk_assessment",
                "aggregate_id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f80",
                "sequence_number": 1,
                "event_type": "risk_assessment.created",
                "actor_type": "SYSTEM",
                "actor_id": None,
                "occurred_at": "2026-01-15T10:02:03Z",
                "redacted_payload": {"decision": "BLOCK", "score": 85},
                "previous_event_hash": None,
                "event_hash": "sha256:" + "c" * 64,
            }
        ]
    }
