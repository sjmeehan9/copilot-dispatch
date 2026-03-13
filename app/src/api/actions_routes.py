"""FastAPI routes for GitHub Actions visibility endpoints.

Provides four endpoints under the ``/actions`` prefix for listing workflows,
listing workflow runs, retrieving run details (with jobs and steps), and
triggering workflow dispatches.  All endpoints require API key authentication
via the :func:`~app.src.auth.api_key.verify_api_key` dependency.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Response

from app.src.api.actions_models import (
    RunFilterParams,
    WorkflowDispatchRequest,
    WorkflowListResponse,
    WorkflowRunDetailResponse,
    WorkflowRunListResponse,
)
from app.src.api.dependencies import get_actions_service_dep
from app.src.auth.api_key import verify_api_key
from app.src.services.actions import ActionsService

logger = logging.getLogger(__name__)

actions_router = APIRouter(prefix="/actions", tags=["Actions"])


@actions_router.get(
    "/{owner}/{repo}/workflows",
    response_model=WorkflowListResponse,
    status_code=200,
    summary="List Workflows",
    description=(
        "List all GitHub Actions workflows for the specified repository. "
        "Returns the total count and a list of workflow definitions."
    ),
    responses={
        200: {
            "description": "Workflows retrieved successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "total_count": 2,
                        "workflows": [
                            {
                                "id": 1,
                                "name": "CI",
                                "path": ".github/workflows/ci.yml",
                                "state": "active",
                                "created_at": "2025-01-01T00:00:00Z",
                                "updated_at": "2025-06-15T12:00:00Z",
                                "html_url": "https://github.com/owner/repo/actions/workflows/ci.yml",
                            }
                        ],
                    }
                }
            },
        },
        401: {"description": "Missing or invalid API key."},
        403: {"description": "Insufficient permissions to access the repository."},
        404: {"description": "Repository not found."},
        502: {"description": "GitHub API returned a server error."},
    },
)
async def list_workflows(
    owner: str,
    repo: str,
    api_key: str = Depends(verify_api_key),
    actions_service: ActionsService = Depends(get_actions_service_dep),
) -> WorkflowListResponse:
    """List workflows for a GitHub repository.

    Args:
        owner: GitHub repository owner (user or organisation).
        repo: GitHub repository name.
        api_key: Validated API key (injected dependency).
        actions_service: GitHub Actions service (injected dependency).

    Returns:
        A WorkflowListResponse containing the total count and workflow list.
    """
    logger.info(
        "Listing workflows",
        extra={"owner": owner, "repo": repo},
    )
    return await actions_service.list_workflows(owner, repo)


@actions_router.get(
    "/{owner}/{repo}/runs",
    response_model=WorkflowRunListResponse,
    status_code=200,
    summary="List Workflow Runs",
    description=(
        "List workflow runs for the specified repository with optional "
        "filtering by branch, status, actor, and event."
    ),
    responses={
        200: {
            "description": "Workflow runs retrieved successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "total_count": 1,
                        "workflow_runs": [
                            {
                                "id": 100,
                                "name": "CI",
                                "workflow_id": 1,
                                "head_branch": "main",
                                "head_sha": "abc123def456",
                                "status": "completed",
                                "conclusion": "success",
                                "event": "push",
                                "actor": "octocat",
                                "run_number": 42,
                                "run_attempt": 1,
                                "created_at": "2025-06-15T12:00:00Z",
                                "updated_at": "2025-06-15T12:05:00Z",
                                "html_url": "https://github.com/owner/repo/actions/runs/100",
                            }
                        ],
                    }
                }
            },
        },
        401: {"description": "Missing or invalid API key."},
        403: {"description": "Insufficient permissions to access the repository."},
        404: {"description": "Repository not found."},
        502: {"description": "GitHub API returned a server error."},
    },
)
async def list_runs(
    owner: str,
    repo: str,
    branch: str | None = Query(default=None, description="Filter by branch name."),
    status: str | None = Query(
        default=None,
        description="Filter by run status (e.g. completed, in_progress).",
    ),
    actor: str | None = Query(
        default=None, description="Filter by triggering user login."
    ),
    event: str | None = Query(
        default=None,
        description="Filter by trigger event (e.g. push, pull_request).",
    ),
    per_page: int = Query(
        default=30,
        ge=1,
        le=100,
        description="Number of results per page (1–100, default 30).",
    ),
    api_key: str = Depends(verify_api_key),
    actions_service: ActionsService = Depends(get_actions_service_dep),
) -> WorkflowRunListResponse:
    """List workflow runs for a GitHub repository.

    Args:
        owner: GitHub repository owner (user or organisation).
        repo: GitHub repository name.
        branch: Optional branch name filter.
        status: Optional run status filter.
        actor: Optional actor login filter.
        event: Optional trigger event filter.
        per_page: Number of results per page (1–100).
        api_key: Validated API key (injected dependency).
        actions_service: GitHub Actions service (injected dependency).

    Returns:
        A WorkflowRunListResponse containing the total count and run list.
    """
    filters = RunFilterParams(
        branch=branch,
        status=status,
        actor=actor,
        event=event,
        per_page=per_page,
    )
    logger.info(
        "Listing workflow runs",
        extra={
            "owner": owner,
            "repo": repo,
            "filters": filters.model_dump(exclude_none=True),
        },
    )
    return await actions_service.list_runs(owner, repo, filters)


@actions_router.get(
    "/{owner}/{repo}/runs/{run_id}",
    response_model=WorkflowRunDetailResponse,
    status_code=200,
    summary="Get Workflow Run Detail",
    description=(
        "Retrieve detailed information for a specific workflow run, "
        "including jobs and their individual steps."
    ),
    responses={
        200: {
            "description": "Run detail retrieved successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "id": 100,
                        "name": "CI",
                        "workflow_id": 1,
                        "head_branch": "main",
                        "head_sha": "abc123def456",
                        "status": "completed",
                        "conclusion": "success",
                        "event": "push",
                        "actor": "octocat",
                        "run_number": 42,
                        "run_attempt": 1,
                        "created_at": "2025-06-15T12:00:00Z",
                        "updated_at": "2025-06-15T12:05:00Z",
                        "html_url": "https://github.com/owner/repo/actions/runs/100",
                        "jobs": [
                            {
                                "id": 200,
                                "name": "build",
                                "status": "completed",
                                "conclusion": "success",
                                "started_at": "2025-06-15T12:00:10Z",
                                "completed_at": "2025-06-15T12:03:00Z",
                                "steps": [
                                    {
                                        "name": "Checkout",
                                        "status": "completed",
                                        "conclusion": "success",
                                        "number": 1,
                                        "started_at": "2025-06-15T12:00:10Z",
                                        "completed_at": "2025-06-15T12:00:15Z",
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
        },
        401: {"description": "Missing or invalid API key."},
        403: {"description": "Insufficient permissions to access the repository."},
        404: {"description": "Workflow run not found."},
        502: {"description": "GitHub API returned a server error."},
    },
)
async def get_run_detail(
    owner: str,
    repo: str,
    run_id: int,
    api_key: str = Depends(verify_api_key),
    actions_service: ActionsService = Depends(get_actions_service_dep),
) -> WorkflowRunDetailResponse:
    """Retrieve detailed information for a workflow run including jobs and steps.

    Args:
        owner: GitHub repository owner (user or organisation).
        repo: GitHub repository name.
        run_id: GitHub Actions workflow run ID.
        api_key: Validated API key (injected dependency).
        actions_service: GitHub Actions service (injected dependency).

    Returns:
        A WorkflowRunDetailResponse with run metadata, jobs, and steps.
    """
    logger.info(
        "Getting workflow run detail",
        extra={"owner": owner, "repo": repo, "run_id": run_id},
    )
    return await actions_service.get_run_detail(owner, repo, run_id)


@actions_router.post(
    "/{owner}/{repo}/workflows/{workflow_id}/dispatch",
    status_code=204,
    summary="Dispatch Workflow",
    description=(
        "Trigger a workflow dispatch on the specified repository. "
        "Requires the workflow ID, a branch/tag/SHA reference, and "
        "optional key-value inputs."
    ),
    responses={
        204: {"description": "Workflow dispatched successfully."},
        401: {"description": "Missing or invalid API key."},
        403: {"description": "Insufficient permissions to dispatch the workflow."},
        404: {"description": "Workflow or repository not found."},
        422: {"description": "GitHub rejected the dispatch payload."},
        502: {"description": "GitHub API returned a server error."},
    },
)
async def dispatch_workflow(
    owner: str,
    repo: str,
    workflow_id: int,
    request_payload: WorkflowDispatchRequest,
    api_key: str = Depends(verify_api_key),
    actions_service: ActionsService = Depends(get_actions_service_dep),
) -> Response:
    """Trigger a workflow dispatch.

    Args:
        owner: GitHub repository owner (user or organisation).
        repo: GitHub repository name.
        workflow_id: Numeric workflow ID.
        request_payload: Dispatch request with ref and optional inputs.
        api_key: Validated API key (injected dependency).
        actions_service: GitHub Actions service (injected dependency).

    Returns:
        An empty 204 No Content response on success.
    """
    logger.info(
        "Dispatching workflow",
        extra={
            "owner": owner,
            "repo": repo,
            "workflow_id": workflow_id,
            "ref": request_payload.ref,
        },
    )
    await actions_service.dispatch_workflow(
        owner=owner,
        repo=repo,
        workflow_id=workflow_id,
        ref=request_payload.ref,
        inputs=request_payload.inputs,
    )
    return Response(status_code=204)
