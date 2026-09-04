"""Feature snapshot contract.

A0 owns the shape. A4 provides the computation logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.contracts.common import (
    Availability,
    ContractModel,
    FeatureSource,
    ensure_utc,
)


class FeatureSnapshot(ContractModel):
    """Immutable point-in-time feature vector for a mandate event or session.

    All feature calculations must be explicit, typed and named. This model
    carries the shape without computation logic.
    """

    schema_version: str = "1.0"
    feature_snapshot_id: Annotated[UUID, Field(default_factory=uuid4)]
    mandate_event_id: UUID | None = None
    risk_session_id: UUID | None = None
    feature_version: str = "rules-v1"
    calculated_at: datetime

    # ── Velocity features ────────────────────────────────────────────────
    ip_velocity_5m: Annotated[int | None, Field(ge=0)] = None
    ip_velocity_1h: Annotated[int | None, Field(ge=0)] = None
    device_velocity_5m: Annotated[int | None, Field(ge=0)] = None
    device_velocity_1h: Annotated[int | None, Field(ge=0)] = None
    customer_velocity_1h: Annotated[int | None, Field(ge=0)] = None
    vpa_velocity_1h: Annotated[int | None, Field(ge=0)] = None
    shared_demo_merchant_count_1h: Annotated[int | None, Field(ge=0)] = None

    # ── Device / session features ────────────────────────────────────────
    is_new_device_for_customer: bool | None = None
    flow_duration_seconds: Annotated[int | None, Field(ge=0, le=86_400)] = None
    user_agent_bot_suspected: bool | None = None

    # ── Network features ─────────────────────────────────────────────────
    network_vpn_or_proxy: bool | None = None
    network_reputation_score: Annotated[int | None, Field(ge=0, le=100)] = None

    # ── Risk flags ───────────────────────────────────────────────────────
    npci_risk_flag: bool | None = None
    payer_bank_or_compliance_flag: bool | None = None

    # ── Mandate features ─────────────────────────────────────────────────
    max_amount_paise: Annotated[int | None, Field(ge=0)] = None
    mandate_frequency: Annotated[str | None, Field(max_length=32)] = None
    expiry_days_from_registration: Annotated[int | None, Field(ge=0)] = None

    # ── Source provenance ────────────────────────────────────────────────
    sources: dict[str, FeatureSource]
    is_demo_simulation: bool = False

    @field_validator("calculated_at", mode="before")
    @classmethod
    def _validate_calculated_at_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    @model_validator(mode="after")
    def _require_at_least_one_id(self) -> FeatureSnapshot:
        if self.mandate_event_id is None and self.risk_session_id is None:
            raise ValueError("At least one of mandate_event_id or risk_session_id is required.")
        return self

    @model_validator(mode="after")
    def _demo_simulation_consistency(self) -> FeatureSnapshot:
        if self.shared_demo_merchant_count_1h is not None:
            if not self.is_demo_simulation:
                raise ValueError(
                    "is_demo_simulation must be True when "
                    "shared_demo_merchant_count_1h has a value."
                )
            src = self.sources.get("shared_demo_merchant_count_1h")
            if src is None or src.availability != Availability.DEMO_SIMULATED:
                raise ValueError(
                    "sources['shared_demo_merchant_count_1h'] must exist with "
                    "availability DEMO_SIMULATED when shared_demo_merchant_count_1h "
                    "has a value."
                )
        return self

    model_config = ContractModel.model_config.copy()
    model_config["json_schema_extra"] = {
        "examples": [
            {
                "schema_version": "1.0",
                "feature_snapshot_id": "c3d4e5f6-a7b8-4c9d-0e1f-2a3b4c5d6e7f",
                "mandate_event_id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
                "risk_session_id": None,
                "feature_version": "rules-v1",
                "calculated_at": "2026-01-15T10:02:02Z",
                "ip_velocity_5m": 3,
                "ip_velocity_1h": 5,
                "device_velocity_5m": 2,
                "device_velocity_1h": 4,
                "customer_velocity_1h": 1,
                "vpa_velocity_1h": 1,
                "shared_demo_merchant_count_1h": None,
                "is_new_device_for_customer": False,
                "flow_duration_seconds": 45,
                "user_agent_bot_suspected": False,
                "network_vpn_or_proxy": False,
                "network_reputation_score": 20,
                "npci_risk_flag": False,
                "payer_bank_or_compliance_flag": False,
                "max_amount_paise": 500000,
                "mandate_frequency": "monthly",
                "expiry_days_from_registration": 365,
                "sources": {
                    "ip_velocity_5m": {
                        "availability": "AVAILABLE",
                        "source": "redis-sliding-window",
                        "captured_at": "2026-01-15T10:02:01Z",
                    }
                },
                "is_demo_simulation": False,
            }
        ]
    }
