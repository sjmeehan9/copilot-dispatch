"""Unit tests for the Workflow Dispatch Service."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.src.api.models import AgentRunRequest
from app.src.exceptions import DispatchError, ExternalServiceError
from app.src.services.dispatcher import DispatchService, get_dispatch_service


@pytest.fixture
def dispatch_service():
    """Fixture providing a configured DispatchService instance."""
    return DispatchService(
        github_pat="test-pat",
        github_owner="test-owner",
        github_repo="test-repo",
        workflow_id="test-workflow.yml",
    )


@pytest.fixture
def valid_request():
    """Fixture providing a valid AgentRunRequest."""
    return AgentRunRequest(
        repository="target-owner/target-repo",
        branch="feature-branch",
        agent_instructions="Do something",
        model="gpt-4",
        role="implement",
        timeout_minutes=30,
    )


@pytest.mark.asyncio
async def test_dispatch_workflow_success(dispatch_service, valid_request, caplog):
    """Test successful workflow dispatch."""
    mock_response = MagicMock()
    mock_response.status_code = 204

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        await dispatch_service.dispatch_workflow(
            run_id="run-123",
            request=valid_request,
            callback_url="https://example.com/callback",
            api_result_url="https://api.example.com/result",
        )

        mock_post.assert_called_once()
        call_args = mock_post.call_args

        assert (
            call_args[0][0]
            == "https://api.github.com/repos/test-owner/test-repo/actions/workflows/test-workflow.yml/dispatches"
        )

        payload = call_args[1]["json"]
        assert payload["ref"] == "main"
        assert payload["inputs"]["run_id"] == "run-123"
        assert payload["inputs"]["target_repository"] == "target-owner/target-repo"
        assert payload["inputs"]["target_branch"] == "feature-branch"
        assert payload["inputs"]["role"] == "implement"
        assert payload["inputs"]["agent_instructions"] == "Do something"
        assert payload["inputs"]["model"] == "gpt-4"
        assert payload["inputs"]["timeout_minutes"] == "30"

        config = json.loads(payload["inputs"]["config"])
        assert config["callback_url"] == "https://example.com/callback"
        assert payload["inputs"]["api_result_url"] == "https://api.example.com/result"

        # Verify no secrets in log output
        log_text = caplog.text
        assert "test-pat" not in log_text
        assert "Do something" not in log_text

        # Verify extra fields are logged
        assert len(caplog.records) > 0
        record = caplog.records[0]
        assert record.run_id == "run-123"
        assert record.repository == "target-owner/target-repo"
        assert record.workflow_id == "test-workflow.yml"


@pytest.mark.asyncio
async def test_dispatch_workflow_optional_fields(dispatch_service):
    """Test workflow dispatch with optional fields provided."""
    mock_response = MagicMock()
    mock_response.status_code = 204

    request_with_optional = AgentRunRequest(
        repository="target-owner/target-repo",
        branch="feature-branch",
        agent_instructions="Do something",
        model="gpt-4",
        role="review",
        pr_number=42,
        system_instructions="System prompt",
        skill_paths=["path/to/skill1", "path/to/skill2"],
        agent_paths=[".github/agents/a.agent.md", ".github/agents/b.agent.md"],
        callback_url="https://example.com/req-callback",
        timeout_minutes=45,
    )

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        await dispatch_service.dispatch_workflow(
            run_id="run-123",
            request=request_with_optional,
        )

        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]

        config = json.loads(payload["inputs"]["config"])
        assert config["pr_number"] == 42
        assert config["system_instructions"] == "System prompt"
        assert config["skill_paths"] == ["path/to/skill1", "path/to/skill2"]
        assert config["agent_paths"] == [
            ".github/agents/a.agent.md",
            ".github/agents/b.agent.md",
        ]
        assert config["callback_url"] == "https://example.com/req-callback"


@pytest.mark.asyncio
async def test_dispatch_workflow_includes_agent_paths_in_config(dispatch_service):
    """Dispatch config includes agent_paths when provided on request."""
    mock_response = MagicMock()
    mock_response.status_code = 204

    request = AgentRunRequest(
        repository="target-owner/target-repo",
        branch="feature-branch",
        agent_instructions="Do something",
        model="gpt-4",
        role="implement",
        agent_paths=[".github/agents/security.agent.md"],
    )

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        await dispatch_service.dispatch_workflow(run_id="run-123", request=request)

        payload = mock_post.call_args[1]["json"]
        config = json.loads(payload["inputs"]["config"])
        assert config["agent_paths"] == [".github/agents/security.agent.md"]


@pytest.mark.asyncio
async def test_dispatch_workflow_omits_agent_paths_when_none(
    dispatch_service, valid_request
):
    """Dispatch config omits agent_paths when request.agent_paths is None."""
    mock_response = MagicMock()
    mock_response.status_code = 204

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        await dispatch_service.dispatch_workflow(
            run_id="run-123", request=valid_request
        )

        payload = mock_post.call_args[1]["json"]
        config = json.loads(payload["inputs"].get("config", "{}"))
        assert "agent_paths" not in config


@pytest.mark.asyncio
async def test_dispatch_workflow_401_auth_error(dispatch_service, valid_request):
    """Test handling of 401 Unauthorized response."""
    mock_response = MagicMock()
    mock_response.status_code = 401

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(DispatchError) as exc_info:
            await dispatch_service.dispatch_workflow(
                run_id="run-123", request=valid_request
            )

        assert exc_info.value.status_code == 500
        assert "authentication failed" in exc_info.value.error_message
        assert exc_info.value.error_details["github_status"] == 401


@pytest.mark.asyncio
async def test_dispatch_workflow_403_rate_limit(dispatch_service, valid_request):
    """Test handling of 403 Forbidden response with rate limit headers."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.headers = {"X-RateLimit-Remaining": "0", "Retry-After": "60"}

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(ExternalServiceError) as exc_info:
            await dispatch_service.dispatch_workflow(
                run_id="run-123", request=valid_request
            )

        assert exc_info.value.status_code == 502
        assert "rate limit exceeded" in exc_info.value.error_message
        assert exc_info.value.error_details["retry_after"] == "60"


@pytest.mark.asyncio
async def test_dispatch_workflow_404_not_found(dispatch_service, valid_request):
    """Test handling of 404 Not Found response."""
    mock_response = MagicMock()
    mock_response.status_code = 404

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(DispatchError) as exc_info:
            await dispatch_service.dispatch_workflow(
                run_id="run-123", request=valid_request
            )

        assert "not found" in exc_info.value.error_message
        assert exc_info.value.error_details["github_status"] == 404


@pytest.mark.asyncio
async def test_dispatch_workflow_422_validation_error(dispatch_service, valid_request):
    """Test handling of 422 Unprocessable Entity response."""
    mock_response = MagicMock()
    mock_response.status_code = 422
    error_json = {"message": "Invalid request", "errors": ["field required"]}
    mock_response.json.return_value = error_json

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(DispatchError) as exc_info:
            await dispatch_service.dispatch_workflow(
                run_id="run-123", request=valid_request
            )

        assert "rejected the workflow dispatch payload" in exc_info.value.error_message
        assert exc_info.value.error_details["github_status"] == 422
        assert exc_info.value.error_details["github_error"] == error_json


@pytest.mark.asyncio
async def test_dispatch_workflow_422_validation_error_non_json(
    dispatch_service, valid_request
):
    """Test handling of 422 response with non-JSON body."""
    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.json.side_effect = ValueError("Not JSON")
    mock_response.text = "Plain text error"

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(DispatchError) as exc_info:
            await dispatch_service.dispatch_workflow(
                run_id="run-123", request=valid_request
            )

        assert "rejected the workflow dispatch payload" in exc_info.value.error_message
        assert exc_info.value.error_details["github_status"] == 422
        assert exc_info.value.error_details["github_error"] == "Plain text error"


@pytest.mark.asyncio
async def test_dispatch_workflow_429_rate_limit(dispatch_service, valid_request):
    """Test handling of 429 Too Many Requests response."""
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {"Retry-After": "120"}

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(ExternalServiceError) as exc_info:
            await dispatch_service.dispatch_workflow(
                run_id="run-123", request=valid_request
            )

        assert "rate limit exceeded" in exc_info.value.error_message
        assert exc_info.value.error_details["retry_after"] == "120"


@pytest.mark.asyncio
async def test_dispatch_workflow_500_unexpected(dispatch_service, valid_request):
    """Test handling of unexpected 500 Internal Server Error response."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(ExternalServiceError) as exc_info:
            await dispatch_service.dispatch_workflow(
                run_id="run-123", request=valid_request
            )

        assert "unexpected status 500" in exc_info.value.error_message
        assert exc_info.value.error_details["github_status"] == 500
        assert exc_info.value.error_details["github_body"] == "Internal Server Error"


@pytest.mark.asyncio
async def test_dispatch_workflow_network_error(dispatch_service, valid_request):
    """Test handling of network errors during dispatch."""
    with patch.object(
        dispatch_service.client, "post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(ExternalServiceError) as exc_info:
            await dispatch_service.dispatch_workflow(
                run_id="run-123", request=valid_request
            )

        assert "Failed to connect to GitHub API" in exc_info.value.error_message
        assert exc_info.value.error_details["exception"] == "ConnectError"


@pytest.mark.asyncio
async def test_close(dispatch_service):
    """Test closing the underlying HTTP client."""
    with patch.object(
        dispatch_service.client, "aclose", new_callable=AsyncMock
    ) as mock_aclose:
        await dispatch_service.close()
        mock_aclose.assert_called_once()


@patch("app.src.services.dispatcher.get_settings")
@patch("app.src.services.dispatcher.SecretsProvider")
def test_get_dispatch_service(mock_secrets_provider, mock_get_settings):
    """Test the factory function for creating a DispatchService."""
    mock_settings = mock_get_settings.return_value
    mock_settings.github_owner = "config-owner"
    mock_settings.github_repo = "config-repo"
    mock_settings.github_workflow_id = "config-workflow.yml"

    mock_provider_instance = mock_secrets_provider.return_value
    mock_provider_instance.get_secret.return_value = "config-pat"

    service = get_dispatch_service()

    assert service.github_owner == "config-owner"
    assert service.github_repo == "config-repo"
    assert service.workflow_id == "config-workflow.yml"

    # Verify PAT is set in headers
    assert service.client.headers["Authorization"] == "Bearer config-pat"

    mock_provider_instance.get_secret.assert_called_once_with(
        "dispatch/github-pat", env_fallback="GITHUB_PAT"
    )
