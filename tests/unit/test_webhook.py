"""Unit tests for webhook delivery service."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.src.auth.hmac_auth import generate_hmac_signature
from app.src.services.webhook import WebhookService


@pytest.mark.asyncio
async def test_deliver_success_first_attempt() -> None:
    """Webhook delivery succeeds immediately on first attempt."""
    service = WebhookService(max_retries=3)
    service.client.post = AsyncMock(return_value=httpx.Response(status_code=200))

    payload = {"run_id": "run-1", "status": "success"}
    delivered = await service.deliver("https://example.com/webhook", payload, "secret")

    assert delivered is True
    assert service.client.post.call_count == 1

    _, kwargs = service.client.post.call_args
    expected_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    expected_sig = generate_hmac_signature(expected_body, "secret")
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["X-Webhook-Signature"] == f"sha256={expected_sig}"

    await service.close()


@pytest.mark.asyncio
async def test_deliver_retries_then_succeeds() -> None:
    """Webhook delivery retries on failure and eventually succeeds."""
    service = WebhookService(max_retries=3)
    service.client.post = AsyncMock(
        side_effect=[
            httpx.Response(status_code=500),
            httpx.Response(status_code=502),
            httpx.Response(status_code=204),
        ]
    )

    with patch(
        "app.src.services.webhook.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        delivered = await service.deliver(
            "https://example.com/webhook", {"run_id": "run-2"}, "secret"
        )

    assert delivered is True
    assert service.client.post.call_count == 3
    assert mock_sleep.await_count == 2
    mock_sleep.assert_any_await(1.0)
    mock_sleep.assert_any_await(4.0)

    await service.close()


@pytest.mark.asyncio
async def test_deliver_exhausts_retries_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Webhook delivery returns false when all attempts fail."""
    service = WebhookService(max_retries=3)
    service.client.post = AsyncMock(return_value=httpx.Response(status_code=503))

    with patch(
        "app.src.services.webhook.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        delivered = await service.deliver(
            "https://example.com/webhook",
            {"run_id": "run-3", "secret_like": "do-not-log"},
            "secret",
        )

    assert delivered is False
    assert service.client.post.call_count == 4
    assert mock_sleep.await_count == 3
    mock_sleep.assert_any_await(1.0)
    mock_sleep.assert_any_await(4.0)
    mock_sleep.assert_any_await(16.0)

    assert "do-not-log" not in caplog.text

    await service.close()


@pytest.mark.asyncio
async def test_deliver_retries_on_request_error() -> None:
    """Webhook delivery retries when the HTTP client raises request errors."""
    service = WebhookService(max_retries=1)
    service.client.post = AsyncMock(
        side_effect=[
            httpx.ConnectError("connection failed"),
            httpx.Response(status_code=200),
        ]
    )

    with patch(
        "app.src.services.webhook.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        delivered = await service.deliver(
            "https://example.com/webhook", {"run_id": "run-4"}, "secret"
        )

    assert delivered is True
    assert service.client.post.call_count == 2
    mock_sleep.assert_awaited_once_with(1.0)

    await service.close()
