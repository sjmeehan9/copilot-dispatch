"""Integration tests for API endpoints."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi.testclient import TestClient

from app.src.api.dependencies import get_dispatch_service_dep, get_run_store_dep
from app.src.auth.api_key import verify_api_key
from app.src.main import app
from app.src.services.run_store import RunStore


@pytest.fixture(autouse=True)
def mock_env_vars():
    """Mock environment variables required for tests."""
    os.environ["GITHUB_PAT"] = "test-pat"
    yield
    del os.environ["GITHUB_PAT"]


@pytest.fixture(scope="module")
def dynamodb_client():
    """Provide a DynamoDB client connected to local instance."""
    import socket

    # Check if DynamoDB Local is running
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(("localhost", 8000))
    sock.close()

    if result != 0:
        pytest.skip("DynamoDB Local is not running on port 8000")

    return boto3.client(
        "dynamodb",
        endpoint_url="http://localhost:8000",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture(scope="module")
def dynamodb_resource():
    """Provide a DynamoDB resource connected to local instance."""
    return boto3.resource(
        "dynamodb",
        endpoint_url="http://localhost:8000",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture
def test_table(dynamodb_client, dynamodb_resource):
    """Create a temporary DynamoDB table for testing."""
    table_name = f"dispatch-runs-test-{uuid.uuid4()}"

    try:
        table = dynamodb_resource.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName=table_name)
        yield table_name
    finally:
        try:
            dynamodb_client.delete_table(TableName=table_name)
        except Exception:
            pass


@pytest.fixture
def run_store(dynamodb_resource, test_table):
    """Provide a RunStore instance connected to the test table."""
    return RunStore(dynamodb_resource, test_table)


@pytest.fixture
def mock_dispatch_service():
    """Mock DispatchService to avoid real GitHub calls."""
    service = AsyncMock()
    service.dispatch_workflow.return_value = None
    return service


@pytest.fixture
def client(run_store, mock_dispatch_service):
    """Test client with real RunStore and mocked DispatchService."""
    app.dependency_overrides[get_run_store_dep] = lambda: run_store
    app.dependency_overrides[get_dispatch_service_dep] = lambda: mock_dispatch_service
    app.dependency_overrides[verify_api_key] = lambda: "test-api-key"

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_full_run_lifecycle(client, run_store):
    """Test the full lifecycle of a run from creation to retrieval."""
    # 1. Create a run
    payload = {
        "repository": "owner/repo",
        "branch": "main",
        "agent_instructions": "Do something",
        "model": "claude-3-5-sonnet-20241022",
        "role": "implement",
    }

    create_response = client.post("/agent/run", json=payload)
    assert create_response.status_code == 202

    data = create_response.json()
    run_id = data["run_id"]
    assert data["status"] == "dispatched"

    # 2. Retrieve the run
    get_response = client.get(f"/agent/run/{run_id}")
    assert get_response.status_code == 200

    run_data = get_response.json()
    assert run_data["run_id"] == run_id
    assert run_data["status"] == "dispatched"
    assert run_data["repository"] == "owner/repo"
    assert run_data["role"] == "implement"
    assert run_data["result"] is None
    assert run_data["error"] is None


def test_get_nonexistent_run(client):
    """Test retrieving a run that doesn't exist."""
    response = client.get("/agent/run/nonexistent-id")
    assert response.status_code == 404
    assert response.json()["error_code"] == "RUN_NOT_FOUND"
