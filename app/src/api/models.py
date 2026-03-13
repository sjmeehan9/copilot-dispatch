"""Pydantic models for the Copilot Dispatch API.

Defines request validation, response serialisation, role-specific result schemas,
error payloads, and the result ingestion endpoint's payload shapes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


class CommitInfo(BaseModel):
    """Information about a commit made by the agent."""

    sha: str = Field(..., description="The full SHA hash of the commit.")
    message: str = Field(..., description="The commit message.")


class TestResults(BaseModel):
    """Summary of test execution results."""

    passed: int = Field(..., description="Number of passing tests.", ge=0)
    failed: int = Field(..., description="Number of failing tests.", ge=0)
    skipped: int = Field(..., description="Number of skipped tests.", ge=0)


class ReviewComment(BaseModel):
    """A code review comment on a specific file and line."""

    file_path: str = Field(..., description="Path to the file being commented on.")
    line: int | None = Field(
        None, description="Line number for the comment, if applicable.", ge=1
    )
    body: str = Field(..., description="The content of the review comment.")


class SuggestedChange(BaseModel):
    """A suggested code change for a specific file and line range."""

    file_path: str = Field(..., description="Path to the file to change.")
    start_line: int = Field(
        ..., description="Starting line number of the change.", ge=1
    )
    end_line: int = Field(..., description="Ending line number of the change.", ge=1)
    suggested_code: str = Field(..., description="The suggested replacement code.")


class ConflictResolution(BaseModel):
    """A suggested resolution for a merge conflict."""

    file_path: str = Field(..., description="Path to the file with the conflict.")
    description: str = Field(
        ..., description="Description of the conflict and how to resolve it."
    )
    suggested_resolution: str = Field(
        ..., description="The suggested code to resolve the conflict."
    )


class ImplementResult(BaseModel):
    """Result payload for the 'implement' role."""

    pr_url: str = Field(..., description="URL of the created pull request.")
    pr_number: int = Field(..., description="Number of the created pull request.", ge=1)
    branch: str = Field(
        ..., description="Name of the branch created for the implementation."
    )
    commits: list[CommitInfo] = Field(
        ..., description="List of commits made by the agent."
    )
    files_changed: list[str] = Field(
        ..., description="List of files modified by the agent."
    )
    test_results: TestResults = Field(
        ..., description="Results of tests run against the implementation."
    )
    security_findings: list[str] = Field(
        ..., description="List of security findings, if any."
    )
    session_summary: str = Field(
        ..., description="Summary of the agent's session and actions taken."
    )
    agent_log_url: str | None = Field(
        None, description="URL to the full agent execution log."
    )


class ReviewResult(BaseModel):
    """Result payload for the 'review' role."""

    review_url: str | None = Field(
        None, description="URL of the submitted review, if applicable."
    )
    assessment: Literal["approve", "request_changes", "comment"] = Field(
        ..., description="The overall assessment of the pull request."
    )
    review_comments: list[ReviewComment] = Field(
        ..., description="List of specific review comments."
    )
    suggested_changes: list[SuggestedChange] = Field(
        ..., description="List of suggested code changes."
    )
    security_concerns: list[str] = Field(
        ..., description="List of security concerns identified during review."
    )
    pr_approved: bool = Field(
        ..., description="Whether the pull request was approved by the agent."
    )
    session_summary: str = Field(
        ..., description="Summary of the agent's review session."
    )
    agent_log_url: str | None = Field(
        None, description="URL to the full agent execution log."
    )


class MergeResult(BaseModel):
    """Result payload for the 'merge' role."""

    merge_status: Literal[
        "merged", "conflicts_resolved_and_merged", "conflicts_unresolved"
    ] = Field(..., description="The outcome of the merge attempt.")
    merge_sha: str | None = Field(
        None, description="The SHA of the merge commit, if successful."
    )
    conflict_files: list[str] = Field(
        ..., description="List of files with merge conflicts."
    )
    conflict_resolutions: list[ConflictResolution] = Field(
        ..., description="Suggested resolutions for conflicts."
    )
    test_results: TestResults | None = Field(
        None, description="Results of tests run after merging, if applicable."
    )
    session_summary: str = Field(
        ..., description="Summary of the agent's merge session."
    )
    agent_log_url: str | None = Field(
        None, description="URL to the full agent execution log."
    )


class ErrorPayload(BaseModel):
    """Standardised error payload structure."""

    error_code: str = Field(..., description="A machine-readable error code.")
    error_message: str = Field(..., description="A human-readable error message.")
    error_details: dict[str, Any] | None = Field(
        None, description="Additional context or details about the error."
    )


class ErrorResponse(BaseModel):
    """Standardised HTTP error response body."""

    error_code: str = Field(..., description="A machine-readable error code.")
    error_message: str = Field(..., description="A human-readable error message.")
    error_details: dict[str, Any] | None = Field(
        None, description="Additional context or details about the error."
    )


class AgentRunRequest(BaseModel):
    """Request payload to initiate a new agent run."""

    repository: str = Field(
        ...,
        description="The GitHub repository in 'owner/repo' format.",
        pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$",
    )
    branch: str = Field(
        ..., description="The target branch for the agent to work on.", min_length=1
    )
    agent_instructions: str = Field(
        ..., description="Instructions for the agent to follow.", min_length=1
    )
    model: str = Field(
        ..., description="The AI model to use for the run.", min_length=1
    )
    role: Literal["implement", "review", "merge"] = Field(
        ..., description="The role the agent should assume."
    )
    pr_number: int | None = Field(
        None,
        description="The pull request number. Required for 'review' and 'merge' roles.",
        ge=1,
    )
    system_instructions: str | None = Field(
        None, description="Optional system-level instructions for the agent."
    )
    skill_paths: list[str] | None = Field(
        None, description="Optional list of paths to custom skills."
    )
    agent_paths: list[str] | None = Field(
        None,
        description=(
            "Optional list of paths to Markdown custom agent definition files "
            "relative to the repo root."
        ),
    )
    callback_url: AnyHttpUrl | None = Field(
        None, description="Optional URL to receive a webhook upon completion."
    )
    timeout_minutes: int = Field(
        30, description="Maximum execution time in minutes.", ge=1, le=60
    )

    @model_validator(mode="after")
    def validate_pr_number_for_role(self) -> AgentRunRequest:
        """Ensure pr_number is provided when required by the role, and omitted when not applicable."""
        if self.role in ("review", "merge") and self.pr_number is None:
            raise ValueError("pr_number is required for review and merge roles")
        if self.role == "implement" and self.pr_number is not None:
            raise ValueError("pr_number is not applicable for implement role")
        return self


class AgentRunResponse(BaseModel):
    """Response payload when an agent run is successfully initiated."""

    run_id: str = Field(..., description="The unique identifier for the run.")
    status: str = Field(
        ..., description="The initial status of the run (e.g., 'queued', 'running')."
    )
    created_at: str = Field(
        ..., description="ISO 8601 timestamp of when the run was created."
    )


class RunStatusResponse(BaseModel):
    """Response payload containing the current status and results of an agent run."""

    run_id: str = Field(..., description="The unique identifier for the run.")
    repository: str = Field(
        ..., description="The GitHub repository in 'owner/repo' format."
    )
    branch: str = Field(..., description="The target branch for the agent to work on.")
    role: str = Field(..., description="The role the agent assumed.")
    status: str = Field(..., description="The current status of the run.")
    model: str = Field(..., description="The AI model used for the run.")
    created_at: str = Field(
        ..., description="ISO 8601 timestamp of when the run was created."
    )
    updated_at: str = Field(
        ..., description="ISO 8601 timestamp of when the run was last updated."
    )
    result: ImplementResult | ReviewResult | MergeResult | None = Field(
        None, description="The result of the run, if completed successfully."
    )
    error: ErrorPayload | None = Field(
        None, description="Error details, if the run failed."
    )


class ResultIngestionPayload(BaseModel):
    """Payload for ingesting results from an agent workflow execution."""

    run_id: str = Field(..., description="The unique identifier for the run.")
    status: Literal["running", "success", "failure"] = Field(
        ..., description="The new status of the run."
    )
    role: str | None = Field(
        None, description="The role the agent assumed. Required if status is 'success'."
    )
    model_used: str | None = Field(
        None, description="The actual AI model used during execution."
    )
    duration_seconds: int | None = Field(
        None, description="Execution duration in seconds.", ge=0
    )
    error: ErrorPayload | None = Field(
        None, description="Error details. Required if status is 'failure'."
    )

    # Optional fields from role-specific result models
    pr_url: str | None = None
    pr_number: int | None = None
    branch: str | None = None
    commits: list[CommitInfo] | None = None
    files_changed: list[str] | None = None
    test_results: TestResults | None = None
    security_findings: list[str] | None = None
    session_summary: str | None = None
    agent_log_url: str | None = None

    review_url: str | None = None
    assessment: Literal["approve", "request_changes", "comment"] | None = None
    review_comments: list[ReviewComment] | None = None
    suggested_changes: list[SuggestedChange] | None = None
    security_concerns: list[str] | None = None
    pr_approved: bool | None = None

    merge_status: (
        Literal["merged", "conflicts_resolved_and_merged", "conflicts_unresolved"]
        | None
    ) = None
    merge_sha: str | None = None
    conflict_files: list[str] | None = None
    conflict_resolutions: list[ConflictResolution] | None = None

    @model_validator(mode="after")
    def validate_payload_shape(self) -> ResultIngestionPayload:
        """Validate that the payload contains the correct fields based on the status."""
        if self.status == "success":
            if not self.role:
                raise ValueError("role is required when status is 'success'")
        return self
