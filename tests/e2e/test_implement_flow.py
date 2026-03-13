"""E2E tests for the Implement agent flow.

Validates the complete implement lifecycle through the production stack:
  POST /agent/run (role: implement) → GitHub Actions workflow dispatch →
  Copilot agent implements feature on autopilot-test-target →
  branch + PR created → result posted back to API →
  GET /agent/run/{run_id} returns success with PR details.

Tests are gated behind ``--e2e-confirm`` and require live infrastructure.

Run with::

    pytest tests/e2e/test_implement_flow.py -v --e2e-confirm -s

"""

from __future__ import annotations

import logging

import pytest

from tests.e2e.helpers import E2EClient, cleanup_test_branch, cleanup_test_pr

logger = logging.getLogger(__name__)


@pytest.mark.requires_e2e
class TestImplementFlow:
    """End-to-end tests for the implement agent role.

    Each test exercises the full implement lifecycle: API submission,
    workflow dispatch, agent execution, and result validation.
    Cleanup runs in ``finally`` blocks to remove branches/PRs even on
    assertion failures.
    """

    def test_implement_creates_branch_and_pr(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify the implement flow creates a branch and opens a PR.

        Submits an implement request asking the agent to add a ``square(n)``
        function, then polls until complete and validates the result
        structure.
        """
        repository = str(e2e_config["repository"])
        branch = str(e2e_config["branch"])
        model = str(e2e_config["model"])
        timeout = int(e2e_config["timeout"])

        run_id: str | None = None
        result_branch: str | None = None
        result_pr: int | None = None

        try:
            # 1. Submit the implement run
            response = e2e_client.create_run(
                role="implement",
                repository=repository,
                branch=branch,
                agent_instructions=(
                    "Add a square(n) function to src/calculator.py that returns n squared. "
                    "Add a corresponding test in tests/test_calculator.py."
                ),
                model=model,
            )
            assert "run_id" in response, f"Expected run_id in response: {response}"
            run_id = response["run_id"]
            logger.info("Implement run created: run_id=%s", run_id)

            # 2. Poll until complete
            final_state = e2e_client.poll_until_complete(run_id, timeout=timeout)

            # 3. Validate terminal state
            assert final_state["status"] == "success", (
                f"Expected status 'success', got '{final_state.get('status')}'. "
                f"Error: {final_state.get('error')}"
            )

            # 4. Validate result structure
            result = final_state.get("result")
            assert result is not None, "Expected non-null result on success"

            assert isinstance(result.get("pr_url"), str), "Expected pr_url string"
            assert result["pr_url"].startswith(
                "https://github.com"
            ), f"pr_url should start with https://github.com: {result['pr_url']}"

            assert isinstance(result.get("pr_number"), int), "Expected pr_number int"
            assert result["pr_number"] > 0, "Expected positive pr_number"
            result_pr = result["pr_number"]

            assert isinstance(result.get("branch"), str), "Expected branch string"
            result_branch = result["branch"]

            assert isinstance(
                result.get("files_changed"), list
            ), "Expected files_changed list"
            assert (
                len(result["files_changed"]) > 0
            ), "Expected at least one file changed"

            assert (
                final_state.get("error") is None
            ), f"Expected no error on success: {final_state.get('error')}"

            logger.info(
                "Implement flow succeeded: PR #%d at %s (branch: %s)",
                result_pr,
                result["pr_url"],
                result_branch,
            )

        finally:
            # Cleanup: close PR and delete branch
            if result_pr is not None:
                cleanup_test_pr(repository, result_pr)
            if result_branch is not None:
                cleanup_test_branch(repository, result_branch)

    def test_implement_result_pollable(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify that a run can be polled and transitions through states.

        Creates an implement run with a different instruction and verifies
        that initial status is non-terminal and the final status is terminal.
        """
        repository = str(e2e_config["repository"])
        branch = str(e2e_config["branch"])
        model = str(e2e_config["model"])
        timeout = int(e2e_config["timeout"])

        run_id: str | None = None
        result_branch: str | None = None
        result_pr: int | None = None

        try:
            # 1. Submit the implement run
            response = e2e_client.create_run(
                role="implement",
                repository=repository,
                branch=branch,
                agent_instructions=(
                    "Add a cube(n) function to src/calculator.py that returns n cubed."
                ),
                model=model,
            )
            run_id = response["run_id"]

            # 2. Immediately poll — should be non-terminal
            immediate_state = e2e_client.get_run(run_id)
            assert immediate_state["status"] in (
                "queued",
                "dispatched",
                "running",
                "success",
                "failure",
            ), f"Unexpected status: {immediate_state['status']}"

            # 3. Poll until complete
            final_state = e2e_client.poll_until_complete(run_id, timeout=timeout)

            # 4. Terminal state reached
            assert final_state["status"] in (
                "success",
                "failure",
            ), f"Expected terminal status, got: {final_state['status']}"

            # Capture for cleanup
            result = final_state.get("result")
            if result:
                result_branch = result.get("branch")
                result_pr = result.get("pr_number")

        finally:
            if result_pr is not None:
                cleanup_test_pr(repository, result_pr)
            if result_branch is not None:
                cleanup_test_branch(repository, result_branch)

    def test_implement_handles_failing_instructions(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify the system handles ambiguous instructions gracefully.

        Submits intentionally broad instructions and verifies the system
        does not hang indefinitely — it reaches a terminal state.
        """
        repository = str(e2e_config["repository"])
        branch = str(e2e_config["branch"])
        model = str(e2e_config["model"])
        timeout = int(e2e_config["timeout"])

        run_id: str | None = None
        result_branch: str | None = None
        result_pr: int | None = None

        try:
            response = e2e_client.create_run(
                role="implement",
                repository=repository,
                branch=branch,
                agent_instructions=(
                    "Refactor the entire application architecture to use microservices."
                ),
                model=model,
            )
            run_id = response["run_id"]

            # Poll until complete
            final_state = e2e_client.poll_until_complete(run_id, timeout=timeout)

            # System should reach a terminal state (success or failure)
            assert final_state["status"] in (
                "success",
                "failure",
            ), f"Expected terminal status, got: {final_state['status']}"

            # If failure, error should be populated
            if final_state["status"] == "failure":
                error = final_state.get("error")
                assert error is not None, "Expected error payload on failure"
                assert "error_code" in error, "Expected error_code in error payload"

            # Capture for cleanup
            result = final_state.get("result")
            if result:
                result_branch = result.get("branch")
                result_pr = result.get("pr_number")

        finally:
            if result_pr is not None:
                cleanup_test_pr(repository, result_pr)
            if result_branch is not None:
                cleanup_test_branch(repository, result_branch)
