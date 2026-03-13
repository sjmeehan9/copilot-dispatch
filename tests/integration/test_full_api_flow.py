"""Comprehensive integration tests for the full API flow.

These tests run against DynamoDB Local and validate the end-to-end API lifecycle
for run creation, result ingestion, status polling, and callback forwarding.
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import boto3
import pytest
from httpx import ASGITransport, AsyncClient

from app.src.api.dependencies import get_dispatch_service_dep, get_webhook_service_dep
from app.src.auth.api_key import verify_api_key
from app.src.auth.hmac_auth import generate_hmac_signature
from app.src.config import get_settings
from app.src.main import app


def _dynamodb_local_available() -> bool:
    """Check whether DynamoDB Local is reachable.

    Returns:
        True when DynamoDB Local is reachable on localhost:8100, else False.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return sock.connect_ex(("localhost", 8100)) == 0
    finally:
        sock.close()


@pytest.fixture(scope="module")
def dynamodb_resource() -> Any:
    """Provide a boto3 DynamoDB resource connected to DynamoDB Local.

    Returns:
        Configured DynamoDB resource for local testing.
    """
    if not _dynamodb_local_available():
        pytest.skip("DynamoDB Local is not running on port 8100")

    return boto3.resource(
        "dynamodb",
        endpoint_url="http://localhost:8100",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture(scope="module")
def dynamodb_client() -> Any:
    """Provide a boto3 DynamoDB client connected to DynamoDB Local.

    Returns:
        Configured DynamoDB client for local testing.
    """
    if not _dynamodb_local_available():
        pytest.skip("DynamoDB Local is not running on port 8100")

    return boto3.client(
        "dynamodb",
        endpoint_url="http://localhost:8100",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture(scope="module")
def integration_table_name(dynamodb_resource: Any, dynamodb_client: Any) -> str:
    """Create and tear down an isolated integration DynamoDB table.

    Args:
        dynamodb_resource: Local DynamoDB resource.
        dynamodb_client: Local DynamoDB client.

    Yields:
        The generated table name.
    """
    table_name = f"dispatch-runs-integration-{uuid.uuid4()}"
    table = dynamodb_resource.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=table_name)

    try:
        yield table_name
    finally:
        try:
            dynamodb_client.delete_table(TableName=table_name)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def integration_env(
    monkeypatch: pytest.MonkeyPatch, integration_table_name: str
) -> None:
    """Configure environment for full-flow integration tests.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        integration_table_name: Generated DynamoDB table name.
    """
    monkeypatch.setenv("DISPATCH_APP_ENV", "test")
    monkeypatch.setenv("DISPATCH_DYNAMODB_ENDPOINT_URL", "http://localhost:8100")
    monkeypatch.setenv("DISPATCH_DYNAMODB_TABLE_NAME", integration_table_name)
    monkeypatch.setenv("DISPATCH_WEBHOOK_SECRET", "integration-webhook-secret")
    monkeypatch.setenv("GITHUB_PAT", "test-pat")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_dispatch_service() -> AsyncMock:
    """Provide a no-op async dispatcher mock.

    Returns:
        AsyncMock dispatch service.
    """
    service = AsyncMock()
    service.dispatch_workflow.return_value = None
    return service


class RecordingWebhookService:
    """Record outbound callback delivery attempts for assertions."""

    def __init__(self, deliver_result: bool = True) -> None:
        """Initialise a recording webhook service.

        Args:
            deliver_result: Boolean returned by deliver to simulate success/failure.
        """
        self.deliver_result = deliver_result
        self.calls: list[dict[str, Any]] = []

    async def deliver(self, url: str, payload: dict[str, Any], secret: str) -> bool:
        """Record a callback delivery invocation.

        Args:
            url: Callback URL.
            payload: Webhook payload body.
            secret: Shared signing secret.

        Returns:
            Configured delivery result.
        """
        self.calls.append({"url": url, "payload": payload, "secret": secret})
        return self.deliver_result


@pytest.fixture
async def async_client(
    mock_dispatch_service: AsyncMock,
) -> AsyncClient:
    """Provide async test client with dependency overrides.

    Args:
        mock_dispatch_service: Mocked dispatch service.

    Yields:
        Async HTTP client for ASGI app calls.
    """
    webhook_service = RecordingWebhookService(deliver_result=True)

    app.dependency_overrides[verify_api_key] = lambda: "test-api-key"
    app.dependency_overrides[get_dispatch_service_dep] = lambda: mock_dispatch_service
    app.dependency_overrides[get_webhook_service_dep] = lambda: webhook_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        client.recording_webhook_service = webhook_service  # type: ignore[attr-defined]
        yield client

    app.dependency_overrides.clear()


def _signed_headers(
    payload: dict[str, Any], secret: str = "integration-webhook-secret"
) -> dict[str, str]:
    """Build signed headers for result ingestion requests.

    Args:
        payload: JSON payload to sign.
        secret: HMAC secret.

    Returns:
        HTTP headers containing content type and HMAC signature.
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = generate_hmac_signature(body, secret)
    return {
        "Content-Type": "application/json",
        "X-Webhook-Signature": f"sha256={signature}",
    }


def _set_callback_url(
    table_name: str,
    run_id: str,
    callback_url: str,
) -> None:
    """Set callback_url directly on a run record in DynamoDB Local.

    Args:
        table_name: Integration table name.
        run_id: Run identifier.
        callback_url: Callback URL string to persist.
    """
    boto3.resource(
        "dynamodb",
        endpoint_url="http://localhost:8100",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    ).Table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET callback_url = :callback_url",
        ExpressionAttributeValues={":callback_url": callback_url},
    )


@pytest.mark.asyncio
async def test_full_happy_path_implement(
    async_client: AsyncClient,
    integration_table_name: str,
) -> None:
    """Validate implement run lifecycle from create to terminal success with callback."""
    create_payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Implement feature X",
        "model": "gpt-5",
        "role": "implement",
    }

    create_response = await async_client.post("/agent/run", json=create_payload)
    assert create_response.status_code == 202
    run_id = create_response.json()["run_id"]
    _set_callback_url(
        integration_table_name, run_id, "https://callback.example.com/implement"
    )

    initial_status = await async_client.get(f"/agent/run/{run_id}")
    assert initial_status.status_code == 200
    assert initial_status.json()["status"] == "dispatched"

    running_payload = {"run_id": run_id, "status": "running"}
    running_response = await async_client.post(
        f"/agent/run/{run_id}/result",
        content=json.dumps(running_payload, separators=(",", ":")).encode("utf-8"),
        headers=_signed_headers(running_payload),
    )
    assert running_response.status_code == 200
    assert running_response.json() == {"status": "accepted", "webhook_delivered": False}

    running_status = await async_client.get(f"/agent/run/{run_id}")
    assert running_status.status_code == 200
    assert running_status.json()["status"] == "running"

    success_payload = {
        "run_id": run_id,
        "status": "success",
        "role": "implement",
        "model_used": "gpt-5",
        "duration_seconds": 18,
        "pr_url": "https://github.com/owner/repo/pull/10",
        "pr_number": 10,
        "branch": "feature/run-implement",
        "commits": [{"sha": "abc123", "message": "feat: implement X"}],
        "files_changed": ["app/src/example.py"],
        "test_results": {"passed": 5, "failed": 0, "skipped": 0},
        "security_findings": [],
        "session_summary": "Implemented requested change",
    }
    success_response = await async_client.post(
        f"/agent/run/{run_id}/result",
        content=json.dumps(success_payload, separators=(",", ":")).encode("utf-8"),
        headers=_signed_headers(success_payload),
    )
    assert success_response.status_code == 200
    assert success_response.json() == {"status": "accepted", "webhook_delivered": True}

    final_status = await async_client.get(f"/agent/run/{run_id}")
    assert final_status.status_code == 200
    final_body = final_status.json()
    assert final_body["status"] == "success"
    assert final_body["result"]["pr_url"] == "https://github.com/owner/repo/pull/10"
    assert final_body["error"] is None

    recorder: RecordingWebhookService = async_client.recording_webhook_service  # type: ignore[attr-defined]
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["url"] == "https://callback.example.com/implement"
    assert recorder.calls[0]["payload"]["run_id"] == run_id


@pytest.mark.asyncio
async def test_full_happy_path_review(async_client: AsyncClient) -> None:
    """Validate review flow lifecycle with role-specific terminal payload."""
    create_payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Review PR 42",
        "model": "gpt-5",
        "role": "review",
        "pr_number": 42,
    }
    create_response = await async_client.post("/agent/run", json=create_payload)
    assert create_response.status_code == 202
    run_id = create_response.json()["run_id"]

    review_result_payload = {
        "run_id": run_id,
        "status": "success",
        "role": "review",
        "model_used": "gpt-5",
        "duration_seconds": 27,
        "review_url": "https://github.com/owner/repo/pull/42#pullrequestreview-1",
        "assessment": "approve",
        "review_comments": [{"file_path": "app/src/main.py", "body": "Looks good"}],
        "suggested_changes": [],
        "security_concerns": [],
        "pr_approved": True,
        "session_summary": "Reviewed and approved",
    }
    review_result_response = await async_client.post(
        f"/agent/run/{run_id}/result",
        content=json.dumps(review_result_payload, separators=(",", ":")).encode(
            "utf-8"
        ),
        headers=_signed_headers(review_result_payload),
    )
    assert review_result_response.status_code == 200

    run_status = await async_client.get(f"/agent/run/{run_id}")
    assert run_status.status_code == 200
    body = run_status.json()
    assert body["status"] == "success"
    assert body["role"] == "review"
    assert body["result"]["assessment"] == "approve"


@pytest.mark.asyncio
async def test_error_only_failure_includes_error_and_webhook(
    async_client: AsyncClient,
    integration_table_name: str,
) -> None:
    """Validate error-only failure updates error field and forwards callback payload."""
    create_payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Do something risky",
        "model": "gpt-5",
        "role": "implement",
    }
    create_response = await async_client.post("/agent/run", json=create_payload)
    run_id = create_response.json()["run_id"]
    _set_callback_url(
        integration_table_name, run_id, "https://callback.example.com/failure"
    )

    failure_payload = {
        "run_id": run_id,
        "status": "failure",
        "error": {
            "error_code": "AGENT_CRASH",
            "error_message": "SDK session terminated",
            "error_details": {"phase": "agent_execution"},
        },
    }
    result_response = await async_client.post(
        f"/agent/run/{run_id}/result",
        content=json.dumps(failure_payload, separators=(",", ":")).encode("utf-8"),
        headers=_signed_headers(failure_payload),
    )
    assert result_response.status_code == 200
    assert result_response.json() == {"status": "accepted", "webhook_delivered": True}

    status_response = await async_client.get(f"/agent/run/{run_id}")
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["status"] == "failure"
    assert body["result"] is None
    assert body["error"]["error_code"] == "AGENT_CRASH"

    recorder: RecordingWebhookService = async_client.recording_webhook_service  # type: ignore[attr-defined]
    assert any(
        call["payload"].get("error", {}).get("error_code") == "AGENT_CRASH"
        for call in recorder.calls
    )


@pytest.mark.asyncio
async def test_failure_payload_with_metadata_keeps_result_null(
    async_client: AsyncClient,
) -> None:
    """Validate failure payload metadata does not populate success-only result."""
    create_payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Trigger a runtime failure",
        "model": "gpt-5",
        "role": "implement",
    }
    create_response = await async_client.post("/agent/run", json=create_payload)
    assert create_response.status_code == 202
    run_id = create_response.json()["run_id"]

    failure_payload = {
        "run_id": run_id,
        "status": "failure",
        "role": "implement",
        "model_used": "gpt-5",
        "duration_seconds": 2,
        "session_summary": "Runtime failed before completion",
        "error": {
            "error_code": "AGENT_RUNTIME_ERROR",
            "error_message": "Failed to list models: 400",
            "error_details": {"phase": "agent_execution"},
        },
    }
    result_response = await async_client.post(
        f"/agent/run/{run_id}/result",
        content=json.dumps(failure_payload, separators=(",", ":")).encode("utf-8"),
        headers=_signed_headers(failure_payload),
    )
    assert result_response.status_code == 200

    status_response = await async_client.get(f"/agent/run/{run_id}")
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["status"] == "failure"
    assert body["result"] is None
    assert body["error"]["error_code"] == "AGENT_RUNTIME_ERROR"


@pytest.mark.asyncio
async def test_stale_run_detection(
    async_client: AsyncClient, integration_table_name: str
) -> None:
    """Validate stale run auto-transition to timeout when overdue."""
    create_payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Wait forever",
        "model": "gpt-5",
        "role": "implement",
        "timeout_minutes": 1,
    }
    create_response = await async_client.post("/agent/run", json=create_payload)
    assert create_response.status_code == 202
    run_id = create_response.json()["run_id"]

    table = boto3.resource(
        "dynamodb",
        endpoint_url="http://localhost:8100",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    ).Table(integration_table_name)
    stale_created_at = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    table.update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET created_at = :created_at",
        ExpressionAttributeValues={":created_at": stale_created_at},
    )

    stale_response = await async_client.get(f"/agent/run/{run_id}")
    assert stale_response.status_code == 200
    stale_body = stale_response.json()
    assert stale_body["status"] == "timeout"
    assert stale_body["error"]["error_code"] == "STALE_RUN_TIMEOUT"


@pytest.mark.asyncio
async def test_idempotency_duplicate_result_returns_conflict(
    async_client: AsyncClient,
) -> None:
    """Validate duplicate terminal submissions return 409 Conflict."""
    create_payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Idempotency test",
        "model": "gpt-5",
        "role": "implement",
    }
    create_response = await async_client.post("/agent/run", json=create_payload)
    run_id = create_response.json()["run_id"]

    success_payload = {
        "run_id": run_id,
        "status": "success",
        "role": "implement",
        "pr_url": "https://github.com/owner/repo/pull/11",
        "pr_number": 11,
        "branch": "feature/idempotency",
        "commits": [{"sha": "def456", "message": "feat: done"}],
        "files_changed": ["app/src/main.py"],
        "test_results": {"passed": 2, "failed": 0, "skipped": 0},
        "security_findings": [],
        "session_summary": "Done",
    }
    first_result = await async_client.post(
        f"/agent/run/{run_id}/result",
        content=json.dumps(success_payload, separators=(",", ":")).encode("utf-8"),
        headers=_signed_headers(success_payload),
    )
    assert first_result.status_code == 200

    duplicate_result = await async_client.post(
        f"/agent/run/{run_id}/result",
        content=json.dumps(success_payload, separators=(",", ":")).encode("utf-8"),
        headers=_signed_headers(success_payload),
    )
    assert duplicate_result.status_code == 409
    assert duplicate_result.json()["error_code"] == "CONFLICT"


@pytest.mark.asyncio
async def test_invalid_hmac_signature_rejected(async_client: AsyncClient) -> None:
    """Validate invalid result signature is rejected with 401."""
    create_payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Bad signature path",
        "model": "gpt-5",
        "role": "implement",
    }
    create_response = await async_client.post("/agent/run", json=create_payload)
    run_id = create_response.json()["run_id"]

    body = json.dumps(
        {"run_id": run_id, "status": "running"}, separators=(",", ":")
    ).encode("utf-8")
    response = await async_client.post(
        f"/agent/run/{run_id}/result",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": "sha256=invalid",
        },
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected_status",
    [
        (
            {
                "branch": "main",
                "agent_instructions": "Missing repository",
                "model": "gpt-5",
                "role": "implement",
            },
            422,
        ),
        (
            {
                "repository": "owner/repo",
                "branch": "main",
                "agent_instructions": "Invalid role",
                "model": "gpt-5",
                "role": "invalid",
            },
            422,
        ),
        (
            {
                "repository": "owner/repo",
                "branch": "main",
                "agent_instructions": "Missing review pr",
                "model": "gpt-5",
                "role": "review",
            },
            422,
        ),
        (
            {
                "repository": "owner/repo",
                "branch": "main",
                "agent_instructions": "Timeout too high",
                "model": "gpt-5",
                "role": "implement",
                "timeout_minutes": 61,
            },
            422,
        ),
    ],
)
async def test_invalid_create_payload_variants(
    async_client: AsyncClient,
    payload: dict[str, Any],
    expected_status: int,
) -> None:
    """Validate invalid create payload variants return validation errors."""
    response = await async_client.post("/agent/run", json=payload)
    assert response.status_code == expected_status
