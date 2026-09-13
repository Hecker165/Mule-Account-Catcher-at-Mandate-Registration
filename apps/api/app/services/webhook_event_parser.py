"""Normalisation of signed Razorpay webhook payloads into MandateWebhookEvent."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pydantic

from app.contracts.common import MandateEventType
from app.contracts.mandate_event import MandateWebhookEvent
from app.services.webhook_pseudonymisation import VpaPseudonymizer

_TOKEN_ID_PATTERN = re.compile(r"^token_[A-Za-z0-9]+$")

_MANDATE_EVENTS = {
    "token.confirmed": MandateEventType.TOKEN_CONFIRMED,
    "token.rejected": MandateEventType.TOKEN_REJECTED,
    "token.cancelled": MandateEventType.TOKEN_CANCELLED,
}

MSG_INVALID_JSON = "webhook body is not valid JSON"
MSG_MISSING_ENTITY = "webhook payload is missing required token entity"
MSG_INVALID_TOKEN_ID = "webhook token id is invalid"


class WebhookParseError(ValueError):
    """Raised with a fixed safe message only; never carries payload fragments."""


@dataclass(frozen=True)
class ParsedClassification:
    kind: str  # "mandate" | "ignored"
    event: MandateWebhookEvent | None
    notes_risk_session_id: UUID | None
    notes_merchant_namespace: str | None
    notes_checkout_order_ref: str | None
    ignore_reason: str | None
    raw_event_type: str | None = None


class RazorpayWebhookParser:
    """Classifies and normalises raw Razorpay webhook bodies."""

    def __init__(
        self,
        vpa_pseudonymizer: VpaPseudonymizer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._vpa = vpa_pseudonymizer
        self._clock = clock
        self._uuid_factory = uuid_factory

    def classify_and_parse(
        self, raw_body: bytes, provider_event_id: str | None, is_demo_event: bool
    ) -> ParsedClassification:
        """Classify the payload and normalise mandate events to the A0 contract."""
        try:
            payload = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            raise WebhookParseError(MSG_INVALID_JSON) from None
        if not isinstance(payload, dict):
            raise WebhookParseError(MSG_INVALID_JSON) from None

        raw_event = payload.get("event")
        event_type = _MANDATE_EVENTS.get(raw_event) if isinstance(raw_event, str) else None
        if isinstance(raw_event, str) and event_type is None:
            return ParsedClassification(
                kind="ignored",
                event=None,
                notes_risk_session_id=None,
                notes_merchant_namespace=None,
                notes_checkout_order_ref=None,
                ignore_reason="unrelated event type",
                raw_event_type=raw_event,
            )

        entity = _dig(payload, "payload", "token", "entity")
        if not isinstance(entity, dict):
            raise WebhookParseError(MSG_MISSING_ENTITY) from None
        if raw_event is None or event_type is None:
            raise WebhookParseError(MSG_MISSING_ENTITY) from None

        method = entity.get("method")
        if method is not None and method != "upi":
            return ParsedClassification(
                kind="ignored",
                event=None,
                notes_risk_session_id=None,
                notes_merchant_namespace=None,
                notes_checkout_order_ref=None,
                ignore_reason="unsupported method",
                raw_event_type=raw_event if isinstance(raw_event, str) else None,
            )

        token_id = entity.get("id")
        if (
            not isinstance(token_id, str)
            or not token_id
            or _TOKEN_ID_PATTERN.match(token_id) is None
        ):
            raise WebhookParseError(MSG_INVALID_TOKEN_ID) from None

        username, handle = _split_vpa(entity.get("vpa"))
        vpa_hash, vpa_handle = self._vpa.hash_vpa(username, handle)

        resolved_provider_event_id = _resolve_provider_event_id(provider_event_id, raw_body)
        received_at = self._clock()
        provider_created_at = _epoch_to_utc(payload.get("created_at"))

        notes_risk_session_id, notes_namespace, notes_order_ref = _extract_notes(entity)

        try:
            event = MandateWebhookEvent.model_validate(
                {
                    "schema_version": "1.0",
                    "mandate_event_id": self._uuid_factory(),
                    "provider": "razorpay",
                    "provider_event_id": resolved_provider_event_id,
                    "event_type": event_type,
                    "token_id": token_id,
                    "risk_session_id": None,
                    "vpa_hash": vpa_hash,
                    "vpa_handle": vpa_handle,
                    "recurring_status": _optional_str(entity.get("recurring_status")),
                    "failure_reason": _failure_reason(entity),
                    "provider_created_at": provider_created_at,
                    "received_at": received_at,
                    "raw_payload_sha256": "sha256:" + hashlib.sha256(raw_body).hexdigest(),
                    "is_demo_event": is_demo_event,
                }
            )
        except pydantic.ValidationError as exc:
            if any("token_id" in err.get("loc", ()) for err in exc.errors()):
                raise WebhookParseError(MSG_INVALID_TOKEN_ID) from None
            raise WebhookParseError(MSG_MISSING_ENTITY) from None

        return ParsedClassification(
            kind="mandate",
            event=event,
            notes_risk_session_id=notes_risk_session_id,
            notes_merchant_namespace=notes_namespace,
            notes_checkout_order_ref=notes_order_ref,
            ignore_reason=None,
            raw_event_type=raw_event if isinstance(raw_event, str) else None,
        )


def _dig(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _resolve_provider_event_id(header_value: str | None, raw_body: bytes) -> str:
    if isinstance(header_value, str):
        stripped = header_value.strip()
        if 1 <= len(stripped) <= 128:
            return stripped
    return hashlib.sha256(raw_body).hexdigest()[:32]


def _epoch_to_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _failure_reason(entity: dict[str, Any]) -> str | None:
    direct = _optional_str(entity.get("failure_reason"))
    if direct is not None:
        return direct
    details = entity.get("recurring_details")
    if isinstance(details, dict):
        return _optional_str(details.get("failure_reason"))
    return None


def _split_vpa(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        username = value.get("username")
        handle = value.get("handle")
        return (
            username if isinstance(username, str) else None,
            handle if isinstance(handle, str) else None,
        )
    if isinstance(value, str):
        if "@" in value:
            username, _, handle = value.rpartition("@")
            return username or None, handle or None
        return value or None, None
    return None, None


def _extract_notes(
    entity: dict[str, Any],
) -> tuple[UUID | None, str | None, str | None]:
    notes = entity.get("notes")
    if not isinstance(notes, dict):
        return None, None, None
    risk_session_id: UUID | None = None
    raw_session = notes.get("risk_session_id")
    if isinstance(raw_session, str):
        try:
            risk_session_id = UUID(raw_session.strip())
        except ValueError:
            risk_session_id = None
    namespace = _truncate(notes.get("merchant_namespace"), 64)
    order_ref = _truncate(notes.get("checkout_order_ref"), 128)
    return risk_session_id, namespace, order_ref


def _truncate(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:limit]
