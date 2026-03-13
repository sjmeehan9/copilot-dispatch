"""E2E tests for the Review agent flow.

Validates the complete review lifecycle through the production stack:
  POST /agent/run (role: review, pr_number) → GitHub Actions workflow
  dispatch → Copilot agent reviews the PR diff → PR review submitted via
  GitHub API → result posted back → GET /agent/run/{run_id} returns
  structured assessment.

Tests are gated behind ``--e2e-confirm`` and require live infrastructure.

Run with::

    pytest tests/e2e/test_review_flow.py -v --e2e-confirm -s

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
class TestReviewFlow:
    """End-to-end tests for the review agent role.

    Each test creates a purpose-built PR, submits a review request via the
    API, and validates the structured assessment returned by the agent.
    Cleanup runs in ``finally`` blocks.
    """

    def test_review_produces_structured_assessment(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify the review flow produces a complete structured assessment.

        Creates a test PR with a ``power(base, exp)`` function and submits
        a review request.  Validates the result contains ``assessment``,
        ``review_comments``, and ``pr_approved`` fields.
        """
        repository = str(e2e_config["repository"])
        model = str(e2e_config["model"])
        timeout = int(e2e_config["timeout"])
        branch = str(e2e_config["branch"])

        test_branch = f"e2e-review-assessment-{uuid.uuid4().hex[:8]}"
        pr_number: int | None = None

        try:
            # 1. Create a test PR with a power function
            pr_number = create_test_pr(
                repository=repository,
                branch_name=test_branch,
                title="Add power function",
                body="Adds a power(base, exp) function for E2E review testing.",
                file_changes={
                    "src/power.py": (
                        "def power(base, exp):\n"
                        '    """Raise base to the power of exp."""\n'
                        "    result = 1\n"
                        "    for _ in range(exp):\n"
                        "        result *= base\n"
                        "    return result\n"
                    ),
                },
            )
            logger.info("Created test PR #%d for review assessment", pr_number)

            # 2. Submit the review run
            response = e2e_client.create_run(
                role="review",
                repository=repository,
                branch=branch,
                agent_instructions=(
                    "Review this PR thoroughly. Evaluate code correctness, "
                    "test coverage, and any security concerns."
                ),
                model=model,
                pr_number=pr_number,
            )
            run_id = response["run_id"]
            logger.info("Review run created: run_id=%s for PR #%d", run_id, pr_number)

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

            assert result.get("assessment") in (
                "approve",
                "request_changes",
                "comment",
            ), f"Expected valid assessment, got: {result.get('assessment')}"

            assert isinstance(
                result.get("review_comments"), list
            ), "Expected review_comments list"

            assert isinstance(
                result.get("pr_approved"), bool
            ), "Expected pr_approved boolean"

            assert final_state.get("error") is None

            logger.info(
                "Review flow succeeded: assessment=%s, %d comments, approved=%s",
                result["assessment"],
                len(result["review_comments"]),
                result["pr_approved"],
            )

        finally:
            if pr_number is not None:
                cleanup_test_pr(repository, pr_number)
            cleanup_test_branch(repository, test_branch)

    def test_review_requests_changes_on_issues(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify the agent flags issues in a PR with known problems.

        Creates a PR with a ``divide_unsafe`` function that lacks error
        handling for division by zero and has no tests.  Validates the
        agent requests changes or comments with specific feedback.
        """
        repository = str(e2e_config["repository"])
        model = str(e2e_config["model"])
        timeout = int(e2e_config["timeout"])
        branch = str(e2e_config["branch"])

        test_branch = f"e2e-review-issues-{uuid.uuid4().hex[:8]}"
        pr_number: int | None = None

        try:
            # 1. Create a PR with intentional issues
            pr_number = create_test_pr(
                repository=repository,
                branch_name=test_branch,
                title="Add divide_unsafe function",
                body="Adds divide_unsafe — intentionally missing error handling.",
                file_changes={
                    "src/divide_unsafe.py": (
                        "def divide_unsafe(a, b):\n"
                        '    """Divide a by b. No error handling."""\n'
                        "    return a / b\n"
                    ),
                },
            )
            logger.info("Created test PR #%d with intentional issues", pr_number)

            # 2. Submit the review run
            response = e2e_client.create_run(
                role="review",
                repository=repository,
                branch=branch,
                agent_instructions=(
                    "Review this PR. Pay special attention to error handling "
                    "and test coverage."
                ),
                model=model,
                pr_number=pr_number,
            )
            run_id = response["run_id"]

            # 3. Poll until complete
            final_state = e2e_client.poll_until_complete(run_id, timeout=timeout)

            # 4. Validate
            assert final_state["status"] == "success", (
                f"Expected status 'success', got '{final_state.get('status')}'. "
                f"Error: {final_state.get('error')}"
            )

            result = final_state.get("result")
            assert result is not None, "Expected non-null result on success"

            # Agent should flag issues — either request_changes or comment
            assert result.get("assessment") in (
                "request_changes",
                "comment",
            ), f"Expected 'request_changes' or 'comment', got: {result.get('assessment')}"

            # Should have at least one review comment about the issue
            assert isinstance(
                result.get("review_comments"), list
            ), "Expected review_comments list"
            assert (
                len(result["review_comments"]) > 0
            ), "Expected at least one review comment flagging the missing error handling"

            logger.info(
                "Review issues flow succeeded: assessment=%s, %d comments",
                result["assessment"],
                len(result["review_comments"]),
            )

        finally:
            if pr_number is not None:
                cleanup_test_pr(repository, pr_number)
            cleanup_test_branch(repository, test_branch)
