"""Service for dispatching GitHub Actions workflows via the GitHub REST API."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.src.api.models import AgentRunRequest
from app.src.auth.secrets import SecretsProvider
from app.src.config import get_settings
from app.src.exceptions import DispatchError, ExternalServiceError

logger = logging.getLogger(__name__)


class DispatchService:
    """Service for dispatching GitHub Actions workflows via the GitHub REST API."""

    def __init__(
        self,
        github_pat: str,
        github_owner: str,
        github_repo: str,
        workflow_id: str,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        """Initialise the DispatchService.

        Args:
            github_pat: GitHub Personal Access Token.
            github_owner: GitHub repository owner.
            github_repo: GitHub repository name.
            workflow_id: GitHub Actions workflow ID or filename.
            api_base_url: Base URL for the GitHub API.
        """
        self.github_owner = github_owner
        self.github_repo = github_repo
        self.workflow_id = workflow_id
        self.api_base_url = api_base_url.rstrip("/")

        headers = {
            "Authorization": f"Bearer {github_pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.client = httpx.AsyncClient(headers=headers)

    async def dispatch_workflow(
        self,
        run_id: str,
        request: AgentRunRequest,
        callback_url: str | None = None,
        api_result_url: str | None = None,
    ) -> None:
        """Dispatch a GitHub Actions workflow.

        Args:
            run_id: The unique identifier for the run.
            request: The agent run request payload.
            callback_url: Optional callback URL for webhook delivery.
            api_result_url: Optional URL for the workflow to POST results back to.

        Raises:
            DispatchError: If the workflow dispatch fails due to client error (401, 403, 404, 422).
            ExternalServiceError: If the workflow dispatch fails due to server error or rate limit.
        """
        url = f"{self.api_base_url}/repos/{self.github_owner}/{self.github_repo}/actions/workflows/{self.workflow_id}/dispatches"

        inputs: dict[str, Any] = {
            "run_id": run_id,
            "target_repository": request.repository,
            "target_branch": request.branch,
            "role": request.role,
            "agent_instructions": request.agent_instructions,
            "model": request.model,
            "timeout_minutes": str(request.timeout_minutes),
        }

        config_payload: dict[str, Any] = {}
        if request.system_instructions is not None:
            config_payload["system_instructions"] = request.system_instructions
        if request.pr_number is not None:
            config_payload["pr_number"] = int(request.pr_number)
        if request.skill_paths is not None:
            config_payload["skill_paths"] = request.skill_paths
        if request.agent_paths is not None:
            config_payload["agent_paths"] = request.agent_paths

        # Use the callback_url from the method argument if provided, otherwise fallback to request
        final_callback_url = callback_url or (
            str(request.callback_url) if request.callback_url else None
        )
        if final_callback_url is not None:
            config_payload["callback_url"] = final_callback_url

        if config_payload:
            inputs["config"] = json.dumps(config_payload)

        if api_result_url is not None:
            inputs["api_result_url"] = api_result_url

        payload = {
            "ref": "main",  # Default branch of the orchestration repo
            "inputs": inputs,
        }

        logger.info(
            "Dispatching workflow",
            extra={
                "run_id": run_id,
                "repository": request.repository,
                "workflow_id": self.workflow_id,
                "role": request.role,
            },
        )

        try:
            response = await self.client.post(url, json=payload)
        except httpx.RequestError as e:
            raise ExternalServiceError(
                error_message=f"Failed to connect to GitHub API: {str(e)}",
                error_details={"exception": type(e).__name__},
            ) from e

        if response.status_code == 204:
            return

        if response.status_code in (401, 403):
            if response.status_code == 403 and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or "rate limit" in response.text.lower()
            ):
                raise ExternalServiceError(
                    error_message="GitHub API rate limit exceeded.",
                    error_details={
                        "retry_after": response.headers.get("Retry-After"),
                        "rate_limit_reset": response.headers.get("X-RateLimit-Reset"),
                    },
                )
            raise DispatchError(
                error_message="GitHub API authentication failed. Check the PAT has 'repo' scope and is not expired.",
                error_details={"github_status": response.status_code},
            )
        elif response.status_code == 429:
            raise ExternalServiceError(
                error_message="GitHub API rate limit exceeded.",
                error_details={
                    "retry_after": response.headers.get("Retry-After"),
                    "rate_limit_reset": response.headers.get("X-RateLimit-Reset"),
                },
            )
        elif response.status_code == 404:
            raise DispatchError(
                error_message=f"Workflow or repository not found: {self.github_owner}/{self.github_repo}/{self.workflow_id}",
                error_details={"github_status": 404},
            )
        elif response.status_code == 422:
            try:
                error_data = response.json()
            except ValueError:
                error_data = response.text
            raise DispatchError(
                error_message="GitHub rejected the workflow dispatch payload.",
                error_details={"github_status": 422, "github_error": error_data},
            )
        else:
            raise ExternalServiceError(
                error_message=f"GitHub API returned unexpected status {response.status_code}",
                error_details={
                    "github_status": response.status_code,
                    "github_body": response.text[:500],
                },
            )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.aclose()


def get_dispatch_service() -> DispatchService:
    """Factory function to create a DispatchService instance.

    Retrieves configuration from settings and secrets.

    Returns:
        A configured DispatchService instance.
    """
    settings = get_settings()
    secrets_provider = SecretsProvider()
    github_pat = secrets_provider.get_secret(
        "dispatch/github-pat", env_fallback="GITHUB_PAT"
    )

    return DispatchService(
        github_pat=github_pat,
        github_owner=settings.github_owner,
        github_repo=settings.github_repo,
        workflow_id=settings.github_workflow_id,
    )
