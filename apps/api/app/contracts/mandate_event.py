"""Normalised mandate webhook event contract.

A0 owns this model. A3 owns the raw-body parsing and signature verification;
only the normalised event enters the contract layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from app.contracts.common import (
    ContractModel,
    HashValue,
    MandateEventType,
    ensure_utc,
)


class MandateWebhookEvent(ContractModel):
    """Normalised mandate webhook event — no raw payload or raw VPA."""

    schema_version: str = "1.0"
    mandate_event_id: Annotated[UUID, Field(default_factory=uuid4)]
    provider: Literal["razorpay"] = "razorpay"
    provider_event_id: Annotated[str, Field(min_length=1, max_length=128)]
    event_type: MandateEventType
    token_id: Annotated[str, Field(pattern=r"^token_[A-Za-z0-9]+$")]
    risk_session_id: UUID | None = None
    vpa_hash: HashValue | None = None
    vpa_handle: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    recurring_status: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    failure_reason: Annotated[str | None, Field(min_length=1, max_length=500)] = None
    provider_created_at: datetime | None = None
    received_at: datetime
    raw_payload_sha256: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    is_demo_event: bool = False

    @field_validator("vpa_handle", mode="before")
    @classmethod
    def _lowercase_handle(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.lower()
        return v

    @field_validator("provider_created_at", "received_at", mode="before")
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
                "mandate_event_id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
                "provider": "razorpay",
                "provider_event_id": "evt_demo_confirmed_001",
                "event_type": "token.confirmed",
                "token_id": "token_demo123",
                "risk_session_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
                "vpa_hash": "hmac-sha256:" + "a" * 64,
                "vpa_handle": "upi",
                "recurring_status": "confirmed",
                "failure_reason": None,
                "provider_created_at": "2026-01-15T10:02:00Z",
                "received_at": "2026-01-15T10:02:01Z",
                "raw_payload_sha256": "sha256:" + "b" * 64,
                "is_demo_event": True,
            }
        ]
    }
