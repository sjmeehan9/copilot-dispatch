"""Smoke tests for the Actions API router and endpoint availability.

Verifies that:
- The actions router is registered in the application.
- All four Actions endpoints are reachable at their documented paths.
- API key authentication is enforced on all endpoints.
- Path parameters and query parameters are validated correctly.
- The dispatch endpoint validates the request body.
- Exception mapping from domain exceptions to HTTP status codes works.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure required environment variables are set for tests."""
    monkeypatch.setenv("GITHUB_PAT", "test-pat-fake")


@pytest.fixture
def mock_actions_service() -> AsyncMock:
    """Provide a fully-mocked ActionsService."""
    service = AsyncMock()

    service.list_workflows.return_value = WorkflowListResponse(
        total_count=1,
        workflows=[
            {
                "id": 1,
                "name": "CI",
                "path": ".github/workflows/ci.yml",
                "state": "active",
                "created_at": "2025-01-01T00:00:00Z",
                "updated_at": "2025-06-15T12:00:00Z",
                "html_url": "https://github.com/o/r/actions/workflows/ci.yml",
            }
        ],
    )

    service.list_runs.return_value = WorkflowRunListResponse(
        total_count=1,
        workflow_runs=[
            {
                "id": 100,
                "name": "CI",
                "workflow_id": 1,
                "head_branch": "main",
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "actor": "octocat",
                "run_number": 42,
                "run_attempt": 1,
                "created_at": "2025-06-15T12:00:00Z",
                "updated_at": "2025-06-15T12:05:00Z",
                "html_url": "https://github.com/o/r/actions/runs/100",
            }
        ],
    )

    service.get_run_detail.return_value = WorkflowRunDetailResponse(
        id=100,
        name="CI",
        workflow_id=1,
        head_branch="main",
        head_sha="abc123",
        status="completed",
        conclusion="success",
        event="push",
        actor="octocat",
        run_number=42,
        run_attempt=1,
        created_at="2025-06-15T12:00:00Z",
        updated_at="2025-06-15T12:05:00Z",
        html_url="https://github.com/o/r/actions/runs/100",
        jobs=[],
    )

    service.dispatch_workflow.return_value = None

    return service


@pytest.fixture
def client(mock_actions_service: AsyncMock) -> TestClient:
    """Test client with mocked ActionsService and bypassed API key auth."""
    app.dependency_overrides[get_actions_service_dep] = lambda: mock_actions_service
    app.dependency_overrides[verify_api_key] = lambda: "test-api-key"

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def unauthed_client(mock_actions_service: AsyncMock) -> TestClient:
    """Test client with mocked service but real API key auth (no override)."""
    app.dependency_overrides[get_actions_service_dep] = lambda: mock_actions_service

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    """Verify that the actions router is registered in the application."""

    def test_actions_router_is_registered(self) -> None:
        """The application should include routes with the /actions prefix."""
        routes = [route.path for route in app.routes]
        assert any("/actions" in route for route in routes)

    def test_all_four_endpoints_are_registered(self) -> None:
        """All four Actions endpoints should appear in the application routes."""
        routes = [route.path for route in app.routes]
        expected_paths = [
            "/actions/{owner}/{repo}/workflows",
            "/actions/{owner}/{repo}/runs",
            "/actions/{owner}/{repo}/runs/{run_id}",
            "/actions/{owner}/{repo}/workflows/{workflow_id}/dispatch",
        ]
        for path in expected_paths:
            assert path in routes, f"Expected route {path} not found in {routes}"


# ---------------------------------------------------------------------------
# GET /actions/{owner}/{repo}/workflows
# ---------------------------------------------------------------------------


class TestListWorkflows:
    """Tests for the list-workflows endpoint."""

    def test_list_workflows_success(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """GET /actions/{owner}/{repo}/workflows returns 200 with workflows."""
        response = client.get("/actions/owner/repo/workflows")
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 1
        assert len(data["workflows"]) == 1
        assert data["workflows"][0]["name"] == "CI"
        mock_actions_service.list_workflows.assert_called_once_with("owner", "repo")

    def test_list_workflows_calls_service_with_correct_params(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """The endpoint passes owner and repo to the service correctly."""
        client.get("/actions/my-org/my-repo/workflows")
        mock_actions_service.list_workflows.assert_called_once_with("my-org", "my-repo")


# ---------------------------------------------------------------------------
# GET /actions/{owner}/{repo}/runs
# ---------------------------------------------------------------------------


class TestListRuns:
    """Tests for the list-runs endpoint."""

    def test_list_runs_success(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """GET /actions/{owner}/{repo}/runs returns 200 with runs."""
        response = client.get("/actions/owner/repo/runs")
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 1
        assert len(data["workflow_runs"]) == 1
        assert data["workflow_runs"][0]["actor"] == "octocat"

    def test_list_runs_with_filters(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """Query parameters are passed to the service via RunFilterParams."""
        response = client.get(
            "/actions/owner/repo/runs",
            params={
                "branch": "main",
                "status": "completed",
                "actor": "octocat",
                "event": "push",
                "per_page": 10,
            },
        )
        assert response.status_code == 200
        call_args = mock_actions_service.list_runs.call_args
        filters = call_args[0][2]
        assert filters.branch == "main"
        assert filters.status == "completed"
        assert filters.actor == "octocat"
        assert filters.event == "push"
        assert filters.per_page == 10

    def test_list_runs_default_per_page(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """per_page defaults to 30 when not specified."""
        client.get("/actions/owner/repo/runs")
        call_args = mock_actions_service.list_runs.call_args
        filters = call_args[0][2]
        assert filters.per_page == 30

    def test_list_runs_per_page_validation_too_high(self, client: TestClient) -> None:
        """per_page > 100 is rejected with 422."""
        response = client.get("/actions/owner/repo/runs", params={"per_page": 200})
        assert response.status_code == 422

    def test_list_runs_per_page_validation_too_low(self, client: TestClient) -> None:
        """per_page < 1 is rejected with 422."""
        response = client.get("/actions/owner/repo/runs", params={"per_page": 0})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /actions/{owner}/{repo}/runs/{run_id}
# ---------------------------------------------------------------------------


class TestGetRunDetail:
    """Tests for the run-detail endpoint."""

    def test_get_run_detail_success(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """GET /actions/{owner}/{repo}/runs/{run_id} returns 200 with detail."""
        response = client.get("/actions/owner/repo/runs/100")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 100
        assert "jobs" in data
        mock_actions_service.get_run_detail.assert_called_once_with(
            "owner", "repo", 100
        )

    def test_get_run_detail_invalid_run_id(self, client: TestClient) -> None:
        """Non-integer run_id is rejected with 422."""
        response = client.get("/actions/owner/repo/runs/not-a-number")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /actions/{owner}/{repo}/workflows/{workflow_id}/dispatch
# ---------------------------------------------------------------------------


class TestDispatchWorkflow:
    """Tests for the dispatch-workflow endpoint."""

    def test_dispatch_success(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """POST dispatch returns 204 on success."""
        response = client.post(
            "/actions/owner/repo/workflows/1/dispatch",
            json={"ref": "main"},
        )
        assert response.status_code == 204
        assert response.content == b""
        mock_actions_service.dispatch_workflow.assert_called_once_with(
            owner="owner",
            repo="repo",
            workflow_id=1,
            ref="main",
            inputs=None,
        )

    def test_dispatch_with_inputs(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """POST dispatch forwards inputs to the service."""
        response = client.post(
            "/actions/owner/repo/workflows/1/dispatch",
            json={"ref": "main", "inputs": {"env": "staging"}},
        )
        assert response.status_code == 204
        mock_actions_service.dispatch_workflow.assert_called_once_with(
            owner="owner",
            repo="repo",
            workflow_id=1,
            ref="main",
            inputs={"env": "staging"},
        )

    def test_dispatch_missing_ref(self, client: TestClient) -> None:
        """POST dispatch without ref returns 422."""
        response = client.post(
            "/actions/owner/repo/workflows/1/dispatch",
            json={},
        )
        assert response.status_code == 422

    def test_dispatch_empty_ref(self, client: TestClient) -> None:
        """POST dispatch with empty ref string returns 422."""
        response = client.post(
            "/actions/owner/repo/workflows/1/dispatch",
            json={"ref": ""},
        )
        assert response.status_code == 422

    def test_dispatch_invalid_workflow_id(self, client: TestClient) -> None:
        """Non-integer workflow_id is rejected with 422."""
        response = client.post(
            "/actions/owner/repo/workflows/not-a-number/dispatch",
            json={"ref": "main"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Authentication enforcement
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Verify all endpoints reject requests without a valid API key."""

    def test_list_workflows_requires_api_key(self, unauthed_client: TestClient) -> None:
        """GET /actions/{owner}/{repo}/workflows returns 401 without API key."""
        response = unauthed_client.get("/actions/owner/repo/workflows")
        assert response.status_code == 401

    def test_list_runs_requires_api_key(self, unauthed_client: TestClient) -> None:
        """GET /actions/{owner}/{repo}/runs returns 401 without API key."""
        response = unauthed_client.get("/actions/owner/repo/runs")
        assert response.status_code == 401

    def test_get_run_detail_requires_api_key(self, unauthed_client: TestClient) -> None:
        """GET /actions/{owner}/{repo}/runs/{run_id} returns 401 without API key."""
        response = unauthed_client.get("/actions/owner/repo/runs/100")
        assert response.status_code == 401

    def test_dispatch_requires_api_key(self, unauthed_client: TestClient) -> None:
        """POST dispatch returns 401 without API key."""
        response = unauthed_client.post(
            "/actions/owner/repo/workflows/1/dispatch",
            json={"ref": "main"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Exception mapping
# ---------------------------------------------------------------------------


class TestExceptionMapping:
    """Verify that domain exceptions from ActionsService map to correct HTTP codes."""

    def test_not_found_maps_to_404(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """ActionsNotFoundError is returned as 404."""
        mock_actions_service.list_workflows.side_effect = ActionsNotFoundError(
            error_message="Repository not found"
        )
        response = client.get("/actions/owner/repo/workflows")
        assert response.status_code == 404
        assert response.json()["error_code"] == "ACTIONS_NOT_FOUND"

    def test_permission_error_maps_to_403(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """ActionsPermissionError is returned as 403."""
        mock_actions_service.list_runs.side_effect = ActionsPermissionError(
            error_message="Permission denied"
        )
        response = client.get("/actions/owner/repo/runs")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ACTIONS_PERMISSION_DENIED"

    def test_validation_error_maps_to_422(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """ActionsValidationError is returned as 422."""
        mock_actions_service.dispatch_workflow.side_effect = ActionsValidationError(
            error_message="Validation failed"
        )
        response = client.post(
            "/actions/owner/repo/workflows/1/dispatch",
            json={"ref": "main"},
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "ACTIONS_VALIDATION_ERROR"

    def test_github_error_maps_to_502(
        self, client: TestClient, mock_actions_service: AsyncMock
    ) -> None:
        """ActionsGitHubError is returned as 502."""
        mock_actions_service.get_run_detail.side_effect = ActionsGitHubError(
            error_message="GitHub API server error"
        )
        response = client.get("/actions/owner/repo/runs/100")
        assert response.status_code == 502
        assert response.json()["error_code"] == "ACTIONS_GITHUB_ERROR"


# ---------------------------------------------------------------------------
# OpenAPI spec
# ---------------------------------------------------------------------------


class TestOpenAPISpec:
    """Verify that the OpenAPI spec includes all Actions endpoints."""

    def test_openapi_includes_actions_endpoints(self, client: TestClient) -> None:
        """The OpenAPI schema should include all four Actions paths."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        paths = schema["paths"]

        expected = [
            "/actions/{owner}/{repo}/workflows",
            "/actions/{owner}/{repo}/runs",
            "/actions/{owner}/{repo}/runs/{run_id}",
            "/actions/{owner}/{repo}/workflows/{workflow_id}/dispatch",
        ]
        for path in expected:
            assert path in paths, f"Expected path {path} not found in OpenAPI spec"

    def test_openapi_actions_tag_present(self, client: TestClient) -> None:
        """The 'Actions' tag should appear in the OpenAPI spec."""
        response = client.get("/openapi.json")
        schema = response.json()
        paths = schema["paths"]

        # Check at least one actions endpoint has the Actions tag
        workflows_path = paths.get("/actions/{owner}/{repo}/workflows", {})
        get_op = workflows_path.get("get", {})
        assert "Actions" in get_op.get("tags", [])
