"""Typed Razorpay token-revoke client: Basic auth, idempotency, safe results."""

from __future__ import annotations

import re
from typing import Protocol

import httpx
from pydantic import SecretStr

from app.integrations.razorpay.types import (
    RevokeOutcome,
    RevokeResult,
    classify_status,
    result_for_status,
    result_for_transport,
)

_TOKEN_ID_PATTERN = re.compile(r"^token_[A-Za-z0-9]+$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[a-z0-9_-]{16,128}$")


class TokenRevokeClient(Protocol):
    async def revoke_token(self, token_id: str, idempotency_key: str) -> RevokeResult: ...
    async def aclose(self) -> None: ...


class RazorpayTokenRevokeClient:
    """Calls Razorpay's token-revoke endpoint; returns failures, never raises them."""

    def __init__(
        self,
        key_id: SecretStr,
        key_secret: SecretStr,
        base_url: str = "https://api.razorpay.com/v1",
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key_id = key_id.get_secret_value()
        self._key_secret = key_secret.get_secret_value()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._owns_client = http_client is None

    async def revoke_token(self, token_id: str, idempotency_key: str) -> RevokeResult:
        """POST the revoke; provider conditions become results, not exceptions."""
        _validate_token_id(token_id)
        _validate_idempotency_key(idempotency_key)
        client = self._client()
        try:
            # Auth is sent per request so injected test clients authenticate too.
            response = await client.post(
                f"{self._base_url}/tokens/{token_id}/revoke",
                headers={"Idempotency-Key": idempotency_key, "Content-Type": "application/json"},
                json={},
                auth=(self._key_id, self._key_secret),
            )
        except httpx.TimeoutException:
            return result_for_transport("timeout")
        except httpx.TransportError:
            return result_for_transport("unreachable")
        outcome = classify_status(response.status_code)
        if outcome == RevokeOutcome.SUCCEEDED:
            try:
                response.json()
            except ValueError:
                return result_for_transport("invalid")
        return result_for_status(response.status_code)

    async def aclose(self) -> None:
        """Close the owned client only."""
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._timeout_seconds, auth=(self._key_id, self._key_secret)
            )
        return self._http_client


def _validate_token_id(token_id: str) -> None:
    if not isinstance(token_id, str) or _TOKEN_ID_PATTERN.match(token_id) is None:
        raise ValueError("invalid token_id")


def _validate_idempotency_key(idempotency_key: str) -> None:
    if (
        not isinstance(idempotency_key, str)
        or _IDEMPOTENCY_KEY_PATTERN.match(idempotency_key) is None
    ):
        raise ValueError("invalid idempotency_key")
