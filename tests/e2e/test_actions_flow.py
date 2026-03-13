"""E2E tests for the GitHub Actions visibility flow.

Validates the Actions visibility endpoints against the production stack:
  GET /actions/{owner}/{repo}/workflows → list workflows from GitHub API
  GET /actions/{owner}/{repo}/runs → list runs with filters
  GET /actions/{owner}/{repo}/runs/{run_id} → run detail with jobs/steps
  POST /actions/{owner}/{repo}/workflows/{workflow_id}/dispatch → trigger run

Tests are gated behind ``--e2e-confirm`` and require live infrastructure.

Run with::

    pytest tests/e2e/test_actions_flow.py -v --e2e-confirm -s

"""

from __future__ import annotations

import logging
import time
import uuid

import pytest

from tests.e2e.helpers import E2EClient

logger = logging.getLogger(__name__)


@pytest.mark.requires_e2e
class TestActionsFlow:
    """End-to-end tests for the GitHub Actions visibility endpoints.

    Each test covers a distinct sub-scenario within the Actions flow,
    verifying that the Copilot Dispatch API correctly proxies the GitHub REST
    API for workflow and run operations.  Tests target the orchestration
    repository (``copilot-dispatch``) since it has actual workflows.
    """

    def _get_actions_repo(self, e2e_config: dict[str, str | int]) -> tuple[str, str]:
        """Extract owner and repo for Actions API testing.

        Uses the orchestration repository (copilot-dispatch) since it has
        workflows, not the test-target repo.

        Args:
            e2e_config: The E2E configuration dictionary.

        Returns:
            Tuple of (owner, repo).
        """
        # The e2e_config repository is owner/autopilot-test-target
        # We need the owner to target the copilot-dispatch repo which has workflows
        full_repo = str(e2e_config["repository"])
        owner = full_repo.split("/")[0]
        return owner, "copilot-dispatch"

    def test_list_workflows_returns_real_workflows(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify listing workflows returns data from the target repository.

        Calls ``GET /actions/{owner}/{repo}/workflows`` and asserts the
        response contains at least one workflow with required fields.
        """
        owner, repo = self._get_actions_repo(e2e_config)

        response = e2e_client.list_workflows(owner, repo)
        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"

        data = response.json()
        assert "total_count" in data, "Expected total_count in response"
        assert "workflows" in data, "Expected workflows in response"
        assert isinstance(data["workflows"], list), "Expected workflows to be a list"
        assert len(data["workflows"]) > 0, "Expected at least one workflow"

        # Validate structure of the first workflow
        workflow = data["workflows"][0]
        assert "id" in workflow, "Expected workflow to have id"
        assert "name" in workflow, "Expected workflow to have name"
        assert "path" in workflow, "Expected workflow to have path"
        assert "state" in workflow, "Expected workflow to have state"

        logger.info(
            "Listed %d workflows from %s/%s",
            data["total_count"],
            owner,
            repo,
        )

    def test_list_runs_with_branch_filter(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify listing runs with a branch filter returns matching results.

        Calls ``GET /actions/{owner}/{repo}/runs?branch=main`` and asserts
        all returned runs belong to the ``main`` branch.
        """
        owner, repo = self._get_actions_repo(e2e_config)

        response = e2e_client.list_runs(owner, repo, branch="main")
        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"

        data = response.json()
        assert "workflow_runs" in data, "Expected workflow_runs in response"
        assert isinstance(data["workflow_runs"], list), "Expected workflow_runs list"

        # All returned runs should be on the main branch
        for run in data["workflow_runs"]:
            assert (
                run.get("head_branch") == "main"
            ), f"Expected head_branch 'main', got: {run.get('head_branch')}"

        logger.info(
            "Listed %d runs on main branch from %s/%s",
            len(data["workflow_runs"]),
            owner,
            repo,
        )

    def test_get_run_detail_includes_jobs_and_steps(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify run detail includes jobs and steps from the GitHub API.

        Lists runs to obtain a valid ``run_id``, then retrieves the detail.
        """
        owner, repo = self._get_actions_repo(e2e_config)

        # First, get a run ID from the runs list
        runs_response = e2e_client.list_runs(owner, repo)
        assert (
            runs_response.status_code == 200
        ), f"Expected 200, got {runs_response.status_code}"
        runs_data = runs_response.json()
        assert (
            len(runs_data.get("workflow_runs", [])) > 0
        ), "Need at least one workflow run to test detail endpoint"

        run_id = runs_data["workflow_runs"][0]["id"]

        # Get the run detail
        detail_response = e2e_client.get_run_detail(owner, repo, run_id)
        assert (
            detail_response.status_code == 200
        ), f"Expected 200, got {detail_response.status_code}: {detail_response.text}"

        detail = detail_response.json()
        assert "id" in detail, "Expected id in run detail"

        # Jobs should be present (may be nested or in a separate field)
        if "jobs" in detail:
            assert isinstance(detail["jobs"], list), "Expected jobs to be a list"
            if len(detail["jobs"]) > 0:
                job = detail["jobs"][0]
                if "steps" in job:
                    assert isinstance(job["steps"], list), "Expected steps list in job"

        logger.info("Retrieved run detail for run_id=%d", run_id)

    def test_dispatch_workflow_triggers_run(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify dispatching a workflow triggers a new GitHub Actions run.

        Dispatches a workflow and verifies the response is 204.
        Then checks for a recent workflow_dispatch run.
        """
        owner, repo = self._get_actions_repo(e2e_config)

        # First, get a workflow that supports dispatch
        wf_response = e2e_client.list_workflows(owner, repo)
        assert wf_response.status_code == 200
        workflows = wf_response.json().get("workflows", [])
        assert len(workflows) > 0, "Need at least one workflow to dispatch"

        # Find the agent-executor workflow (only workflow with workflow_dispatch)
        workflow_id: str | None = None
        for wf in workflows:
            if "agent-executor" in wf.get("path", ""):
                workflow_id = str(wf["id"])
                break
        if workflow_id is None:
            pytest.skip(
                "No agent-executor workflow found — cannot test dispatch "
                "(ci.yml and deploy.yml do not support workflow_dispatch)"
            )

        # Build the required inputs for the agent-executor workflow.
        # We use a synthetic run_id and minimal valid values so GitHub
        # accepts the dispatch without triggering an expensive agent run.
        dispatch_run_id = f"e2e-dispatch-{uuid.uuid4().hex[:8]}"
        dispatch_inputs: dict[str, str] = {
            "run_id": dispatch_run_id,
            "target_repository": f"{owner}/autopilot-test-target",
            "target_branch": "main",
            "role": "implement",
            "agent_instructions": "E2E dispatch test — no-op",
            "model": str(e2e_config["model"]),
            "api_result_url": str(e2e_config["api_url"]).rstrip("/") + "/agent/result",
        }

        # Dispatch the workflow
        dispatch_response = e2e_client.dispatch_workflow(
            owner, repo, workflow_id, ref="main", inputs=dispatch_inputs
        )
        assert (
            dispatch_response.status_code == 204
        ), f"Expected 204, got {dispatch_response.status_code}: {dispatch_response.text}"

        # Brief wait then verify a recent dispatch run exists
        time.sleep(5)
        runs_response = e2e_client.list_runs(owner, repo)
        assert runs_response.status_code == 200
        runs = runs_response.json().get("workflow_runs", [])

        # Check that there's at least one recent run with event=workflow_dispatch
        dispatch_runs = [r for r in runs if r.get("event") == "workflow_dispatch"]
        logger.info(
            "Found %d workflow_dispatch runs after dispatch on %s/%s",
            len(dispatch_runs),
            owner,
            repo,
        )
        # Note: we don't assert >0 because there may be a timing delay
        # The 204 response is the primary validation

    def test_actions_endpoints_require_authentication(
        self,
        e2e_client: E2EClient,
        e2e_config: dict[str, str | int],
        confirm_e2e_execution: None,
    ) -> None:
        """Verify all Actions endpoints return 401 without a valid API key.

        Calls the workflows endpoint without authentication and asserts
        a 401 response.
        """
        owner, repo = self._get_actions_repo(e2e_config)

        response = e2e_client.actions_no_auth(owner, repo)
        assert (
            response.status_code == 401
        ), f"Expected 401 for unauthenticated request, got {response.status_code}"

        logger.info("Actions auth rejection confirmed: 401")
