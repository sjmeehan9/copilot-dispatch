"""Unit tests for API key authentication."""

import os
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.src.auth.api_key import verify_api_key
from app.src.auth.secrets import SecretsProvider
from app.src.exceptions import AuthenticationError, register_exception_handlers


@pytest.fixture
def mock_boto3_client():
    """Mock boto3 client for Secrets Manager."""
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": "prod-secret-key"}
    return client


@pytest.fixture
def secrets_provider(mock_boto3_client):
    """SecretsProvider instance with mocked boto3 client."""
    return SecretsProvider(boto3_client=mock_boto3_client)


def test_secrets_provider_local_env_fallback(monkeypatch, secrets_provider):
    """Test SecretsProvider falls back to env var in local mode."""
    monkeypatch.setenv("DISPATCH_API_KEY", "test-local-key")
    # Assuming app_env is "local" by default in tests
    secret = secrets_provider.get_secret(
        "dispatch/api-key", env_fallback="DISPATCH_API_KEY"
    )
    assert secret == "test-local-key"


def test_secrets_provider_local_missing_env(monkeypatch, secrets_provider):
    """Test SecretsProvider raises error if env var is missing in local mode."""
    monkeypatch.delenv("DISPATCH_API_KEY", raising=False)
    with pytest.raises(AuthenticationError, match="Missing environment variable"):
        secrets_provider.get_secret(
            "dispatch/api-key", env_fallback="DISPATCH_API_KEY"
        )


def test_secrets_provider_production_cache(
    monkeypatch, secrets_provider, mock_boto3_client
):
    """Test SecretsProvider fetches from Secrets Manager and caches in production."""
    # Force production mode
    monkeypatch.setenv("DISPATCH_APP_ENV", "production")
    from app.src.config import get_settings

    get_settings.cache_clear()

    # First call should hit Secrets Manager
    secret1 = secrets_provider.get_secret("dispatch/api-key")
    assert secret1 == "prod-secret-key"
    mock_boto3_client.get_secret_value.assert_called_once_with(
        SecretId="dispatch/api-key"
    )

    # Second call should hit cache
    mock_boto3_client.reset_mock()
    secret2 = secrets_provider.get_secret("dispatch/api-key")
    assert secret2 == "prod-secret-key"
    mock_boto3_client.get_secret_value.assert_not_called()

    # Clear cache and call again
    secrets_provider.clear_cache()
    secret3 = secrets_provider.get_secret("dispatch/api-key")
    assert secret3 == "prod-secret-key"
    mock_boto3_client.get_secret_value.assert_called_once_with(
        SecretId="dispatch/api-key"
    )


# --- FastAPI Dependency Tests ---

app = FastAPI()
register_exception_handlers(app)


@app.get("/protected")
async def protected_route(api_key: str = Depends(verify_api_key)):
    return {"message": "success", "key": api_key}


client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch):
    """Set up environment variables for tests."""
    monkeypatch.setenv("DISPATCH_API_KEY", "valid-test-key")
    monkeypatch.setenv("DISPATCH_APP_ENV", "local")
    from app.src.config import get_settings

    get_settings.cache_clear()
    # Clear the cache of the singleton instance used by the dependency
    from app.src.auth.api_key import _secrets_provider

    _secrets_provider.clear_cache()


def test_verify_api_key_valid():
    """Test valid API key passes authentication."""
    response = client.get("/protected", headers={"X-API-Key": "valid-test-key"})
    assert response.status_code == 200
    assert response.json() == {"message": "success", "key": "valid-test-key"}


def test_verify_api_key_missing():
    """Test missing API key raises AuthenticationError."""
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_ERROR"
    assert "Missing API key" in response.json()["error_message"]


def test_verify_api_key_invalid():
    """Test invalid API key raises AuthenticationError."""
    response = client.get("/protected", headers={"X-API-Key": "invalid-key"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_ERROR"
    assert "Invalid API key" in response.json()["error_message"]


def test_verify_api_key_empty():
    """Test empty API key raises AuthenticationError."""
    response = client.get("/protected", headers={"X-API-Key": ""})
    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_ERROR"
    assert "Missing API key" in response.json()["error_message"]
