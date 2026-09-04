"""Action request and attempt contracts.

A0 owns the shape. A6 owns action execution and retry policy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from app.contracts.common import (
    ActionStatus,
    ActionType,
    ContractModel,
    ensure_utc,
)


class ActionRequest(ContractModel):
    """A queued request for a downstream action (e.g. token revocation)."""

    schema_version: str = "1.0"
    action_request_id: Annotated[UUID, Field(default_factory=uuid4)]
    assessment_id: UUID
    mandate_event_id: UUID
    action_type: ActionType
    token_id: Annotated[str, Field(pattern=r"^token_[A-Za-z0-9]+$")]
    idempotency_key: Annotated[str, Field(pattern=r"^[a-z0-9_-]{16,128}$")]
    status: ActionStatus
    requested_at: datetime

    @field_validator("requested_at", mode="before")
    @classmethod
    def _validate_requested_at_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "schema_version": "1.0",
                "action_request_id": "e5f6a7b8-c9d0-4e1f-2a3b-4c5d6e7f8091",
                "assessment_id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f80",
                "mandate_event_id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
                "action_type": "TOKEN_REVOKE",
                "token_id": "token_demo123",
                "idempotency_key": "revoke-token_demo123-d4e5f6a7",
                "status": "QUEUED",
                "requested_at": "2026-01-15T10:02:04Z",
            }
        ]
    }


class ActionAttempt(ContractModel):
    """A single attempt to execute a queued action.

    ``safe_error_message`` must never be a raw upstream body.
    """

    schema_version: str = "1.0"
    action_attempt_id: Annotated[UUID, Field(default_factory=uuid4)]
    action_request_id: UUID
    attempt_number: Annotated[int, Field(ge=1, le=100)]
    status: ActionStatus
    provider_status_code: Annotated[int | None, Field(ge=100, le=599)] = None
    safe_error_code: Annotated[str | None, Field(min_length=1, max_length=80)] = None
    safe_error_message: Annotated[str | None, Field(min_length=1, max_length=240)] = None
    attempted_at: datetime
    completed_at: datetime | None = None

    @field_validator("attempted_at", "completed_at", mode="before")
    @classmethod
    def _validate_dt_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "schema_version": "1.0",
                "action_attempt_id": "f6a7b8c9-d0e1-4f2a-3b4c-5d6e7f809102",
                "action_request_id": "e5f6a7b8-c9d0-4e1f-2a3b-4c5d6e7f8091",
                "attempt_number": 1,
                "status": "RETRYING",
                "provider_status_code": 503,
                "safe_error_code": "PROVIDER_UNAVAILABLE",
                "safe_error_message": "Token revoke service temporarily unavailable.",
                "attempted_at": "2026-01-15T10:02:05Z",
                "completed_at": None,
            }
        ]
    }
