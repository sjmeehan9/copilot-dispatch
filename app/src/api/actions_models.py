"""Pydantic models for the GitHub Actions visibility endpoints.

Defines request validation, response serialisation, and query parameter models
for the Actions visibility API: workflow listing, run listing, run detail
(with jobs and steps), and workflow dispatch.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkflowItem(BaseModel):
    """A single GitHub Actions workflow definition.

    Maps the most useful fields from the GitHub REST API
    ``GET /repos/{owner}/{repo}/actions/workflows`` response items.
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(..., description="Unique workflow identifier.")
    name: str = Field(..., description="Human-readable workflow name.")
    path: str = Field(
        ...,
        description="Relative path to the workflow file (e.g. .github/workflows/ci.yml).",
    )
    state: str = Field(
        ..., description="Workflow state (e.g. active, disabled_manually)."
    )
    created_at: str = Field(..., description="ISO 8601 creation timestamp.")
    updated_at: str = Field(..., description="ISO 8601 last-update timestamp.")
    html_url: str = Field(..., description="Browser URL for the workflow.")


class WorkflowListResponse(BaseModel):
    """Response model for the list-workflows endpoint.

    Wraps the GitHub ``GET /repos/{owner}/{repo}/actions/workflows`` response,
    exposing only ``total_count`` and the list of workflow items.
    """

    model_config = ConfigDict(extra="ignore")

    total_count: int = Field(
        ..., description="Total number of workflows in the repository."
    )
    workflows: list[WorkflowItem] = Field(
        ..., description="List of workflow definitions."
    )


class WorkflowRunItem(BaseModel):
    """A single GitHub Actions workflow run.

    Maps the most useful fields from the GitHub REST API
    ``GET /repos/{owner}/{repo}/actions/runs`` response items.  The nested
    ``actor.login`` value is automatically flattened into the ``actor`` string
    field via a pre-validation hook.
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(..., description="Unique workflow run identifier.")
    name: str = Field(..., description="Display name of the workflow run.")
    workflow_id: int = Field(..., description="ID of the parent workflow definition.")
    head_branch: str = Field(..., description="Branch that triggered the run.")
    head_sha: str = Field(..., description="Commit SHA at the head of the branch.")
    status: str = Field(
        ..., description="Run status (e.g. queued, in_progress, completed)."
    )
    conclusion: str | None = Field(
        None,
        description="Run conclusion (e.g. success, failure). Null while in progress.",
    )
    event: str = Field(
        ..., description="Event that triggered the run (e.g. push, pull_request)."
    )
    actor: str = Field(..., description="Login of the user who triggered the run.")
    run_number: int = Field(
        ..., description="Sequential run number within the workflow."
    )
    run_attempt: int = Field(..., description="Attempt number for this run.")
    created_at: str = Field(..., description="ISO 8601 creation timestamp.")
    updated_at: str = Field(..., description="ISO 8601 last-update timestamp.")
    html_url: str = Field(..., description="Browser URL for the workflow run.")

    @model_validator(mode="before")
    @classmethod
    def extract_actor_login(cls, data: Any) -> Any:
        """Extract ``actor.login`` from the nested GitHub API actor object.

        If ``actor`` is a dict containing a ``login`` key the value is promoted
        to a top-level string.  If ``actor`` is already a string (e.g. when
        constructing from our own serialised output) it is left unchanged.
        """
        if isinstance(data, dict):
            actor_value = data.get("actor")
            if isinstance(actor_value, dict):
                data = {**data, "actor": actor_value.get("login", "")}
        return data


class WorkflowRunListResponse(BaseModel):
    """Response model for the list-runs endpoint.

    Wraps the GitHub ``GET /repos/{owner}/{repo}/actions/runs`` response,
    exposing ``total_count`` and the list of run items.
    """

    model_config = ConfigDict(extra="ignore")

    total_count: int = Field(..., description="Total number of matching workflow runs.")
    workflow_runs: list[WorkflowRunItem] = Field(
        ..., description="List of workflow run items."
    )


class StepItem(BaseModel):
    """A single step within a GitHub Actions job.

    Maps the most useful fields from the jobs endpoint response.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Step name.")
    status: str = Field(..., description="Step status (e.g. completed, in_progress).")
    conclusion: str | None = Field(
        None, description="Step conclusion (e.g. success, failure). Null while running."
    )
    number: int = Field(..., description="1-based step number within the job.")
    started_at: str | None = Field(None, description="ISO 8601 start timestamp.")
    completed_at: str | None = Field(None, description="ISO 8601 completion timestamp.")


class JobItem(BaseModel):
    """A single job within a GitHub Actions workflow run.

    Maps the most useful fields from the GitHub REST API
    ``GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs`` response items.
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(..., description="Unique job identifier.")
    name: str = Field(..., description="Job name.")
    status: str = Field(..., description="Job status (e.g. completed, in_progress).")
    conclusion: str | None = Field(
        None, description="Job conclusion (e.g. success, failure). Null while running."
    )
    started_at: str | None = Field(None, description="ISO 8601 start timestamp.")
    completed_at: str | None = Field(None, description="ISO 8601 completion timestamp.")
    steps: list[StepItem] = Field(
        ..., description="Ordered list of steps within the job."
    )


class WorkflowRunDetailResponse(WorkflowRunItem):
    """Detail response for a single workflow run, including jobs and steps.

    Inherits all fields from :class:`WorkflowRunItem` and adds the ``jobs``
    list, providing a hierarchical view of the run → jobs → steps structure.
    """

    jobs: list[JobItem] = Field(
        ..., description="List of jobs belonging to this workflow run."
    )


class WorkflowDispatchRequest(BaseModel):
    """Request payload for triggering a workflow dispatch.

    ``ref`` is the branch, tag, or commit SHA to run the workflow against.
    ``inputs`` are optional key-value pairs forwarded to the workflow.
    """

    ref: str = Field(
        ...,
        min_length=1,
        description="Branch, tag, or commit SHA to dispatch against.",
    )
    inputs: dict[str, str] | None = Field(
        None, description="Optional key-value inputs for the workflow."
    )


class RunFilterParams(BaseModel):
    """Query parameter model for the list-runs endpoint.

    All fields are optional.  ``per_page`` defaults to 30 and is constrained
    to the 1–100 range enforced by the GitHub REST API.
    """

    branch: str | None = Field(None, description="Filter by branch name.")
    status: str | None = Field(
        None,
        description="Filter by run status (e.g. completed, in_progress).",
    )
    actor: str | None = Field(None, description="Filter by triggering user login.")
    event: str | None = Field(
        None, description="Filter by trigger event (e.g. push, pull_request)."
    )
    per_page: int = Field(
        30,
        ge=1,
        le=100,
        description="Number of results per page (1–100, default 30).",
    )
