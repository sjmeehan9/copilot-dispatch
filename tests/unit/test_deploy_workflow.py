"""Unit tests for the CI/CD deploy workflow YAML.

Validates the structure and configuration of ``.github/workflows/deploy.yml``
to ensure trigger rules, job dependencies, environment settings, concurrency
guards, and CDK deploy commands are correct.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixture: load the deploy workflow YAML once per test session
# ---------------------------------------------------------------------------

_WORKFLOW_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / ".github"
    / "workflows"
    / "deploy.yml"
)


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    """Load and return the parsed deploy workflow YAML."""
    assert _WORKFLOW_PATH.exists(), f"Deploy workflow not found at {_WORKFLOW_PATH}"
    with _WORKFLOW_PATH.open() as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeployWorkflowStructure:
    """Tests for the deploy.yml workflow YAML structure."""

    def test_deploy_workflow_yaml_valid(self, workflow: dict[str, Any]) -> None:
        """The workflow YAML is parseable and contains expected top-level keys."""
        assert "name" in workflow
        # PyYAML parses the YAML key 'on' as boolean True
        assert True in workflow or "on" in workflow
        assert "jobs" in workflow

    def test_deploy_triggers_only_on_main(self, workflow: dict[str, Any]) -> None:
        """The deploy workflow triggers only on pushes to the main branch."""
        # PyYAML parses the YAML key 'on' as boolean True
        trigger = workflow.get("on") or workflow.get(True)
        assert trigger is not None, "Workflow must have an 'on' trigger block"
        assert "push" in trigger, "Workflow must trigger on push events"
        branches = trigger["push"]["branches"]
        assert branches == ["main"], f"Expected branches ['main'], got {branches}"
        # Must NOT trigger on pull_request
        assert (
            "pull_request" not in trigger
        ), "Deploy workflow must not trigger on pull_request events"

    def test_deploy_has_test_job(self, workflow: dict[str, Any]) -> None:
        """A 'test' job exists with lint, pytest, and evals steps."""
        jobs = workflow["jobs"]
        assert "test" in jobs, "Workflow must contain a 'test' job"

        step_names = [s.get("name", "") for s in jobs["test"].get("steps", [])]
        step_text = " ".join(step_names).lower()
        assert "black" in step_text, "Test job must include a Black formatting step"
        assert "isort" in step_text, "Test job must include an isort step"

        # Check for pytest and evals in run commands
        run_commands = " ".join(s.get("run", "") for s in jobs["test"].get("steps", []))
        assert "pytest" in run_commands, "Test job must run pytest"
        assert "evals" in run_commands, "Test job must run evals"

    def test_deploy_build_needs_test(self, workflow: dict[str, Any]) -> None:
        """The build-and-push job depends on the test job."""
        jobs = workflow["jobs"]
        assert "build-and-push" in jobs, "Workflow must contain a 'build-and-push' job"
        needs = jobs["build-and-push"].get("needs")
        if isinstance(needs, list):
            assert "test" in needs
        else:
            assert needs == "test"

    def test_deploy_job_needs_build(self, workflow: dict[str, Any]) -> None:
        """The deploy job depends on the build-and-push job."""
        jobs = workflow["jobs"]
        assert "deploy" in jobs, "Workflow must contain a 'deploy' job"
        needs = jobs["deploy"].get("needs")
        if isinstance(needs, list):
            assert "build-and-push" in needs
        else:
            assert needs == "build-and-push"

    def test_deploy_uses_production_environment(self, workflow: dict[str, Any]) -> None:
        """The deploy job targets the 'production' environment."""
        deploy_job = workflow["jobs"]["deploy"]
        assert (
            deploy_job.get("environment") == "production"
        ), "Deploy job must use the 'production' environment"

    def test_deploy_has_concurrency_group(self, workflow: dict[str, Any]) -> None:
        """A concurrency group prevents parallel deployments."""
        concurrency = workflow.get("concurrency")
        assert concurrency is not None, "Workflow must define a concurrency group"
        assert "group" in concurrency, "Concurrency must specify a group name"
        assert (
            concurrency.get("cancel-in-progress") is False
        ), "cancel-in-progress must be false to protect running deployments"

    def test_deploy_cdk_command(self, workflow: dict[str, Any]) -> None:
        """The CDK deploy step uses --require-approval never and specifies the app."""
        deploy_steps = workflow["jobs"]["deploy"].get("steps", [])
        cdk_runs = [
            s.get("run", "") for s in deploy_steps if "cdk deploy" in s.get("run", "")
        ]
        assert len(cdk_runs) >= 1, "Deploy job must contain a 'cdk deploy' step"
        cdk_command = cdk_runs[0]
        assert (
            "--require-approval never" in cdk_command
        ), "CDK deploy must use --require-approval never"
        assert (
            "python infra/app.py" in cdk_command
        ), "CDK deploy must specify the app entry point"
