"""Integration tests for the GitHub Actions visibility API endpoints.

Tests all four Actions endpoints using FastAPI's ``TestClient`` with the
``ActionsService`` mocked at the dependency-injection level.  Covers:

- Success paths for all endpoints with correct response shapes.
- Query parameter filter combinations on the runs endpoint.
- Jobs and steps inclusion in the run detail response.
- Authentication enforcement (401 on all endpoints without API key).
- Error scenarios: 404 (not found), 403 (permission), 422 (validation), 502 (GitHub).
- Error response format consistency (``ErrorResponse`` shape).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.src.api.actions_models import (
    WorkflowListResponse,
    WorkflowRunDetailResponse,
    WorkflowRunListResponse,
)
from app.src.api.dependencies import get_actions_service_dep
from app.src.auth.api_key import verify_api_key
from app.src.exceptions import (
    ActionsGitHubError,
    ActionsNotFoundError,
    ActionsPermissionError,
    ActionsValidationError,
)
from app.src.main import app

# ---------------------------------------------------------------------------
# Sample payloads — realistic GitHub-style data
# ---------------------------------------------------------------------------

SAMPLE_WORKFLOW = {
    "id": 5001,
    "name": "Deploy Pipeline",
    "path": ".github/workflows/deploy.yml",
    "state": "active",
    "created_at": "2025-03-01T00:00:00Z",
    "updated_at": "2026-01-10T08:00:00Z",
    "html_url": "https://github.com/acme/webapp/actions/workflows/deploy.yml",
}

SAMPLE_RUN = {
    "id": 9001,
    "name": "Deploy Pipeline",
    "workflow_id": 5001,
    "head_branch": "main",
    "head_sha": "deadbeef1234",
    "status": "completed",
    "conclusion": "success",
    "event": "push",
    "actor": "deployer",
    "run_number": 17,
    "run_attempt": 1,
    "created_at": "2026-02-28T14:00:00Z",
    "updated_at": "2026-02-28T14:03:30Z",
    "html_url": "https://github.com/acme/webapp/actions/runs/9001",
}

SAMPLE_STEP = {
    "name": "Run tests",
    "status": "completed",
    "conclusion": "success",
    "number": 1,
    "started_at": "2026-02-28T14:00:10Z",
    "completed_at": "2026-02-28T14:01:00Z",
}

SAMPLE_JOB = {
    "id": 7001,
    "name": "test-suite",
    "status": "completed",
    "conclusion": "success",
    "started_at": "2026-02-28T14:00:05Z",
    "completed_at": "2026-02-28T14:03:00Z",
    "steps": [SAMPLE_STEP],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure required environment variables are set for tests."""
    monkeypatch.setenv("GITHUB_PAT", "test-pat-integration")


@pytest.fixture
def mock_actions_service() -> AsyncMock:
    """Provide a fully-mocked ActionsService for dependency injection."""
    service = AsyncMock()

    service.list_workflows.return_value = WorkflowListResponse(
        total_count=2,
        workflows=[
            SAMPLE_WORKFLOW,
            {
                **SAMPLE_WORKFLOW,
                "id": 5002,
                "name": "Lint",
                "path": ".github/workflows/lint.yml",
                "html_url": "https://github.com/acme/webapp/actions/workflows/lint.yml",
            },
        ],
    )

    service.list_runs.return_value = WorkflowRunListResponse(
        total_count=1,
        workflow_runs=[SAMPLE_RUN],
    )

    service.get_run_detail.return_value = WorkflowRunDetailResponse(
        **{**SAMPLE_RUN, "jobs": [SAMPLE_JOB]},
    )

    service.dispatch_workflow.return_value = None

    return service


@pytest.fixture
def client(mock_actions_service: AsyncMock) -> TestClient:
    """Authenticated test client with mocked ActionsService."""
    app.dependency_overrides[get_actions_service_dep] = lambda: mock_actions_service
    app.dependency_overrides[verify_api_key] = lambda: "integration-key"

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def unauthed_client(mock_actions_service: AsyncMock) -> TestClient:
    """Test client with mocked service but real (unbypassable) API key auth."""
    app.dependency_overrides[get_actions_service_dep] = lambda: mock_actions_service

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ===================================================================
# 3.4.4 — GET /actions/{owner}/{repo}/workflows
# ===================================================================


class TestListWorkflowsIntegration:
    """Integration tests for the list-workflows endpoint."""

    def test_returns_200_with_workflow_list(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Endpoint returns 200 with the complete workflow list response."""
        response = client.get("/actions/acme/webapp/workflows")

        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 2
        assert len(data["workflows"]) == 2
        assert data["workflows"][0]["name"] == "Deploy Pipeline"
        assert data["workflows"][1]["name"] == "Lint"

    def test_response_contains_all_workflow_fields(self, client: TestClient) -> None:
        """Each workflow item contains all expected fields."""
        response = client.get("/actions/acme/webapp/workflows")
        wf = response.json()["workflows"][0]

        expected_fields = {
            "id",
            "name",
            "path",
            "state",
            "created_at",
            "updated_at",
            "html_url",
        }
        assert expected_fields.issubset(set(wf.keys()))

    def test_service_receives_correct_owner_repo(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Owner and repo path params are passed to the service correctly."""
        client.get("/actions/my-org/my-repo/workflows")
        mock_actions_service.list_workflows.assert_called_once_with("my-org", "my-repo")

    def test_empty_workflow_list(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Endpoint handles repositories with no workflows."""
        mock_actions_service.list_workflows.return_value = WorkflowListResponse(
            total_count=0, workflows=[]
        )
        response = client.get("/actions/acme/webapp/workflows")

        assert response.status_code == 200
        assert response.json()["total_count"] == 0
        assert response.json()["workflows"] == []


# ===================================================================
# 3.4.5 — GET /actions/{owner}/{repo}/runs
# ===================================================================


class TestListRunsIntegration:
    """Integration tests for the list-runs endpoint with filter combinations."""

    def test_returns_200_with_runs_no_filters(self, client: TestClient) -> None:
        """Endpoint returns 200 with runs when no filters are applied."""
        response = client.get("/actions/acme/webapp/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 1
        assert data["workflow_runs"][0]["id"] == 9001
        assert data["workflow_runs"][0]["actor"] == "deployer"

    def test_response_contains_all_run_fields(self, client: TestClient) -> None:
        """Each run item contains all expected fields."""
        response = client.get("/actions/acme/webapp/runs")
        run = response.json()["workflow_runs"][0]

        expected_fields = {
            "id",
            "name",
            "workflow_id",
            "head_branch",
            "head_sha",
            "status",
            "conclusion",
            "event",
            "actor",
            "run_number",
            "run_attempt",
            "created_at",
            "updated_at",
            "html_url",
        }
        assert expected_fields.issubset(set(run.keys()))

    def test_branch_filter_only(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Branch filter is forwarded to the service correctly."""
        client.get("/actions/acme/webapp/runs", params={"branch": "develop"})

        filters = mock_actions_service.list_runs.call_args[0][2]
        assert filters.branch == "develop"
        assert filters.status is None
        assert filters.actor is None
        assert filters.event is None
        assert filters.per_page == 30

    def test_status_filter_only(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Status filter is forwarded to the service correctly."""
        client.get("/actions/acme/webapp/runs", params={"status": "in_progress"})

        filters = mock_actions_service.list_runs.call_args[0][2]
        assert filters.status == "in_progress"

    def test_actor_and_event_filters(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Actor and event filters work together."""
        client.get(
            "/actions/acme/webapp/runs",
            params={"actor": "bot-user", "event": "pull_request"},
        )

        filters = mock_actions_service.list_runs.call_args[0][2]
        assert filters.actor == "bot-user"
        assert filters.event == "pull_request"

    def test_all_filters_combined(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """All filters applied simultaneously are forwarded."""
        client.get(
            "/actions/acme/webapp/runs",
            params={
                "branch": "main",
                "status": "completed",
                "actor": "octocat",
                "event": "push",
                "per_page": 5,
            },
        )

        filters = mock_actions_service.list_runs.call_args[0][2]
        assert filters.branch == "main"
        assert filters.status == "completed"
        assert filters.actor == "octocat"
        assert filters.event == "push"
        assert filters.per_page == 5

    def test_per_page_boundary_min(self, client: TestClient) -> None:
        """per_page=1 is accepted (minimum boundary)."""
        response = client.get("/actions/acme/webapp/runs", params={"per_page": 1})
        assert response.status_code == 200

    def test_per_page_boundary_max(self, client: TestClient) -> None:
        """per_page=100 is accepted (maximum boundary)."""
        response = client.get("/actions/acme/webapp/runs", params={"per_page": 100})
        assert response.status_code == 200

    def test_per_page_exceeds_max_rejected(self, client: TestClient) -> None:
        """per_page > 100 returns 422."""
        response = client.get("/actions/acme/webapp/runs", params={"per_page": 101})
        assert response.status_code == 422

    def test_per_page_below_min_rejected(self, client: TestClient) -> None:
        """per_page < 1 returns 422."""
        response = client.get("/actions/acme/webapp/runs", params={"per_page": 0})
        assert response.status_code == 422


# ===================================================================
# 3.4.6 — GET /actions/{owner}/{repo}/runs/{run_id}
# ===================================================================


class TestGetRunDetailIntegration:
    """Integration tests for the run-detail endpoint verifying jobs/steps."""

    def test_returns_200_with_detail_including_jobs(self, client: TestClient) -> None:
        """Run detail response includes jobs and steps in the response body."""
        response = client.get("/actions/acme/webapp/runs/9001")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 9001
        assert data["actor"] == "deployer"
        assert "jobs" in data
        assert len(data["jobs"]) == 1

    def test_job_contains_steps(self, client: TestClient) -> None:
        """Each job in the response contains a steps array."""
        response = client.get("/actions/acme/webapp/runs/9001")
        job = response.json()["jobs"][0]

        assert job["id"] == 7001
        assert job["name"] == "test-suite"
        assert len(job["steps"]) == 1
        assert job["steps"][0]["name"] == "Run tests"
        assert job["steps"][0]["number"] == 1

    def test_step_has_all_fields(self, client: TestClient) -> None:
        """Step items contain all expected fields."""
        response = client.get("/actions/acme/webapp/runs/9001")
        step = response.json()["jobs"][0]["steps"][0]

        expected_fields = {
            "name",
            "status",
            "conclusion",
            "number",
            "started_at",
            "completed_at",
        }
        assert expected_fields.issubset(set(step.keys()))

    def test_run_with_empty_jobs(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Empty jobs list is returned correctly."""
        mock_actions_service.get_run_detail.return_value = WorkflowRunDetailResponse(
            **{**SAMPLE_RUN, "jobs": []},
        )
        response = client.get("/actions/acme/webapp/runs/9001")

        assert response.status_code == 200
        assert response.json()["jobs"] == []

    def test_run_with_null_conclusion(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """In-progress run with null conclusion is serialised correctly."""
        mock_actions_service.get_run_detail.return_value = WorkflowRunDetailResponse(
            **{**SAMPLE_RUN, "status": "in_progress", "conclusion": None, "jobs": []},
        )
        response = client.get("/actions/acme/webapp/runs/9001")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "in_progress"
        assert data["conclusion"] is None

    def test_service_receives_integer_run_id(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """The run_id path parameter is passed as an integer to the service."""
        client.get("/actions/acme/webapp/runs/12345")
        mock_actions_service.get_run_detail.assert_called_once_with(
            "acme", "webapp", 12345
        )

    def test_non_integer_run_id_returns_422(self, client: TestClient) -> None:
        """Non-integer run_id is rejected with 422."""
        response = client.get("/actions/acme/webapp/runs/abc")
        assert response.status_code == 422


# ===================================================================
# 3.4.7 — POST /actions/{owner}/{repo}/workflows/{workflow_id}/dispatch
# ===================================================================


class TestDispatchWorkflowIntegration:
    """Integration tests for the dispatch-workflow endpoint."""

    def test_dispatch_success_returns_204(self, client: TestClient) -> None:
        """Successful dispatch returns 204 No Content with empty body."""
        response = client.post(
            "/actions/acme/webapp/workflows/5001/dispatch",
            json={"ref": "main"},
        )

        assert response.status_code == 204
        assert response.content == b""

    def test_dispatch_with_inputs(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Dispatch with inputs forwards them to the service."""
        response = client.post(
            "/actions/acme/webapp/workflows/5001/dispatch",
            json={"ref": "v2.0.0", "inputs": {"deploy_env": "production"}},
        )

        assert response.status_code == 204
        mock_actions_service.dispatch_workflow.assert_called_once_with(
            owner="acme",
            repo="webapp",
            workflow_id=5001,
            ref="v2.0.0",
            inputs={"deploy_env": "production"},
        )

    def test_dispatch_without_inputs(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Dispatch without inputs passes None to the service."""
        response = client.post(
            "/actions/acme/webapp/workflows/5001/dispatch",
            json={"ref": "develop"},
        )

        assert response.status_code == 204
        mock_actions_service.dispatch_workflow.assert_called_once_with(
            owner="acme",
            repo="webapp",
            workflow_id=5001,
            ref="develop",
            inputs=None,
        )

    def test_dispatch_missing_ref_returns_422(self, client: TestClient) -> None:
        """Missing ref in request body returns 422."""
        response = client.post(
            "/actions/acme/webapp/workflows/5001/dispatch",
            json={},
        )
        assert response.status_code == 422

    def test_dispatch_empty_ref_returns_422(self, client: TestClient) -> None:
        """Empty string ref in request body returns 422."""
        response = client.post(
            "/actions/acme/webapp/workflows/5001/dispatch",
            json={"ref": ""},
        )
        assert response.status_code == 422

    def test_dispatch_non_integer_workflow_id_returns_422(
        self, client: TestClient
    ) -> None:
        """Non-integer workflow_id is rejected with 422."""
        response = client.post(
            "/actions/acme/webapp/workflows/not-int/dispatch",
            json={"ref": "main"},
        )
        assert response.status_code == 422

    def test_dispatch_github_422_returns_422(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """ActionsValidationError from GitHub maps to 422."""
        mock_actions_service.dispatch_workflow.side_effect = ActionsValidationError(
            error_message="Workflow does not have 'workflow_dispatch' trigger"
        )
        response = client.post(
            "/actions/acme/webapp/workflows/5001/dispatch",
            json={"ref": "main"},
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error_code"] == "ACTIONS_VALIDATION_ERROR"
        assert "workflow_dispatch" in body["error_message"]


# ===================================================================
# 3.4.8 — Authentication enforcement on all endpoints
# ===================================================================


class TestAuthenticationIntegration:
    """Verify all endpoints reject requests without a valid API key."""

    def test_workflows_returns_401_without_api_key(
        self, unauthed_client: TestClient
    ) -> None:
        """GET workflows returns 401 when no API key is provided."""
        response = unauthed_client.get("/actions/acme/webapp/workflows")
        assert response.status_code == 401

    def test_runs_returns_401_without_api_key(
        self, unauthed_client: TestClient
    ) -> None:
        """GET runs returns 401 when no API key is provided."""
        response = unauthed_client.get("/actions/acme/webapp/runs")
        assert response.status_code == 401

    def test_run_detail_returns_401_without_api_key(
        self, unauthed_client: TestClient
    ) -> None:
        """GET run detail returns 401 when no API key is provided."""
        response = unauthed_client.get("/actions/acme/webapp/runs/9001")
        assert response.status_code == 401

    def test_dispatch_returns_401_without_api_key(
        self, unauthed_client: TestClient
    ) -> None:
        """POST dispatch returns 401 when no API key is provided."""
        response = unauthed_client.post(
            "/actions/acme/webapp/workflows/5001/dispatch",
            json={"ref": "main"},
        )
        assert response.status_code == 401

    def test_401_response_has_error_format(self, unauthed_client: TestClient) -> None:
        """401 response follows the standard ErrorResponse shape."""
        response = unauthed_client.get("/actions/acme/webapp/workflows")
        body = response.json()
        assert "error_code" in body
        assert "error_message" in body


# ===================================================================
# 3.4.9 — Error scenarios
# ===================================================================


class TestErrorScenariosIntegration:
    """Integration tests for error response mapping and format consistency."""

    def test_not_found_returns_404(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """ActionsNotFoundError maps to 404 with correct error format."""
        mock_actions_service.list_workflows.side_effect = ActionsNotFoundError(
            error_message="Repository acme/missing not found"
        )
        response = client.get("/actions/acme/missing/workflows")

        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == "ACTIONS_NOT_FOUND"
        assert "acme/missing" in body["error_message"]

    def test_permission_error_returns_403(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """ActionsPermissionError maps to 403 with correct error format."""
        mock_actions_service.list_runs.side_effect = ActionsPermissionError(
            error_message="Insufficient permissions for acme/private"
        )
        response = client.get("/actions/acme/private/runs")

        assert response.status_code == 403
        body = response.json()
        assert body["error_code"] == "ACTIONS_PERMISSION_DENIED"

    def test_github_error_returns_502(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """ActionsGitHubError maps to 502 with correct error format."""
        mock_actions_service.get_run_detail.side_effect = ActionsGitHubError(
            error_message="GitHub API returned 500"
        )
        response = client.get("/actions/acme/webapp/runs/9001")

        assert response.status_code == 502
        body = response.json()
        assert body["error_code"] == "ACTIONS_GITHUB_ERROR"

    def test_not_found_on_run_detail(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """404 on run detail endpoint returns correct error format."""
        mock_actions_service.get_run_detail.side_effect = ActionsNotFoundError(
            error_message="Run 99999 not found"
        )
        response = client.get("/actions/acme/webapp/runs/99999")

        assert response.status_code == 404
        assert "99999" in response.json()["error_message"]

    def test_not_found_on_dispatch(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """404 on dispatch endpoint returns correct error format."""
        mock_actions_service.dispatch_workflow.side_effect = ActionsNotFoundError(
            error_message="Workflow 999 not found"
        )
        response = client.post(
            "/actions/acme/webapp/workflows/999/dispatch",
            json={"ref": "main"},
        )

        assert response.status_code == 404
        assert "999" in response.json()["error_message"]

    def test_permission_error_on_dispatch(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """403 on dispatch endpoint returns correct error format."""
        mock_actions_service.dispatch_workflow.side_effect = ActionsPermissionError(
            error_message="Cannot dispatch workflow"
        )
        response = client.post(
            "/actions/acme/webapp/workflows/5001/dispatch",
            json={"ref": "main"},
        )

        assert response.status_code == 403
        assert response.json()["error_code"] == "ACTIONS_PERMISSION_DENIED"

    def test_github_error_on_runs(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """502 on runs endpoint returns correct error format."""
        mock_actions_service.list_runs.side_effect = ActionsGitHubError(
            error_message="GitHub API unavailable"
        )
        response = client.get("/actions/acme/webapp/runs")

        assert response.status_code == 502

    def test_error_response_shape_is_consistent(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """All error responses follow the ErrorResponse schema."""
        mock_actions_service.list_workflows.side_effect = ActionsNotFoundError(
            error_message="Not found"
        )
        response = client.get("/actions/acme/webapp/workflows")
        body = response.json()

        # ErrorResponse must have error_code and error_message at minimum
        assert "error_code" in body
        assert "error_message" in body
        assert isinstance(body["error_code"], str)
        assert isinstance(body["error_message"], str)
