"""Unit tests for GitHub Actions visibility Pydantic models.

Covers valid construction, validation errors, serialisation round-trips,
``extra="ignore"`` behaviour, actor extraction from nested objects, and
edge cases such as null conclusions and missing optional fields.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.src.api.actions_models import (
    JobItem,
    RunFilterParams,
    StepItem,
    WorkflowDispatchRequest,
    WorkflowItem,
    WorkflowListResponse,
    WorkflowRunDetailResponse,
    WorkflowRunItem,
    WorkflowRunListResponse,
)

# ---------------------------------------------------------------------------
# Fixtures — representative GitHub API payloads
# ---------------------------------------------------------------------------

WORKFLOW_GITHUB_JSON: dict = {
    "id": 12345,
    "node_id": "W_kwDOTest",
    "name": "CI Pipeline",
    "path": ".github/workflows/ci.yml",
    "state": "active",
    "created_at": "2026-01-15T08:00:00Z",
    "updated_at": "2026-02-20T14:30:00Z",
    "html_url": "https://github.com/owner/repo/actions/workflows/ci.yml",
    "badge_url": "https://github.com/owner/repo/workflows/CI/badge.svg",
    "url": "https://api.github.com/repos/owner/repo/actions/workflows/12345",
}

RUN_GITHUB_JSON: dict = {
    "id": 67890,
    "name": "CI Pipeline",
    "workflow_id": 12345,
    "head_branch": "main",
    "head_sha": "abc123def456",
    "status": "completed",
    "conclusion": "success",
    "event": "push",
    "actor": {"login": "sjmeehan9", "id": 1234, "type": "User"},
    "run_number": 42,
    "run_attempt": 1,
    "created_at": "2026-02-28T10:00:00Z",
    "updated_at": "2026-02-28T10:05:30Z",
    "html_url": "https://github.com/owner/repo/actions/runs/67890",
    "node_id": "WFR_kwDOTest",
    "check_suite_id": 99999,
    "repository": {"id": 111, "name": "repo"},
}

STEP_GITHUB_JSON: dict = {
    "name": "Checkout code",
    "status": "completed",
    "conclusion": "success",
    "number": 1,
    "started_at": "2026-02-28T10:00:16Z",
    "completed_at": "2026-02-28T10:00:20Z",
}

JOB_GITHUB_JSON: dict = {
    "id": 11111,
    "name": "build",
    "status": "completed",
    "conclusion": "success",
    "started_at": "2026-02-28T10:00:15Z",
    "completed_at": "2026-02-28T10:03:45Z",
    "steps": [STEP_GITHUB_JSON],
    "run_id": 67890,
    "runner_id": 1,
    "runner_name": "GitHub Actions 2",
}


# ===================================================================
# 3.1.1 — WorkflowItem
# ===================================================================


class TestWorkflowItem:
    """Tests for WorkflowItem model."""

    def test_valid_construction(self) -> None:
        """Test construction with all required fields."""
        item = WorkflowItem(**WORKFLOW_GITHUB_JSON)
        assert item.id == 12345
        assert item.name == "CI Pipeline"
        assert item.path == ".github/workflows/ci.yml"
        assert item.state == "active"
        assert item.created_at == "2026-01-15T08:00:00Z"
        assert item.updated_at == "2026-02-20T14:30:00Z"
        assert item.html_url == "https://github.com/owner/repo/actions/workflows/ci.yml"

    def test_extra_fields_ignored(self) -> None:
        """Test that extra GitHub API fields are silently ignored."""
        item = WorkflowItem(**WORKFLOW_GITHUB_JSON)
        dumped = item.model_dump()
        assert "node_id" not in dumped
        assert "badge_url" not in dumped
        assert "url" not in dumped

    def test_missing_required_field(self) -> None:
        """Test that a missing required field raises ValidationError."""
        data = {**WORKFLOW_GITHUB_JSON}
        del data["name"]
        with pytest.raises(ValidationError) as exc_info:
            WorkflowItem(**data)
        assert "name" in str(exc_info.value)

    def test_serialisation_round_trip(self) -> None:
        """Test JSON serialisation and deserialisation round-trip."""
        item = WorkflowItem(**WORKFLOW_GITHUB_JSON)
        json_str = item.model_dump_json()
        restored = WorkflowItem.model_validate_json(json_str)
        assert restored == item


# ===================================================================
# 3.1.2 — WorkflowListResponse
# ===================================================================


class TestWorkflowListResponse:
    """Tests for WorkflowListResponse model."""

    def test_valid_construction(self) -> None:
        """Test construction with a list of workflows."""
        response = WorkflowListResponse(
            total_count=1,
            workflows=[WORKFLOW_GITHUB_JSON],
        )
        assert response.total_count == 1
        assert len(response.workflows) == 1
        assert response.workflows[0].name == "CI Pipeline"

    def test_empty_workflows(self) -> None:
        """Test construction with no workflows."""
        response = WorkflowListResponse(total_count=0, workflows=[])
        assert response.total_count == 0
        assert response.workflows == []

    def test_extra_fields_ignored(self) -> None:
        """Test that extra top-level fields are ignored."""
        response = WorkflowListResponse(
            total_count=1,
            workflows=[WORKFLOW_GITHUB_JSON],
            url="https://api.github.com/repos/owner/repo/actions/workflows",
        )
        dumped = response.model_dump()
        assert "url" not in dumped

    def test_serialisation_round_trip(self) -> None:
        """Test JSON round-trip for the list response."""
        response = WorkflowListResponse(
            total_count=1,
            workflows=[WORKFLOW_GITHUB_JSON],
        )
        json_str = response.model_dump_json()
        restored = WorkflowListResponse.model_validate_json(json_str)
        assert restored == response


# ===================================================================
# 3.1.3 — WorkflowRunItem
# ===================================================================


class TestWorkflowRunItem:
    """Tests for WorkflowRunItem model."""

    def test_valid_construction_from_github_json(self) -> None:
        """Test construction from a representative GitHub API response."""
        item = WorkflowRunItem(**RUN_GITHUB_JSON)
        assert item.id == 67890
        assert item.name == "CI Pipeline"
        assert item.workflow_id == 12345
        assert item.head_branch == "main"
        assert item.head_sha == "abc123def456"
        assert item.status == "completed"
        assert item.conclusion == "success"
        assert item.event == "push"
        assert item.actor == "sjmeehan9"
        assert item.run_number == 42
        assert item.run_attempt == 1

    def test_actor_extracted_from_nested_dict(self) -> None:
        """Test that actor.login is extracted from the nested GitHub actor object."""
        item = WorkflowRunItem(**RUN_GITHUB_JSON)
        assert item.actor == "sjmeehan9"

    def test_actor_as_plain_string(self) -> None:
        """Test construction when actor is already a plain string."""
        data = {**RUN_GITHUB_JSON, "actor": "direct-user"}
        item = WorkflowRunItem(**data)
        assert item.actor == "direct-user"

    def test_actor_nested_missing_login(self) -> None:
        """Test that actor defaults to empty string when login key is missing."""
        data = {**RUN_GITHUB_JSON, "actor": {"id": 999, "type": "Bot"}}
        item = WorkflowRunItem(**data)
        assert item.actor == ""

    def test_null_conclusion(self) -> None:
        """Test that conclusion can be None (run in progress)."""
        data = {**RUN_GITHUB_JSON, "conclusion": None, "status": "in_progress"}
        item = WorkflowRunItem(**data)
        assert item.conclusion is None
        assert item.status == "in_progress"

    def test_extra_fields_ignored(self) -> None:
        """Test that extra GitHub API fields are silently ignored."""
        item = WorkflowRunItem(**RUN_GITHUB_JSON)
        dumped = item.model_dump()
        assert "node_id" not in dumped
        assert "check_suite_id" not in dumped
        assert "repository" not in dumped

    def test_missing_required_field(self) -> None:
        """Test that a missing required field raises ValidationError."""
        data = {**RUN_GITHUB_JSON}
        del data["head_sha"]
        with pytest.raises(ValidationError) as exc_info:
            WorkflowRunItem(**data)
        assert "head_sha" in str(exc_info.value)

    def test_serialisation_round_trip(self) -> None:
        """Test JSON serialisation and deserialisation round-trip."""
        item = WorkflowRunItem(**RUN_GITHUB_JSON)
        json_str = item.model_dump_json()
        restored = WorkflowRunItem.model_validate_json(json_str)
        assert restored == item


# ===================================================================
# 3.1.4 — WorkflowRunListResponse
# ===================================================================


class TestWorkflowRunListResponse:
    """Tests for WorkflowRunListResponse model."""

    def test_valid_construction(self) -> None:
        """Test construction with a list of runs."""
        response = WorkflowRunListResponse(
            total_count=1,
            workflow_runs=[RUN_GITHUB_JSON],
        )
        assert response.total_count == 1
        assert len(response.workflow_runs) == 1
        assert response.workflow_runs[0].actor == "sjmeehan9"

    def test_empty_runs(self) -> None:
        """Test construction with no workflow runs."""
        response = WorkflowRunListResponse(total_count=0, workflow_runs=[])
        assert response.total_count == 0
        assert response.workflow_runs == []

    def test_serialisation_round_trip(self) -> None:
        """Test JSON round-trip for the run list response."""
        response = WorkflowRunListResponse(
            total_count=1,
            workflow_runs=[RUN_GITHUB_JSON],
        )
        json_str = response.model_dump_json()
        restored = WorkflowRunListResponse.model_validate_json(json_str)
        assert restored == response


# ===================================================================
# 3.1.5 — StepItem
# ===================================================================


class TestStepItem:
    """Tests for StepItem model."""

    def test_valid_construction(self) -> None:
        """Test construction with all fields."""
        step = StepItem(**STEP_GITHUB_JSON)
        assert step.name == "Checkout code"
        assert step.status == "completed"
        assert step.conclusion == "success"
        assert step.number == 1
        assert step.started_at == "2026-02-28T10:00:16Z"
        assert step.completed_at == "2026-02-28T10:00:20Z"

    def test_null_conclusion_and_timestamps(self) -> None:
        """Test step with null conclusion and timestamps (queued step)."""
        step = StepItem(
            name="Pending step",
            status="queued",
            conclusion=None,
            number=2,
            started_at=None,
            completed_at=None,
        )
        assert step.conclusion is None
        assert step.started_at is None
        assert step.completed_at is None

    def test_extra_fields_ignored(self) -> None:
        """Test that extra fields are silently ignored."""
        data = {**STEP_GITHUB_JSON, "log": "https://example.com/log"}
        step = StepItem(**data)
        assert "log" not in step.model_dump()

    def test_missing_required_field(self) -> None:
        """Test that a missing required field raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            StepItem(status="completed", conclusion="success", number=1)
        assert "name" in str(exc_info.value)

    def test_serialisation_round_trip(self) -> None:
        """Test JSON serialisation and deserialisation round-trip."""
        step = StepItem(**STEP_GITHUB_JSON)
        json_str = step.model_dump_json()
        restored = StepItem.model_validate_json(json_str)
        assert restored == step


# ===================================================================
# 3.1.6 — JobItem
# ===================================================================


class TestJobItem:
    """Tests for JobItem model."""

    def test_valid_construction(self) -> None:
        """Test construction from GitHub API JSON."""
        job = JobItem(**JOB_GITHUB_JSON)
        assert job.id == 11111
        assert job.name == "build"
        assert job.status == "completed"
        assert job.conclusion == "success"
        assert len(job.steps) == 1
        assert job.steps[0].name == "Checkout code"

    def test_null_conclusion_and_timestamps(self) -> None:
        """Test job in progress with null conclusion and timestamps."""
        job = JobItem(
            id=22222,
            name="test",
            status="in_progress",
            conclusion=None,
            started_at=None,
            completed_at=None,
            steps=[],
        )
        assert job.conclusion is None
        assert job.started_at is None
        assert job.completed_at is None
        assert job.steps == []

    def test_extra_fields_ignored(self) -> None:
        """Test that extra fields such as run_id are silently ignored."""
        job = JobItem(**JOB_GITHUB_JSON)
        dumped = job.model_dump()
        assert "run_id" not in dumped
        assert "runner_id" not in dumped

    def test_serialisation_round_trip(self) -> None:
        """Test JSON serialisation and deserialisation round-trip."""
        job = JobItem(**JOB_GITHUB_JSON)
        json_str = job.model_dump_json()
        restored = JobItem.model_validate_json(json_str)
        assert restored == job


# ===================================================================
# 3.1.7 — WorkflowRunDetailResponse
# ===================================================================


class TestWorkflowRunDetailResponse:
    """Tests for WorkflowRunDetailResponse model."""

    def test_valid_construction(self) -> None:
        """Test construction with run data and jobs."""
        detail = WorkflowRunDetailResponse(
            **{**RUN_GITHUB_JSON, "jobs": [JOB_GITHUB_JSON]},
        )
        assert detail.id == 67890
        assert detail.actor == "sjmeehan9"
        assert len(detail.jobs) == 1
        assert detail.jobs[0].id == 11111
        assert len(detail.jobs[0].steps) == 1

    def test_empty_jobs(self) -> None:
        """Test detail response with no jobs."""
        detail = WorkflowRunDetailResponse(
            **{**RUN_GITHUB_JSON, "jobs": []},
        )
        assert detail.jobs == []

    def test_inherits_workflow_run_item_fields(self) -> None:
        """Test that all WorkflowRunItem fields are accessible."""
        detail = WorkflowRunDetailResponse(
            **{**RUN_GITHUB_JSON, "jobs": []},
        )
        assert detail.head_branch == "main"
        assert detail.workflow_id == 12345
        assert detail.conclusion == "success"

    def test_actor_extraction_preserved(self) -> None:
        """Test that actor extraction from nested dict works in the subclass."""
        detail = WorkflowRunDetailResponse(
            **{**RUN_GITHUB_JSON, "jobs": []},
        )
        assert detail.actor == "sjmeehan9"

    def test_serialisation_round_trip(self) -> None:
        """Test JSON serialisation and deserialisation round-trip."""
        detail = WorkflowRunDetailResponse(
            **{**RUN_GITHUB_JSON, "jobs": [JOB_GITHUB_JSON]},
        )
        json_str = detail.model_dump_json()
        restored = WorkflowRunDetailResponse.model_validate_json(json_str)
        assert restored == detail

    def test_serialised_json_matches_solution_design(self) -> None:
        """Test that serialised output matches the solution design response schema."""
        detail = WorkflowRunDetailResponse(
            **{**RUN_GITHUB_JSON, "jobs": [JOB_GITHUB_JSON]},
        )
        data = json.loads(detail.model_dump_json())
        # Top-level run fields
        assert "id" in data
        assert "name" in data
        assert "actor" in data
        assert isinstance(data["actor"], str)
        # Jobs array
        assert "jobs" in data
        assert isinstance(data["jobs"], list)
        assert len(data["jobs"]) == 1
        job = data["jobs"][0]
        assert "steps" in job
        assert isinstance(job["steps"], list)


# ===================================================================
# 3.1.8 — WorkflowDispatchRequest
# ===================================================================


class TestWorkflowDispatchRequest:
    """Tests for WorkflowDispatchRequest model."""

    def test_valid_with_ref_only(self) -> None:
        """Test valid request with ref and no inputs."""
        req = WorkflowDispatchRequest(ref="main")
        assert req.ref == "main"
        assert req.inputs is None

    def test_valid_with_inputs(self) -> None:
        """Test valid request with ref and inputs."""
        req = WorkflowDispatchRequest(
            ref="v1.0.0",
            inputs={"environment": "staging", "dry_run": "true"},
        )
        assert req.ref == "v1.0.0"
        assert req.inputs == {"environment": "staging", "dry_run": "true"}

    def test_empty_ref_rejected(self) -> None:
        """Test that an empty string ref is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowDispatchRequest(ref="")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("ref",) for e in errors)

    def test_missing_ref_rejected(self) -> None:
        """Test that a missing ref is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowDispatchRequest()  # type: ignore[call-arg]
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("ref",) for e in errors)

    def test_empty_inputs_dict(self) -> None:
        """Test that an empty inputs dict is valid."""
        req = WorkflowDispatchRequest(ref="main", inputs={})
        assert req.inputs == {}

    def test_serialisation_round_trip(self) -> None:
        """Test JSON serialisation and deserialisation round-trip."""
        req = WorkflowDispatchRequest(ref="main", inputs={"key": "value"})
        json_str = req.model_dump_json()
        restored = WorkflowDispatchRequest.model_validate_json(json_str)
        assert restored == req


# ===================================================================
# 3.1.9 — RunFilterParams
# ===================================================================


class TestRunFilterParams:
    """Tests for RunFilterParams model."""

    def test_defaults(self) -> None:
        """Test that all fields default correctly."""
        params = RunFilterParams()
        assert params.branch is None
        assert params.status is None
        assert params.actor is None
        assert params.event is None
        assert params.per_page == 30

    def test_all_fields_set(self) -> None:
        """Test construction with all fields provided."""
        params = RunFilterParams(
            branch="main",
            status="completed",
            actor="sjmeehan9",
            event="push",
            per_page=50,
        )
        assert params.branch == "main"
        assert params.status == "completed"
        assert params.actor == "sjmeehan9"
        assert params.event == "push"
        assert params.per_page == 50

    def test_per_page_minimum(self) -> None:
        """Test that per_page at minimum (1) is accepted."""
        params = RunFilterParams(per_page=1)
        assert params.per_page == 1

    def test_per_page_maximum(self) -> None:
        """Test that per_page at maximum (100) is accepted."""
        params = RunFilterParams(per_page=100)
        assert params.per_page == 100

    def test_per_page_below_minimum_rejected(self) -> None:
        """Test that per_page below 1 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RunFilterParams(per_page=0)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("per_page",) for e in errors)

    def test_per_page_above_maximum_rejected(self) -> None:
        """Test that per_page above 100 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RunFilterParams(per_page=101)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("per_page",) for e in errors)

    def test_serialisation_round_trip(self) -> None:
        """Test JSON serialisation and deserialisation round-trip."""
        params = RunFilterParams(branch="develop", per_page=10)
        json_str = params.model_dump_json()
        restored = RunFilterParams.model_validate_json(json_str)
        assert restored == params
