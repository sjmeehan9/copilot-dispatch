"""Shared E2E test helper utilities.

Provides an HTTP client wrapper for the Copilot Dispatch production API, polling
helpers for async workflow completion, and GitHub CLI helpers for creating
and cleaning up test pull requests.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({"success", "failure", "timeout"})
_DEFAULT_POLL_INTERVAL = 15  # seconds
_DEFAULT_POLL_TIMEOUT = 1800  # 30 minutes — matches agent-executor workflow timeout


# ---------------------------------------------------------------------------
# E2E Client
# ---------------------------------------------------------------------------


class E2EClient:
    """Synchronous HTTP client for the Copilot Dispatch production API.

    Wraps ``httpx.Client`` with pre-configured authentication headers,
    convenience methods for run CRUD, and a polling helper for async
    workflow completion.

    Args:
        base_url: Production API base URL (e.g. ``https://<apprunner-url>``).
        api_key: API key for ``X-API-Key`` authentication.
        timeout: Default request timeout in seconds.
    """

    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # -- API methods -------------------------------------------------------

    def create_run(
        self,
        role: str,
        repository: str,
        branch: str,
        agent_instructions: str,
        model: str,
        pr_number: int | None = None,
        system_instructions: str | None = None,
        callback_url: str | None = None,
        max_transient_errors: int = 3,
    ) -> dict:
        """Submit a new agent run via ``POST /agent/run``.

        Args:
            role: Agent role (``implement``, ``review``, ``merge``).
            repository: GitHub repository in ``owner/repo`` format.
            branch: Target branch name.
            agent_instructions: Instructions for the agent.
            model: AI model identifier.
            pr_number: Pull request number (required for review/merge).
            system_instructions: Optional system-level instructions.
            callback_url: Optional webhook URL for result delivery.
            max_transient_errors: Maximum consecutive transient 5xx errors
                to retry before raising.

        Returns:
            The parsed JSON response dict (includes ``run_id``, ``status``).
        """
        payload: dict = {
            "repository": repository,
            "branch": branch,
            "role": role,
            "agent_instructions": agent_instructions,
            "model": model,
        }
        if pr_number is not None:
            payload["pr_number"] = pr_number
        if system_instructions is not None:
            payload["system_instructions"] = system_instructions
        if callback_url is not None:
            payload["callback_url"] = callback_url

        consecutive_errors = 0
        while True:
            response = self._client.post("/agent/run", json=payload)
            try:
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    consecutive_errors += 1
                    logger.warning(
                        "Transient %d creating run (%d/%d)",
                        exc.response.status_code,
                        consecutive_errors,
                        max_transient_errors,
                    )
                    if consecutive_errors >= max_transient_errors:
                        raise
                    time.sleep(2 * consecutive_errors)
                    continue
                raise

    def get_run(self, run_id: str) -> dict:
        """Retrieve run status via ``GET /agent/run/{run_id}``.

        Args:
            run_id: The unique run identifier.

        Returns:
            The parsed JSON response dict.
        """
        response = self._client.get(f"/agent/run/{run_id}")
        response.raise_for_status()
        return response.json()

    def poll_until_complete(
        self,
        run_id: str,
        timeout: int = _DEFAULT_POLL_TIMEOUT,
        interval: int = _DEFAULT_POLL_INTERVAL,
        max_transient_errors: int = 5,
    ) -> dict:
        """Poll ``GET /agent/run/{run_id}`` until the run reaches a terminal state.

        Terminal states are ``success`` and ``failure``.  The method polls at
        ``interval``-second intervals until either a terminal state is reached
        or ``timeout`` seconds elapse.

        Transient server errors (5xx) are retried up to ``max_transient_errors``
        consecutive times before being raised.

        Args:
            run_id: The unique run identifier.
            timeout: Maximum wait time in seconds.
            interval: Time between poll requests in seconds.
            max_transient_errors: Maximum consecutive 5xx errors before raising.

        Returns:
            The final run state dict.  If timeout expires, the last known
            state is returned (the calling test should assert on the status).
        """
        deadline = time.monotonic() + timeout
        last_state: dict = {}
        consecutive_errors = 0

        while time.monotonic() < deadline:
            try:
                last_state = self.get_run(run_id)
                consecutive_errors = 0  # reset on success
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    consecutive_errors += 1
                    logger.warning(
                        "Transient %d error polling run_id=%s (%d/%d)",
                        exc.response.status_code,
                        run_id,
                        consecutive_errors,
                        max_transient_errors,
                    )
                    if consecutive_errors >= max_transient_errors:
                        raise
                    time.sleep(interval)
                    continue
                raise

            status = last_state.get("status", "")
            logger.info("Poll run_id=%s  status=%s", run_id, status)

            if status in _TERMINAL_STATUSES:
                return last_state

            time.sleep(interval)

        logger.warning(
            "Polling timed out after %ds for run_id=%s (last status=%s)",
            timeout,
            run_id,
            last_state.get("status"),
        )
        return last_state

    # -- Unauthenticated requests ------------------------------------------

    def get_health(self) -> httpx.Response:
        """Send a ``GET /health`` request (no API key required).

        Returns:
            The raw ``httpx.Response`` object.
        """
        return self._client.get("/health")

    def post_run_no_auth(self, payload: dict | None = None) -> httpx.Response:
        """Send a ``POST /agent/run`` without the API key header.

        Useful for testing authentication rejection.

        Args:
            payload: Optional JSON payload.

        Returns:
            The raw ``httpx.Response`` object.
        """
        client = httpx.Client(
            base_url=self._base_url,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        try:
            return client.post("/agent/run", json=payload or {})
        finally:
            client.close()

    # -- Actions API methods -----------------------------------------------

    def list_workflows(self, owner: str, repo: str) -> httpx.Response:
        """Send ``GET /actions/{owner}/{repo}/workflows``.

        Args:
            owner: Repository owner.
            repo: Repository name.

        Returns:
            The raw ``httpx.Response`` object.
        """
        return self._client.get(f"/actions/{owner}/{repo}/workflows")

    def list_runs(
        self,
        owner: str,
        repo: str,
        branch: str | None = None,
        status: str | None = None,
    ) -> httpx.Response:
        """Send ``GET /actions/{owner}/{repo}/runs`` with optional filters.

        Args:
            owner: Repository owner.
            repo: Repository name.
            branch: Optional branch filter.
            status: Optional status filter.

        Returns:
            The raw ``httpx.Response`` object.
        """
        params: dict[str, str] = {}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        return self._client.get(f"/actions/{owner}/{repo}/runs", params=params)

    def get_run_detail(self, owner: str, repo: str, run_id: int) -> httpx.Response:
        """Send ``GET /actions/{owner}/{repo}/runs/{run_id}``.

        Args:
            owner: Repository owner.
            repo: Repository name.
            run_id: GitHub Actions run ID.

        Returns:
            The raw ``httpx.Response`` object.
        """
        return self._client.get(f"/actions/{owner}/{repo}/runs/{run_id}")

    def dispatch_workflow(
        self,
        owner: str,
        repo: str,
        workflow_id: str,
        ref: str = "main",
        inputs: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send ``POST /actions/{owner}/{repo}/workflows/{workflow_id}/dispatch``.

        Args:
            owner: Repository owner.
            repo: Repository name.
            workflow_id: Workflow file name or numeric ID.
            ref: Branch or tag ref to dispatch on.
            inputs: Optional key-value inputs forwarded to the workflow.

        Returns:
            The raw ``httpx.Response`` object.
        """
        payload: dict[str, str | dict[str, str]] = {"ref": ref}
        if inputs is not None:
            payload["inputs"] = inputs
        return self._client.post(
            f"/actions/{owner}/{repo}/workflows/{workflow_id}/dispatch",
            json=payload,
        )

    def actions_no_auth(self, owner: str, repo: str) -> httpx.Response:
        """Send ``GET /actions/{owner}/{repo}/workflows`` without API key.

        Args:
            owner: Repository owner.
            repo: Repository name.

        Returns:
            The raw ``httpx.Response`` object.
        """
        client = httpx.Client(
            base_url=self._base_url,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        try:
            return client.get(f"/actions/{owner}/{repo}/workflows")
        finally:
            client.close()


# ---------------------------------------------------------------------------
# GitHub CLI helpers
# ---------------------------------------------------------------------------


def create_test_pr(
    repository: str,
    branch_name: str,
    title: str,
    body: str,
    file_changes: dict[str, str],
) -> int:
    """Create a test pull request on a GitHub repository via the ``gh`` CLI.

    Creates a new branch, commits file changes, pushes, and opens a PR.

    Args:
        repository: Repository in ``owner/repo`` format.
        branch_name: Name for the new feature branch.
        title: Pull request title.
        body: Pull request body/description.
        file_changes: Mapping of file path → file content to commit.

    Returns:
        The pull request number.

    Raises:
        RuntimeError: If any ``gh``/``git`` command fails.
    """
    _ensure_gh_available()

    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir) / "repo"

        # Clone the repository
        _run_cmd(
            ["gh", "repo", "clone", repository, str(work_dir), "--", "--depth=1"],
            cwd=tmpdir,
        )

        # Create and checkout the new branch
        _run_cmd(["git", "checkout", "-b", branch_name], cwd=str(work_dir))

        # Apply file changes
        for file_path, content in file_changes.items():
            target = work_dir / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        # Stage and commit
        _run_cmd(["git", "add", "-A"], cwd=str(work_dir))
        _run_cmd(
            ["git", "commit", "-m", f"test: {title}"],
            cwd=str(work_dir),
            env_extra={
                "GIT_AUTHOR_NAME": "dispatch-e2e",
                "GIT_AUTHOR_EMAIL": "e2e@test.local",
                "GIT_COMMITTER_NAME": "dispatch-e2e",
                "GIT_COMMITTER_EMAIL": "e2e@test.local",
            },
        )

        # Push the branch
        _run_cmd(["git", "push", "origin", branch_name], cwd=str(work_dir))

        # Create the PR
        result = _run_cmd(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repository,
                "--base",
                "main",
                "--head",
                branch_name,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=str(work_dir),
            capture=True,
        )

        # Extract PR number from the URL returned by gh pr create
        # Output is like: https://github.com/owner/repo/pull/42
        pr_url = result.strip().splitlines()[-1].strip()
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        logger.info(
            "Created test PR #%d on %s (branch: %s)", pr_number, repository, branch_name
        )
        return pr_number


def cleanup_test_branch(repository: str, branch_name: str) -> None:
    """Delete a remote branch on a GitHub repository.

    Silently ignores errors if the branch does not exist or was already
    deleted.

    Args:
        repository: Repository in ``owner/repo`` format.
        branch_name: Branch name to delete.
    """
    try:
        _run_cmd(
            [
                "gh",
                "api",
                f"repos/{repository}/git/refs/heads/{branch_name}",
                "--method",
                "DELETE",
            ],
        )
        logger.info("Deleted branch %s on %s", branch_name, repository)
    except RuntimeError:
        logger.debug(
            "Branch %s may not exist on %s — skipping cleanup", branch_name, repository
        )


def cleanup_test_pr(repository: str, pr_number: int) -> None:
    """Close an open pull request on a GitHub repository.

    Silently ignores errors if the PR is already closed or merged.

    Args:
        repository: Repository in ``owner/repo`` format.
        pr_number: Pull request number to close.
    """
    try:
        _run_cmd(
            ["gh", "pr", "close", str(pr_number), "--repo", repository],
        )
        logger.info("Closed PR #%d on %s", pr_number, repository)
    except RuntimeError:
        logger.debug(
            "PR #%d may already be closed on %s — skipping", pr_number, repository
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_gh_available() -> None:
    """Verify that the ``gh`` CLI is installed and available on PATH.

    Raises:
        RuntimeError: If ``gh`` is not found.
    """
    if shutil.which("gh") is None:
        raise RuntimeError(
            "The GitHub CLI (gh) is required for E2E tests but was not found on PATH. "
            "Install it via: brew install gh"
        )


def _run_cmd(
    cmd: list[str],
    cwd: str | None = None,
    capture: bool = False,
    env_extra: dict[str, str] | None = None,
) -> str:
    """Run a shell command and return its stdout.

    Args:
        cmd: Command and arguments.
        cwd: Working directory for the command.
        capture: If ``True``, return stdout; otherwise return empty string.
        env_extra: Additional environment variables to merge into the process env.

    Returns:
        Captured stdout (if ``capture=True``), otherwise empty string.

    Raises:
        RuntimeError: If the command exits with a non-zero code.
    """
    env = None
    if env_extra:
        env = {**os.environ, **env_extra}

    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    return result.stdout if capture else ""
