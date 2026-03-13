"""E2E tests for the Merge agent flow.

Validates the complete merge lifecycle through the production stack:
  POST /agent/run (role: merge, pr_number) → GitHub Actions workflow
  dispatch → Copilot agent merges the PR (or resolves conflicts) →
  merge executed via GitHub API → result posted back →
  GET /agent/run/{run_id} returns merge status and SHA.

Tests are gated behind ``--e2e-confirm`` and require live infrastructure.

Run with::

    pytest tests/e2e/test_merge_flow.py -v --e2e-confirm -s

"""

from __future__ import annotations

import logging
import uuid

import pytest

from tests.e2e.helpers import (
    E2EClient,
    cleanup_test_branch,
    cleanup_test_pr,
    create_test_pr,
)

logger = logging.getLogger(__name__)


@pytest.mark.requires_e2e
class TestMergeFlow:
    """End-to-end tests for the merge agent role.

    Each test covers a distinct sub-scenario of the merge flow: clean
    merges and conflict handling.  Cleanup runs in ``finally`` blocks.
    """

    def test_merge_clean_pr(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify the agent merges a clean PR with no conflicts.

        Creates a simple PR adding an ``is_positive(n)`` function, submits
        a merge request, and validates that the PR is merged with a valid
        ``merge_sha``.
        """
        repository = str(e2e_config["repository"])
        model = str(e2e_config["model"])
        timeout = int(e2e_config["timeout"])
        branch = str(e2e_config["branch"])

        test_branch = f"e2e-merge-clean-{uuid.uuid4().hex[:8]}"
        pr_number: int | None = None

        try:
            # 1. Create a clean test PR
            pr_number = create_test_pr(
                repository=repository,
                branch_name=test_branch,
                title="Add is_positive function",
                body="Adds a simple is_positive(n) check for E2E merge testing.",
                file_changes={
                    f"src/is_positive_{uuid.uuid4().hex[:6]}.py": (
                        "def is_positive(n):\n"
                        '    """Return True if n is positive."""\n'
                        "    return n > 0\n"
                    ),
                },
            )
            logger.info("Created test PR #%d for clean merge", pr_number)

            # 2. Submit the merge run
            response = e2e_client.create_run(
                role="merge",
                repository=repository,
                branch=branch,
                agent_instructions="Merge this PR if tests pass.",
                model=model,
                pr_number=pr_number,
            )
            run_id = response["run_id"]
            logger.info("Merge run created: run_id=%s for PR #%d", run_id, pr_number)

            # 3. Poll until complete
            final_state = e2e_client.poll_until_complete(run_id, timeout=timeout)

            # 4. Validate terminal state
            assert final_state["status"] == "success", (
                f"Expected status 'success', got '{final_state.get('status')}'. "
                f"Error: {final_state.get('error')}"
            )

            # 5. Validate result structure
            result = final_state.get("result")
            assert result is not None, "Expected non-null result on success"

            assert result.get("merge_status") in (
                "merged",
                "conflicts_resolved_and_merged",
                "conflicts_unresolved",
            ), f"Unexpected merge_status: {result.get('merge_status')}"

            # If merged, merge_sha should be present
            if result["merge_status"] in ("merged", "conflicts_resolved_and_merged"):
                assert isinstance(
                    result.get("merge_sha"), str
                ), "Expected merge_sha string when merged"
                assert len(result["merge_sha"]) > 0, "Expected non-empty merge_sha"

            assert final_state.get("error") is None

            logger.info(
                "Merge flow succeeded: merge_status=%s, merge_sha=%s",
                result["merge_status"],
                result.get("merge_sha", "N/A"),
            )

        finally:
            # If the PR was merged, we only need to clean the branch
            # If not merged, close the PR first
            if pr_number is not None:
                cleanup_test_pr(repository, pr_number)
            cleanup_test_branch(repository, test_branch)

    def test_merge_reports_status_on_failure(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify the agent produces a valid status even for difficult merges.

        Creates a PR and submits a merge request.  Validates the agent
        completes with a valid ``merge_status`` regardless of outcome.
        """
        repository = str(e2e_config["repository"])
        model = str(e2e_config["model"])
        timeout = int(e2e_config["timeout"])
        branch = str(e2e_config["branch"])

        test_branch = f"e2e-merge-status-{uuid.uuid4().hex[:8]}"
        pr_number: int | None = None

        try:
            # 1. Create a test PR
            pr_number = create_test_pr(
                repository=repository,
                branch_name=test_branch,
                title="Add merge test module",
                body="Adds a module for E2E merge status testing.",
                file_changes={
                    f"src/merge_test_{uuid.uuid4().hex[:6]}.py": (
                        "def greet(name):\n"
                        '    """Return a greeting."""\n'
                        '    return f"Hello, {name}!"\n'
                    ),
                },
            )
            logger.info("Created test PR #%d for merge status test", pr_number)

            # 2. Submit the merge run
            response = e2e_client.create_run(
                role="merge",
                repository=repository,
                branch=branch,
                agent_instructions="Attempt to merge this PR. Report any issues.",
                model=model,
                pr_number=pr_number,
            )
            run_id = response["run_id"]

            # 3. Poll until complete
            final_state = e2e_client.poll_until_complete(run_id, timeout=timeout)

            # 4. System should reach a terminal state
            assert final_state["status"] in (
                "success",
                "failure",
            ), f"Expected terminal status, got: {final_state['status']}"

            # 5. If success, validate merge result structure
            if final_state["status"] == "success":
                result = final_state.get("result")
                assert result is not None, "Expected non-null result on success"
                assert result.get("merge_status") in (
                    "merged",
                    "conflicts_resolved_and_merged",
                    "conflicts_unresolved",
                ), f"Unexpected merge_status: {result.get('merge_status')}"

                # If conflicts unresolved, conflict_files should be populated
                if result["merge_status"] == "conflicts_unresolved":
                    assert isinstance(
                        result.get("conflict_files"), list
                    ), "Expected conflict_files list"

            logger.info(
                "Merge status test complete: status=%s",
                final_state["status"],
            )

        finally:
            if pr_number is not None:
                cleanup_test_pr(repository, pr_number)
            cleanup_test_branch(repository, test_branch)
