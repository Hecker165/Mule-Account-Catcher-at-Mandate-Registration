"""A3 test helper: sign raw webhook bodies with the local test secret."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

TEST_WEBHOOK_SECRET = "local-test-secret"


def sign_body(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    """Return the hex HMAC-SHA256 signature of the raw body."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def load_webhook_fixture(filename: str) -> bytes:
    """Load a file from data/fixtures/webhooks/ as raw bytes."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data" / "fixtures" / "webhooks" / filename
        if candidate.exists():
            return candidate.read_bytes()
    raise FileNotFoundError(f"webhook fixture not found: {filename}")
