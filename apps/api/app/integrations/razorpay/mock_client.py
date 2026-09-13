"""Credential-free mock revoke client: recording, dedupe and scheduled failures."""

from __future__ import annotations

from dataclasses import dataclass

from app.integrations.razorpay.client import _validate_idempotency_key, _validate_token_id
from app.integrations.razorpay.types import RevokeOutcome, RevokeResult, result_for_status


@dataclass(frozen=True)
class MockRevokeCall:
    token_id: str
    idempotency_key: str
    result_outcome: RevokeOutcome


@dataclass
class _ScheduledFailure:
    outcome: RevokeOutcome
    status_code: int


class MockTokenRevokeClient:
    """Test/demo stand-in with provider-side idempotency semantics."""

    def __init__(self) -> None:
        self._calls: list[MockRevokeCall] = []
        self._scheduled: list[_ScheduledFailure] = []
        self._succeeded_keys: set[str] = set()

    def schedule_failures(
        self,
        count: int,
        outcome: RevokeOutcome = RevokeOutcome.RETRYABLE,
        status_code: int = 503,
    ) -> None:
        """Make the next ``count`` calls fail; afterwards calls succeed."""
        self._scheduled.extend(_ScheduledFailure(outcome, status_code) for _ in range(count))

    @property
    def recorded_calls(self) -> tuple[MockRevokeCall, ...]:
        return tuple(self._calls)

    async def revoke_token(self, token_id: str, idempotency_key: str) -> RevokeResult:
        """Succeed by default; replay success for a deduplicated key without recording."""
        _validate_token_id(token_id)
        _validate_idempotency_key(idempotency_key)
        if idempotency_key in self._succeeded_keys:
            return RevokeResult(
                outcome=RevokeOutcome.SUCCEEDED,
                provider_status_code=200,
                safe_error_code=None,
                safe_error_message=None,
            )
        if self._scheduled:
            failure = self._scheduled.pop(0)
            result = result_for_status(failure.status_code)
            result = RevokeResult(
                outcome=failure.outcome,
                provider_status_code=failure.status_code,
                safe_error_code=result.safe_error_code,
                safe_error_message=result.safe_error_message,
            )
            self._calls.append(MockRevokeCall(token_id, idempotency_key, result.outcome))
            return result
        self._succeeded_keys.add(idempotency_key)
        self._calls.append(MockRevokeCall(token_id, idempotency_key, RevokeOutcome.SUCCEEDED))
        return RevokeResult(
            outcome=RevokeOutcome.SUCCEEDED,
            provider_status_code=200,
            safe_error_code=None,
            safe_error_message=None,
        )

    async def aclose(self) -> None:
        """No-op: the mock owns no connections."""
