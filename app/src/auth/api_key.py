"""API key authentication module.

Provides a FastAPI dependency for validating the X-API-Key header against
a secret stored in AWS Secrets Manager (or environment variables locally).
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Header

from app.src.auth.secrets import SecretsProvider
from app.src.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

# Singleton instance for the dependency
_secrets_provider = SecretsProvider()


async def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """FastAPI dependency to verify the API key.

    Retrieves the expected API key from the SecretsProvider and compares it
    to the provided X-API-Key header using a constant-time comparison.

    Args:
        x_api_key: The API key provided in the request header.

    Returns:
        The validated API key.

    Raises:
        AuthenticationError: If the header is missing, empty, or invalid.
    """
    if not x_api_key:
        raise AuthenticationError(
            error_message="Missing API key. Provide X-API-Key header."
        )

    try:
        expected_key = _secrets_provider.get_secret(
            secret_name="dispatch/api-key",
            env_fallback="DISPATCH_API_KEY",
        )
    except AuthenticationError as e:
        logger.error("Failed to retrieve expected API key: %s", e)
        # Do not leak the internal error to the client
        raise AuthenticationError(
            error_message="Internal server error during authentication."
        ) from e

    if not hmac.compare_digest(x_api_key.encode("utf-8"), expected_key.encode("utf-8")):
        raise AuthenticationError(error_message="Invalid API key.")

    return x_api_key
