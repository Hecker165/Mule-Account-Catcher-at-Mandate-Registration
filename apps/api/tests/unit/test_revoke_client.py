"""A6 unit tests: revoke client classification, auth and redaction."""

from __future__ import annotations

import base64

import httpx
import pytest
from pydantic import SecretStr

from app.integrations.razorpay.client import RazorpayTokenRevokeClient
from app.integrations.razorpay.mock_client import MockRevokeCall, MockTokenRevokeClient
from app.integrations.razorpay.types import RevokeOutcome

KEY_ID = "test_key_id"
KEY_SECRET = "test_key_secret"
TOKEN_ID = "token_demo123"
IDEMPOTENCY_KEY = "revoke-test-key-0001"


def _client(handler, calls: list) -> RazorpayTokenRevokeClient:
    def recording_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler)
    return RazorpayTokenRevokeClient(
        key_id=SecretStr(KEY_ID),
        key_secret=SecretStr(KEY_SECRET),
        http_client=httpx.AsyncClient(transport=transport),
    )


async def test_2xx_returns_succeeded_with_status_code() -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(200, json={"status": "revoked"}), calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.SUCCEEDED
    assert result.provider_status_code == 200
    assert result.safe_error_code is None
    assert result.safe_error_message is None
    await client.aclose()


async def test_request_uses_post_revoke_endpoint_and_basic_auth() -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(200, json={}), calls)
    await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/tokens/token_demo123/revoke"
    auth = request.headers.get("authorization", "")
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth[len("Basic ") :]).decode() == f"{KEY_ID}:{KEY_SECRET}"
    await client.aclose()


async def test_idempotency_key_header_is_sent_on_every_call() -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(200, json={}), calls)
    await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert [request.headers["Idempotency-Key"] for request in calls] == [
        IDEMPOTENCY_KEY,
        IDEMPOTENCY_KEY,
    ]
    await client.aclose()


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_429_and_5xx_are_classified_retryable(status: int) -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(status, json={}), calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.RETRYABLE
    assert result.provider_status_code == status
    await client.aclose()


async def test_408_is_classified_retryable() -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(408, json={}), calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.RETRYABLE
    assert result.safe_error_code == "UPSTREAM_TRANSIENT"
    await client.aclose()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_4xx_other_than_408_429_is_classified_permanent(status: int) -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(status, json={}), calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.PERMANENT
    assert result.safe_error_code == "UPSTREAM_REJECTED"
    await client.aclose()


@pytest.mark.parametrize("status", [103, 302])
async def test_1xx_and_3xx_are_classified_retryable(status: int) -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(status, json={}), calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.RETRYABLE
    await client.aclose()


async def test_timeout_is_classified_retryable_with_safe_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    calls: list = []
    client = _client(handler, calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.RETRYABLE
    assert result.provider_status_code is None
    assert result.safe_error_code == "UPSTREAM_TIMEOUT"
    await client.aclose()


async def test_connection_error_is_classified_retryable_with_safe_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    calls: list = []
    client = _client(handler, calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.RETRYABLE
    assert result.safe_error_code == "UPSTREAM_UNREACHABLE"
    await client.aclose()


@pytest.mark.parametrize("token_id", ["bad-id!", "", "tok_demo123", "token_demo 123"])
async def test_invalid_token_id_raises_before_any_http_call(token_id: str) -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(200, json={}), calls)
    with pytest.raises(ValueError):
        await client.revoke_token(token_id, IDEMPOTENCY_KEY)
    assert calls == []
    await client.aclose()


@pytest.mark.parametrize("key", ["short", "", "UPPERCASE-KEY-0123456789", "has space in key 12345"])
async def test_invalid_idempotency_key_raises_before_any_http_call(key: str) -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(200, json={}), calls)
    with pytest.raises(ValueError):
        await client.revoke_token(TOKEN_ID, key)
    assert calls == []
    await client.aclose()


async def test_upstream_body_never_appears_in_safe_error_message() -> None:
    marker = "secret-upstream-detail-xyz"
    calls: list = []
    client = _client(lambda request: httpx.Response(400, text=marker), calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.safe_error_message is not None
    assert marker not in result.safe_error_message
    assert "400" in result.safe_error_message
    await client.aclose()


async def test_secret_never_appears_in_result_or_exception_repr() -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(200, json={}), calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert KEY_SECRET not in repr(result)
    assert KEY_SECRET not in str(result)
    assert all(KEY_SECRET not in str(request.headers) for request in calls)
    auth = calls[0].headers.get("authorization", "")
    assert KEY_SECRET not in auth.split(" ", 1)[0]
    await client.aclose()


async def test_client_returns_failures_instead_of_raising() -> None:
    calls: list = []
    client = _client(lambda request: httpx.Response(500, json={}), calls)
    result = await client.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.RETRYABLE
    await client.aclose()


async def test_mock_default_succeeds_and_records_call() -> None:
    mock = MockTokenRevokeClient()
    result = await mock.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert result.outcome == RevokeOutcome.SUCCEEDED
    assert result.provider_status_code == 200
    assert mock.recorded_calls == (
        MockRevokeCall(TOKEN_ID, IDEMPOTENCY_KEY, RevokeOutcome.SUCCEEDED),
    )
    await mock.aclose()


async def test_mock_scheduled_failures_then_success() -> None:
    mock = MockTokenRevokeClient()
    mock.schedule_failures(2, RevokeOutcome.RETRYABLE, 503)
    first = await mock.revoke_token(TOKEN_ID, "revoke-mock-key-0001")
    second = await mock.revoke_token(TOKEN_ID, "revoke-mock-key-0002")
    third = await mock.revoke_token(TOKEN_ID, "revoke-mock-key-0003")
    assert first.outcome == RevokeOutcome.RETRYABLE
    assert first.provider_status_code == 503
    assert second.outcome == RevokeOutcome.RETRYABLE
    assert third.outcome == RevokeOutcome.SUCCEEDED
    assert len(mock.recorded_calls) == 3
    await mock.aclose()


async def test_mock_dedupes_same_idempotency_key_after_success() -> None:
    mock = MockTokenRevokeClient()
    first = await mock.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    second = await mock.revoke_token(TOKEN_ID, IDEMPOTENCY_KEY)
    assert first.outcome == RevokeOutcome.SUCCEEDED
    assert second.outcome == RevokeOutcome.SUCCEEDED
    assert len(mock.recorded_calls) == 1
    await mock.aclose()


async def test_mock_rejects_invalid_arguments() -> None:
    mock = MockTokenRevokeClient()
    with pytest.raises(ValueError):
        await mock.revoke_token("bad-id!", IDEMPOTENCY_KEY)
    with pytest.raises(ValueError):
        await mock.revoke_token(TOKEN_ID, "short")
    assert mock.recorded_calls == ()
    await mock.aclose()
