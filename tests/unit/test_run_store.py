"""Unit tests for the DynamoDB run store service."""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.src.api.models import AgentRunRequest
from app.src.exceptions import ConflictError, ExternalServiceError, RunNotFoundError
from app.src.services.run_store import RunStore, _decimal_to_native


@pytest.fixture
def mock_table():
    return MagicMock()


@pytest.fixture
def mock_dynamodb(mock_table):
    dynamodb = MagicMock()
    dynamodb.Table.return_value = mock_table
    return dynamodb


@pytest.fixture
def run_store(mock_dynamodb):
    return RunStore(mock_dynamodb, "test-table")


@pytest.fixture
def valid_request():
    return AgentRunRequest(
        repository="owner/repo",
        branch="main",
        agent_instructions="Do something",
        model="claude-3-5-sonnet",
        role="implement",
        timeout_minutes=30,
    )


def test_decimal_to_native():
    assert _decimal_to_native(Decimal("10.5")) == 10.5
    assert _decimal_to_native(Decimal("10")) == 10
    assert _decimal_to_native({"a": Decimal("1")}) == {"a": 1}
    assert _decimal_to_native([Decimal("2.5")]) == [2.5]
    assert _decimal_to_native("string") == "string"


def test_create_run_success(run_store, mock_table, valid_request):
    run_id = "test-run-123"

    with patch("app.src.services.run_store.get_settings") as mock_settings:
        mock_settings.return_value.default_callback_url = "https://example.com/cb"

        result = run_store.create_run(run_id, valid_request)

        assert result["run_id"] == run_id
        assert result["repository"] == "owner/repo"
        assert result["status"] == "dispatched"
        assert result["callback_url"] == "https://example.com/cb"
        assert "created_at" in result
        assert "updated_at" in result
        assert "ttl" in result

        mock_table.put_item.assert_called_once()
        call_kwargs = mock_table.put_item.call_args.kwargs
        assert call_kwargs["Item"] == result
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(run_id)"


def test_create_run_optional_fields(run_store, mock_table, valid_request):
    run_id = "test-run-123"
    valid_request.system_instructions = "System instructions"
    valid_request.pr_number = 42
    valid_request.skill_paths = ["path/to/skill"]
    valid_request.agent_paths = [".github/agents/security.agent.md"]

    with patch("app.src.services.run_store.get_settings") as mock_settings:
        mock_settings.return_value.default_callback_url = "https://example.com/cb"

        result = run_store.create_run(run_id, valid_request)

        assert result["system_instructions"] == "System instructions"
        assert result["pr_number"] == 42
        assert result["skill_paths"] == ["path/to/skill"]
        assert result["agent_paths"] == [".github/agents/security.agent.md"]


def test_create_run_with_agent_paths(run_store, mock_table):
    """Run records persist agent_paths when provided."""
    run_id = "test-run-123"
    request = AgentRunRequest(
        repository="owner/repo",
        branch="main",
        agent_instructions="Do something",
        model="claude-3-5-sonnet",
        role="implement",
        timeout_minutes=30,
        agent_paths=[".github/agents/security.agent.md"],
    )

    with patch("app.src.services.run_store.get_settings") as mock_settings:
        mock_settings.return_value.default_callback_url = "https://example.com/cb"
        result = run_store.create_run(run_id, request)

    assert result["agent_paths"] == [".github/agents/security.agent.md"]


def test_create_run_without_agent_paths(run_store, mock_table):
    """Run records omit agent_paths when not provided."""
    run_id = "test-run-123"
    request = AgentRunRequest(
        repository="owner/repo",
        branch="main",
        agent_instructions="Do something",
        model="claude-3-5-sonnet",
        role="implement",
        timeout_minutes=30,
    )

    with patch("app.src.services.run_store.get_settings") as mock_settings:
        mock_settings.return_value.default_callback_url = "https://example.com/cb"
        result = run_store.create_run(run_id, request)

    assert "agent_paths" not in result


def test_create_run_conflict(run_store, mock_table, valid_request):
    run_id = "test-run-123"

    error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    mock_table.put_item.side_effect = ClientError(error_response, "PutItem")

    with pytest.raises(ConflictError) as exc_info:
        run_store.create_run(run_id, valid_request)

    assert "already exists" in exc_info.value.error_message


def test_create_run_dynamodb_error(run_store, mock_table, valid_request):
    run_id = "test-run-123"

    error_response = {"Error": {"Code": "InternalServerError"}}
    mock_table.put_item.side_effect = ClientError(error_response, "PutItem")

    with pytest.raises(ExternalServiceError):
        run_store.create_run(run_id, valid_request)


def test_get_run_success(run_store, mock_table):
    run_id = "test-run-123"
    mock_item = {
        "run_id": run_id,
        "status": "success",
        "timeout_minutes": Decimal("30"),
    }
    mock_table.get_item.return_value = {"Item": mock_item}

    result = run_store.get_run(run_id)

    assert result["run_id"] == run_id
    assert result["status"] == "success"
    assert result["timeout_minutes"] == 30  # Decimal converted to int
    mock_table.get_item.assert_called_once_with(Key={"run_id": run_id})


def test_get_run_not_found(run_store, mock_table):
    run_id = "test-run-123"
    mock_table.get_item.return_value = {}

    with pytest.raises(RunNotFoundError):
        run_store.get_run(run_id)


def test_get_run_dynamodb_error(run_store, mock_table):
    run_id = "test-run-123"
    error_response = {"Error": {"Code": "InternalServerError"}}
    mock_table.get_item.side_effect = ClientError(error_response, "GetItem")

    with pytest.raises(ExternalServiceError):
        run_store.get_run(run_id)


def test_update_run_status_success(run_store, mock_table):
    run_id = "test-run-123"
    mock_table.update_item.return_value = {
        "Attributes": {"run_id": run_id, "status": "running"}
    }

    result = run_store.update_run_status(run_id, "running")

    assert result["status"] == "running"
    mock_table.update_item.assert_called_once()
    call_kwargs = mock_table.update_item.call_args.kwargs
    assert call_kwargs["Key"] == {"run_id": run_id}
    assert call_kwargs["ExpressionAttributeValues"][":status"] == "running"


def test_update_run_status_not_found(run_store, mock_table):
    run_id = "test-run-123"
    error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    mock_table.update_item.side_effect = ClientError(error_response, "UpdateItem")

    with pytest.raises(RunNotFoundError):
        run_store.update_run_status(run_id, "running")


def test_update_run_status_dynamodb_error(run_store, mock_table):
    run_id = "test-run-123"
    error_response = {"Error": {"Code": "InternalServerError"}}
    mock_table.update_item.side_effect = ClientError(error_response, "UpdateItem")

    with pytest.raises(ExternalServiceError):
        run_store.update_run_status(run_id, "running")


def test_update_run_result_success(run_store, mock_table):
    run_id = "test-run-123"
    mock_table.update_item.return_value = {
        "Attributes": {"run_id": run_id, "status": "success", "result": {"foo": "bar"}}
    }

    result = run_store.update_run_result(run_id, "success", result={"foo": "bar"})

    assert result["status"] == "success"
    assert result["result"] == {"foo": "bar"}
    mock_table.update_item.assert_called_once()
    call_kwargs = mock_table.update_item.call_args.kwargs
    assert ":result" in call_kwargs["ExpressionAttributeValues"]
    assert call_kwargs["ExpressionAttributeValues"][":result"] == {"foo": "bar"}


def test_update_run_result_conflict(run_store, mock_table):
    run_id = "test-run-123"
    error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    mock_table.update_item.side_effect = ClientError(error_response, "UpdateItem")

    with pytest.raises(ConflictError):
        run_store.update_run_result(run_id, "success", result={"foo": "bar"})


def test_update_run_result_dynamodb_error(run_store, mock_table):
    run_id = "test-run-123"
    error_response = {"Error": {"Code": "InternalServerError"}}
    mock_table.update_item.side_effect = ClientError(error_response, "UpdateItem")

    with pytest.raises(ExternalServiceError):
        run_store.update_run_result(run_id, "success", result={"foo": "bar"})


def test_update_run_error(run_store, mock_table):
    run_id = "test-run-123"
    mock_table.update_item.return_value = {
        "Attributes": {"run_id": run_id, "status": "failure"}
    }

    result = run_store.update_run_error(
        run_id, "ERR_CODE", "Error message", {"detail": 1}
    )

    assert result["status"] == "failure"
    mock_table.update_item.assert_called_once()
    call_kwargs = mock_table.update_item.call_args.kwargs
    assert call_kwargs["ExpressionAttributeValues"][":status"] == "failure"
    assert call_kwargs["ExpressionAttributeValues"][":error"] == {
        "error_code": "ERR_CODE",
        "error_message": "Error message",
        "error_details": {"detail": 1},
    }


def test_check_stale_run_not_stale(run_store):
    now = datetime.datetime.now(datetime.timezone.utc)
    item = {
        "run_id": "123",
        "status": "running",
        "created_at": now.isoformat(),
        "timeout_minutes": 30,
    }

    result = run_store._check_stale_run(item)
    assert result == item


def test_check_stale_run_is_stale(run_store, mock_table):
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=45)
    item = {
        "run_id": "123",
        "status": "running",
        "created_at": past.isoformat(),
        "timeout_minutes": 30,
    }

    mock_table.update_item.return_value = {
        "Attributes": {
            "run_id": "123",
            "status": "timeout",
            "error": {"error_code": "STALE_RUN_TIMEOUT"},
        }
    }

    result = run_store._check_stale_run(item)

    assert result["status"] == "timeout"
    assert result["error"]["error_code"] == "STALE_RUN_TIMEOUT"
    mock_table.update_item.assert_called_once()


def test_check_stale_run_already_updated(run_store, mock_table):
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=45)
    item = {
        "run_id": "123",
        "status": "running",
        "created_at": past.isoformat(),
        "timeout_minutes": 30,
    }

    error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    mock_table.update_item.side_effect = ClientError(error_response, "UpdateItem")

    mock_table.get_item.return_value = {
        "Item": {
            "run_id": "123",
            "status": "timeout",
            "error": {"error_code": "STALE_RUN_TIMEOUT"},
        }
    }

    result = run_store._check_stale_run(item)

    assert result["status"] == "timeout"
    mock_table.update_item.assert_called_once()
    mock_table.get_item.assert_called_once_with(Key={"run_id": "123"})


def test_check_stale_run_missing_created_at(run_store):
    item = {
        "run_id": "123",
        "status": "running",
        "timeout_minutes": 30,
    }
    result = run_store._check_stale_run(item)
    assert result == item


def test_check_stale_run_invalid_created_at(run_store):
    item = {
        "run_id": "123",
        "status": "running",
        "created_at": "invalid-date",
        "timeout_minutes": 30,
    }
    result = run_store._check_stale_run(item)
    assert result == item


def test_check_stale_run_refetch_fails(run_store, mock_table):
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=45)
    item = {
        "run_id": "123",
        "status": "running",
        "created_at": past.isoformat(),
        "timeout_minutes": 30,
    }

    error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    mock_table.update_item.side_effect = ClientError(error_response, "UpdateItem")

    mock_table.get_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError"}}, "GetItem"
    )

    result = run_store._check_stale_run(item)

    assert result == item
    mock_table.update_item.assert_called_once()
    mock_table.get_item.assert_called_once_with(Key={"run_id": "123"})


def test_get_run_store():
    from app.src.services.run_store import get_run_store

    with patch("app.src.services.run_store.get_settings") as mock_settings:
        mock_settings.return_value.dynamodb_endpoint_url = "http://localhost:8000"
        mock_settings.return_value.dynamodb_table_name = "test-table"

        store = get_run_store()
        assert isinstance(store, RunStore)

    with (
        patch("app.src.services.run_store.get_settings") as mock_settings,
        patch("app.src.services.run_store.boto3") as mock_boto3,
    ):
        mock_settings.return_value.dynamodb_endpoint_url = None
        mock_settings.return_value.dynamodb_table_name = "test-table"
        mock_boto3.resource.return_value = MagicMock()

        store2 = get_run_store()
        assert isinstance(store2, RunStore)
        mock_boto3.resource.assert_called_once_with("dynamodb")
