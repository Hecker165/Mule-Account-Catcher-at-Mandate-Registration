"""Outbox worker: exclusive claims, revoke execution, backoff and audit.

Each claimed message is processed in its own transaction: attempt insert +
request status update + outbox transition + audit either all commit or all
roll back. A failed transaction leaves the message claimable; the
provider-side idempotency key keeps a later retry safe.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.action import ActionAttempt
from app.contracts.common import ActionStatus, AuditActorType
from app.integrations.razorpay.client import TokenRevokeClient
from app.integrations.razorpay.types import RevokeOutcome
from app.persistence.outbox import OutboxRepository
from app.persistence.session import transaction
from app.persistence.types import OutboxMessage
from app.repositories import ActionRepository, AuditRepository
from app.repositories.action_attempt_read import ActionAttemptReadRepository
from app.workers.config import WorkerConfig

logger = logging.getLogger(__name__)

_REVOKE_MESSAGE_TYPE = "token_revoke_requested"
_REQUIRED_PAYLOAD_KEYS = (
    "action_request_id",
    "mandate_event_id",
    "token_id",
    "idempotency_key",
    "requested_at",
)


class OutboxWorker:
    """Consumes ``token_revoke_requested`` outbox messages in the background."""

    def __init__(
        self,
        revoke_client: TokenRevokeClient,
        config: WorkerConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        outbox_repository: Callable[[AsyncSession], OutboxRepository] = OutboxRepository,
        action_repository: Callable[[AsyncSession], ActionRepository] = ActionRepository,
        audit_repository: Callable[[AsyncSession], AuditRepository] = AuditRepository,
        attempt_reads: Callable[[AsyncSession], ActionAttemptReadRepository] = (
            ActionAttemptReadRepository
        ),
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
    ) -> None:
        self._revoke_client = revoke_client
        self._config = config or WorkerConfig()
        self._clock = clock
        self._outbox = outbox_repository
        self._actions = action_repository
        self._audits = audit_repository
        self._attempt_reads = attempt_reads
        self._session_factory = session_factory or transaction

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Poll until ``stop_event`` is set; never let an error kill the loop."""
        while not stop_event.is_set():
            try:
                await self.process_pending_once()
            except Exception:
                logger.exception("outbox worker cycle failed worker_id=%s", self._config.worker_id)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._config.poll_interval_seconds
                )
            except TimeoutError:
                continue

    async def process_pending_once(self, limit: int | None = None) -> int:
        """Claim and process up to ``limit`` messages; return the processed count."""
        batch = limit if limit is not None else self._config.batch_size
        now = self._clock()
        processed = 0
        for _ in range(batch):
            try:
                async with self._session_factory() as session:
                    claimed = await self._outbox(session).claim_batch(
                        self._config.worker_id, 1, now
                    )
                    if not claimed:
                        break
                    await self._process_message(session, claimed[0], now)
                    processed += 1
            except Exception:
                logger.exception(
                    "outbox message processing failed worker_id=%s", self._config.worker_id
                )
                continue
        return processed

    async def _process_message(
        self, session: AsyncSession, message: OutboxMessage, now: datetime
    ) -> None:
        outbox = self._outbox(session)
        actions = self._actions(session)
        audits = self._audits(session)
        reads = self._attempt_reads(session)

        if message.message_type != _REVOKE_MESSAGE_TYPE:
            await outbox.mark_dead_letter(message.outbox_message_id, "UNKNOWN_MESSAGE_TYPE")
            await self._audit(
                audits,
                aggregate_type="outbox_message",
                aggregate_id=message.outbox_message_id,
                event_type="outbox_message.dead_lettered",
                attempt_number=0,
                attempt_status="FAILED",
                safe_error_code="UNKNOWN_MESSAGE_TYPE",
                provider_status_code=None,
                retry_available_at=None,
                now=now,
            )
            return

        try:
            request_id, payload = _parse_payload(message.payload)
        except ValueError:
            guessed_id = _best_effort_request_id(message.payload)
            await outbox.mark_dead_letter(message.outbox_message_id, "INVALID_PAYLOAD")
            aggregate_type: str
            aggregate_id: UUID
            if guessed_id is not None:
                aggregate_type, aggregate_id = "action_request", guessed_id
            else:
                aggregate_type, aggregate_id = "outbox_message", message.outbox_message_id
            await self._audit(
                audits,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type="outbox_message.dead_lettered",
                attempt_number=0,
                attempt_status="FAILED",
                safe_error_code="INVALID_PAYLOAD",
                provider_status_code=None,
                retry_available_at=None,
                now=now,
            )
            return

        request = await actions.get_request(request_id)
        if request is None:
            await outbox.mark_dead_letter(message.outbox_message_id, "ACTION_REQUEST_MISSING")
            await self._audit(
                audits,
                aggregate_type="action_request",
                aggregate_id=request_id,
                event_type="outbox_message.dead_lettered",
                attempt_number=0,
                attempt_status="FAILED",
                safe_error_code="ACTION_REQUEST_MISSING",
                provider_status_code=None,
                retry_available_at=None,
                now=now,
            )
            return

        await actions.update_request_status(request_id, ActionStatus.IN_PROGRESS)
        attempt_number = await reads.count_attempts(request_id) + 1
        token_id, idempotency_key = payload["token_id"], payload["idempotency_key"]

        if attempt_number > self._config.max_attempts:
            await actions.create_attempt(
                _attempt(
                    request_id,
                    attempt_number,
                    ActionStatus.FAILED,
                    None,
                    "ATTEMPTS_EXHAUSTED",
                    now,
                    now,
                )
            )
            await actions.update_request_status(request_id, ActionStatus.FAILED)
            await outbox.mark_dead_letter(message.outbox_message_id, "ATTEMPTS_EXHAUSTED")
            await self._audit(
                audits,
                aggregate_type="action_request",
                aggregate_id=request_id,
                event_type="action_attempt.failed",
                attempt_number=attempt_number,
                attempt_status="FAILED",
                safe_error_code="ATTEMPTS_EXHAUSTED",
                provider_status_code=None,
                retry_available_at=None,
                now=now,
            )
            return

        result = await self._revoke_client.revoke_token(token_id, idempotency_key)
        logger.info(
            "revoke attempt worker_id=%s action_request_id=%s attempt=%s outcome=%s",
            self._config.worker_id,
            request_id,
            attempt_number,
            result.outcome.value,
        )

        if result.outcome == RevokeOutcome.SUCCEEDED:
            await actions.create_attempt(
                _attempt(
                    request_id,
                    attempt_number,
                    ActionStatus.SUCCEEDED,
                    result.provider_status_code,
                    None,
                    now,
                    now,
                )
            )
            await actions.update_request_status(request_id, ActionStatus.SUCCEEDED)
            await outbox.mark_processed(message.outbox_message_id, now)
            await self._audit(
                audits,
                aggregate_type="action_request",
                aggregate_id=request_id,
                event_type="action_attempt.succeeded",
                attempt_number=attempt_number,
                attempt_status="SUCCEEDED",
                safe_error_code=None,
                provider_status_code=result.provider_status_code,
                retry_available_at=None,
                now=now,
            )
            return

        if result.outcome == RevokeOutcome.PERMANENT or attempt_number >= self._config.max_attempts:
            await actions.create_attempt(
                _attempt(
                    request_id,
                    attempt_number,
                    ActionStatus.FAILED,
                    result.provider_status_code,
                    result.safe_error_code,
                    now,
                    now,
                )
            )
            await actions.update_request_status(request_id, ActionStatus.FAILED)
            await outbox.mark_dead_letter(
                message.outbox_message_id, result.safe_error_code or "UPSTREAM_REJECTED"
            )
            await self._audit(
                audits,
                aggregate_type="action_request",
                aggregate_id=request_id,
                event_type="action_attempt.failed",
                attempt_number=attempt_number,
                attempt_status="FAILED",
                safe_error_code=result.safe_error_code,
                provider_status_code=result.provider_status_code,
                retry_available_at=None,
                now=now,
            )
            return

        retry_at = now + timedelta(seconds=self._config.backoff_for(attempt_number))
        await actions.create_attempt(
            _attempt(
                request_id,
                attempt_number,
                ActionStatus.RETRYING,
                result.provider_status_code,
                result.safe_error_code,
                now,
                None,
            )
        )
        await actions.update_request_status(request_id, ActionStatus.RETRYING)
        await outbox.release_for_retry(
            message.outbox_message_id, retry_at, result.safe_error_code or "UPSTREAM_TRANSIENT"
        )
        await self._audit(
            audits,
            aggregate_type="action_request",
            aggregate_id=request_id,
            event_type="action_attempt.retrying",
            attempt_number=attempt_number,
            attempt_status="RETRYING",
            safe_error_code=result.safe_error_code,
            provider_status_code=result.provider_status_code,
            retry_available_at=retry_at,
            now=now,
        )

    async def _audit(
        self,
        audits: AuditRepository,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        attempt_number: int,
        attempt_status: str,
        safe_error_code: str | None,
        provider_status_code: int | None,
        retry_available_at: datetime | None,
        now: datetime,
    ) -> None:
        payload: dict[str, Any] = {
            "attempt_number": attempt_number,
            "attempt_status": attempt_status,
            "safe_error_code": safe_error_code,
            "provider_status_code": provider_status_code,
        }
        if retry_available_at is not None:
            payload["retry_available_at"] = retry_available_at.isoformat().replace("+00:00", "Z")
        await audits.append(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            actor_type=AuditActorType.WORKER,
            actor_id=self._config.worker_id,
            occurred_at=now,
            redacted_payload=payload,
        )


def _attempt(
    request_id: UUID,
    attempt_number: int,
    status: ActionStatus,
    provider_status_code: int | None,
    safe_error_code: str | None,
    attempted_at: datetime,
    completed_at: datetime | None,
) -> ActionAttempt:
    messages = {
        "UPSTREAM_TRANSIENT": "Razorpay asked to retry later.",
        "UPSTREAM_SERVER_ERROR": "Razorpay server error; will retry.",
        "UPSTREAM_TIMEOUT": "Razorpay call timed out; will retry.",
        "UPSTREAM_UNREACHABLE": "Could not reach Razorpay; will retry.",
        "UPSTREAM_INVALID_RESPONSE": "Razorpay returned an unexpected response shape.",
        "UPSTREAM_REJECTED": "Razorpay rejected the revoke request.",
        "ATTEMPTS_EXHAUSTED": "Revoke attempts exhausted.",
    }
    message = messages.get(safe_error_code or "")
    return ActionAttempt(
        schema_version="1.0",
        action_attempt_id=uuid4(),
        action_request_id=request_id,
        attempt_number=attempt_number,
        status=status,
        provider_status_code=provider_status_code,
        safe_error_code=safe_error_code,
        safe_error_message=message,
        attempted_at=attempted_at,
        completed_at=completed_at,
    )


def _parse_payload(payload: Any) -> tuple[UUID, dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("invalid payload")
    values: dict[str, str] = {}
    for key in _REQUIRED_PAYLOAD_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError("invalid payload")
        values[key] = value
    try:
        request_id = UUID(values["action_request_id"])
        UUID(values["mandate_event_id"])
    except ValueError:
        raise ValueError("invalid payload") from None
    return request_id, values


def _best_effort_request_id(payload: Any) -> UUID | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("action_request_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None
