"""Integration tests for the DynamoDB run store service."""

import datetime
import uuid
from typing import Any, Generator

import boto3
import pytest
from botocore.exceptions import ClientError

from app.src.api.models import AgentRunRequest
from app.src.exceptions import ConflictError, RunNotFoundError
from app.src.services.run_store import RunStore

# Use the local DynamoDB endpoint for integration tests
DYNAMODB_ENDPOINT = "http://localhost:8100"


@pytest.fixture(scope="session")
def dynamodb_resource() -> Any:
    """Provide a boto3 DynamoDB resource connected to local DynamoDB."""
    try:
        resource = boto3.resource(
            "dynamodb",
            endpoint_url=DYNAMODB_ENDPOINT,
            region_name="us-east-1",
            aws_access_key_id="dummy",
            aws_secret_access_key="dummy",
        )
        # Test connection
        list(resource.tables.limit(1))
        return resource
    except Exception as e:
        pytest.skip(f"DynamoDB Local is not available at {DYNAMODB_ENDPOINT}: {e}")


@pytest.fixture
def test_table(dynamodb_resource: Any) -> Generator[Any, None, None]:
    """Create a temporary DynamoDB table for testing."""
    table_name = f"dispatch-runs-test-{uuid.uuid4().hex[:8]}"

    table = dynamodb_resource.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()

    yield table

    table.delete()
    table.wait_until_not_exists()


@pytest.fixture
def run_store(dynamodb_resource: Any, test_table: Any) -> RunStore:
    """Provide a RunStore instance connected to the test table."""
    return RunStore(dynamodb_resource, test_table.name)


@pytest.fixture
def valid_request() -> AgentRunRequest:
    return AgentRunRequest(
        repository="owner/repo",
        branch="main",
        agent_instructions="Do something",
        model="claude-3-5-sonnet",
        role="implement",
        timeout_minutes=30,
    )


def test_full_lifecycle(run_store: RunStore, valid_request: AgentRunRequest):
    run_id = f"run-{uuid.uuid4().hex}"

    # 1. Create run
    created = run_store.create_run(run_id, valid_request)
    assert created["run_id"] == run_id
    assert created["status"] == "dispatched"

    # 2. Get run
    retrieved = run_store.get_run(run_id)
    assert retrieved["run_id"] == run_id
    assert retrieved["status"] == "dispatched"

    # 3. Update status
    updated = run_store.update_run_status(run_id, "running")
    assert updated["status"] == "running"

    # 4. Update result
    result_payload = {"pr_url": "https://github.com/owner/repo/pull/1"}
    final = run_store.update_run_result(run_id, "success", result=result_payload)
    assert final["status"] == "success"
    assert final["result"] == result_payload

    # 5. Get final state
    final_retrieved = run_store.get_run(run_id)
    assert final_retrieved["status"] == "success"
    assert final_retrieved["result"] == result_payload


def test_stale_run_detection(
    run_store: RunStore, test_table: Any, valid_request: AgentRunRequest
):
    run_id = f"run-{uuid.uuid4().hex}"

    # Create run
    run_store.create_run(run_id, valid_request)

    # Manually update created_at to be in the past (stale)
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=45)
    test_table.update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET created_at = :past",
        ExpressionAttributeValues={":past": past.isoformat()},
    )

    # Get run should trigger stale detection
    retrieved = run_store.get_run(run_id)

    assert retrieved["status"] == "timeout"
    assert retrieved["error"]["error_code"] == "STALE_RUN_TIMEOUT"


def test_idempotency_guard(run_store: RunStore, valid_request: AgentRunRequest):
    run_id = f"run-{uuid.uuid4().hex}"

    # Create run
    run_store.create_run(run_id, valid_request)

    # Update to terminal state
    run_store.update_run_result(run_id, "success", result={"foo": "bar"})

    # Attempt to update again
    with pytest.raises(ConflictError):
        run_store.update_run_result(run_id, "failure", error={"error_code": "ERR"})
