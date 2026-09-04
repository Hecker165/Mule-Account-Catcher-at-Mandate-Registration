"""Shared types, enums and base model for all canonical contracts.

A0 owns this module. No agent may remove or rename an existing enum member
or field. Additions must be backwards-compatible.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Enums ────────────────────────────────────────────────────────────────


class Decision(enum.StrEnum):
    """Outcome of a risk evaluation."""

    ALLOW = "ALLOW"
    CHALLENGE = "CHALLENGE"
    BLOCK = "BLOCK"


class DecisionStage(enum.StrEnum):
    """The phase of the mandate lifecycle at which the decision was made."""

    PRECHECK = "PRECHECK"
    POST_CONFIRMATION = "POST_CONFIRMATION"
    REJECTION_AUDIT = "REJECTION_AUDIT"


class MandateEventType(enum.StrEnum):
    """Razorpay webhook event types for recurring mandates."""

    TOKEN_CONFIRMED = "token.confirmed"
    TOKEN_REJECTED = "token.rejected"
    TOKEN_CANCELLED = "token.cancelled"


class ActionType(enum.StrEnum):
    """Types of downstream actions the system can request."""

    TOKEN_REVOKE = "TOKEN_REVOKE"


class ActionStatus(enum.StrEnum):
    """Lifecycle status of an action request or attempt."""

    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    NOT_REQUIRED = "NOT_REQUIRED"


class Availability(enum.StrEnum):
    """Whether a feature source was available at evaluation time."""

    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DEMO_SIMULATED = "DEMO_SIMULATED"


class AuditActorType(enum.StrEnum):
    """Who or what created an audit event."""

    SYSTEM = "SYSTEM"
    WEBHOOK = "WEBHOOK"
    WORKER = "WORKER"
    OPERATOR = "OPERATOR"
    DEMO = "DEMO"


class RiskSessionStatus(enum.StrEnum):
    """Lifecycle status of a checkout risk session."""

    CREATED = "CREATED"
    READY = "READY"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


# ── UTC datetime enforcement ─────────────────────────────────────────────


def ensure_utc(value: datetime) -> datetime:
    """Validate and normalise a datetime to UTC.

    Rejects naive datetimes (no tzinfo). Converts other timezones to UTC.
    """
    if value.tzinfo is None:
        raise ValueError("Naive datetimes are not allowed; provide a timezone-aware value.")
    return value.astimezone(UTC)


# ── Constrained hash string type ─────────────────────────────────────────

HashValue = Annotated[
    str,
    Field(
        pattern=r"^hmac-sha256:[a-f0-9]{64}$",
        description="HMAC-SHA256 pseudonymised value: 'hmac-sha256:' followed by 64 hex chars.",
    ),
]

Sha256Hash = Annotated[
    str,
    Field(
        pattern=r"^sha256:[a-f0-9]{64}$",
        description="SHA-256 hash prefixed with 'sha256:'.",
    ),
]


# ── Base contract model ──────────────────────────────────────────────────


class ContractModel(BaseModel):
    """Base model for all canonical API and event contracts.

    Enforces:
    - ``extra="forbid"`` to reject undeclared fields.
    - ``populate_by_name=True`` for flexible construction.
    - ``str_strip_whitespace=True`` to normalise string inputs.
    - JSON-mode serialisation of UUIDs and datetimes.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        json_encoders={
            UUID: lambda v: str(v).lower(),
            datetime: lambda v: v.isoformat(),
        },
    )


# ── Feature source ───────────────────────────────────────────────────────


class FeatureSource(ContractModel):
    """Describes the provenance and availability of a single feature value.

    When availability is ``AVAILABLE`` or ``DEMO_SIMULATED``, ``captured_at``
    is required. Otherwise it must be ``None``.
    """

    availability: Availability
    source: Annotated[str, Field(min_length=1, max_length=80)]
    captured_at: datetime | None = None

    @field_validator("captured_at", mode="before")
    @classmethod
    def _validate_captured_at_tz(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_utc(v)
        return v

    @model_validator(mode="after")
    def _captured_at_consistency(self) -> FeatureSource:
        needs_ts = self.availability in (Availability.AVAILABLE, Availability.DEMO_SIMULATED)
        if needs_ts and self.captured_at is None:
            raise ValueError(
                f"captured_at is required when availability is {self.availability.value}."
            )
        if not needs_ts and self.captured_at is not None:
            raise ValueError(
                f"captured_at must be None when availability is {self.availability.value}."
            )
        return self

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "availability": "AVAILABLE",
                    "source": "redis-sliding-window",
                    "captured_at": "2026-01-15T10:01:00Z",
                }
            ]
        },
    )
