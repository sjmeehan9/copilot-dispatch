"""Unit tests for the result compiler and API poster.

Tests :class:`~app.src.agent.result.ResultCompiler` for success, error, and
timeout payload compilation across all three roles, and
:class:`~app.src.agent.result.ApiPoster` for HMAC-signed HTTP posting with
retry logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.src.agent.result import ApiPoster, ResultCompiler
from app.src.agent.runner import AgentExecutionError, AgentSessionLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_log(
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    final_message: str | None = "Session completed.",
    timed_out: bool = False,
    messages: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> AgentSessionLog:
    """Build an :class:`AgentSessionLog` with sensible test defaults."""
    log = AgentSessionLog()
    log.start_time = start_time or "2026-03-01T10:00:00+00:00"
    log.end_time = end_time or "2026-03-01T10:05:00+00:00"
    log.final_message = final_message
    log.timed_out = timed_out
    if messages is not None:
        log.messages = messages
    if tool_calls is not None:
        log.tool_calls = tool_calls
    if errors is not None:
        log.errors = errors
    return log


def _make_implement_result() -> dict[str, Any]:
    """Build a sample implement role result dict."""
    return {
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_number": 42,
        "branch": "feature/run-123",
        "commits": [{"sha": "abc123", "message": "Add feature"}],
        "files_changed": ["src/main.py", "tests/test_main.py"],
        "test_results": {"passed": 10, "failed": 0, "skipped": 1},
        "security_findings": [],
    }


def _make_review_result() -> dict[str, Any]:
    """Build a sample review role result dict."""
    return {
        "review_url": "https://github.com/org/repo/pull/42#pullrequestreview-1",
        "assessment": "approve",
        "review_comments": [
            {"file_path": "src/main.py", "line": 10, "body": "Good change."}
        ],
        "suggested_changes": [],
        "security_concerns": [],
        "pr_approved": True,
    }


def _make_merge_result() -> dict[str, Any]:
    """Build a sample merge role result dict."""
    return {
        "merge_status": "merged",
        "merge_sha": "def456",
        "conflict_files": [],
        "conflict_resolutions": [],
        "test_results": {"passed": 15, "failed": 0, "skipped": 0},
    }


# ===========================================================================
# ResultCompiler — success
# ===========================================================================


class TestCompileSuccessImplement:
    """Tests for compile_success with implement role results."""

    def test_contains_all_common_fields(self) -> None:
        """All common metadata fields are present in the payload."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log()
        payload = compiler.compile_success(
            _make_implement_result(), session_log, agent_log_url="https://log.url"
        )

        assert payload["run_id"] == "run-1"
        assert payload["status"] == "success"
        assert payload["role"] == "implement"
        assert payload["model_used"] == "gpt-4"
        assert payload["agent_log_url"] == "https://log.url"
        assert payload["session_summary"] == "Session completed."

    def test_contains_implement_role_fields(self) -> None:
        """All implement-specific result fields are merged into the payload."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        payload = compiler.compile_success(
            _make_implement_result(), _make_session_log()
        )

        assert payload["pr_url"] == "https://github.com/org/repo/pull/42"
        assert payload["pr_number"] == 42
        assert payload["branch"] == "feature/run-123"
        assert len(payload["commits"]) == 1
        assert payload["files_changed"] == ["src/main.py", "tests/test_main.py"]
        assert payload["test_results"]["passed"] == 10
        assert payload["security_findings"] == []

    def test_missing_optional_field_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A WARNING is logged when an expected role field is missing."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        incomplete_result = {"pr_url": "https://example.com/pr/1"}

        with caplog.at_level("WARNING", logger="dispatch.agent.result"):
            compiler.compile_success(incomplete_result, _make_session_log())

        assert "Missing expected field" in caplog.text


class TestCompileSuccessReview:
    """Tests for compile_success with review role results."""

    def test_contains_review_role_fields(self) -> None:
        """All review-specific result fields are present."""
        compiler = ResultCompiler("run-2", "review", "claude-sonnet-4-20250514")
        payload = compiler.compile_success(_make_review_result(), _make_session_log())

        assert payload["status"] == "success"
        assert payload["assessment"] == "approve"
        assert payload["pr_approved"] is True
        assert len(payload["review_comments"]) == 1
        assert payload["suggested_changes"] == []
        assert payload["security_concerns"] == []


class TestCompileSuccessMerge:
    """Tests for compile_success with merge role results."""

    def test_contains_merge_role_fields(self) -> None:
        """All merge-specific result fields are present."""
        compiler = ResultCompiler("run-3", "merge", "gpt-4")
        payload = compiler.compile_success(_make_merge_result(), _make_session_log())

        assert payload["status"] == "success"
        assert payload["merge_status"] == "merged"
        assert payload["merge_sha"] == "def456"
        assert payload["conflict_files"] == []
        assert payload["conflict_resolutions"] == []


class TestCompileSuccessDuration:
    """Tests for duration_seconds calculation."""

    def test_duration_calculated_from_timestamps(self) -> None:
        """duration_seconds is correctly computed from session timestamps."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(
            start_time="2026-03-01T10:00:00+00:00",
            end_time="2026-03-01T10:03:30+00:00",
        )
        payload = compiler.compile_success(_make_implement_result(), session_log)

        assert payload["duration_seconds"] == 210  # 3 min 30 sec

    def test_duration_zero_when_times_identical(self) -> None:
        """duration_seconds is 0 when start and end are the same."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(
            start_time="2026-03-01T10:00:00+00:00",
            end_time="2026-03-01T10:00:00+00:00",
        )
        payload = compiler.compile_success(_make_implement_result(), session_log)

        assert payload["duration_seconds"] == 0

    def test_duration_fallback_on_missing_end_time(self) -> None:
        """duration_seconds uses current UTC time when end_time is None."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(end_time=None)

        payload = compiler.compile_success(_make_implement_result(), session_log)

        # Duration should be a positive integer (based on current time)
        assert payload["duration_seconds"] >= 0

    def test_duration_zero_on_invalid_timestamp(self) -> None:
        """duration_seconds is 0 when timestamps are unparseable."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(
            start_time="not-a-date",
            end_time="also-not-a-date",
        )
        payload = compiler.compile_success(_make_implement_result(), session_log)

        assert payload["duration_seconds"] == 0


class TestCompileSuccessSessionSummary:
    """Tests for session_summary extraction."""

    def test_session_summary_from_final_message(self) -> None:
        """session_summary uses the final_message from the session log."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(
            final_message="Implemented the calculator feature."
        )
        payload = compiler.compile_success(_make_implement_result(), session_log)

        assert payload["session_summary"] == "Implemented the calculator feature."

    def test_session_summary_fallback_when_no_final_message(self) -> None:
        """session_summary uses default text when final_message is None."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(final_message=None)
        payload = compiler.compile_success(_make_implement_result(), session_log)

        assert payload["session_summary"] == "Agent session completed successfully."


# ===========================================================================
# ResultCompiler — error
# ===========================================================================


class TestCompileError:
    """Tests for compile_error with various exception types."""

    def test_error_from_agent_execution_error(self) -> None:
        """Structured error fields are extracted from AgentExecutionError."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        error = AgentExecutionError(
            error_message="SDK failed to start",
            error_code="SDK_INIT_ERROR",
            error_details={"stderr": "copilot not found"},
        )
        session_log = _make_session_log()

        payload = compiler.compile_error(error, session_log)

        assert payload["status"] == "failure"
        assert payload["run_id"] == "run-1"
        assert payload["error"]["error_code"] == "SDK_INIT_ERROR"
        assert payload["error"]["error_message"] == "SDK failed to start"
        assert payload["error"]["error_details"]["stderr"] == "copilot not found"
        assert payload["duration_seconds"] == 300  # 5 minutes

    def test_error_from_generic_exception(self) -> None:
        """Generic exceptions produce UNKNOWN_ERROR code with type info."""
        compiler = ResultCompiler("run-1", "review", "gpt-4")
        error = RuntimeError("Something unexpected happened")

        payload = compiler.compile_error(error)

        assert payload["status"] == "failure"
        assert payload["error"]["error_code"] == "UNKNOWN_ERROR"
        assert payload["error"]["error_message"] == "Something unexpected happened"
        assert payload["error"]["error_details"]["exception_type"] == "RuntimeError"

    def test_error_duration_zero_without_session_log(self) -> None:
        """duration_seconds is 0 when no session log is provided."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        error = AgentExecutionError(
            error_message="Crash",
            error_code="AGENT_RUNTIME_ERROR",
        )

        payload = compiler.compile_error(error)

        assert payload["duration_seconds"] == 0

    def test_error_session_summary_from_log(self) -> None:
        """session_summary is populated from the session log if available."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(final_message="Partial progress made")
        error = AgentExecutionError(
            error_message="Push failed",
            error_code="GIT_PUSH_ERROR",
        )

        payload = compiler.compile_error(error, session_log)

        assert payload["session_summary"] == "Partial progress made"

    def test_error_session_summary_none_without_log(self) -> None:
        """session_summary is None when no session log is provided."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        error = AgentExecutionError(
            error_message="Init failed",
            error_code="SDK_INIT_ERROR",
        )

        payload = compiler.compile_error(error)

        assert payload["session_summary"] is None

    def test_error_agent_log_url_included(self) -> None:
        """agent_log_url is included in error payloads."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        error = AgentExecutionError(
            error_message="Session crash",
            error_code="SESSION_CREATE_ERROR",
        )

        payload = compiler.compile_error(error, agent_log_url="https://actions.log/123")

        assert payload["agent_log_url"] == "https://actions.log/123"


# ===========================================================================
# ResultCompiler — timeout
# ===========================================================================


class TestCompileTimeout:
    """Tests for compile_timeout."""

    def test_timeout_error_code(self) -> None:
        """Timeout payloads use AGENT_TIMEOUT error code."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(
            timed_out=True,
            messages=[{"content": "msg1"}, {"content": "msg2"}],
            tool_calls=[{"name": "tool1"}],
            errors=[],
        )

        payload = compiler.compile_timeout(session_log)

        assert payload["status"] == "failure"
        assert payload["error"]["error_code"] == "AGENT_TIMEOUT"
        assert "timed out" in payload["error"]["error_message"]

    def test_timeout_partial_details(self) -> None:
        """Timeout payloads include partial session statistics."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(
            timed_out=True,
            messages=[{"content": "msg1"}, {"content": "msg2"}, {"content": "msg3"}],
            tool_calls=[{"name": "t1"}, {"name": "t2"}],
            errors=[{"message": "err1"}],
        )

        payload = compiler.compile_timeout(session_log)

        details = payload["error"]["error_details"]
        assert details["messages_count"] == 3
        assert details["tool_calls_count"] == 2
        assert details["errors_count"] == 1
        assert details["timed_out"] is True

    def test_timeout_session_summary_fallback(self) -> None:
        """Timeout uses fallback summary when final_message is None."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(final_message=None, timed_out=True)

        payload = compiler.compile_timeout(session_log)

        assert (
            payload["session_summary"] == "Agent session timed out before completion."
        )

    def test_timeout_duration_included(self) -> None:
        """Timeout payloads include correct duration_seconds."""
        compiler = ResultCompiler("run-1", "implement", "gpt-4")
        session_log = _make_session_log(
            start_time="2026-03-01T10:00:00+00:00",
            end_time="2026-03-01T10:30:00+00:00",
            timed_out=True,
        )

        payload = compiler.compile_timeout(session_log)

        assert payload["duration_seconds"] == 1800  # 30 minutes


# ===========================================================================
# ApiPoster — post_result
# ===========================================================================


class TestPostResult:
    """Tests for ApiPoster.post_result."""

    @pytest.mark.asyncio
    async def test_post_result_success(self) -> None:
        """Successful POST returns True with correct HMAC header."""
        poster = ApiPoster("https://api.example.com/result", "test-secret")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(poster._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await poster.post_result({"run_id": "x", "status": "success"})

        assert result is True
        mock_post.assert_called_once()

        # Verify HMAC header is present and correctly formatted
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs["headers"]
        assert "X-Webhook-Signature" in headers
        assert headers["X-Webhook-Signature"].startswith("sha256=")
        assert headers["Content-Type"] == "application/json"

        await poster.close()

    @pytest.mark.asyncio
    async def test_post_result_retries_on_failure(self) -> None:
        """POST retries on non-2xx responses and succeeds on third attempt."""
        poster = ApiPoster(
            "https://api.example.com/result",
            "test-secret",
            max_retries=3,
            backoff_base=0.01,
            backoff_multiplier=1.0,
        )

        mock_fail = MagicMock()
        mock_fail.status_code = 500
        mock_fail.text = "Internal Server Error"

        mock_success = MagicMock()
        mock_success.status_code = 200

        with patch.object(poster._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = [mock_fail, mock_fail, mock_success]
            result = await poster.post_result({"run_id": "x", "status": "success"})

        assert result is True
        assert mock_post.call_count == 3

        await poster.close()

    @pytest.mark.asyncio
    async def test_post_result_all_retries_exhausted(self) -> None:
        """Returns False when all retry attempts are exhausted."""
        poster = ApiPoster(
            "https://api.example.com/result",
            "test-secret",
            max_retries=3,
            backoff_base=0.01,
            backoff_multiplier=1.0,
        )

        mock_fail = MagicMock()
        mock_fail.status_code = 503
        mock_fail.text = "Service Unavailable"

        with patch.object(poster._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_fail
            result = await poster.post_result({"run_id": "x", "status": "success"})

        assert result is False
        assert mock_post.call_count == 3

        await poster.close()

    @pytest.mark.asyncio
    async def test_post_result_network_error(self) -> None:
        """Gracefully handles network errors with retries."""
        poster = ApiPoster(
            "https://api.example.com/result",
            "test-secret",
            max_retries=2,
            backoff_base=0.01,
            backoff_multiplier=1.0,
        )

        with patch.object(poster._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.ConnectError("Connection refused")
            result = await poster.post_result({"run_id": "x", "status": "success"})

        assert result is False
        assert mock_post.call_count == 2

        await poster.close()


class TestPostResultHmac:
    """Tests for HMAC signature correctness in API posting."""

    @pytest.mark.asyncio
    async def test_hmac_signature_matches_payload(self) -> None:
        """The HMAC signature in the header matches the posted payload bytes."""
        from app.src.auth.hmac_auth import generate_hmac_signature

        poster = ApiPoster("https://api.example.com/result", "my-secret")
        payload = {"run_id": "test-123", "status": "success", "role": "implement"}

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(poster._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            await poster.post_result(payload)

        # Reconstruct expected signature from the same payload bytes
        call_kwargs = mock_post.call_args
        posted_content = call_kwargs.kwargs["content"]
        expected_sig = generate_hmac_signature(posted_content, "my-secret")

        actual_header = call_kwargs.kwargs["headers"]["X-Webhook-Signature"]
        assert actual_header == f"sha256={expected_sig}"

        await poster.close()


# ===========================================================================
# ApiPoster — post_status_update
# ===========================================================================


class TestPostStatusUpdate:
    """Tests for ApiPoster.post_status_update."""

    @pytest.mark.asyncio
    async def test_post_status_update_running(self) -> None:
        """Status update sends minimal payload with correct HMAC."""
        poster = ApiPoster("https://api.example.com/result", "test-secret")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(poster._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await poster.post_status_update("run-42")

        assert result is True

        # Verify the payload is minimal: run_id + status only
        call_kwargs = mock_post.call_args
        posted_bytes = call_kwargs.kwargs["content"]
        posted_payload = json.loads(posted_bytes)
        assert posted_payload == {"run_id": "run-42", "status": "running"}

        # Verify HMAC header
        headers = call_kwargs.kwargs["headers"]
        assert "X-Webhook-Signature" in headers
        assert headers["X-Webhook-Signature"].startswith("sha256=")

        await poster.close()

    @pytest.mark.asyncio
    async def test_post_status_update_custom_status(self) -> None:
        """Status update supports custom status values."""
        poster = ApiPoster("https://api.example.com/result", "test-secret")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(poster._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await poster.post_status_update("run-42", status="failure")

        assert result is True
        posted_bytes = mock_post.call_args.kwargs["content"]
        posted_payload = json.loads(posted_bytes)
        assert posted_payload["status"] == "failure"

        await poster.close()

    @pytest.mark.asyncio
    async def test_post_status_update_retries_on_failure(self) -> None:
        """Status update retries on failure like post_result."""
        poster = ApiPoster(
            "https://api.example.com/result",
            "test-secret",
            max_retries=2,
            backoff_base=0.01,
            backoff_multiplier=1.0,
        )

        mock_fail = MagicMock()
        mock_fail.status_code = 502
        mock_fail.text = "Bad Gateway"

        with patch.object(poster._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_fail
            result = await poster.post_status_update("run-42")

        assert result is False
        assert mock_post.call_count == 2

        await poster.close()


# ===========================================================================
# ApiPoster — close
# ===========================================================================


class TestApiPosterClose:
    """Tests for ApiPoster.close."""

    @pytest.mark.asyncio
    async def test_close_shuts_down_client(self) -> None:
        """Close calls aclose on the underlying httpx client."""
        poster = ApiPoster("https://api.example.com/result", "test-secret")

        with patch.object(
            poster._client, "aclose", new_callable=AsyncMock
        ) as mock_close:
            await poster.close()
            mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_suppresses_event_loop_closed_error(self) -> None:
        """Close gracefully handles RuntimeError from a closed event loop.

        When the httpx client's connections were opened in a different event
        loop, aclose() raises RuntimeError('Event loop is closed').  The
        poster must suppress this error and log a warning.
        """
        poster = ApiPoster("https://api.example.com/result", "test-secret")

        with patch.object(
            poster._client,
            "aclose",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Event loop is closed"),
        ):
            # Should NOT raise
            await poster.close()

    @pytest.mark.asyncio
    async def test_close_reraises_other_runtime_errors(self) -> None:
        """Close re-raises RuntimeError that is not about event loop closure."""
        poster = ApiPoster("https://api.example.com/result", "test-secret")

        with patch.object(
            poster._client,
            "aclose",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Some other RuntimeError"),
        ):
            with pytest.raises(RuntimeError, match="Some other RuntimeError"):
                await poster.close()
