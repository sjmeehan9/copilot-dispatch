"""Unit tests for result ingestion endpoint."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.src.api.dependencies import get_run_store_dep, get_webhook_service_dep
from app.src.api.routes import _secrets_provider
from app.src.auth.hmac_auth import generate_hmac_signature
from app.src.exceptions import RunNotFoundError
from app.src.main import app


@pytest.fixture(autouse=True)
def test_env() -> None:
    """Set required environment variables for tests."""
    os.environ["GITHUB_PAT"] = "test-pat"
    os.environ["DISPATCH_WEBHOOK_SECRET"] = "test-webhook-secret"
    _secrets_provider.clear_cache()
    yield
    _secrets_provider.clear_cache()
    os.environ.pop("GITHUB_PAT", None)
    os.environ.pop("DISPATCH_WEBHOOK_SECRET", None)


@pytest.fixture
def mock_run_store() -> MagicMock:
    """Provide a mocked RunStore dependency."""
    return MagicMock()


@pytest.fixture
def mock_webhook_service() -> AsyncMock:
    """Provide a mocked WebhookService dependency."""
    service = AsyncMock()
    service.deliver = AsyncMock(return_value=True)
    return service


@pytest.fixture
def client(mock_run_store: MagicMock, mock_webhook_service: AsyncMock) -> TestClient:
    """Create a test client with dependency overrides."""
    app.dependency_overrides[get_run_store_dep] = lambda: mock_run_store
    app.dependency_overrides[get_webhook_service_dep] = lambda: mock_webhook_service

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _post_signed_result(client: TestClient, run_id: str, payload: dict) -> any:
    """Post a signed result payload to the ingestion endpoint."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = generate_hmac_signature(body, "test-webhook-secret")
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": f"sha256={signature}",
    }
    return client.post(f"/agent/run/{run_id}/result", data=body, headers=headers)


def test_ingest_running_status_updates_state_no_webhook(
    client: TestClient,
    mock_run_store: MagicMock,
    mock_webhook_service: AsyncMock,
) -> None:
    """Running status updates only run state and does not trigger webhook."""
    mock_run_store.update_run_status.return_value = {
        "run_id": "run-1",
        "status": "running",
    }

    payload = {"run_id": "run-1", "status": "running"}
    response = _post_signed_result(client, "run-1", payload)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "webhook_delivered": False}
    mock_run_store.update_run_status.assert_called_once_with("run-1", "running")
    mock_run_store.update_run_result.assert_not_called()
    mock_webhook_service.deliver.assert_not_called()


def test_ingest_success_result_triggers_webhook(
    client: TestClient,
    mock_run_store: MagicMock,
    mock_webhook_service: AsyncMock,
) -> None:
    """Success payload updates run result and triggers webhook delivery."""
    mock_run_store.get_run.side_effect = [
        {"run_id": "run-2", "status": "running"},
        {
            "run_id": "run-2",
            "status": "success",
            "callback_url": "https://example.com/callback",
            "result": {"pr_url": "https://github.com/o/r/pull/1"},
            "error": None,
        },
    ]
    mock_run_store.update_run_result.return_value = {
        "run_id": "run-2",
        "status": "success",
    }

    payload = {
        "run_id": "run-2",
        "status": "success",
        "role": "implement",
        "model_used": "gpt-5",
        "duration_seconds": 42,
        "pr_url": "https://github.com/o/r/pull/1",
        "pr_number": 1,
        "branch": "feature/run-2",
        "commits": [{"sha": "abc", "message": "feat"}],
        "files_changed": ["app.py"],
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "security_findings": [],
        "session_summary": "done",
    }

    response = _post_signed_result(client, "run-2", payload)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "webhook_delivered": True}
    mock_run_store.update_run_result.assert_called_once()
    mock_webhook_service.deliver.assert_called_once()


def test_ingest_error_only_failure_triggers_webhook(
    client: TestClient,
    mock_run_store: MagicMock,
    mock_webhook_service: AsyncMock,
) -> None:
    """Failure payload with only error data is persisted and forwarded."""
    mock_run_store.get_run.side_effect = [
        {"run_id": "run-3", "status": "running"},
        {
            "run_id": "run-3",
            "status": "failure",
            "callback_url": "https://example.com/callback",
            "result": None,
            "error": {
                "error_code": "AGENT_CRASH",
                "error_message": "Crashed",
                "error_details": {"phase": "agent_execution"},
            },
        },
    ]

    payload = {
        "run_id": "run-3",
        "status": "failure",
        "error": {
            "error_code": "AGENT_CRASH",
            "error_message": "Crashed",
            "error_details": {"phase": "agent_execution"},
        },
    }

    response = _post_signed_result(client, "run-3", payload)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "webhook_delivered": True}
    _, kwargs = mock_run_store.update_run_result.call_args
    assert kwargs["status"] == "failure"
    assert kwargs["result"] is None
    assert kwargs["error"]["error_code"] == "AGENT_CRASH"
    mock_webhook_service.deliver.assert_called_once()


def test_ingest_invalid_signature_returns_401(client: TestClient) -> None:
    """Invalid signature is rejected."""
    payload = {"run_id": "run-4", "status": "running"}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": "sha256=bad-signature",
    }

    response = client.post("/agent/run/run-4/result", data=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_ERROR"


def test_ingest_missing_signature_returns_401(client: TestClient) -> None:
    """Missing signature header is rejected."""
    payload = {"run_id": "run-5", "status": "running"}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    response = client.post(
        "/agent/run/run-5/result",
        data=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_ERROR"


def test_ingest_unknown_run_returns_404(
    client: TestClient, mock_run_store: MagicMock
) -> None:
    """Unknown run_id returns not found."""
    mock_run_store.update_run_status.side_effect = RunNotFoundError(
        error_message="Run run-6 not found"
    )

    payload = {"run_id": "run-6", "status": "running"}
    response = _post_signed_result(client, "run-6", payload)

    assert response.status_code == 404
    assert response.json()["error_code"] == "RUN_NOT_FOUND"


def test_duplicate_terminal_update_returns_409(
    client: TestClient,
    mock_run_store: MagicMock,
) -> None:
    """Submitting a terminal result for an already terminal run returns conflict."""
    mock_run_store.get_run.return_value = {"run_id": "run-7", "status": "success"}

    payload = {
        "run_id": "run-7",
        "status": "success",
        "role": "implement",
        "session_summary": "done",
        "pr_url": "https://github.com/o/r/pull/1",
        "pr_number": 1,
        "branch": "feature/run-7",
        "commits": [{"sha": "abc", "message": "feat"}],
        "files_changed": ["app.py"],
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "security_findings": [],
    }

    response = _post_signed_result(client, "run-7", payload)

    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"


def test_run_id_mismatch_returns_400(client: TestClient) -> None:
    """Run ID mismatch between URL and payload is rejected."""
    payload = {"run_id": "different-run", "status": "running"}

    response = _post_signed_result(client, "run-8", payload)

    assert response.status_code == 400
    assert response.json()["error_code"] == "VALIDATION_ERROR"


def test_terminal_result_without_callback_skips_webhook(
    client: TestClient,
    mock_run_store: MagicMock,
    mock_webhook_service: AsyncMock,
) -> None:
    """Terminal updates do not forward webhook when callback_url is absent."""
    mock_run_store.get_run.side_effect = [
        {"run_id": "run-9", "status": "running"},
        {"run_id": "run-9", "status": "success", "callback_url": None},
    ]

    payload = {
        "run_id": "run-9",
        "status": "success",
        "role": "implement",
        "session_summary": "done",
        "pr_url": "https://github.com/o/r/pull/1",
        "pr_number": 1,
        "branch": "feature/run-9",
        "commits": [{"sha": "abc", "message": "feat"}],
        "files_changed": ["app.py"],
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "security_findings": [],
    }

    response = _post_signed_result(client, "run-9", payload)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "webhook_delivered": False}
    mock_webhook_service.deliver.assert_not_called()


def test_webhook_failure_does_not_fail_endpoint(
    client: TestClient,
    mock_run_store: MagicMock,
    mock_webhook_service: AsyncMock,
) -> None:
    """Webhook delivery failure remains non-fatal for ingestion endpoint."""
    mock_run_store.get_run.side_effect = [
        {"run_id": "run-10", "status": "running"},
        {
            "run_id": "run-10",
            "status": "failure",
            "callback_url": "https://example.com/callback",
            "error": {"error_code": "X", "error_message": "Y"},
        },
    ]
    mock_webhook_service.deliver.return_value = False

    payload = {
        "run_id": "run-10",
        "status": "failure",
        "error": {"error_code": "AGENT_FAILURE", "error_message": "failed"},
    }

    response = _post_signed_result(client, "run-10", payload)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "webhook_delivered": False}
    mock_webhook_service.deliver.assert_called_once()
