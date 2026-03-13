"""Unit tests for Pydantic models."""

import pytest
from pydantic import ValidationError

from app.src.api.models import (
    AgentRunRequest,
    AgentRunResponse,
    ErrorPayload,
    ResultIngestionPayload,
    RunStatusResponse,
)


def test_agent_run_request_valid_implement():
    """Test valid AgentRunRequest for implement role."""
    request = AgentRunRequest(
        repository="owner/repo",
        branch="feature-branch",
        agent_instructions="Do something",
        model="gpt-4",
        role="implement",
    )
    assert request.repository == "owner/repo"
    assert request.role == "implement"
    assert request.pr_number is None
    assert request.timeout_minutes == 30


def test_agent_run_request_with_agent_paths() -> None:
    """Test AgentRunRequest accepts optional agent_paths."""
    request = AgentRunRequest(
        repository="owner/repo",
        branch="feature-branch",
        agent_instructions="Do something",
        model="gpt-4",
        role="implement",
        agent_paths=[
            ".github/agents/security-reviewer.agent.md",
            ".github/agents/test-writer.agent.md",
        ],
    )

    assert request.agent_paths == [
        ".github/agents/security-reviewer.agent.md",
        ".github/agents/test-writer.agent.md",
    ]


def test_agent_run_request_agent_paths_optional() -> None:
    """Test agent_paths defaults to None when omitted."""
    request = AgentRunRequest(
        repository="owner/repo",
        branch="feature-branch",
        agent_instructions="Do something",
        model="gpt-4",
        role="implement",
    )

    assert request.agent_paths is None


def test_agent_run_request_valid_review():
    """Test valid AgentRunRequest for review role."""
    request = AgentRunRequest(
        repository="owner/repo",
        branch="feature-branch",
        agent_instructions="Review this",
        model="gpt-4",
        role="review",
        pr_number=123,
    )
    assert request.role == "review"
    assert request.pr_number == 123


def test_agent_run_request_valid_merge():
    """Test valid AgentRunRequest for merge role."""
    request = AgentRunRequest(
        repository="owner/repo",
        branch="feature-branch",
        agent_instructions="Merge this",
        model="gpt-4",
        role="merge",
        pr_number=123,
    )
    assert request.role == "merge"
    assert request.pr_number == 123


def test_agent_run_request_invalid_repository():
    """Test AgentRunRequest with invalid repository format."""
    with pytest.raises(ValidationError) as exc_info:
        AgentRunRequest(
            repository="invalid-repo-format",
            branch="feature-branch",
            agent_instructions="Do something",
            model="gpt-4",
            role="implement",
        )
    assert "String should match pattern" in str(exc_info.value)


def test_agent_run_request_missing_required_fields():
    """Test AgentRunRequest with missing required fields."""
    with pytest.raises(ValidationError) as exc_info:
        AgentRunRequest(
            repository="owner/repo",
            # missing branch, agent_instructions, model, role
        )
    assert "Field required" in str(exc_info.value)


def test_agent_run_request_pr_number_required_for_review():
    """Test AgentRunRequest enforces pr_number for review role."""
    with pytest.raises(ValidationError) as exc_info:
        AgentRunRequest(
            repository="owner/repo",
            branch="feature-branch",
            agent_instructions="Review this",
            model="gpt-4",
            role="review",
            # missing pr_number
        )
    assert "pr_number is required for review and merge roles" in str(exc_info.value)


def test_agent_run_request_pr_number_rejected_for_implement():
    """Test AgentRunRequest rejects pr_number for implement role."""
    with pytest.raises(ValidationError) as exc_info:
        AgentRunRequest(
            repository="owner/repo",
            branch="feature-branch",
            agent_instructions="Do something",
            model="gpt-4",
            role="implement",
            pr_number=123,
        )
    assert "pr_number is not applicable for implement role" in str(exc_info.value)


def test_agent_run_request_timeout_boundaries():
    """Test AgentRunRequest timeout_minutes boundaries."""
    # Valid boundaries
    AgentRunRequest(
        repository="owner/repo",
        branch="feature-branch",
        agent_instructions="Do something",
        model="gpt-4",
        role="implement",
        timeout_minutes=1,
    )
    AgentRunRequest(
        repository="owner/repo",
        branch="feature-branch",
        agent_instructions="Do something",
        model="gpt-4",
        role="implement",
        timeout_minutes=60,
    )

    # Invalid boundaries
    with pytest.raises(ValidationError):
        AgentRunRequest(
            repository="owner/repo",
            branch="feature-branch",
            agent_instructions="Do something",
            model="gpt-4",
            role="implement",
            timeout_minutes=0,
        )
    with pytest.raises(ValidationError):
        AgentRunRequest(
            repository="owner/repo",
            branch="feature-branch",
            agent_instructions="Do something",
            model="gpt-4",
            role="implement",
            timeout_minutes=61,
        )


def test_agent_run_response_serialisation():
    """Test AgentRunResponse construction and serialisation."""
    response = AgentRunResponse(
        run_id="run-123",
        status="queued",
        created_at="2023-10-27T10:00:00Z",
    )
    data = response.model_dump()
    assert data["run_id"] == "run-123"
    assert data["status"] == "queued"
    assert data["created_at"] == "2023-10-27T10:00:00Z"


def test_run_status_response_serialisation():
    """Test RunStatusResponse construction and serialisation."""
    response = RunStatusResponse(
        run_id="run-123",
        repository="owner/repo",
        branch="feature-branch",
        role="implement",
        status="running",
        model="gpt-4",
        created_at="2023-10-27T10:00:00Z",
        updated_at="2023-10-27T10:05:00Z",
    )
    data = response.model_dump()
    assert data["run_id"] == "run-123"
    assert data["status"] == "running"
    assert data["result"] is None
    assert data["error"] is None


def test_result_ingestion_payload_full():
    """Test ResultIngestionPayload with full success shape."""
    payload = ResultIngestionPayload(
        run_id="run-123",
        status="success",
        role="implement",
        model_used="gpt-4",
        duration_seconds=120,
        pr_url="https://github.com/owner/repo/pull/1",
        pr_number=1,
        branch="feature-branch",
        commits=[{"sha": "abcdef", "message": "Initial commit"}],
        files_changed=["main.py"],
        test_results={"passed": 10, "failed": 0, "skipped": 0},
        security_findings=[],
        session_summary="Implemented feature successfully.",
    )
    assert payload.status == "success"
    assert payload.role == "implement"


def test_result_ingestion_payload_success_missing_role():
    """Test ResultIngestionPayload enforces role when status is success."""
    with pytest.raises(ValidationError) as exc_info:
        ResultIngestionPayload(
            run_id="run-123",
            status="success",
            # missing role
        )
    assert "role is required when status is 'success'" in str(exc_info.value)


def test_result_ingestion_payload_status_only():
    """Test ResultIngestionPayload with status-only shape."""
    payload = ResultIngestionPayload(
        run_id="run-123",
        status="running",
    )
    assert payload.status == "running"
    assert payload.role is None


def test_result_ingestion_payload_error_only():
    """Test ResultIngestionPayload with error-only shape."""
    payload = ResultIngestionPayload(
        run_id="run-123",
        status="failure",
        error=ErrorPayload(
            error_code="AGENT_ERROR",
            error_message="Agent failed to complete task.",
        ),
    )
    assert payload.status == "failure"
    assert payload.error is not None
    assert payload.error.error_code == "AGENT_ERROR"


def test_error_payload_construction():
    """Test ErrorPayload construction with and without details."""
    error1 = ErrorPayload(
        error_code="ERR_01",
        error_message="An error occurred.",
    )
    assert error1.error_details is None

    error2 = ErrorPayload(
        error_code="ERR_02",
        error_message="Another error occurred.",
        error_details={"key": "value", "count": 5},
    )
    assert error2.error_details == {"key": "value", "count": 5}
