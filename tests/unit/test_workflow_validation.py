"""Unit tests for the GitHub Actions agent-executor workflow YAML.

Validates the structural correctness of ``.github/workflows/agent-executor.yml``
by loading the YAML, checking ``workflow_dispatch`` inputs, verifying the failure
handler step, timeout configuration, and environment settings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixture: load workflow YAML once per test session
# ---------------------------------------------------------------------------

_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "agent-executor.yml"
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Load and return the parsed workflow YAML."""
    assert _WORKFLOW_PATH.is_file(), f"Workflow file not found at {_WORKFLOW_PATH}"
    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict), "Workflow YAML did not parse to a dict"
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkflowYamlIsValid:
    """Verify the workflow YAML is syntactically valid."""

    def test_workflow_yaml_parses_without_errors(self, workflow: dict) -> None:
        """Loading the YAML file with ``yaml.safe_load`` must succeed."""
        assert workflow is not None
        assert "name" in workflow

    def test_workflow_has_name(self, workflow: dict) -> None:
        """Workflow must declare a human-readable name."""
        assert workflow["name"] == "Agent Executor"


class TestWorkflowDispatchTrigger:
    """Verify ``workflow_dispatch`` trigger and inputs."""

    def test_workflow_has_workflow_dispatch_trigger(self, workflow: dict) -> None:
        """The ``on`` key must include ``workflow_dispatch``."""
        on_triggers = workflow.get("on", workflow.get(True, {}))
        assert "workflow_dispatch" in on_triggers

    def test_workflow_dispatch_inputs_complete(self, workflow: dict) -> None:
        """All 9 expected inputs must be declared."""
        on_triggers = workflow.get("on", workflow.get(True, {}))
        inputs = on_triggers["workflow_dispatch"]["inputs"]

        expected_required = {
            "run_id",
            "target_repository",
            "target_branch",
            "role",
            "agent_instructions",
            "model",
            "api_result_url",
        }
        expected_optional = {
            "timeout_minutes",
            "config",
        }
        all_expected = expected_required | expected_optional

        actual_inputs = set(inputs.keys())
        assert (
            actual_inputs == all_expected
        ), f"Expected inputs {all_expected}, got {actual_inputs}"

    def test_required_inputs_are_required(self, workflow: dict) -> None:
        """The 7 required inputs must have ``required: true``."""
        on_triggers = workflow.get("on", workflow.get(True, {}))
        inputs = on_triggers["workflow_dispatch"]["inputs"]

        required_names = [
            "run_id",
            "target_repository",
            "target_branch",
            "role",
            "agent_instructions",
            "model",
            "api_result_url",
        ]
        for name in required_names:
            assert (
                inputs[name].get("required") is True
            ), f"Input '{name}' should be required"

    def test_config_input_is_optional_with_default(self, workflow: dict) -> None:
        """The ``config`` input must be optional with ``default: '{}'``."""
        on_triggers = workflow.get("on", workflow.get(True, {}))
        config_input = on_triggers["workflow_dispatch"]["inputs"]["config"]

        assert config_input.get("required") is False
        assert config_input.get("default") == "{}"
        assert "agent_paths" in str(config_input.get("description", ""))

    def test_timeout_minutes_input_is_optional_with_default(
        self, workflow: dict
    ) -> None:
        """The ``timeout_minutes`` input must be optional with default 30."""
        on_triggers = workflow.get("on", workflow.get(True, {}))
        timeout_input = on_triggers["workflow_dispatch"]["inputs"]["timeout_minutes"]

        assert timeout_input.get("required") is False
        assert timeout_input.get("default") == "30"

    def test_all_inputs_have_descriptions(self, workflow: dict) -> None:
        """Every input must have a non-empty description."""
        on_triggers = workflow.get("on", workflow.get(True, {}))
        inputs = on_triggers["workflow_dispatch"]["inputs"]

        for name, spec in inputs.items():
            assert spec.get("description"), f"Input '{name}' is missing a description"


class TestWorkflowJobConfiguration:
    """Verify job-level settings."""

    def _get_job(self, workflow: dict) -> dict:
        """Return the ``execute-agent`` job definition."""
        jobs = workflow.get("jobs", {})
        assert "execute-agent" in jobs, "Job 'execute-agent' not found"
        return jobs["execute-agent"]

    def test_workflow_uses_copilot_environment(self, workflow: dict) -> None:
        """Job must declare ``environment: copilot`` for BYOK secrets."""
        job = self._get_job(workflow)
        assert job.get("environment") == "copilot"

    def test_workflow_runs_on_ubuntu(self, workflow: dict) -> None:
        """Job must run on ``ubuntu-latest``."""
        job = self._get_job(workflow)
        assert job.get("runs-on") == "ubuntu-latest"

    def test_workflow_timeout_has_default(self, workflow: dict) -> None:
        """Job ``timeout-minutes`` must have a sensible static default.

        GitHub Actions expressions do not support the ``+`` operator, so
        the job uses a static default of 35 minutes (30 + 5 buffer) and
        a ``Compute adjusted timeout`` step calculates the dynamic value
        from ``inputs.timeout_minutes``.
        """
        job = self._get_job(workflow)
        timeout = job.get("timeout-minutes")
        assert (
            timeout == 35
        ), f"timeout-minutes should be 35 (default 30 + 5 buffer), got: {timeout}"

    def test_workflow_env_includes_pat(self, workflow: dict) -> None:
        """Job ``env`` must set ``GITHUB_TOKEN`` from ``secrets.PAT``."""
        job = self._get_job(workflow)
        env = job.get("env", {})
        github_token = str(env.get("GITHUB_TOKEN", ""))
        assert "secrets.PAT" in github_token


class TestWorkflowSteps:
    """Verify critical workflow steps exist and are configured correctly."""

    def _get_steps(self, workflow: dict) -> list[dict]:
        """Return the list of steps for the ``execute-agent`` job."""
        job = workflow.get("jobs", {}).get("execute-agent", {})
        return job.get("steps", [])

    def _find_step_by_name(self, steps: list[dict], name_substring: str) -> dict | None:
        """Find a step whose ``name`` contains the given substring."""
        for step in steps:
            if name_substring.lower() in step.get("name", "").lower():
                return step
        return None

    def _find_step_by_id(self, steps: list[dict], step_id: str) -> dict | None:
        """Find a step by its ``id``."""
        for step in steps:
            if step.get("id") == step_id:
                return step
        return None

    def test_has_checkout_orchestration_step(self, workflow: dict) -> None:
        """Must checkout orchestration repo (this repo)."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "checkout orchestration")
        assert step is not None, "Checkout orchestration repo step not found"
        assert step.get("uses", "").startswith("actions/checkout")

    def test_has_checkout_target_repo_step(self, workflow: dict) -> None:
        """Must checkout target repo with full history."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "checkout target")
        assert step is not None, "Checkout target repository step not found"
        with_config = step.get("with", {})
        assert "inputs.target_repository" in str(with_config.get("repository", ""))
        assert with_config.get("fetch-depth") == 0

    def test_has_setup_python_step(self, workflow: dict) -> None:
        """Must setup Python 3.13."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "setup python")
        assert step is not None, "Setup Python step not found"
        assert step.get("uses", "").startswith("actions/setup-python")

    def test_has_node_detection_step(self, workflow: dict) -> None:
        """Must detect Node.js requirement from target repo."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "detect node")
        assert step is not None, "Detect Node.js step not found"

    def test_has_copilot_cli_install_step(self, workflow: dict) -> None:
        """Must install the Copilot CLI."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "copilot cli")
        assert step is not None, "Install Copilot CLI step not found"

    def test_has_agent_runner_deps_step(self, workflow: dict) -> None:
        """Must install agent runner dependencies."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "agent runner dependencies")
        assert step is not None, "Install agent runner dependencies step not found"

    def test_python_dependency_step_skips_legacy_backend(self, workflow: dict) -> None:
        """Python dependency install must skip legacy setuptools editable installs."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "target repo python dependencies")
        assert (
            step is not None
        ), "Install target repo Python dependencies step not found"

        run_script = step.get("run", "")
        assert "BUILD_BACKEND=" in run_script
        assert "setuptools.backends.legacy:build" in run_script
        assert "Skipping editable install" in run_script
        assert "pip install -e" in run_script

    def test_target_repo_checkout_path_is_isolated(self, workflow: dict) -> None:
        """Target repo must be checked out to a workspace-relative path then moved to an isolated location.

        ``actions/checkout@v4`` requires the ``path`` to be under ``$GITHUB_WORKSPACE``,
        so we checkout to a relative path (``target-repo``) and then move it to the
        isolated ``TARGET_REPO_PATH`` in a subsequent step.
        """
        steps = self._get_steps(workflow)
        checkout_steps = [
            s
            for s in steps
            if s.get("uses", "").startswith("actions/checkout")
            and "inputs.target_repository"
            in str(s.get("with", {}).get("repository", ""))
        ]
        assert (
            len(checkout_steps) == 1
        ), "Expected exactly one target repo checkout step"
        checkout_path = checkout_steps[0]["with"]["path"]
        # Checkout path must be a simple relative path (not absolute) to satisfy
        # actions/checkout@v4's workspace constraint.
        assert checkout_path == "target-repo", (
            f"Target repo checkout path must be 'target-repo' (relative), "
            f"got: {checkout_path}"
        )

    def test_has_isolate_target_repo_step(self, workflow: dict) -> None:
        """A step must move the target repo from workspace to the isolated path."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "isolate target")
        assert step is not None, "Isolate target repository step not found"
        run_script = step.get("run", "")
        assert "mv" in run_script, "Isolate step must use 'mv' to move the directory"
        assert (
            "TARGET_REPO_PATH" in run_script
        ), "Isolate step must reference TARGET_REPO_PATH"

    def test_target_repo_path_env_var_defined(self, workflow: dict) -> None:
        """The TARGET_REPO_PATH env var must be defined at the job level."""
        job = workflow.get("jobs", {}).get("execute-agent", {})
        env = job.get("env", {})
        assert "TARGET_REPO_PATH" in env, "TARGET_REPO_PATH must be defined in job env"
        path_value = env["TARGET_REPO_PATH"]
        assert path_value.startswith(
            "/"
        ), f"TARGET_REPO_PATH must be an absolute path, got: {path_value}"
        assert "github.workspace" not in str(
            path_value
        ), "TARGET_REPO_PATH must NOT be under github.workspace"

    def test_agent_execution_uses_target_repo_path_env(self, workflow: dict) -> None:
        """Agent execution step must reference TARGET_REPO_PATH, not a relative path."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_id(steps, "agent-execution")
        assert step is not None, "Agent execution step not found"
        run_script = step.get("run", "")
        assert (
            "TARGET_REPO_PATH" in run_script
        ), "Agent execution --repo-path must use TARGET_REPO_PATH env var"
        assert (
            "github.workspace" not in run_script or "TARGET_REPO_PATH" in run_script
        ), "Agent execution must not use github.workspace for repo-path"

    def test_gh_auth_via_env_var(self, workflow: dict) -> None:
        """gh CLI auth is provided by the GITHUB_TOKEN job-level env var."""
        job = workflow["jobs"]["execute-agent"]
        env = job.get("env", {})
        assert "GITHUB_TOKEN" in env, "GITHUB_TOKEN env var not set at job level"

    def test_has_running_status_step(self, workflow: dict) -> None:
        """Must report ``running`` status as an early execution step."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "running status")
        assert step is not None, "Report running status step not found"
        # Verify it calls ApiPoster.post_status_update.
        run_script = step.get("run", "")
        assert "post_status_update" in run_script

    def test_has_agent_execution_step(self, workflow: dict) -> None:
        """Must execute the agent runner script."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_id(steps, "agent-execution")
        assert step is not None, "Agent execution step (id=agent-execution) not found"
        run_script = step.get("run", "")
        assert "app.src.agent.runner" in run_script
        assert "--agent-paths" in run_script

    def test_parse_config_exports_agent_paths(self, workflow: dict) -> None:
        """Parse config step must export AGENT_PATHS to the environment."""
        steps = self._get_steps(workflow)
        step = self._find_step_by_name(steps, "parse config")
        assert step is not None, "Parse config input step not found"
        run_script = step.get("run", "")
        assert "AGENT_PATHS" in run_script
        assert "config.get('agent_paths', [])" in run_script

    def test_has_failure_handler_step(self, workflow: dict) -> None:
        """Must have a failure handler that fires on ``failure()`` or ``cancelled()``."""
        steps = self._get_steps(workflow)
        failure_steps = [
            s
            for s in steps
            if "failure()" in str(s.get("if", ""))
            and "cancelled()" in str(s.get("if", ""))
        ]
        assert (
            len(failure_steps) >= 1
        ), "No step with 'if: failure() || cancelled()' found"

    def test_failure_handler_posts_error_payload(self, workflow: dict) -> None:
        """Failure handler must POST an error payload using stdlib (no third-party deps)."""
        steps = self._get_steps(workflow)
        failure_steps = [s for s in steps if "failure()" in str(s.get("if", ""))]
        assert failure_steps, "No failure handler step found"
        run_script = failure_steps[0].get("run", "")
        # Must use stdlib — no httpx, no app.src imports
        assert (
            "urllib.request" in run_script
        ), "Failure handler must use stdlib urllib.request (not httpx)"
        assert (
            "httpx" not in run_script
        ), "Failure handler must not import httpx (may not be installed)"
        assert (
            "app.src" not in run_script
        ), "Failure handler must not import app.src modules (may not be installed)"
        assert "error_code" in run_script
        assert "error_message" in run_script

    def test_failure_handler_includes_agent_log_url(self, workflow: dict) -> None:
        """Failure handler must include ``agent_log_url`` pointing to the workflow run."""
        steps = self._get_steps(workflow)
        failure_steps = [s for s in steps if "failure()" in str(s.get("if", ""))]
        assert failure_steps, "No failure handler step found"
        run_script = failure_steps[0].get("run", "")
        assert "agent_log_url" in run_script
        assert "github.run_id" in run_script

    def test_step_ordering_status_before_execution(self, workflow: dict) -> None:
        """The running status step must come before the agent execution step."""
        steps = self._get_steps(workflow)
        status_idx = None
        exec_idx = None
        for i, step in enumerate(steps):
            if "running status" in step.get("name", "").lower():
                status_idx = i
            if step.get("id") == "agent-execution":
                exec_idx = i
        assert status_idx is not None, "Running status step not found"
        assert exec_idx is not None, "Agent execution step not found"
        assert status_idx < exec_idx, (
            f"Running status (step {status_idx}) must come before "
            f"agent execution (step {exec_idx})"
        )
