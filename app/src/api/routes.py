"""FastAPI routes for the Copilot Dispatch API.

Provides endpoints for agent run management.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.src.api.dependencies import (
    get_dispatch_service_dep,
    get_run_store_dep,
    get_webhook_service_dep,
)
from app.src.api.models import (
    AgentRunRequest,
    AgentRunResponse,
    ResultIngestionPayload,
    RunStatusResponse,
)
from app.src.auth.api_key import verify_api_key
from app.src.auth.hmac_auth import verify_hmac_signature
from app.src.auth.secrets import SecretsProvider
from app.src.config import Settings, get_settings
from app.src.exceptions import (
    AuthenticationError,
    ConflictError,
    DispatchError,
    ExternalServiceError,
    PayloadValidationError,
)
from app.src.services.dispatcher import DispatchService
from app.src.services.run_store import RunStore
from app.src.services.webhook import WebhookService

router = APIRouter(prefix="/agent", tags=["Agent Runs"])
_secrets_provider = SecretsProvider()

logger = logging.getLogger("dispatch.api.routes")


def _resolve_base_url(settings: Settings, request: Request) -> str:
    """Resolve the externally reachable base URL for callback construction.

    Returns the configured ``service_url`` when available, falling back to
    ``request.base_url`` for local development.  A warning is logged when
    the fallback is used in production, since ``request.base_url`` is
    unreliable behind TLS-terminating reverse proxies.

    Args:
        settings: Application settings (may contain ``service_url``).
        request: The incoming HTTP request (used as fallback).

    Returns:
        The base URL string with no trailing slash.
    """
    if settings.service_url:
        return settings.service_url.rstrip("/")

    fallback = str(request.base_url).rstrip("/")
    if settings.app_env == "production":
        logger.warning(
            "DISPATCH_SERVICE_URL is not configured — falling back to "
            "request.base_url (%s). This is unreliable behind a reverse "
            "proxy. Set DISPATCH_SERVICE_URL to the external HTTPS URL.",
            fallback,
        )
    return fallback


@router.post(
    "/run",
    response_model=AgentRunResponse,
    status_code=202,
    summary="Create Agent Run",
    description="Create a new agent run, store it in DynamoDB, and dispatch the GitHub Actions workflow.",
    responses={
        202: {"description": "Run created and workflow dispatched successfully."},
        400: {"description": "Invalid request payload."},
        401: {"description": "Missing or invalid API key."},
        422: {"description": "Validation error in request payload."},
        500: {"description": "Failed to dispatch workflow."},
    },
)
async def create_agent_run(
    request_payload: AgentRunRequest,
    request: Request,
    api_key: str = Depends(verify_api_key),
    run_store: RunStore = Depends(get_run_store_dep),
    dispatch_service: DispatchService = Depends(get_dispatch_service_dep),
) -> AgentRunResponse:
    """Create a new agent run and dispatch the workflow.

    Args:
        request_payload: The run request payload.
        request: The incoming HTTP request.
        api_key: The validated API key.
        run_store: The DynamoDB run store service.
        dispatch_service: The GitHub Actions dispatch service.

    Returns:
        An AgentRunResponse with the new run ID and status.
    """
    run_id = str(uuid.uuid4())
    settings = get_settings()

    callback_url = request_payload.callback_url or settings.default_callback_url
    if callback_url is not None:
        callback_url = str(callback_url)

    # Store the run in DynamoDB
    run_store.create_run(run_id, request_payload)

    # Construct the API result URL.  Prefer the explicit service_url setting
    # (which is mandatory in deployed environments behind a TLS-terminating
    # load balancer like AWS App Runner) over the request-derived base URL.
    base_url = _resolve_base_url(settings, request)
    api_result_url = f"{base_url}/agent/run/{run_id}/result"

    try:
        await dispatch_service.dispatch_workflow(
            run_id=run_id,
            request=request_payload,
            callback_url=callback_url,
            api_result_url=api_result_url,
        )
    except (DispatchError, ExternalServiceError) as e:
        # Update the run status to failure if dispatch fails
        run_store.update_run_error(
            run_id=run_id,
            error_code=e.error_code,
            error_message=e.error_message,
            error_details=e.error_details,
        )
        raise e

    # Get the created run to return the exact created_at timestamp
    run_record = run_store.get_run(run_id)

    return AgentRunResponse(
        run_id=run_id,
        status="dispatched",
        created_at=run_record["created_at"],
    )


@router.get(
    "/run/{run_id}",
    response_model=RunStatusResponse,
    summary="Get Agent Run Status",
    description="Retrieve the status and result of an agent run. Automatically detects and handles stale runs.",
    responses={
        200: {"description": "Run status retrieved successfully."},
        401: {"description": "Missing or invalid API key."},
        404: {"description": "Run not found."},
    },
)
async def get_agent_run(
    run_id: str,
    api_key: str = Depends(verify_api_key),
    run_store: RunStore = Depends(get_run_store_dep),
) -> RunStatusResponse:
    """Retrieve the status of an agent run.

    Args:
        run_id: The ID of the run to retrieve.
        api_key: The validated API key.
        run_store: The DynamoDB run store service.

    Returns:
        A RunStatusResponse with the run details.
    """
    run_record = run_store.get_run(run_id)
    return RunStatusResponse(**run_record)


def _extract_terminal_result(payload: ResultIngestionPayload) -> dict[str, Any] | None:
    """Build the run `result` payload for terminal ingestion updates.

    Args:
        payload: Validated result ingestion payload.

    Returns:
        A dictionary for the `result` field, or None when no result fields exist.
    """
    excluded_fields = {
        "run_id",
        "status",
        "error",
        "role",
        "model_used",
        "duration_seconds",
    }
    payload_data = payload.model_dump(exclude_none=True)
    result_data = {
        key: value for key, value in payload_data.items() if key not in excluded_fields
    }

    if payload.role is not None:
        result_data["role"] = payload.role
    if payload.model_used is not None:
        result_data["model_used"] = payload.model_used
    if payload.duration_seconds is not None:
        result_data["duration_seconds"] = payload.duration_seconds

    return result_data or None


@router.post(
    "/run/{run_id}/result",
    status_code=200,
    summary="Ingest Agent Run Result",
    description=(
        "Ingests workflow result callbacks with HMAC signature verification. "
        "Supports status-only running updates, full terminal results, and error-only failure payloads."
    ),
    responses={
        200: {"description": "Result accepted."},
        400: {"description": "Invalid payload or run identifier mismatch."},
        401: {"description": "Invalid or missing webhook signature."},
        404: {"description": "Run not found."},
        409: {"description": "Run already in terminal state."},
    },
)
async def ingest_result(
    run_id: str,
    request: Request,
    run_store: RunStore = Depends(get_run_store_dep),
    webhook_service: WebhookService = Depends(get_webhook_service_dep),
) -> dict[str, bool | str]:
    """Ingest workflow results, update run state, and forward webhooks when terminal.

    Args:
        run_id: Run identifier from URL path.
        request: Incoming HTTP request containing raw JSON body and signature header.
        run_store: The run persistence service.
        webhook_service: Service used for outbound callback delivery.

    Returns:
        Delivery acknowledgement and webhook delivery status.
    """
    raw_body = await request.body()
    signature_header = request.headers.get("X-Webhook-Signature")
    if not signature_header:
        raise AuthenticationError(error_message="Invalid webhook signature.")

    signature_value = signature_header.removeprefix("sha256=")
    webhook_secret = _secrets_provider.get_secret(
        "dispatch/webhook-secret", env_fallback="DISPATCH_WEBHOOK_SECRET"
    )
    if not verify_hmac_signature(raw_body, signature_value, webhook_secret):
        raise AuthenticationError(error_message="Invalid webhook signature.")

    try:
        payload = ResultIngestionPayload.model_validate_json(raw_body)
    except Exception as exc:  # pragma: no cover - normalised by domain error
        raise PayloadValidationError(
            error_message="Result payload validation failed.",
            error_details={"reason": str(exc)},
        ) from exc

    if payload.run_id != run_id:
        raise PayloadValidationError(
            error_message="run_id in URL does not match payload run_id."
        )

    if payload.status == "running":
        run_store.update_run_status(run_id, "running")
        return {"status": "accepted", "webhook_delivered": False}

    existing_run = run_store.get_run(run_id)
    if existing_run.get("status") in {"success", "failure", "timeout"}:
        raise ConflictError(error_message=f"Run {run_id} is already in terminal state")

    result_payload = None
    if payload.status == "success":
        result_payload = _extract_terminal_result(payload)
    error_payload = (
        payload.error.model_dump(exclude_none=True)
        if payload.error is not None
        else None
    )

    run_store.update_run_result(
        run_id=run_id,
        status=payload.status,
        result=result_payload,
        error=error_payload,
    )

    final_run_record = run_store.get_run(run_id)
    callback_url = final_run_record.get("callback_url")
    if not callback_url:
        return {"status": "accepted", "webhook_delivered": False}

    delivered = await webhook_service.deliver(
        url=callback_url,
        payload=final_run_record,
        secret=webhook_secret,
    )
    return {"status": "accepted", "webhook_delivered": delivered}
