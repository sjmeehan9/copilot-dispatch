"""Unit tests for API routes."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.src.api.dependencies import get_dispatch_service_dep, get_run_store_dep
from app.src.api.routes import _resolve_base_url
from app.src.auth.api_key import verify_api_key
from app.src.config import Settings
from app.src.exceptions import DispatchError
from app.src.main import app


@pytest.fixture(autouse=True)
def mock_env_vars():
    """Mock environment variables required for tests."""
    os.environ["GITHUB_PAT"] = "test-pat"
    yield
    del os.environ["GITHUB_PAT"]


@pytest.fixture
def mock_run_store():
    """Mock RunStore dependency."""
    store = MagicMock()
    store.create_run.return_value = None
    store.get_run.return_value = {
        "run_id": "test-run-id",
        "repository": "owner/repo",
        "branch": "main",
        "role": "implement",
        "status": "dispatched",
        "model": "claude-3-5-sonnet-20241022",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "result": None,
        "error": None,
    }
    return store


@pytest.fixture
def mock_dispatch_service():
    """Mock DispatchService dependency."""
    service = AsyncMock()
    service.dispatch_workflow.return_value = None
    return service


@pytest.fixture
def client(mock_run_store, mock_dispatch_service):
    """Test client with mocked dependencies."""
    app.dependency_overrides[get_run_store_dep] = lambda: mock_run_store
    app.dependency_overrides[get_dispatch_service_dep] = lambda: mock_dispatch_service
    app.dependency_overrides[verify_api_key] = lambda: "test-api-key"

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_create_agent_run_success(client, mock_run_store, mock_dispatch_service):
    """Test successful run creation and dispatch."""
    payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Do something",
        "model": "claude-3-5-sonnet-20241022",
        "role": "implement",
    }

    response = client.post("/agent/run", json=payload)

    assert response.status_code == 202
    data = response.json()
    assert "run_id" in data
    assert data["status"] == "dispatched"
    assert data["created_at"] == "2024-01-01T00:00:00Z"

    mock_run_store.create_run.assert_called_once()
    mock_dispatch_service.dispatch_workflow.assert_called_once()


def test_create_agent_run_invalid_payload(client):
    """Test run creation with invalid payload."""
    payload = {
        "repository": "owner/repo",
        # Missing required fields
    }

    response = client.post("/agent/run", json=payload)

    assert response.status_code == 422


def test_create_agent_run_dispatch_failure(
    client, mock_run_store, mock_dispatch_service
):
    """Test run creation when dispatch fails."""
    mock_dispatch_service.dispatch_workflow.side_effect = DispatchError(
        error_message="GitHub API error", error_details={"status": 404}
    )

    payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Do something",
        "model": "claude-3-5-sonnet-20241022",
        "role": "implement",
    }

    response = client.post("/agent/run", json=payload)

    assert response.status_code == 500
    data = response.json()
    assert data["error_code"] == "DISPATCH_ERROR"

    mock_run_store.create_run.assert_called_once()
    mock_run_store.update_run_error.assert_called_once()


def test_get_agent_run_success(client, mock_run_store):
    """Test successful run retrieval."""
    response = client.get("/agent/run/test-run-id")

    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == "test-run-id"
    assert data["status"] == "dispatched"

    mock_run_store.get_run.assert_called_once_with("test-run-id")


def test_health_endpoint(client):
    """Test health endpoint is accessible without API key."""
    # Clear API key override to ensure it's not required
    app.dependency_overrides.pop(verify_api_key, None)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


# ---------------------------------------------------------------------------
# _resolve_base_url
# ---------------------------------------------------------------------------


class TestResolveBaseUrl:
    """Tests for _resolve_base_url — Bug A regression guard.

    Verifies that the configured service_url is preferred over
    request.base_url so that callback URLs are correct behind a
    TLS-terminating reverse proxy (e.g. AWS App Runner).
    """

    def test_uses_service_url_when_configured(self) -> None:
        """Configured service_url is returned (trailing slash stripped)."""
        settings = Settings(service_url="https://abc.awsapprunner.com/")
        request = MagicMock()
        request.base_url = "http://0.0.0.0:8000/"

        result = _resolve_base_url(settings, request)

        assert result == "https://abc.awsapprunner.com"

    def test_falls_back_to_request_base_url_when_not_configured(self) -> None:
        """Falls back to request.base_url when service_url is None."""
        settings = Settings(service_url=None, app_env="local")
        request = MagicMock()
        request.base_url = "http://localhost:8000/"

        result = _resolve_base_url(settings, request)

        assert result == "http://localhost:8000"

    def test_logs_warning_in_production_without_service_url(self) -> None:
        """Logs a warning when production env falls back to request.base_url."""
        settings = Settings(service_url=None, app_env="production")
        request = MagicMock()
        request.base_url = "http://internal:8000/"

        with patch("app.src.api.routes.logger") as mock_logger:
            result = _resolve_base_url(settings, request)
            mock_logger.warning.assert_called_once()
            assert "DISPATCH_SERVICE_URL" in mock_logger.warning.call_args[0][0]

        assert result == "http://internal:8000"

    def test_create_run_uses_service_url_for_api_result_url(
        self,
        mock_run_store: MagicMock,
        mock_dispatch_service: AsyncMock,
    ) -> None:
        """POST /agent/run constructs api_result_url from service_url."""
        app.dependency_overrides[get_run_store_dep] = lambda: mock_run_store
        app.dependency_overrides[get_dispatch_service_dep] = (
            lambda: mock_dispatch_service
        )
        app.dependency_overrides[verify_api_key] = lambda: "test-key"

        test_url = "https://prod.awsapprunner.com"
        with patch(
            "app.src.api.routes.get_settings",
            return_value=Settings(service_url=test_url),
        ):
            with TestClient(app) as tc:
                tc.post(
                    "/agent/run",
                    json={
                        "repository": "owner/repo",
                        "branch": "main",
                        "agent_instructions": "Do something",
                        "model": "gpt-4",
                        "role": "implement",
                    },
                )

        # Verify the dispatched api_result_url uses the configured service_url
        call_kwargs = mock_dispatch_service.dispatch_workflow.call_args
        dispatched_url = call_kwargs.kwargs.get("api_result_url") or call_kwargs[1].get(
            "api_result_url"
        )
        assert dispatched_url is not None
        assert dispatched_url.startswith(test_url)
        assert "/agent/run/" in dispatched_url
        assert dispatched_url.endswith("/result")

        app.dependency_overrides.clear()
