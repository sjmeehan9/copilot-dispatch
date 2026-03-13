"""Secrets management module.

Provides a SecretsProvider class that retrieves and caches secrets from AWS
Secrets Manager in production, with a fallback to environment variables for
local development and testing.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.src.config import get_settings
from app.src.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


class SecretsProvider:
    """Retrieves and caches secrets from AWS Secrets Manager or environment variables.

    In production (when app_env != "local" and app_env != "test"), secrets are
    fetched from AWS Secrets Manager. In local/test mode, secrets are read from
    environment variables. Secrets are cached in memory for 5 minutes to avoid
    repeated API calls.
    """

    def __init__(self, boto3_client: Any | None = None) -> None:
        """Initialise the SecretsProvider.

        Args:
            boto3_client: Optional boto3 client for dependency injection in tests.
        """
        self._cache: dict[str, dict[str, Any]] = {}
        self._ttl_seconds = 300  # 5 minutes
        self._boto3_client = boto3_client

    def _get_client(self) -> Any:
        """Get or create the boto3 Secrets Manager client."""
        if self._boto3_client is None:
            self._boto3_client = boto3.client("secretsmanager")
        return self._boto3_client

    def get_secret(self, secret_name: str, env_fallback: str | None = None) -> str:
        """Retrieve a secret value.

        Checks the in-memory cache first. If the secret is missing or the cache
        TTL has expired, fetches the secret from the appropriate source based on
        the application environment.

        Args:
            secret_name: The name of the secret in AWS Secrets Manager.
            env_fallback: The name of the environment variable to use as a fallback
                in local/test environments.

        Returns:
            The secret string value.

        Raises:
            AuthenticationError: If the secret cannot be retrieved.
        """
        now = time.time()
        cached = self._cache.get(secret_name)

        if cached and (now - cached["timestamp"]) < self._ttl_seconds:
            logger.debug("Cache hit for secret: %s", secret_name)
            return cached["value"]

        logger.debug("Cache miss or expired for secret: %s", secret_name)
        settings = get_settings()

        if settings.app_env in ("local", "test"):
            if not env_fallback:
                raise AuthenticationError(
                    error_message=f"Cannot retrieve secret '{secret_name}' locally without an env_fallback."
                )
            secret_value = os.environ.get(env_fallback)
            if not secret_value:
                raise AuthenticationError(
                    error_message=f"Missing environment variable '{env_fallback}' for secret '{secret_name}'."
                )
        else:
            try:
                client = self._get_client()
                response = client.get_secret_value(SecretId=secret_name)
                if "SecretString" in response:
                    secret_value = response["SecretString"]
                else:
                    raise AuthenticationError(
                        error_message=f"Secret '{secret_name}' does not contain a string value."
                    )
            except (BotoCoreError, ClientError) as e:
                logger.error(
                    "Failed to retrieve secret '%s' from Secrets Manager: %s",
                    secret_name,
                    e,
                )
                raise AuthenticationError(
                    error_message=f"Failed to retrieve secret '{secret_name}'."
                ) from e

        self._cache[secret_name] = {"value": secret_value, "timestamp": now}
        return secret_value

    def clear_cache(self) -> None:
        """Clear the in-memory secrets cache."""
        self._cache.clear()
        logger.debug("Secrets cache cleared.")
