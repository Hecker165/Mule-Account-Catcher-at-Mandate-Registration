"""A6 unit tests: worker lifecycle with fake repositories (no I/O)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.contracts.common import ActionStatus
from app.integrations.razorpay.mock_client import MockTokenRevokeClient
from app.integrations.razorpay.types import RevokeOutcome
from app.workers.config import WorkerConfig
from app.workers.outbox_worker import OutboxWorker

T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
TOKEN_ID = "token_demo123"


class FakeMessage:
    def __init__(self, payload: dict[str, Any], available_at: datetime) -> None:
        self.outbox_message_id = uuid4()
        self.message_type = "token_revoke_requested"
        self.payload = payload
        self.status = "PENDING"
        self.available_at = available_at
        self.last_error_code: str | None = None


class FakeOutboxRepository:
    def __init__(self) -> None:
        self.messages: dict[UUID, FakeMessage] = {}

    def seed(self, message: FakeMessage) -> FakeMessage:
        self.messages[message.outbox_message_id] = message
        return message

    async def claim_batch(self, worker_id: str, limit: int, now: datetime) -> list[FakeMessage]:
        del worker_id
        claimable = sorted(
            (
                message
                for message in self.messages.values()
                if message.status == "PENDING" and message.available_at <= now
            ),
            key=lambda message: message.available_at,
        )[:limit]
        for message in claimable:
            message.status = "PROCESSING"
        return claimable

    async def mark_processed(self, outbox_message_id: UUID, processed_at: datetime) -> None:
        del processed_at
        self.messages[outbox_message_id].status = "PROCESSED"

    async def release_for_retry(
        self, outbox_message_id: UUID, available_at: datetime, safe_error_code: str
    ) -> None:
        message = self.messages[outbox_message_id]
        message.status = "PENDING"
        message.available_at = available_at
        message.last_error_code = safe_error_code

    async def mark_dead_letter(self, outbox_message_id: UUID, safe_error_code: str) -> None:
        message = self.messages[outbox_message_id]
        message.status = "DEAD_LETTER"
        message.last_error_code = safe_error_code


class FakeRequest:
    def __init__(self, request_id: UUID) -> None:
        self.action_request_id = request_id
        self.status = ActionStatus.QUEUED


class FakeAttempt:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class FakeActionRepository:
    def __init__(self) -> None:
        self.requests: dict[UUID, FakeRequest] = {}
        self.attempts: list[FakeAttempt] = []

    async def get_request(self, action_request_id: UUID) -> FakeRequest | None:
        return self.requests.get(action_request_id)

    async def update_request_status(
        self, action_request_id: UUID, status: ActionStatus
    ) -> FakeRequest:
        self.requests[action_request_id].status = status
        return self.requests[action_request_id]

    async def create_attempt(self, attempt: Any) -> Any:
        record = FakeAttempt(
            action_request_id=attempt.action_request_id,
            attempt_number=attempt.attempt_number,
            status=attempt.status,
            provider_status_code=attempt.provider_status_code,
            safe_error_code=attempt.safe_error_code,
        )
        self.attempts.append(record)
        return record


class FakeAttemptReads:
    def __init__(self, actions: FakeActionRepository) -> None:
        self.actions = actions

    async def count_attempts(self, action_request_id: UUID) -> int:
        return sum(
            1 for attempt in self.actions.attempts if attempt.action_request_id == action_request_id
        )

    async def latest_attempt_number(self, action_request_id: UUID) -> int | None:
        numbers = [
            attempt.attempt_number
            for attempt in self.actions.attempts
            if attempt.action_request_id == action_request_id
        ]
        return max(numbers) if numbers else None


class FakeAuditRepository:
    def __init__(self, fail_once: bool = False, fail_always: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail_once = fail_once
        self.fail_always = fail_always

    async def append(self, **kwargs: Any) -> None:
        if self.fail_always:
            raise RuntimeError("audit store exploded")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("audit store exploded")
        self.events.append(kwargs)


class FakeTx:
    """Emulates one transaction: snapshots fake state, restores on error."""

    def __init__(self, fakes: _Fakes) -> None:
        self.fakes = fakes
        self.snapshot: Any = None

    async def __aenter__(self) -> object:
        self.snapshot = (
            {
                key: (msg.status, msg.available_at, msg.last_error_code)
                for key, msg in self.fakes.outbox.messages.items()
            },
            {key: req.status for key, req in self.fakes.actions.requests.items()},
            list(self.fakes.actions.attempts),
            list(self.fakes.audits.events),
        )
        return object()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            outbox_state, request_state, attempts, audits = self.snapshot
            for key, (status, available_at, code) in outbox_state.items():
                self.fakes.outbox.messages[key].status = status
                self.fakes.outbox.messages[key].available_at = available_at
                self.fakes.outbox.messages[key].last_error_code = code
            for key, status in request_state.items():
                self.fakes.actions.requests[key].status = status
            del self.fakes.actions.attempts[len(attempts) :]
            del self.fakes.audits.events[len(audits) :]
        return False


class _Fakes:
    def __init__(self) -> None:
        self.outbox = FakeOutboxRepository()
        self.actions = FakeActionRepository()
        self.audits = FakeAuditRepository()
        self.reads = FakeAttemptReads(self.actions)
        self.mock = MockTokenRevokeClient()
        self.now = [T0]

    def factory(self) -> FakeTx:
        return FakeTx(self)

    def worker(self, **overrides: Any) -> OutboxWorker:
        params: dict[str, Any] = {
            "revoke_client": self.mock,
            "clock": lambda: self.now[0],
            "outbox_repository": lambda session: self.outbox,
            "action_repository": lambda session: self.actions,
            "audit_repository": lambda session: self.audits,
            "attempt_reads": lambda session: self.reads,
            "session_factory": lambda: self.factory(),
        }
        params.update(overrides)
        return OutboxWorker(**params)  # type: ignore[arg-type]

    def seed(self, request_id: UUID | None = None) -> tuple[UUID, FakeMessage]:
        request_id = request_id or uuid4()
        self.actions.requests[request_id] = FakeRequest(request_id)
        message = FakeMessage(
            payload={
                "action_request_id": str(request_id),
                "mandate_event_id": str(uuid4()),
                "token_id": TOKEN_ID,
                "idempotency_key": f"revoke-{request_id}",
                "requested_at": T0.isoformat(),
            },
            available_at=T0,
        )
        return request_id, self.outbox.seed(message)


async def test_success_flow_marks_attempt_request_and_outbox() -> None:
    fakes = _Fakes()
    request_id, _ = fakes.seed()
    assert await fakes.worker().process_pending_once() == 1

    assert len(fakes.mock.recorded_calls) == 1
    assert fakes.actions.requests[request_id].status == ActionStatus.SUCCEEDED
    assert len(fakes.actions.attempts) == 1
    attempt = fakes.actions.attempts[0]
    assert (attempt.attempt_number, attempt.status) == (1, ActionStatus.SUCCEEDED)
    assert attempt.provider_status_code == 200
    assert len(fakes.audits.events) == 1
    assert fakes.audits.events[0]["event_type"] == "action_attempt.succeeded"


async def test_transient_failure_releases_with_backoff() -> None:
    fakes = _Fakes()
    fakes.mock.schedule_failures(1, RevokeOutcome.RETRYABLE, 503)
    request_id, message = fakes.seed()
    assert await fakes.worker().process_pending_once() == 1

    assert fakes.actions.requests[request_id].status == ActionStatus.RETRYING
    assert message.status == "PENDING"
    assert message.available_at == T0 + timedelta(seconds=5)
    assert message.last_error_code == "UPSTREAM_SERVER_ERROR"
    attempt = fakes.actions.attempts[0]
    assert attempt.status == ActionStatus.RETRYING
    assert attempt.provider_status_code == 503


def test_backoff_sequence_is_5_10_20_40_80_160_300_300() -> None:
    config = WorkerConfig()
    assert [config.backoff_for(n) for n in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]


async def test_retry_attempt_uses_same_idempotency_key() -> None:
    fakes = _Fakes()
    fakes.mock.schedule_failures(1, RevokeOutcome.RETRYABLE, 503)
    fakes.seed()
    await fakes.worker().process_pending_once()
    fakes.now[0] += timedelta(seconds=6)
    await fakes.worker().process_pending_once()

    calls = fakes.mock.recorded_calls
    assert len(calls) == 2
    assert calls[0].idempotency_key == calls[1].idempotency_key
    assert calls[0].result_outcome == RevokeOutcome.RETRYABLE
    assert calls[1].result_outcome == RevokeOutcome.SUCCEEDED


async def test_permanent_failure_dead_letters_without_retry() -> None:
    fakes = _Fakes()
    fakes.mock.schedule_failures(5, RevokeOutcome.PERMANENT, 400)
    request_id, message = fakes.seed()
    assert await fakes.worker().process_pending_once() == 1

    assert fakes.actions.requests[request_id].status == ActionStatus.FAILED
    assert message.status == "DEAD_LETTER"
    assert len(fakes.mock.recorded_calls) == 1
    assert await fakes.worker().process_pending_once() == 0
    assert len(fakes.mock.recorded_calls) == 1


async def test_max_attempts_ends_in_failed_request_and_dead_letter() -> None:
    fakes = _Fakes()
    fakes.mock.schedule_failures(10, RevokeOutcome.RETRYABLE, 503)
    config = WorkerConfig(max_attempts=2)
    request_id, message = fakes.seed()

    await fakes.worker(config=config).process_pending_once()
    assert fakes.actions.requests[request_id].status == ActionStatus.RETRYING
    fakes.now[0] += timedelta(seconds=6)
    await fakes.worker(config=config).process_pending_once()

    assert fakes.actions.requests[request_id].status == ActionStatus.FAILED
    assert message.status == "DEAD_LETTER"
    assert len(fakes.mock.recorded_calls) == 2
    assert [attempt.attempt_number for attempt in fakes.actions.attempts] == [1, 2]


async def test_attempt_number_increments_per_retry() -> None:
    fakes = _Fakes()
    fakes.mock.schedule_failures(1, RevokeOutcome.RETRYABLE, 503)
    fakes.seed()
    await fakes.worker().process_pending_once()
    fakes.now[0] += timedelta(seconds=6)
    await fakes.worker().process_pending_once()
    assert [attempt.attempt_number for attempt in fakes.actions.attempts] == [1, 2]


async def test_request_status_transitions_queued_to_in_progress_to_terminal() -> None:
    fakes = _Fakes()
    request_id, _ = fakes.seed()
    assert fakes.actions.requests[request_id].status == ActionStatus.QUEUED
    await fakes.worker().process_pending_once()
    assert fakes.actions.requests[request_id].status == ActionStatus.SUCCEEDED


async def test_one_audit_event_per_attempt() -> None:
    fakes = _Fakes()
    fakes.mock.schedule_failures(1, RevokeOutcome.RETRYABLE, 503)
    fakes.seed()
    await fakes.worker().process_pending_once()
    fakes.now[0] += timedelta(seconds=6)
    await fakes.worker().process_pending_once()
    assert [event["event_type"] for event in fakes.audits.events] == [
        "action_attempt.retrying",
        "action_attempt.succeeded",
    ]


async def test_audit_payload_contains_fixed_keys_only() -> None:
    fakes = _Fakes()
    fakes.mock.schedule_failures(1, RevokeOutcome.RETRYABLE, 503)
    fakes.seed()
    await fakes.worker().process_pending_once()

    payload = fakes.audits.events[0]["redacted_payload"]
    assert set(payload) == {
        "attempt_number",
        "attempt_status",
        "safe_error_code",
        "provider_status_code",
        "retry_available_at",
    }
    assert payload["retry_available_at"] == "2026-01-15T10:00:05Z"

    fakes.now[0] += timedelta(seconds=6)
    await fakes.worker().process_pending_once()
    success_payload = fakes.audits.events[1]["redacted_payload"]
    assert set(success_payload) == {
        "attempt_number",
        "attempt_status",
        "safe_error_code",
        "provider_status_code",
    }
    assert "hmac-sha256" not in str(success_payload)


async def test_unknown_message_type_is_dead_lettered_without_call() -> None:
    fakes = _Fakes()
    _, message = fakes.seed()
    message.message_type = "narrative_requested"
    assert await fakes.worker().process_pending_once() == 1
    assert message.status == "DEAD_LETTER"
    assert message.last_error_code == "UNKNOWN_MESSAGE_TYPE"
    assert len(fakes.mock.recorded_calls) == 0
    assert len(fakes.actions.attempts) == 0


async def test_missing_action_request_is_dead_lettered_without_call() -> None:
    fakes = _Fakes()
    orphan = FakeMessage(
        payload={
            "action_request_id": str(uuid4()),
            "mandate_event_id": str(uuid4()),
            "token_id": TOKEN_ID,
            "idempotency_key": "revoke-orphan-key-0001",
            "requested_at": T0.isoformat(),
        },
        available_at=T0,
    )
    fakes.outbox.seed(orphan)
    assert await fakes.worker().process_pending_once() == 1
    assert orphan.status == "DEAD_LETTER"
    assert orphan.last_error_code == "ACTION_REQUEST_MISSING"
    assert len(fakes.mock.recorded_calls) == 0


async def test_invalid_payload_is_dead_lettered_without_call() -> None:
    fakes = _Fakes()
    bad = FakeMessage(payload={"token_id": TOKEN_ID}, available_at=T0)
    fakes.outbox.seed(bad)
    assert await fakes.worker().process_pending_once() == 1
    assert bad.status == "DEAD_LETTER"
    assert bad.last_error_code == "INVALID_PAYLOAD"
    assert len(fakes.mock.recorded_calls) == 0


async def test_processed_message_is_not_processed_again() -> None:
    fakes = _Fakes()
    fakes.seed()
    assert await fakes.worker().process_pending_once() == 1
    assert await fakes.worker().process_pending_once() == 0
    assert len(fakes.mock.recorded_calls) == 1
    assert len(fakes.actions.attempts) == 1


async def test_exception_during_processing_leaves_message_claimable() -> None:
    fakes = _Fakes()
    fakes.audits.fail_always = True
    request_id, message = fakes.seed()
    assert await fakes.worker().process_pending_once() == 0
    assert message.status == "PENDING"
    assert len(fakes.actions.attempts) == 0
    assert fakes.actions.requests[request_id].status == ActionStatus.QUEUED

    fakes.audits.fail_always = False
    assert await fakes.worker().process_pending_once() == 1
    assert fakes.actions.requests[request_id].status == ActionStatus.SUCCEEDED


async def test_run_forever_stops_promptly_on_stop_event() -> None:
    fakes = _Fakes()
    stop_event = asyncio.Event()
    stop_event.set()
    await asyncio.wait_for(
        fakes.worker(config=WorkerConfig(poll_interval_seconds=60)).run_forever(stop_event),
        timeout=5,
    )


async def test_demo_failure_and_recovery_scenario() -> None:
    fakes = _Fakes()
    request_id, _ = fakes.seed()
    fakes.mock.schedule_failures(1, RevokeOutcome.RETRYABLE, 503)

    await fakes.worker().process_pending_once()
    assert fakes.actions.requests[request_id].status == ActionStatus.RETRYING
    assert fakes.actions.attempts[0].provider_status_code == 503

    fakes.now[0] += timedelta(seconds=6)
    await fakes.worker().process_pending_once()
    assert fakes.actions.requests[request_id].status == ActionStatus.SUCCEEDED

    assert await fakes.worker().process_pending_once() == 0
    calls = fakes.mock.recorded_calls
    assert len(calls) == 2
    assert calls[0].idempotency_key == calls[1].idempotency_key
