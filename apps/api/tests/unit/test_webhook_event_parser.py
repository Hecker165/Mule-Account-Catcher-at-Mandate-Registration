"""A3 unit tests: normalisation of A0 webhook fixtures into MandateWebhookEvent."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.contracts.common import MandateEventType
from app.services.webhook_event_parser import (
    MSG_INVALID_TOKEN_ID,
    MSG_MISSING_ENTITY,
    RazorpayWebhookParser,
    WebhookParseError,
)
from app.services.webhook_pseudonymisation import VpaPseudonymizer
from tests.helpers.webhook_signing import load_webhook_fixture

PEPPER = "test-pepper-1234567890"
FIXED_NOW = datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC)


@pytest.fixture
def parser() -> RazorpayWebhookParser:
    return RazorpayWebhookParser(
        VpaPseudonymizer(PEPPER),
        clock=lambda: FIXED_NOW,
        uuid_factory=lambda: UUID("b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e"),
    )


def _base_body(**overrides: Any) -> bytes:
    body: dict[str, Any] = {
        "entity": "event",
        "account_id": "acc_demo_123",
        "event": "token.confirmed",
        "contains": ["token"],
        "payload": {
            "token": {
                "entity": {
                    "id": "token_demo123",
                    "entity": "token",
                    "method": "upi",
                    "recurring_status": "confirmed",
                    "notes": {"risk_session_id": str(uuid4())},
                    "vpa": {"username": "demo.user", "handle": "upi"},
                }
            }
        },
        "created_at": 1705312920,
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


def test_confirmed_fixture_parses_to_valid_mandate_event(parser: RazorpayWebhookParser) -> None:
    raw = load_webhook_fixture("token_confirmed_body.json")
    result = parser.classify_and_parse(raw, "evt_demo_confirmed_001", False)
    assert result.kind == "mandate"
    assert result.event is not None
    event = result.event
    assert event.provider == "razorpay"
    assert event.provider_event_id == "evt_demo_confirmed_001"
    assert event.event_type == MandateEventType.TOKEN_CONFIRMED
    assert event.token_id == "token_demo123"
    assert event.risk_session_id is None
    assert event.vpa_hash is not None and event.vpa_hash.startswith("hmac-sha256:")
    assert event.vpa_handle == "upi"
    assert event.raw_payload_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert event.is_demo_event is False
    assert event.received_at == FIXED_NOW


def test_rejected_fixture_preserves_npci_risk_failure_reason(
    parser: RazorpayWebhookParser,
) -> None:
    raw = load_webhook_fixture("token_rejected_npci_risk_body.json")
    result = parser.classify_and_parse(raw, "evt_demo_rejected_001", False)
    assert result.kind == "mandate"
    assert result.event is not None
    assert result.event.event_type == MandateEventType.TOKEN_REJECTED
    assert result.event.failure_reason == "Mandate rejected: NPCI risk flag raised by payer bank"


def test_provider_event_id_header_wins_over_derived_prefix(
    parser: RazorpayWebhookParser,
) -> None:
    result = parser.classify_and_parse(_base_body(), "  evt_custom_001  ", False)
    assert result.event is not None
    assert result.event.provider_event_id == "evt_custom_001"


def test_missing_provider_event_id_derives_body_digest_prefix(
    parser: RazorpayWebhookParser,
) -> None:
    raw = _base_body()
    result = parser.classify_and_parse(raw, None, False)
    assert result.event is not None
    assert result.event.provider_event_id == hashlib.sha256(raw).hexdigest()[:32]


@pytest.mark.parametrize("header", ["", "   ", "x" * 129])
def test_blank_or_overlong_provider_event_id_falls_back_to_digest(
    parser: RazorpayWebhookParser, header: str
) -> None:
    raw = _base_body()
    result = parser.classify_and_parse(raw, header, False)
    assert result.event is not None
    assert result.event.provider_event_id == hashlib.sha256(raw).hexdigest()[:32]


def test_vpa_object_and_string_forms_hash_identically(parser: RazorpayWebhookParser) -> None:
    raw_object = _base_body()
    parsed = json.loads(raw_object)
    parsed["payload"]["token"]["entity"]["vpa"] = "demo.user@upi"
    raw_string = json.dumps(parsed).encode("utf-8")
    first = parser.classify_and_parse(raw_object, "evt-1", False)
    second = parser.classify_and_parse(raw_string, "evt-2", False)
    assert first.event is not None and second.event is not None
    assert first.event.vpa_hash == second.event.vpa_hash
    assert first.event.vpa_handle == second.event.vpa_handle == "upi"


def test_vpa_hash_uses_vpa_domain_and_pepper(parser: RazorpayWebhookParser) -> None:
    result = parser.classify_and_parse(_base_body(), "evt-1", False)
    assert result.event is not None
    expected = hmac.new(
        PEPPER.encode("utf-8"), b"vpa\x00" + b"demo.user@upi", hashlib.sha256
    ).hexdigest()
    assert result.event.vpa_hash == f"hmac-sha256:{expected}"


def test_vpa_username_without_handle_hashes_username_alone(
    parser: RazorpayWebhookParser,
) -> None:
    raw = _base_body()
    parsed = json.loads(raw)
    parsed["payload"]["token"]["entity"]["vpa"] = {"username": "SoloUser", "handle": None}
    result = parser.classify_and_parse(json.dumps(parsed).encode("utf-8"), "evt-1", False)
    assert result.event is not None
    expected = hmac.new(
        PEPPER.encode("utf-8"), b"vpa\x00" + b"solouser", hashlib.sha256
    ).hexdigest()
    assert result.event.vpa_hash == f"hmac-sha256:{expected}"
    assert result.event.vpa_handle is None


def test_epoch_created_at_becomes_timezone_aware_utc(parser: RazorpayWebhookParser) -> None:
    result = parser.classify_and_parse(_base_body(), "evt-1", False)
    assert result.event is not None
    assert result.event.provider_created_at is not None
    assert result.event.provider_created_at.tzinfo is not None
    assert result.event.provider_created_at == datetime(2024, 1, 15, 10, 2, tzinfo=UTC)


def test_unrelated_event_type_is_classified_ignored(parser: RazorpayWebhookParser) -> None:
    raw = _base_body(event="payment.captured")
    result = parser.classify_and_parse(raw, "evt-1", False)
    assert result.kind == "ignored"
    assert result.event is None
    assert result.ignore_reason == "unrelated event type"


def test_non_upi_method_is_classified_ignored(parser: RazorpayWebhookParser) -> None:
    raw = _base_body()
    parsed = json.loads(raw)
    parsed["payload"]["token"]["entity"]["method"] = "card"
    result = parser.classify_and_parse(json.dumps(parsed).encode("utf-8"), "evt-1", False)
    assert result.kind == "ignored"
    assert result.ignore_reason == "unsupported method"


def test_missing_token_entity_raises_fixed_parse_error(parser: RazorpayWebhookParser) -> None:
    with pytest.raises(WebhookParseError, match="missing required token entity"):
        parser.classify_and_parse(_base_body(payload={}), "evt-1", False)
    assert MSG_MISSING_ENTITY == "webhook payload is missing required token entity"


@pytest.mark.parametrize("token_id", ["bad-id!", "", None, 123, "tok_demo123"])
def test_invalid_token_id_raises_fixed_parse_error(
    parser: RazorpayWebhookParser, token_id: Any
) -> None:
    raw = _base_body()
    parsed = json.loads(raw)
    parsed["payload"]["token"]["entity"]["id"] = token_id
    with pytest.raises(WebhookParseError) as exc_info:
        parser.classify_and_parse(json.dumps(parsed).encode("utf-8"), "evt-1", False)
    assert str(exc_info.value) == MSG_INVALID_TOKEN_ID


def test_malformed_json_raises_fixed_parse_error(parser: RazorpayWebhookParser) -> None:
    with pytest.raises(WebhookParseError) as exc_info:
        parser.classify_and_parse(b'{"event": ', "evt-1", False)
    assert str(exc_info.value) == "webhook body is not valid JSON"


def test_notes_fields_are_extracted_and_invalid_uuid_ignored(
    parser: RazorpayWebhookParser,
) -> None:
    session_id = uuid4()
    raw = _base_body()
    parsed = json.loads(raw)
    parsed["payload"]["token"]["entity"]["notes"] = {
        "risk_session_id": str(session_id),
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_demo_001",
    }
    result = parser.classify_and_parse(json.dumps(parsed).encode("utf-8"), "evt-1", False)
    assert result.notes_risk_session_id == session_id
    assert result.notes_merchant_namespace == "demo-merchant-01"
    assert result.notes_checkout_order_ref == "order_demo_001"

    parsed["payload"]["token"]["entity"]["notes"] = {"risk_session_id": "not-a-uuid"}
    result = parser.classify_and_parse(json.dumps(parsed).encode("utf-8"), "evt-2", False)
    assert result.kind == "mandate"
    assert result.notes_risk_session_id is None


def test_demo_flag_comes_from_argument_not_payload(parser: RazorpayWebhookParser) -> None:
    parsed = json.loads(_base_body())
    parsed["is_demo_event"] = True
    raw = json.dumps(parsed).encode("utf-8")
    assert parser.classify_and_parse(raw, "evt-1", False).event is not None
    assert parser.classify_and_parse(raw, "evt-1", False).event.is_demo_event is False
    assert parser.classify_and_parse(raw, "evt-1", True).event is not None
    assert parser.classify_and_parse(raw, "evt-1", True).event.is_demo_event is True


def test_raw_body_and_signature_never_appear_in_event_or_error_text(
    parser: RazorpayWebhookParser,
) -> None:
    secret_marker = "secret-vpa-user-xyz"
    raw = _base_body()
    parsed = json.loads(raw)
    parsed["payload"]["token"]["entity"]["vpa"] = {
        "username": secret_marker,
        "handle": "upi",
    }
    result = parser.classify_and_parse(json.dumps(parsed).encode("utf-8"), "evt-1", False)
    assert result.event is not None
    assert secret_marker not in result.event.model_dump_json()

    with pytest.raises(WebhookParseError) as exc_info:
        parser.classify_and_parse(b"not-json{{{", "evt-1", False)
    assert "not-json" not in str(exc_info.value)
