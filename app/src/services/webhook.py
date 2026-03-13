"""Webhook delivery service.

Provides best-effort HMAC-signed webhook delivery with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.src.auth.hmac_auth import generate_hmac_signature

logger = logging.getLogger(__name__)


class WebhookService:
    """Service for delivering signed webhook payloads with retries."""

    def __init__(
        self,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_multiplier: float = 4.0,
    ) -> None:
        """Initialise the WebhookService.

        Args:
            max_retries: Number of retries after the initial attempt.
            backoff_base: Base delay in seconds used for exponential backoff.
            backoff_multiplier: Backoff multiplier applied per retry.
        """
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_multiplier = backoff_multiplier
        self.client = httpx.AsyncClient()

    async def deliver(self, url: str, payload: dict[str, Any], secret: str) -> bool:
        """Deliver a webhook payload with HMAC signing and retries.

        Args:
            url: Callback URL that will receive the webhook.
            payload: Webhook payload body.
            secret: Shared HMAC secret used for signature generation.

        Returns:
            True when delivery succeeds with a 2xx response, otherwise False.
        """
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = generate_hmac_signature(payload_bytes, secret)
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={signature}",
        }

        total_attempts = self.max_retries + 1

        for attempt_number in range(1, total_attempts + 1):
            try:
                response = await self.client.post(
                    url, content=payload_bytes, headers=headers
                )
                if 200 <= response.status_code < 300:
                    logger.info(
                        "Webhook delivery succeeded",
                        extra={
                            "url": url,
                            "status_code": response.status_code,
                            "attempt_number": attempt_number,
                        },
                    )
                    return True

                log_payload = {
                    "url": url,
                    "status_code": response.status_code,
                    "attempt_number": attempt_number,
                    "remaining_retries": total_attempts - attempt_number,
                }
                if attempt_number < total_attempts:
                    logger.warning(
                        "Webhook delivery failed, retrying", extra=log_payload
                    )
                else:
                    logger.error("Webhook delivery failed", extra=log_payload)
            except httpx.RequestError as exc:
                log_payload = {
                    "url": url,
                    "attempt_number": attempt_number,
                    "remaining_retries": total_attempts - attempt_number,
                    "error": str(exc),
                }
                if attempt_number < total_attempts:
                    logger.warning(
                        "Webhook delivery request error, retrying", extra=log_payload
                    )
                else:
                    logger.error("Webhook delivery request error", extra=log_payload)

            if attempt_number < total_attempts:
                delay_seconds = self.backoff_base * (
                    self.backoff_multiplier ** (attempt_number - 1)
                )
                await asyncio.sleep(delay_seconds)

        return False

    async def close(self) -> None:
        """Close the underlying async HTTP client."""
        await self.client.aclose()


_webhook_service: WebhookService | None = None


def get_webhook_service() -> WebhookService:
    """Return a singleton WebhookService instance.

    Returns:
        A configured WebhookService instance.
    """
    global _webhook_service
    if _webhook_service is None:
        _webhook_service = WebhookService()
    return _webhook_service
