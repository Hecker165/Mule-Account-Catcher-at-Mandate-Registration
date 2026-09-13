"""A3 unit tests: Razorpay webhook HMAC signature verification vectors."""

import hashlib
import hmac
from unittest.mock import patch

import pytest

from app.services.webhook_signature import WebhookSignatureVerifier

SECRET = "local-test-secret"
BODY = b'{"event":"token.confirmed"}'


def test_known_secret_and_body_produce_expected_hmac_hex() -> None:
    """Fixed vector computed with the documented algorithm."""
    expected = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    verifier = WebhookSignatureVerifier(SECRET)
    assert verifier.verify(BODY, expected) is True
    assert expected == hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    assert len(expected) == 64


def test_verify_accepts_exact_body_and_signature() -> None:
    verifier = WebhookSignatureVerifier(SECRET)
    signature = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    assert verifier.verify(BODY, signature) is True


def test_verify_accepts_uppercase_and_padded_signature() -> None:
    verifier = WebhookSignatureVerifier(SECRET)
    signature = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    assert verifier.verify(BODY, f"  {signature.upper()}  ") is True


def test_verify_rejects_tampered_body() -> None:
    verifier = WebhookSignatureVerifier(SECRET)
    signature = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    assert verifier.verify(b'{"event":"token.confirmed!"}', signature) is False


def test_verify_rejects_tampered_signature() -> None:
    verifier = WebhookSignatureVerifier(SECRET)
    signature = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    tampered = ("0" if signature[0] != "0" else "1") + signature[1:]
    assert verifier.verify(BODY, tampered) is False


@pytest.mark.parametrize("header", [None, "", "   ", "not-hex", "abc123"])
def test_verify_rejects_missing_and_malformed_header_values(header: str | None) -> None:
    verifier = WebhookSignatureVerifier(SECRET)
    assert verifier.verify(BODY, header) is False


def test_verify_uses_constant_time_compare() -> None:
    verifier = WebhookSignatureVerifier(SECRET)
    signature = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    with patch("app.services.webhook_signature.hmac.compare_digest") as mocked:
        mocked.return_value = True
        assert verifier.verify(BODY, signature) is True
        assert mocked.call_count == 1


@pytest.mark.parametrize("secret", [None, "", "   "])
def test_empty_or_none_secret_means_not_configured_and_always_fails(
    secret: str | None,
) -> None:
    verifier = WebhookSignatureVerifier(secret)
    assert verifier.is_configured is False
    signature = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    assert verifier.verify(BODY, signature) is False


def test_configured_secret_reports_is_configured() -> None:
    assert WebhookSignatureVerifier(SECRET).is_configured is True
