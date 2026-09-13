"""A6 shared revoke types: outcome classification and safe error mapping."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class RevokeOutcome(str, enum.Enum):  # noqa: UP042 - spec mandates (str, Enum)
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


@dataclass(frozen=True)
class RevokeResult:
    outcome: RevokeOutcome
    provider_status_code: int | None  # 100..599 when an HTTP response existed, else None
    safe_error_code: str | None  # fixed codes below; None on success
    safe_error_message: str | None  # fixed strings; never upstream body content


def classify_status(status_code: int) -> RevokeOutcome:
    """Classify an HTTP status: 2xx success, 408/429/5xx retryable, else permanent."""
    if 200 <= status_code <= 299:
        return RevokeOutcome.SUCCEEDED
    if status_code in (408, 429) or 500 <= status_code <= 599:
        return RevokeOutcome.RETRYABLE
    if 400 <= status_code <= 499:
        return RevokeOutcome.PERMANENT
    return RevokeOutcome.RETRYABLE  # 1xx/3xx and anything unexpected: fail-safe retry


def result_for_status(status_code: int) -> RevokeResult:
    """Build the fixed safe result for an HTTP response with the given status."""
    outcome = classify_status(status_code)
    if outcome == RevokeOutcome.SUCCEEDED:
        return RevokeResult(
            outcome=outcome,
            provider_status_code=status_code,
            safe_error_code=None,
            safe_error_message=None,
        )
    if status_code in (408, 429):
        code, message = "UPSTREAM_TRANSIENT", f"Razorpay asked to retry later (HTTP {status_code})."
    elif 500 <= status_code <= 599:
        code, message = "UPSTREAM_SERVER_ERROR", f"Razorpay server error (HTTP {status_code})."
    else:
        code, message = (
            "UPSTREAM_REJECTED",
            f"Razorpay rejected the revoke request (HTTP {status_code}).",
        )
    return RevokeResult(
        outcome=outcome,
        provider_status_code=status_code,
        safe_error_code=code,
        safe_error_message=message,
    )


def result_for_transport(kind: str) -> RevokeResult:
    """Build the fixed safe result when no HTTP response exists."""
    if kind == "timeout":
        code, message = "UPSTREAM_TIMEOUT", "Razorpay call timed out."
    elif kind == "invalid":
        code, message = (
            "UPSTREAM_INVALID_RESPONSE",
            "Razorpay returned an unexpected response shape.",
        )
    else:
        code, message = "UPSTREAM_UNREACHABLE", "Could not reach Razorpay."
    return RevokeResult(
        outcome=RevokeOutcome.RETRYABLE,
        provider_status_code=None,
        safe_error_code=code,
        safe_error_message=message,
    )
