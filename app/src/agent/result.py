"""Result compilation and API posting for agent execution results.

Provides :class:`ResultCompiler` for building structured JSON payloads matching
the solution design schema, and :class:`ApiPoster` for delivering results (and
status updates) to the API's result ingestion endpoint with HMAC signing and
retry logic.

The :class:`ResultCompiler` is a pure data transformation layer — it takes
role-specific result dicts, a :class:`AgentSessionLog`, and run metadata to
produce the final payload shape matching :class:`ResultIngestionPayload`.

The :class:`ApiPoster` reuses the HMAC generation utility from
``app.src.auth.hmac_auth``, ensuring consistency between API-side verification
and agent-side signing.

Example::

    compiler = ResultCompiler(run_id="abc-123", role="implement", model="gpt-4")
    payload = compiler.compile_success(role_result, session_log)

    poster = ApiPoster(api_result_url="https://api.example.com/run/abc/result",
                       webhook_secret="secret")
    success = await poster.post_result(payload)
    await poster.close()

Note:
    Error payloads use a fixed set of ``error_code`` values that the API and
    callers can switch on programmatically:
    ``SDK_INIT_ERROR``, ``SESSION_CREATE_ERROR``, ``AGENT_RUNTIME_ERROR``,
    ``AGENT_TIMEOUT``, ``GIT_PRE_PROCESS_ERROR``, ``GIT_PUSH_ERROR``,
    ``PR_CREATE_ERROR``, ``MERGE_EXECUTION_ERROR``, ``PR_PRE_PROCESS_ERROR``,
    ``WORKFLOW_STEP_FAILURE``, ``WORKFLOW_CANCELLED``, ``WORKFLOW_TIMEOUT``,
    ``UNKNOWN_ERROR``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.src.agent.runner import AgentExecutionError, AgentSessionLog
from app.src.auth.hmac_auth import generate_hmac_signature

logger = logging.getLogger("dispatch.agent.result")


# ---------------------------------------------------------------------------
# Required fields per role (used for validation warnings)
# ---------------------------------------------------------------------------

_ROLE_REQUIRED_FIELDS: dict[str, list[str]] = {
    "implement": [
        "pr_url",
        "pr_number",
        "branch",
        "commits",
        "files_changed",
        "test_results",
        "security_findings",
    ],
    "review": [
        "assessment",
        "review_comments",
        "suggested_changes",
        "security_concerns",
        "pr_approved",
    ],
    "merge": [
        "merge_status",
        "conflict_files",
        "conflict_resolutions",
    ],
}


# ---------------------------------------------------------------------------
# ResultCompiler
# ---------------------------------------------------------------------------


class ResultCompiler:
    """Compiles role-specific result data into structured JSON payloads.

    Takes role-specific result dicts (from the role modules), session log
    metadata, and run identifiers to produce payloads matching the
    :class:`~app.src.api.models.ResultIngestionPayload` schema.

    Handles success, failure, and timeout scenarios with appropriate
    ``status`` and ``error`` field population.

    Args:
        run_id: Unique identifier for the agent run.
        role: Agent role (``implement``, ``review``, or ``merge``).
        model: LLM model identifier used during execution.
    """

    def __init__(self, run_id: str, role: str, model: str) -> None:
        """Initialise the ResultCompiler with run metadata.

        Args:
            run_id: Unique identifier for the agent run.
            role: Agent role (``implement``, ``review``, or ``merge``).
            model: LLM model identifier used during execution.
        """
        self._run_id = run_id
        self._role = role
        self._model = model

    def compile_success(
        self,
        role_result: dict[str, Any],
        session_log: AgentSessionLog,
        agent_log_url: str | None = None,
    ) -> dict[str, Any]:
        """Build a complete success result payload.

        Merges role-specific result fields with common metadata fields
        (``run_id``, ``status``, ``model_used``, ``duration_seconds``,
        ``agent_log_url``, ``session_summary``).

        Validates that all required fields for the given role are present
        in ``role_result``, logging a WARNING for any missing fields
        (but does not fail).

        Args:
            role_result: Dictionary containing role-specific result fields
                (e.g., ``pr_url``, ``assessment``, ``merge_status``).
            session_log: The session log from the agent run, used to
                extract timing and summary information.
            agent_log_url: Optional URL to the full agent execution log
                (typically a GitHub Actions run URL).

        Returns:
            A dictionary matching the ``ResultIngestionPayload`` schema
            with ``status="success"``.
        """
        duration_seconds = self._calculate_duration(session_log)
        session_summary = (
            session_log.final_message or "Agent session completed successfully."
        )

        # Validate role-specific required fields
        required = _ROLE_REQUIRED_FIELDS.get(self._role, [])
        for field_name in required:
            if field_name not in role_result:
                logger.warning(
                    "Missing expected field '%s' in %s role result for run %s",
                    field_name,
                    self._role,
                    self._run_id,
                )

        payload: dict[str, Any] = {
            "run_id": self._run_id,
            "status": "success",
            "role": self._role,
            "model_used": self._model,
            "duration_seconds": duration_seconds,
            "agent_log_url": agent_log_url,
            "session_summary": session_summary,
        }

        # Merge role-specific fields into the flat payload
        payload.update(role_result)

        return payload

    def compile_error(
        self,
        error: AgentExecutionError | Exception,
        session_log: AgentSessionLog | None = None,
        agent_log_url: str | None = None,
    ) -> dict[str, Any]:
        """Build an error result payload for failure scenarios.

        Extracts error code, message, and details from the exception.
        For :class:`AgentExecutionError` instances, uses the structured
        ``error_code`` and ``error_details``. For generic exceptions, falls
        back to ``UNKNOWN_ERROR``.

        Args:
            error: The exception that caused the failure. Preferably an
                :class:`AgentExecutionError` with structured error info.
            session_log: Optional session log if the session was partially
                completed before the error. Used for timing data.
            agent_log_url: Optional URL to the full agent execution log.

        Returns:
            A dictionary matching the ``ResultIngestionPayload`` schema
            with ``status="failure"`` and an ``error`` dict containing
            ``error_code``, ``error_message``, and ``error_details``.
        """
        duration_seconds = self._calculate_duration(session_log) if session_log else 0
        session_summary = session_log.final_message if session_log else None

        # Extract structured error info
        if isinstance(error, AgentExecutionError):
            error_code = error.error_code
            error_message = error.error_message
            error_details = error.error_details
        else:
            error_code = "UNKNOWN_ERROR"
            error_message = str(error)
            error_details = {"exception_type": type(error).__name__}

        payload: dict[str, Any] = {
            "run_id": self._run_id,
            "status": "failure",
            "role": self._role,
            "model_used": self._model,
            "duration_seconds": duration_seconds,
            "agent_log_url": agent_log_url,
            "session_summary": session_summary,
            "error": {
                "error_code": error_code,
                "error_message": error_message,
                "error_details": error_details,
            },
        }

        return payload

    def compile_timeout(
        self,
        session_log: AgentSessionLog,
        agent_log_url: str | None = None,
    ) -> dict[str, Any]:
        """Build an error payload specifically for timeout scenarios.

        Produces a payload with ``error_code="AGENT_TIMEOUT"`` and includes
        partial session data (messages, tool calls) from the session log.

        Args:
            session_log: The session log from the timed-out run. Contains
                any partial results the agent produced before timeout.
            agent_log_url: Optional URL to the full agent execution log.

        Returns:
            A dictionary matching the ``ResultIngestionPayload`` schema
            with ``status="failure"`` and ``error_code="AGENT_TIMEOUT"``.
        """
        duration_seconds = self._calculate_duration(session_log)
        session_summary = (
            session_log.final_message or "Agent session timed out before completion."
        )

        partial_details: dict[str, Any] = {
            "messages_count": len(session_log.messages),
            "tool_calls_count": len(session_log.tool_calls),
            "errors_count": len(session_log.errors),
            "timed_out": True,
        }

        payload: dict[str, Any] = {
            "run_id": self._run_id,
            "status": "failure",
            "role": self._role,
            "model_used": self._model,
            "duration_seconds": duration_seconds,
            "agent_log_url": agent_log_url,
            "session_summary": session_summary,
            "error": {
                "error_code": "AGENT_TIMEOUT",
                "error_message": (
                    f"Agent session timed out after {duration_seconds} seconds."
                ),
                "error_details": partial_details,
            },
        }

        return payload

    @staticmethod
    def _calculate_duration(session_log: AgentSessionLog) -> int:
        """Calculate the session duration in whole seconds.

        Parses ISO 8601 timestamps from the session log's ``start_time``
        and ``end_time``. If ``end_time`` is not set (session did not
        complete normally), uses the current UTC time.

        Args:
            session_log: The session log containing timing information.

        Returns:
            Duration in seconds as an integer, or ``0`` if the timestamps
            cannot be parsed.
        """
        try:
            start = datetime.fromisoformat(session_log.start_time)
            if session_log.end_time:
                end = datetime.fromisoformat(session_log.end_time)
            else:
                end = datetime.now(timezone.utc)
            delta = (end - start).total_seconds()
            return max(int(delta), 0)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Failed to calculate session duration: %s",
                exc,
            )
            return 0


# ---------------------------------------------------------------------------
# ApiPoster
# ---------------------------------------------------------------------------


class ApiPoster:
    """Posts result payloads and status updates to the API result endpoint.

    Handles HMAC-SHA256 signing of payloads, retry logic with exponential
    backoff, and graceful error handling for network failures and non-2xx
    responses.

    The poster reuses :func:`~app.src.auth.hmac_auth.generate_hmac_signature`
    to produce signatures consistent with the API's verification logic.

    Args:
        api_result_url: The full URL of the API result ingestion endpoint
            (e.g., ``https://api.example.com/agent/run/{run_id}/result``).
        webhook_secret: The shared secret used for HMAC-SHA256 signing.
        max_retries: Maximum number of retry attempts on failure (default 3).
        backoff_base: Base delay in seconds for exponential backoff
            (default 2.0).
        backoff_multiplier: Multiplier for exponential backoff between
            retries (default 2.0).
    """

    def __init__(
        self,
        api_result_url: str,
        webhook_secret: str,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        backoff_multiplier: float = 2.0,
    ) -> None:
        """Initialise the ApiPoster.

        Args:
            api_result_url: The full URL of the API result ingestion endpoint.
            webhook_secret: The shared secret for HMAC-SHA256 signing.
            max_retries: Maximum number of retry attempts (default 3).
            backoff_base: Initial backoff delay in seconds (default 2.0).
            backoff_multiplier: Backoff multiplier per retry (default 2.0).
        """
        self._api_result_url = api_result_url
        self._webhook_secret = webhook_secret
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_multiplier = backoff_multiplier
        self._client = httpx.AsyncClient(timeout=30.0)

    async def post_result(self, payload: dict[str, Any]) -> bool:
        """Post a result payload to the API result ingestion endpoint.

        Serialises the payload to JSON, generates an HMAC-SHA256 signature,
        and sends an HTTP POST with retry logic. Retries up to
        ``max_retries`` times with exponential backoff on failure (non-2xx
        responses or connection errors).

        Args:
            payload: The result payload dictionary to post.

        Returns:
            ``True`` if the result was successfully posted (2xx response),
            ``False`` if all retry attempts were exhausted.
        """
        payload_bytes = json.dumps(payload, default=str).encode("utf-8")
        signature = generate_hmac_signature(payload_bytes, self._webhook_secret)

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={signature}",
        }

        return await self._post_with_retry(
            url=self._api_result_url,
            content=payload_bytes,
            headers=headers,
            description="result payload",
        )

    async def post_status_update(
        self,
        run_id: str,
        status: str = "running",
    ) -> bool:
        """Post a lightweight status update to the API.

        Sends a minimal payload containing only ``run_id`` and ``status``.
        Used immediately at workflow startup to report ``running`` status,
        which extends the stale run detection window.

        Args:
            run_id: The unique identifier for the agent run.
            status: The status to report (default ``"running"``).

        Returns:
            ``True`` if the status update was successfully posted,
            ``False`` if all retry attempts were exhausted.
        """
        payload = {"run_id": run_id, "status": status}
        payload_bytes = json.dumps(payload).encode("utf-8")
        signature = generate_hmac_signature(payload_bytes, self._webhook_secret)

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={signature}",
        }

        return await self._post_with_retry(
            url=self._api_result_url,
            content=payload_bytes,
            headers=headers,
            description="status update",
        )

    async def close(self) -> None:
        """Close the underlying HTTP client.

        Should be called when the poster is no longer needed to release
        network resources. Safe to call multiple times.  Handles
        ``RuntimeError`` from closed event loops gracefully — this occurs
        when the client was used in a different ``asyncio.run()`` call
        than the one invoking ``close()``.
        """
        try:
            await self._client.aclose()
        except RuntimeError as exc:
            if "Event loop is closed" in str(exc):
                logger.warning(
                    "Suppressed 'Event loop is closed' during client close — "
                    "resources will be released by the OS on process exit."
                )
            else:
                raise

    async def _post_with_retry(
        self,
        url: str,
        content: bytes,
        headers: dict[str, str],
        description: str,
    ) -> bool:
        """Execute an HTTP POST with exponential backoff retry logic.

        Retries on non-2xx responses and connection errors. Logs each
        attempt at INFO level and logs exhaustion at ERROR level.

        Args:
            url: The target URL for the POST request.
            content: The raw bytes to send as the request body.
            headers: HTTP headers to include in the request.
            description: Human-readable label for log messages (e.g.,
                ``"result payload"``, ``"status update"``).

        Returns:
            ``True`` if a 2xx response was received, ``False`` if all
            retries were exhausted.
        """
        last_error: str = ""

        for attempt in range(1, self._max_retries + 1):
            try:
                logger.info(
                    "Posting %s to %s (attempt %d/%d)",
                    description,
                    url,
                    attempt,
                    self._max_retries,
                )

                response = await self._client.post(
                    url, content=content, headers=headers
                )

                if 200 <= response.status_code < 300:
                    logger.info(
                        "Successfully posted %s (status %d)",
                        description,
                        response.status_code,
                    )
                    return True

                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    "Failed to post %s (attempt %d/%d): %s",
                    description,
                    attempt,
                    self._max_retries,
                    last_error,
                )

            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Network error posting %s (attempt %d/%d): %s",
                    description,
                    attempt,
                    self._max_retries,
                    last_error,
                )

            # Apply backoff delay before retrying (skip on last attempt)
            if attempt < self._max_retries:
                delay = self._backoff_base * (self._backoff_multiplier ** (attempt - 1))
                logger.info(
                    "Retrying %s in %.1f seconds...",
                    description,
                    delay,
                )
                await asyncio.sleep(delay)

        logger.error(
            "All %d attempts to post %s exhausted. Last error: %s",
            self._max_retries,
            description,
            last_error,
        )
        return False
