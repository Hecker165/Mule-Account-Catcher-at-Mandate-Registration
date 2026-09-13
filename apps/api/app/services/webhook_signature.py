"""Constant-time Razorpay webhook HMAC-SHA256 verification over raw request bytes."""

from __future__ import annotations

import hashlib
import hmac

from pydantic import SecretStr


class WebhookSignatureVerifier:
    """Verifies Razorpay webhook signatures over untouched raw body bytes."""

    def __init__(self, secret: SecretStr | str | None) -> None:
        if isinstance(secret, SecretStr):
            raw = secret.get_secret_value()
        elif isinstance(secret, str):
            raw = secret
        else:
            raw = ""
        self._secret: bytes | None = raw.encode("utf-8") if raw and raw.strip() else None

    @property
    def is_configured(self) -> bool:
        """True only when a non-empty secret was supplied."""
        return self._secret is not None

    def verify(self, raw_body: bytes, signature_header: str | None) -> bool:
        """Return True only for a valid signature; never raises or logs."""
        if self._secret is None:
            return False
        if not isinstance(signature_header, str):
            return False
        expected = hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        provided = signature_header.strip().lower()
        try:
            return hmac.compare_digest(expected.encode("ascii"), provided.encode("ascii"))
        except (UnicodeEncodeError, ValueError):
            return False
