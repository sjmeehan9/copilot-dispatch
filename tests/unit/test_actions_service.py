"""Unit tests for the GitHub Actions visibility service.

Tests all :class:`ActionsService` methods using httpx mock transport to
simulate GitHub REST API responses without real network calls.  Covers
success paths, filter parameter building, and all error scenarios (404,
403, 422, 5xx, connection errors).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.src.api.actions_models import (
    RunFilterParams,
    WorkflowListResponse,
    WorkflowRunDetailResponse,
    WorkflowRunListResponse,
)
from app.src.exceptions import (
    ActionsGitHubError,
    ActionsNotFoundError,
    ActionsPermissionError,
    ActionsValidationError,
)
from app.src.services.actions import ActionsService

# ---------------------------------------------------------------------------
# Fixtures — mock transport & service factory
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
    text_body: str | None = None,
) -> httpx.Response:
    """Build an httpx.Response appropriate for a mock transport handler."""
    if json_body is not None:
        content = json.dumps(json_body).encode()
        headers = {"content-type": "application/json"}
    elif text_body is not None:
        content = text_body.encode()
        headers = {"content-type": "text/plain"}
    else:
        content = b""
        headers = {}
    return httpx.Response(status_code=status_code, content=content, headers=headers)


def _make_service(
    handler: httpx.MockTransport | None = None,
) -> ActionsService:
    """Create an ActionsService with a mock transport."""
    service = ActionsService(
        github_pat="test-pat-fake",
        api_base_url="https://api.github.com",
    )
    if handler is not None:
        service.client = httpx.AsyncClient(transport=handler)
    return service


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

SAMPLE_WORKFLOW = {
    "id": 1,
    "name": "CI",
    "path": ".github/workflows/ci.yml",
    "state": "active",
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-06-15T12:00:00Z",
    "html_url": "https://github.com/owner/repo/actions/workflows/ci.yml",
    "node_id": "W_kwDOabc",
}

SAMPLE_WORKFLOWS_RESPONSE: dict[str, Any] = {
    "total_count": 1,
    "workflows": [SAMPLE_WORKFLOW],
}

SAMPLE_RUN = {
    "id": 100,
    "name": "CI",
    "workflow_id": 1,
    "head_branch": "main",
    "head_sha": "abc123",
    "status": "completed",
    "conclusion": "success",
    "event": "push",
    "actor": {"login": "octocat", "id": 1},
    "run_number": 42,
    "run_attempt": 1,
    "created_at": "2025-06-15T12:00:00Z",
    "updated_at": "2025-06-15T12:05:00Z",
    "html_url": "https://github.com/owner/repo/actions/runs/100",
    "node_id": "WR_kwDOabc",
}

SAMPLE_RUNS_RESPONSE: dict[str, Any] = {
    "total_count": 1,
    "workflow_runs": [SAMPLE_RUN],
}

SAMPLE_STEP = {
    "name": "Checkout",
    "status": "completed",
    "conclusion": "success",
    "number": 1,
    "started_at": "2025-06-15T12:00:10Z",
    "completed_at": "2025-06-15T12:00:15Z",
}

SAMPLE_JOB = {
    "id": 200,
    "name": "build",
    "status": "completed",
    "conclusion": "success",
    "started_at": "2025-06-15T12:00:05Z",
    "completed_at": "2025-06-15T12:05:00Z",
    "steps": [SAMPLE_STEP],
}

SAMPLE_JOBS_RESPONSE: dict[str, Any] = {
    "total_count": 1,
    "jobs": [SAMPLE_JOB],
}


# ===================================================================
# list_workflows
# ===================================================================


class TestListWorkflows:
    """Tests for ActionsService.list_workflows."""

    @pytest.mark.asyncio
    async def test_list_workflows_success(self) -> None:
        """Return a WorkflowListResponse from a 200 GitHub response."""
        transport = httpx.MockTransport(
            lambda request: _make_response(200, SAMPLE_WORKFLOWS_RESPONSE)
        )
        service = _make_service(transport)

        result = await service.list_workflows("owner", "repo")

        assert isinstance(result, WorkflowListResponse)
        assert result.total_count == 1
        assert len(result.workflows) == 1
        assert result.workflows[0].name == "CI"
        assert result.workflows[0].id == 1

    @pytest.mark.asyncio
    async def test_list_workflows_sends_correct_url(self) -> None:
        """Verify the request is sent to the correct GitHub API URL."""
        captured_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            return _make_response(200, SAMPLE_WORKFLOWS_RESPONSE)

        service = _make_service(httpx.MockTransport(handler))
        await service.list_workflows("my-org", "my-repo")

        assert len(captured_urls) == 1
        assert captured_urls[0] == (
            "https://api.github.com/repos/my-org/my-repo/actions/workflows"
        )

    @pytest.mark.asyncio
    async def test_list_workflows_404(self) -> None:
        """Raise ActionsNotFoundError on a 404 GitHub response."""
        transport = httpx.MockTransport(
            lambda request: _make_response(404, {"message": "Not Found"})
        )
        service = _make_service(transport)

        with pytest.raises(ActionsNotFoundError) as exc_info:
            await service.list_workflows("owner", "missing-repo")

        assert exc_info.value.status_code == 404
        assert "Not Found" in exc_info.value.error_message

    @pytest.mark.asyncio
    async def test_list_workflows_403(self) -> None:
        """Raise ActionsPermissionError on a 403 GitHub response."""
        transport = httpx.MockTransport(
            lambda request: _make_response(
                403, {"message": "Resource not accessible by integration"}
            )
        )
        service = _make_service(transport)

        with pytest.raises(ActionsPermissionError) as exc_info:
            await service.list_workflows("owner", "private-repo")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_workflows_500(self) -> None:
        """Raise ActionsGitHubError on a 500 GitHub response."""
        transport = httpx.MockTransport(
            lambda request: _make_response(500, {"message": "Internal Server Error"})
        )
        service = _make_service(transport)

        with pytest.raises(ActionsGitHubError) as exc_info:
            await service.list_workflows("owner", "repo")

        assert exc_info.value.status_code == 502
        assert "500" in exc_info.value.error_message

    @pytest.mark.asyncio
    async def test_list_workflows_connection_error(self) -> None:
        """Raise ActionsGitHubError on a connection failure."""

        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        service = _make_service(httpx.MockTransport(failing_handler))

        with pytest.raises(ActionsGitHubError) as exc_info:
            await service.list_workflows("owner", "repo")

        assert "Failed to connect" in exc_info.value.error_message


# ===================================================================
# list_runs
# ===================================================================


class TestListRuns:
    """Tests for ActionsService.list_runs."""

    @pytest.mark.asyncio
    async def test_list_runs_success_no_filters(self) -> None:
        """Return runs when no filters are provided."""
        transport = httpx.MockTransport(
            lambda request: _make_response(200, SAMPLE_RUNS_RESPONSE)
        )
        service = _make_service(transport)

        result = await service.list_runs("owner", "repo")

        assert isinstance(result, WorkflowRunListResponse)
        assert result.total_count == 1
        assert result.workflow_runs[0].actor == "octocat"

    @pytest.mark.asyncio
    async def test_list_runs_with_all_filters(self) -> None:
        """Pass all filter values as query parameters to GitHub API."""
        captured_params: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_params.append(request.url)
            return _make_response(200, SAMPLE_RUNS_RESPONSE)

        service = _make_service(httpx.MockTransport(handler))
        filters = RunFilterParams(
            branch="main",
            status="completed",
            actor="octocat",
            event="push",
            per_page=10,
        )
        await service.list_runs("owner", "repo", filters)

        assert len(captured_params) == 1
        url = captured_params[0]
        assert url.params["branch"] == "main"
        assert url.params["status"] == "completed"
        assert url.params["actor"] == "octocat"
        assert url.params["event"] == "push"
        assert url.params["per_page"] == "10"

    @pytest.mark.asyncio
    async def test_list_runs_partial_filters(self) -> None:
        """Only include non-None filter values in query parameters."""
        captured_urls: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(request.url)
            return _make_response(200, SAMPLE_RUNS_RESPONSE)

        service = _make_service(httpx.MockTransport(handler))
        filters = RunFilterParams(branch="develop")
        await service.list_runs("owner", "repo", filters)

        url = captured_urls[0]
        assert url.params["branch"] == "develop"
        assert url.params["per_page"] == "30"
        assert "status" not in url.params
        assert "actor" not in url.params
        assert "event" not in url.params

    @pytest.mark.asyncio
    async def test_list_runs_404(self) -> None:
        """Raise ActionsNotFoundError on 404."""
        transport = httpx.MockTransport(
            lambda request: _make_response(404, {"message": "Not Found"})
        )
        service = _make_service(transport)

        with pytest.raises(ActionsNotFoundError):
            await service.list_runs("owner", "missing-repo")

    @pytest.mark.asyncio
    async def test_list_runs_actor_extraction(self) -> None:
        """Verify nested actor.login is extracted to string by the model."""
        transport = httpx.MockTransport(
            lambda request: _make_response(200, SAMPLE_RUNS_RESPONSE)
        )
        service = _make_service(transport)

        result = await service.list_runs("owner", "repo")

        assert result.workflow_runs[0].actor == "octocat"


# ===================================================================
# get_run_detail
# ===================================================================


class TestGetRunDetail:
    """Tests for ActionsService.get_run_detail."""

    @pytest.mark.asyncio
    async def test_get_run_detail_success(self) -> None:
        """Merge run + jobs into a single WorkflowRunDetailResponse."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if "/jobs" in str(request.url):
                return _make_response(200, SAMPLE_JOBS_RESPONSE)
            return _make_response(200, SAMPLE_RUN)

        service = _make_service(httpx.MockTransport(handler))
        result = await service.get_run_detail("owner", "repo", 100)

        assert isinstance(result, WorkflowRunDetailResponse)
        assert result.id == 100
        assert result.actor == "octocat"
        assert len(result.jobs) == 1
        assert result.jobs[0].name == "build"
        assert len(result.jobs[0].steps) == 1
        assert result.jobs[0].steps[0].name == "Checkout"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_get_run_detail_correct_urls(self) -> None:
        """Verify both run and jobs URLs are called."""
        captured_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            if "/jobs" in str(request.url):
                return _make_response(200, SAMPLE_JOBS_RESPONSE)
            return _make_response(200, SAMPLE_RUN)

        service = _make_service(httpx.MockTransport(handler))
        await service.get_run_detail("the-org", "the-repo", 999)

        assert len(captured_urls) == 2
        assert captured_urls[0] == (
            "https://api.github.com/repos/the-org/the-repo/actions/runs/999"
        )
        assert captured_urls[1] == (
            "https://api.github.com/repos/the-org/the-repo/actions/runs/999/jobs"
        )

    @pytest.mark.asyncio
    async def test_get_run_detail_empty_jobs(self) -> None:
        """Handle a run with no jobs gracefully."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "/jobs" in str(request.url):
                return _make_response(200, {"total_count": 0, "jobs": []})
            return _make_response(200, SAMPLE_RUN)

        service = _make_service(httpx.MockTransport(handler))
        result = await service.get_run_detail("owner", "repo", 100)

        assert result.jobs == []

    @pytest.mark.asyncio
    async def test_get_run_detail_run_404(self) -> None:
        """Raise ActionsNotFoundError when the run itself returns 404."""
        transport = httpx.MockTransport(
            lambda request: _make_response(404, {"message": "Not Found"})
        )
        service = _make_service(transport)

        with pytest.raises(ActionsNotFoundError):
            await service.get_run_detail("owner", "repo", 9999)

    @pytest.mark.asyncio
    async def test_get_run_detail_jobs_404(self) -> None:
        """Raise ActionsNotFoundError when the jobs endpoint returns 404."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "/jobs" in str(request.url):
                return _make_response(404, {"message": "Not Found"})
            return _make_response(200, SAMPLE_RUN)

        service = _make_service(httpx.MockTransport(handler))

        with pytest.raises(ActionsNotFoundError):
            await service.get_run_detail("owner", "repo", 100)

    @pytest.mark.asyncio
    async def test_get_run_detail_run_in_progress(self) -> None:
        """Handle a run that is still in progress (null conclusion)."""
        in_progress_run = {**SAMPLE_RUN, "status": "in_progress", "conclusion": None}

        def handler(request: httpx.Request) -> httpx.Response:
            if "/jobs" in str(request.url):
                return _make_response(200, {"total_count": 0, "jobs": []})
            return _make_response(200, in_progress_run)

        service = _make_service(httpx.MockTransport(handler))
        result = await service.get_run_detail("owner", "repo", 100)

        assert result.status == "in_progress"
        assert result.conclusion is None


# ===================================================================
# dispatch_workflow
# ===================================================================


class TestDispatchWorkflow:
    """Tests for ActionsService.dispatch_workflow."""

    @pytest.mark.asyncio
    async def test_dispatch_success(self) -> None:
        """Return None on a successful 204 dispatch."""
        transport = httpx.MockTransport(lambda request: _make_response(204))
        service = _make_service(transport)

        result = await service.dispatch_workflow("owner", "repo", 1, "main")

        assert result is None

    @pytest.mark.asyncio
    async def test_dispatch_sends_correct_payload(self) -> None:
        """Verify the POST body contains ref and inputs."""
        captured_bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            return _make_response(204)

        service = _make_service(httpx.MockTransport(handler))
        await service.dispatch_workflow(
            "owner", "repo", 42, "feature-branch", {"key": "value"}
        )

        assert len(captured_bodies) == 1
        body = captured_bodies[0]
        assert body["ref"] == "feature-branch"
        assert body["inputs"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_dispatch_without_inputs(self) -> None:
        """Inputs key is omitted when inputs is None."""
        captured_bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            return _make_response(204)

        service = _make_service(httpx.MockTransport(handler))
        await service.dispatch_workflow("owner", "repo", 42, "main")

        assert "inputs" not in captured_bodies[0]

    @pytest.mark.asyncio
    async def test_dispatch_correct_url(self) -> None:
        """Verify the POST is sent to the correct URL."""
        captured_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            return _make_response(204)

        service = _make_service(httpx.MockTransport(handler))
        await service.dispatch_workflow("my-org", "my-repo", 55, "main")

        assert captured_urls[0] == (
            "https://api.github.com/repos/my-org/my-repo"
            "/actions/workflows/55/dispatches"
        )

    @pytest.mark.asyncio
    async def test_dispatch_404(self) -> None:
        """Raise ActionsNotFoundError on 404."""
        transport = httpx.MockTransport(
            lambda request: _make_response(404, {"message": "Not Found"})
        )
        service = _make_service(transport)

        with pytest.raises(ActionsNotFoundError):
            await service.dispatch_workflow("owner", "repo", 1, "main")

    @pytest.mark.asyncio
    async def test_dispatch_403(self) -> None:
        """Raise ActionsPermissionError on 403."""
        transport = httpx.MockTransport(
            lambda request: _make_response(403, {"message": "Forbidden"})
        )
        service = _make_service(transport)

        with pytest.raises(ActionsPermissionError):
            await service.dispatch_workflow("owner", "repo", 1, "main")

    @pytest.mark.asyncio
    async def test_dispatch_422(self) -> None:
        """Raise ActionsValidationError on 422."""
        transport = httpx.MockTransport(
            lambda request: _make_response(422, {"message": "Validation Failed"})
        )
        service = _make_service(transport)

        with pytest.raises(ActionsValidationError) as exc_info:
            await service.dispatch_workflow("owner", "repo", 1, "main")

        assert "Validation" in exc_info.value.error_message

    @pytest.mark.asyncio
    async def test_dispatch_500(self) -> None:
        """Raise ActionsGitHubError on 500."""
        transport = httpx.MockTransport(
            lambda request: _make_response(500, {"message": "Server Error"})
        )
        service = _make_service(transport)

        with pytest.raises(ActionsGitHubError) as exc_info:
            await service.dispatch_workflow("owner", "repo", 1, "main")

        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_dispatch_connection_error(self) -> None:
        """Raise ActionsGitHubError on connection failure."""

        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        service = _make_service(httpx.MockTransport(failing_handler))

        with pytest.raises(ActionsGitHubError) as exc_info:
            await service.dispatch_workflow("owner", "repo", 1, "main")

        assert "Failed to connect" in exc_info.value.error_message


# ===================================================================
# _build_filter_params
# ===================================================================


class TestBuildFilterParams:
    """Tests for the static _build_filter_params helper."""

    def test_none_filters_returns_empty(self) -> None:
        """Return an empty dict when filters is None."""
        assert ActionsService._build_filter_params(None) == {}

    def test_all_filters_set(self) -> None:
        """Include all parameters when every filter is set."""
        filters = RunFilterParams(
            branch="main",
            status="completed",
            actor="octocat",
            event="push",
            per_page=50,
        )
        params = ActionsService._build_filter_params(filters)

        assert params == {
            "branch": "main",
            "status": "completed",
            "actor": "octocat",
            "event": "push",
            "per_page": 50,
        }

    def test_only_defaults(self) -> None:
        """Include only per_page when no optional filters are set."""
        filters = RunFilterParams()
        params = ActionsService._build_filter_params(filters)

        assert params == {"per_page": 30}

    def test_partial_filters(self) -> None:
        """Include only non-None optional filters plus per_page."""
        filters = RunFilterParams(branch="develop", per_page=10)
        params = ActionsService._build_filter_params(filters)

        assert params == {"branch": "develop", "per_page": 10}
        assert "status" not in params
        assert "actor" not in params
        assert "event" not in params


# ===================================================================
# _handle_error_response
# ===================================================================


class TestHandleErrorResponse:
    """Tests for the static _handle_error_response helper."""

    def test_404_raises_not_found(self) -> None:
        """Map 404 to ActionsNotFoundError."""
        response = _make_response(404, {"message": "Not Found"})
        with pytest.raises(ActionsNotFoundError):
            ActionsService._handle_error_response(response)

    def test_403_raises_permission_error(self) -> None:
        """Map 403 to ActionsPermissionError."""
        response = _make_response(403, {"message": "Forbidden"})
        with pytest.raises(ActionsPermissionError):
            ActionsService._handle_error_response(response)

    def test_422_raises_validation_error(self) -> None:
        """Map 422 to ActionsValidationError."""
        response = _make_response(422, {"message": "Validation Failed"})
        with pytest.raises(ActionsValidationError):
            ActionsService._handle_error_response(response)

    def test_500_raises_github_error(self) -> None:
        """Map 500 to ActionsGitHubError."""
        response = _make_response(500, {"message": "Internal Server Error"})
        with pytest.raises(ActionsGitHubError) as exc_info:
            ActionsService._handle_error_response(response)

        assert exc_info.value.status_code == 502
        assert "500" in exc_info.value.error_message

    def test_502_raises_github_error(self) -> None:
        """Map 502 to ActionsGitHubError."""
        response = _make_response(502, {"message": "Bad Gateway"})
        with pytest.raises(ActionsGitHubError):
            ActionsService._handle_error_response(response)

    def test_unexpected_status_raises_github_error(self) -> None:
        """Map unexpected non-2xx status codes to ActionsGitHubError."""
        response = _make_response(400, {"message": "Bad Request"})
        with pytest.raises(ActionsGitHubError) as exc_info:
            ActionsService._handle_error_response(response)

        assert "400" in exc_info.value.error_message

    def test_non_json_body(self) -> None:
        """Handle responses with non-JSON bodies gracefully."""
        response = _make_response(404, text_body="Page not found")
        with pytest.raises(ActionsNotFoundError) as exc_info:
            ActionsService._handle_error_response(response)

        assert "Resource not found" in exc_info.value.error_message


# ===================================================================
# close
# ===================================================================


class TestServiceLifecycle:
    """Tests for ActionsService lifecycle management."""

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        """Ensure close gracefully shuts down the HTTP client."""
        service = _make_service()
        # Should not raise
        await service.close()

    def test_auth_headers(self) -> None:
        """Verify the service sets correct authorization headers."""
        service = ActionsService(github_pat="my-secret-pat")

        assert service.client.headers["Authorization"] == "Bearer my-secret-pat"
        assert service.client.headers["Accept"] == "application/vnd.github+json"
        assert service.client.headers["X-GitHub-Api-Version"] == "2022-11-28"

    def test_api_base_url_trailing_slash_stripped(self) -> None:
        """Ensure trailing slashes are stripped from api_base_url."""
        service = ActionsService(
            github_pat="pat", api_base_url="https://api.github.com/"
        )
        assert service.api_base_url == "https://api.github.com"
