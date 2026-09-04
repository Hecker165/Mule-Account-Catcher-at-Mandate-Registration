"""Risk session contracts.

A0 owns these models. A2 provides the routes and hashes raw telemetry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.contracts.common import (
    ContractModel,
    HashValue,
    RiskSessionStatus,
    ensure_utc,
)


class MandateIntent(ContractModel):
    """The mandate parameters the customer intends to register."""

    max_amount_paise: Annotated[int | None, Field(ge=0, le=10_000_000)] = None
    frequency: Annotated[str | None, Field(max_length=32)] = None
    expire_at: datetime | None = None

    @field_validator("expire_at", mode="before")
    @classmethod
    def _validate_expire_at_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "max_amount_paise": 500000,
                "frequency": "monthly",
                "expire_at": "2027-01-15T10:00:00Z",
            }
        ]
    }


class RiskSessionCreateRequest(ContractModel):
    """Request body to create a new checkout risk session."""

    merchant_namespace: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")]
    checkout_order_ref: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    customer_reference: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            json_schema_extra={"writeOnly": True},
        ),
    ] = None
    device_fingerprint: Annotated[
        str | None,
        Field(
            min_length=8,
            max_length=512,
            json_schema_extra={"writeOnly": True},
        ),
    ] = None
    flow_started_at: datetime | None = None
    mandate_intent: MandateIntent

    @field_validator("flow_started_at", mode="before")
    @classmethod
    def _validate_flow_started_at_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "merchant_namespace": "demo-merchant-01",
                "checkout_order_ref": "order_demo_001",
                "customer_reference": "cust_demo_ref_sensitive",
                "device_fingerprint": "fp_ab12cd34ef56gh78",
                "flow_started_at": "2026-01-15T10:00:00Z",
                "mandate_intent": {
                    "max_amount_paise": 500000,
                    "frequency": "monthly",
                    "expire_at": "2027-01-15T10:00:00Z",
                },
            }
        ]
    }


class RiskSessionTelemetryUpdateRequest(ContractModel):
    """Partial update for telemetry captured during the checkout flow."""

    device_fingerprint: Annotated[
        str | None,
        Field(min_length=8, max_length=512),
    ] = None
    flow_completed_at: datetime | None = None
    client_user_agent: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    client_ip: Annotated[str | None, Field(max_length=64)] = None
    is_vpn_claimed: bool | None = None

    @field_validator("flow_completed_at", mode="before")
    @classmethod
    def _validate_flow_completed_at_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "device_fingerprint": "fp_ab12cd34ef56gh78",
                "flow_completed_at": "2026-01-15T10:05:00Z",
                "client_user_agent": "Mozilla/5.0 (Linux; Android 14)",
                "client_ip": "192.168.1.100",
                "is_vpn_claimed": False,
            }
        ]
    }


class RiskSession(ContractModel):
    """Pseudonymised risk session — the output contract.

    Never contains raw customer_reference, raw IP, raw device fingerprint or
    raw user-agent. Only HMAC hashes of those values are stored.
    """

    schema_version: str = "1.0"
    risk_session_id: Annotated[UUID, Field(default_factory=uuid4)]
    merchant_namespace: str
    checkout_order_ref: str | None = None
    customer_reference_hash: HashValue | None = None
    ip_hash: HashValue | None = None
    device_fingerprint_hash: HashValue | None = None
    user_agent_hash: HashValue | None = None
    flow_started_at: datetime | None = None
    flow_completed_at: datetime | None = None
    mandate_intent: MandateIntent
    status: RiskSessionStatus
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "flow_started_at",
        "flow_completed_at",
        "created_at",
        "updated_at",
        mode="before",
    )
    @classmethod
    def _validate_dt_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    @model_validator(mode="after")
    def _flow_time_order(self) -> RiskSession:
        if (
            self.flow_started_at is not None
            and self.flow_completed_at is not None
            and self.flow_completed_at < self.flow_started_at
        ):
            raise ValueError("flow_completed_at must not be before flow_started_at.")
        return self

    @model_validator(mode="after")
    def _expiry_after_start(self) -> RiskSession:
        if (
            self.flow_started_at is not None
            and self.mandate_intent.expire_at is not None
            and self.mandate_intent.expire_at <= self.flow_started_at
        ):
            raise ValueError("mandate_intent.expire_at must be after flow_started_at.")
        return self

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "schema_version": "1.0",
                "risk_session_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
                "merchant_namespace": "demo-merchant-01",
                "checkout_order_ref": "order_demo_001",
                "customer_reference_hash": "hmac-sha256:" + "a" * 64,
                "ip_hash": None,
                "device_fingerprint_hash": None,
                "user_agent_hash": None,
                "flow_started_at": "2026-01-15T10:00:00Z",
                "flow_completed_at": "2026-01-15T10:05:00Z",
                "mandate_intent": {
                    "max_amount_paise": 500000,
                    "frequency": "monthly",
                    "expire_at": "2027-01-15T10:00:00Z",
                },
                "status": "CREATED",
                "created_at": "2026-01-15T10:00:00Z",
                "updated_at": "2026-01-15T10:00:00Z",
            }
        ]
    }
