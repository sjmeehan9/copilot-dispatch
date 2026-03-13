"""GitHub Actions visibility service.

Wraps the GitHub REST API for read-only visibility into GitHub Actions
workflows and runs, plus workflow dispatch triggering.  Follows the same
patterns established by :class:`DispatchService` — async httpx client,
PAT-based authentication, and structured domain exception mapping.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.src.api.actions_models import (
    JobItem,
    RunFilterParams,
    WorkflowListResponse,
    WorkflowRunDetailResponse,
    WorkflowRunItem,
    WorkflowRunListResponse,
)
from app.src.auth.secrets import SecretsProvider
from app.src.exceptions import (
    ActionsGitHubError,
    ActionsNotFoundError,
    ActionsPermissionError,
    ActionsValidationError,
)

logger = logging.getLogger(__name__)


class ActionsService:
    """Service layer for GitHub Actions visibility operations.

    Provides typed methods for listing workflows, listing and inspecting
    workflow runs, and dispatching workflows.  All responses are validated
    through the Pydantic models defined in :mod:`app.src.api.actions_models`.

    The service is stateless — no DynamoDB interaction — and maps GitHub
    REST API responses into domain types while translating HTTP error codes
    to domain-specific exceptions.

    Args:
        github_pat: GitHub Personal Access Token with ``actions`` scope.
        api_base_url: Base URL for the GitHub REST API (default
            ``https://api.github.com``).
    """

    def __init__(
        self,
        github_pat: str,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        """Initialise the ActionsService.

        Args:
            github_pat: GitHub Personal Access Token.
            api_base_url: Base URL for the GitHub API.
        """
        self.api_base_url = api_base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {github_pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.client = httpx.AsyncClient(headers=headers)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_workflows(self, owner: str, repo: str) -> WorkflowListResponse:
        """List workflows for a repository.

        Args:
            owner: GitHub repository owner (user or organisation).
            repo: GitHub repository name.

        Returns:
            A :class:`WorkflowListResponse` containing the total count and
            list of workflow definitions.

        Raises:
            ActionsNotFoundError: Repository or resource not found (404).
            ActionsPermissionError: Insufficient permissions (403).
            ActionsGitHubError: GitHub server error (5xx).
        """
        url = f"{self.api_base_url}/repos/{owner}/{repo}/actions/workflows"
        data = await self._get(url)
        return WorkflowListResponse.model_validate(data)

    async def list_runs(
        self,
        owner: str,
        repo: str,
        filters: RunFilterParams | None = None,
    ) -> WorkflowRunListResponse:
        """List workflow runs for a repository.

        Args:
            owner: GitHub repository owner.
            repo: GitHub repository name.
            filters: Optional query parameter filters (branch, status, actor,
                event, per_page).

        Returns:
            A :class:`WorkflowRunListResponse` containing the total count and
            list of workflow run items.

        Raises:
            ActionsNotFoundError: Repository not found (404).
            ActionsPermissionError: Insufficient permissions (403).
            ActionsGitHubError: GitHub server error (5xx).
        """
        url = f"{self.api_base_url}/repos/{owner}/{repo}/actions/runs"
        params = self._build_filter_params(filters)
        data = await self._get(url, params=params)
        return WorkflowRunListResponse.model_validate(data)

    async def get_run_detail(
        self, owner: str, repo: str, run_id: int
    ) -> WorkflowRunDetailResponse:
        """Retrieve detailed information for a workflow run including jobs.

        Makes two API calls — one for the run itself and one for its jobs —
        then merges the results into a single hierarchical response.

        Args:
            owner: GitHub repository owner.
            repo: GitHub repository name.
            run_id: GitHub Actions workflow run ID.

        Returns:
            A :class:`WorkflowRunDetailResponse` with run metadata, jobs,
            and steps.

        Raises:
            ActionsNotFoundError: Run not found (404).
            ActionsPermissionError: Insufficient permissions (403).
            ActionsGitHubError: GitHub server error (5xx).
        """
        run_url = f"{self.api_base_url}/repos/{owner}/{repo}/actions/runs/{run_id}"
        jobs_url = (
            f"{self.api_base_url}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        )

        run_data = await self._get(run_url)
        jobs_data = await self._get(jobs_url)

        # Parse individual jobs with their steps
        jobs: list[JobItem] = [
            JobItem.model_validate(job) for job in jobs_data.get("jobs", [])
        ]

        # Merge run data with parsed jobs into the detail response
        run_data["jobs"] = [job.model_dump() for job in jobs]
        return WorkflowRunDetailResponse.model_validate(run_data)

    async def dispatch_workflow(
        self,
        owner: str,
        repo: str,
        workflow_id: int,
        ref: str,
        inputs: dict[str, str] | None = None,
    ) -> None:
        """Trigger a workflow dispatch.

        Args:
            owner: GitHub repository owner.
            repo: GitHub repository name.
            workflow_id: Numeric workflow ID.
            ref: Branch, tag, or commit SHA to dispatch against.
            inputs: Optional key-value inputs forwarded to the workflow.

        Raises:
            ActionsNotFoundError: Workflow or repository not found (404).
            ActionsPermissionError: Insufficient permissions (403).
            ActionsValidationError: GitHub rejected the payload (422).
            ActionsGitHubError: GitHub server error (5xx).
        """
        url = (
            f"{self.api_base_url}/repos/{owner}/{repo}"
            f"/actions/workflows/{workflow_id}/dispatches"
        )
        payload: dict[str, Any] = {"ref": ref}
        if inputs is not None:
            payload["inputs"] = inputs

        start = time.monotonic()
        logger.info(
            "Dispatching workflow",
            extra={
                "url": url,
                "owner": owner,
                "repo": repo,
                "workflow_id": workflow_id,
            },
        )

        try:
            response = await self.client.post(url, json=payload)
        except httpx.RequestError as exc:
            logger.error(
                "Request error dispatching workflow",
                extra={"url": url, "error": str(exc)},
            )
            raise ActionsGitHubError(
                error_message=f"Failed to connect to GitHub API: {exc}",
                error_details={"exception": type(exc).__name__},
            ) from exc

        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.info(
            "Workflow dispatch response",
            extra={
                "url": url,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )

        if response.status_code == 204:
            return

        self._handle_error_response(response)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a GET request against the GitHub API.

        Handles timing, logging, and error response mapping.

        Args:
            url: Fully qualified GitHub API URL.
            params: Optional query parameters.

        Returns:
            Parsed JSON response body.

        Raises:
            ActionsNotFoundError: 404 from GitHub.
            ActionsPermissionError: 403 from GitHub.
            ActionsGitHubError: 5xx from GitHub or connection failure.
        """
        start = time.monotonic()
        logger.info("GitHub API request", extra={"url": url, "params": params})

        try:
            response = await self.client.get(url, params=params)
        except httpx.RequestError as exc:
            logger.error(
                "Request error contacting GitHub API",
                extra={"url": url, "error": str(exc)},
            )
            raise ActionsGitHubError(
                error_message=f"Failed to connect to GitHub API: {exc}",
                error_details={"exception": type(exc).__name__},
            ) from exc

        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.info(
            "GitHub API response",
            extra={
                "url": url,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]

        self._handle_error_response(response)
        # _handle_error_response always raises, but we need a return for mypy
        raise AssertionError("Unreachable")  # pragma: no cover

    @staticmethod
    def _build_filter_params(
        filters: RunFilterParams | None,
    ) -> dict[str, Any]:
        """Build query parameter dict from a RunFilterParams model.

        Only non-None filter values are included in the returned dict.

        Args:
            filters: Optional filter parameters.

        Returns:
            Dictionary of query parameter key-value pairs.
        """
        if filters is None:
            return {}

        params: dict[str, Any] = {}
        if filters.branch is not None:
            params["branch"] = filters.branch
        if filters.status is not None:
            params["status"] = filters.status
        if filters.actor is not None:
            params["actor"] = filters.actor
        if filters.event is not None:
            params["event"] = filters.event
        params["per_page"] = filters.per_page
        return params

    @staticmethod
    def _handle_error_response(response: httpx.Response) -> None:
        """Map a non-success HTTP response to the appropriate domain exception.

        Args:
            response: The httpx response object.

        Raises:
            ActionsNotFoundError: For 404 responses.
            ActionsPermissionError: For 403 responses.
            ActionsValidationError: For 422 responses.
            ActionsGitHubError: For 5xx or other unexpected responses.
        """
        status = response.status_code
        try:
            body = response.json()
        except Exception:
            body = response.text

        details: dict[str, Any] = {
            "github_status": status,
            "github_body": body if isinstance(body, str) else body,
        }

        if status == 404:
            message = (
                body.get("message", "Resource not found")
                if isinstance(body, dict)
                else "Resource not found"
            )
            raise ActionsNotFoundError(
                error_message=f"GitHub resource not found: {message}",
                error_details=details,
            )

        if status == 403:
            message = (
                body.get("message", "Permission denied")
                if isinstance(body, dict)
                else "Permission denied"
            )
            raise ActionsPermissionError(
                error_message=f"GitHub permission denied: {message}",
                error_details=details,
            )

        if status == 422:
            message = (
                body.get("message", "Validation failed")
                if isinstance(body, dict)
                else "Validation failed"
            )
            raise ActionsValidationError(
                error_message=f"GitHub validation error: {message}",
                error_details=details,
            )

        if status >= 500:
            raise ActionsGitHubError(
                error_message=f"GitHub API server error (HTTP {status})",
                error_details=details,
            )

        # Catch-all for unexpected non-2xx status codes (e.g. 400, 401)
        raise ActionsGitHubError(
            error_message=f"GitHub API returned unexpected status {status}",
            error_details=details,
        )


def get_actions_service() -> ActionsService:
    """Factory function to create a configured ActionsService instance.

    Retrieves the GitHub PAT from :class:`SecretsProvider` and returns
    a service instance ready for use.

    Returns:
        A configured :class:`ActionsService` instance.
    """
    secrets_provider = SecretsProvider()
    github_pat = secrets_provider.get_secret(
        "dispatch/github-pat", env_fallback="GITHUB_PAT"
    )
    return ActionsService(github_pat=github_pat)
