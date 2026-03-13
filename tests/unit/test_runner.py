"""Unit tests for the agent runner core (Component 4.2).

Tests cover:
- :class:`AgentExecutionError` exception attributes.
- :class:`AgentSessionLog` dataclass construction and defaults.
- :class:`AgentRunner` lifecycle: ``start``, ``create_session``,
  ``send_instructions``, ``shutdown``, and ``run``.
- Error handling: CLI not found, auth failure, runtime errors, timeout.
- Session event handler dispatch via the unified callback.
- CLI argument parser: valid args, ``@filepath`` instructions.

Note:
    The Copilot SDK exposes native async methods. In tests we mock the SDK
    methods as ``AsyncMock`` callables so the runner can ``await`` them
    without a real CLI process.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# We import the enum for constructing mock events.
from copilot import PermissionHandler
from copilot.generated.session_events import SessionEventType

from app.src.agent.runner import (
    AgentExecutionError,
    AgentRunner,
    AgentSessionLog,
    _build_argument_parser,
    _parse_list_arg,
    _resolve_instructions,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session() -> MagicMock:
    """Create a mock CopilotSession.

    Async methods (``send_and_wait``, ``destroy``, ``abort``) use
    ``AsyncMock`` so that the runner can ``await`` them. The synchronous
    ``on()`` method uses a regular ``MagicMock``.

    Returns:
        A :class:`MagicMock` imitating a ``CopilotSession``.
    """
    session = MagicMock()
    session.send_and_wait = AsyncMock(
        return_value=MagicMock()  # Non-None means success.
    )
    session.send = AsyncMock(return_value="msg-id")
    session.destroy = AsyncMock()
    session.abort = AsyncMock()
    # on() returns an unsubscribe callable (synchronous).
    session.on = MagicMock(return_value=MagicMock())
    return session


def _make_mock_client(
    *,
    start_side_effect: Exception | None = None,
    create_session_side_effect: Exception | None = None,
    session: MagicMock | None = None,
) -> MagicMock:
    """Create a mock CopilotClient with configurable side effects.

    Async methods (``start``, ``create_session``, ``stop``) use ``AsyncMock``
    so the runner can ``await`` them.

    Args:
        start_side_effect: Exception to raise on ``start()``.
        create_session_side_effect: Exception to raise on
            ``create_session()``.
        session: Optional pre-built mock session. If ``None``, a default
            mock session is created.

    Returns:
        A :class:`MagicMock` imitating a ``CopilotClient``.
    """
    client = MagicMock()
    mock_session = session or _make_mock_session()

    if start_side_effect:
        client.start = AsyncMock(side_effect=start_side_effect)
    else:
        client.start = AsyncMock()

    if create_session_side_effect:
        client.create_session = AsyncMock(side_effect=create_session_side_effect)
    else:
        client.create_session = AsyncMock(return_value=mock_session)

    client.stop = AsyncMock()
    return client


def _make_runner(**overrides: Any) -> AgentRunner:
    """Create an AgentRunner with default test parameters.

    Args:
        **overrides: Keyword arguments to override default parameters.

    Returns:
        A configured :class:`AgentRunner` instance.
    """
    defaults: dict[str, Any] = {
        "run_id": "test-run-001",
        "model": "claude-sonnet-4-20250514",
        "system_message": "You are a test agent.",
        "timeout_minutes": 1,
    }
    defaults.update(overrides)
    return AgentRunner(**defaults)


def _make_session_event(
    event_type: SessionEventType,
    **data_attrs: Any,
) -> MagicMock:
    """Create a mock SessionEvent with the given type and data attributes.

    Args:
        event_type: The :class:`SessionEventType` to assign.
        **data_attrs: Attributes to set on the event's ``data`` namespace.

    Returns:
        A mock event matching the ``SessionEvent`` contract.
    """
    data = types.SimpleNamespace(**data_attrs)
    event = MagicMock()
    event.type = event_type
    event.data = data
    return event


# ===========================================================================
# AgentExecutionError tests
# ===========================================================================


class TestAgentExecutionError:
    """Tests for the :class:`AgentExecutionError` exception."""

    def test_attributes_stored(self) -> None:
        """Verify error_code, error_message, and error_details are stored."""
        exc = AgentExecutionError(
            error_message="Something went wrong",
            error_code="SDK_INIT_ERROR",
            error_details={"key": "value"},
        )
        assert exc.error_code == "SDK_INIT_ERROR"
        assert exc.error_message == "Something went wrong"
        assert exc.error_details == {"key": "value"}
        assert str(exc) == "Something went wrong"

    def test_default_error_code(self) -> None:
        """Verify default error_code is AGENT_RUNTIME_ERROR."""
        exc = AgentExecutionError(error_message="Oops")
        assert exc.error_code == "AGENT_RUNTIME_ERROR"
        assert exc.error_details is None

    def test_inherits_from_exception(self) -> None:
        """Verify AgentExecutionError is an Exception subclass."""
        exc = AgentExecutionError(error_message="test")
        assert isinstance(exc, Exception)


# ===========================================================================
# AgentSessionLog tests
# ===========================================================================


class TestAgentSessionLog:
    """Tests for the :class:`AgentSessionLog` dataclass."""

    def test_default_construction(self) -> None:
        """Verify default field values on construction."""
        log = AgentSessionLog()
        assert log.messages == []
        assert log.tool_calls == []
        assert log.errors == []
        assert log.usage == []
        assert log.end_time is None
        assert log.final_message is None
        assert log.timed_out is False
        assert log.turn_count == 0
        assert log.total_input_tokens == 0
        assert log.total_output_tokens == 0
        assert log.start_time  # ISO 8601 string, non-empty

    def test_mutable_fields_independent(self) -> None:
        """Verify mutable list fields are independent across instances."""
        log1 = AgentSessionLog()
        log2 = AgentSessionLog()
        log1.messages.append({"content": "hello"})
        assert log2.messages == []

    def test_attribute_assignment(self) -> None:
        """Verify fields can be mutated after construction."""
        log = AgentSessionLog()
        log.timed_out = True
        log.end_time = "2026-01-01T00:00:00Z"
        log.final_message = "Done"
        assert log.timed_out is True
        assert log.end_time == "2026-01-01T00:00:00Z"
        assert log.final_message == "Done"


# ===========================================================================
# AgentRunner.start() tests
# ===========================================================================


class TestAgentRunnerStart:
    """Tests for :meth:`AgentRunner.start`."""

    @pytest.mark.asyncio
    async def test_start_initialises_client(self) -> None:
        """Verify CopilotClient is created and start() is called."""
        runner = _make_runner()
        mock_client = _make_mock_client()

        with (
            patch.dict(
                "os.environ",
                {"GITHUB_PAT": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""},
                clear=False,
            ),
            patch("app.src.agent.runner.CopilotClient", return_value=mock_client),
        ):
            await runner.start()

        mock_client.start.assert_called_once()
        assert runner._client is mock_client

    @pytest.mark.asyncio
    async def test_start_with_cli_path(self) -> None:
        """Verify cli_path is passed to CopilotClient via options dict."""
        runner = _make_runner(cli_path="/usr/local/bin/copilot")
        mock_client = _make_mock_client()

        with (
            patch.dict(
                "os.environ",
                {"GITHUB_PAT": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""},
                clear=False,
            ),
            patch(
                "app.src.agent.runner.CopilotClient", return_value=mock_client
            ) as mock_cls,
        ):
            await runner.start()

        mock_cls.assert_called_once_with(
            {"cli_path": "/usr/local/bin/copilot", "use_logged_in_user": False}
        )

    @pytest.mark.asyncio
    async def test_start_with_repo_path_sets_cwd(self, tmp_path: Path) -> None:
        """Verify repo_path is passed as cwd in CopilotClient options."""
        runner = _make_runner(repo_path=tmp_path)
        mock_client = _make_mock_client()

        with (
            patch.dict(
                "os.environ",
                {"GITHUB_PAT": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""},
                clear=False,
            ),
            patch(
                "app.src.agent.runner.CopilotClient", return_value=mock_client
            ) as mock_cls,
        ):
            await runner.start()

        mock_cls.assert_called_once_with(
            {"cwd": str(tmp_path), "use_logged_in_user": False}
        )

    @pytest.mark.asyncio
    async def test_start_logs_cwd_when_repo_path_provided(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify CWD confirmation log is emitted when repo_path is set."""
        runner = _make_runner(repo_path=tmp_path)
        mock_client = _make_mock_client()

        with (
            patch.dict(
                "os.environ",
                {"GITHUB_PAT": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""},
                clear=False,
            ),
            patch("app.src.agent.runner.CopilotClient", return_value=mock_client),
            caplog.at_level("INFO", logger="app.src.agent.runner"),
        ):
            await runner.start()

        cwd_log_messages = [
            r.message
            for r in caplog.records
            if "Agent workspace CWD set to" in r.message
        ]
        assert len(cwd_log_messages) == 1, "Expected exactly one CWD log message"
        assert str(tmp_path) in cwd_log_messages[0]

    @pytest.mark.asyncio
    async def test_start_with_cli_path_and_repo_path(self, tmp_path: Path) -> None:
        """Verify cli_path and repo_path are both included in options."""
        runner = _make_runner(cli_path="/usr/local/bin/copilot", repo_path=tmp_path)
        mock_client = _make_mock_client()

        with (
            patch.dict(
                "os.environ",
                {"GITHUB_PAT": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""},
                clear=False,
            ),
            patch(
                "app.src.agent.runner.CopilotClient", return_value=mock_client
            ) as mock_cls,
        ):
            await runner.start()

        mock_cls.assert_called_once_with(
            {
                "cli_path": "/usr/local/bin/copilot",
                "cwd": str(tmp_path),
                "use_logged_in_user": False,
            }
        )

    @pytest.mark.asyncio
    async def test_start_passes_github_token_when_available(self) -> None:
        """Verify github_token is passed to CopilotClient when env provides it."""
        runner = _make_runner()
        mock_client = _make_mock_client()

        with (
            patch.dict(
                "os.environ",
                {"GITHUB_PAT": "test-token", "OPENAI_API_KEY": ""},
                clear=False,
            ),
            patch(
                "app.src.agent.runner.CopilotClient", return_value=mock_client
            ) as mock_cls,
        ):
            await runner.start()

        mock_cls.assert_called_once_with({"github_token": "test-token"})

    @pytest.mark.asyncio
    async def test_start_skips_explicit_token_when_disabled(self) -> None:
        """Verify explicit token auth can be disabled via environment flag."""
        with patch.dict(
            "os.environ",
            {
                "GITHUB_PAT": "test-token",
                "DISPATCH_DISABLE_EXPLICIT_GITHUB_TOKEN": "true",
            },
            clear=False,
        ):
            runner = _make_runner()
            mock_client = _make_mock_client()
            with patch(
                "app.src.agent.runner.CopilotClient", return_value=mock_client
            ) as mock_cls:
                await runner.start()

            mock_cls.assert_called_once_with({"use_logged_in_user": False})

    @pytest.mark.asyncio
    async def test_start_handles_cli_not_found(self) -> None:
        """Verify FileNotFoundError raises AgentExecutionError(SDK_INIT_ERROR)."""
        runner = _make_runner()
        mock_client = _make_mock_client(
            start_side_effect=FileNotFoundError("copilot: command not found")
        )

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            with pytest.raises(AgentExecutionError) as exc_info:
                await runner.start()

        assert exc_info.value.error_code == "SDK_INIT_ERROR"
        assert "not found" in exc_info.value.error_message.lower()
        assert exc_info.value.error_details is not None
        assert exc_info.value.error_details["exception_type"] == "FileNotFoundError"

    @pytest.mark.asyncio
    async def test_start_handles_permission_error(self) -> None:
        """Verify PermissionError raises AgentExecutionError(SDK_INIT_ERROR)."""
        runner = _make_runner()
        mock_client = _make_mock_client(
            start_side_effect=PermissionError("Permission denied")
        )

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            with pytest.raises(AgentExecutionError) as exc_info:
                await runner.start()

        assert exc_info.value.error_code == "SDK_INIT_ERROR"
        assert exc_info.value.error_details is not None
        assert exc_info.value.error_details["exception_type"] == "PermissionError"

    @pytest.mark.asyncio
    async def test_start_handles_generic_error(self) -> None:
        """Verify generic Exception raises AgentExecutionError(SDK_INIT_ERROR)."""
        runner = _make_runner()
        mock_client = _make_mock_client(
            start_side_effect=RuntimeError("Unexpected SDK failure")
        )

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            with pytest.raises(AgentExecutionError) as exc_info:
                await runner.start()

        assert exc_info.value.error_code == "SDK_INIT_ERROR"
        assert exc_info.value.error_details is not None
        assert exc_info.value.error_details["exception_type"] == "RuntimeError"


# ===========================================================================
# AgentRunner.create_session() tests
# ===========================================================================


class TestAgentRunnerCreateSession:
    """Tests for :meth:`AgentRunner.create_session`."""

    @pytest.mark.asyncio
    async def test_create_session_with_model_and_system_message(self) -> None:
        """Verify create_session is called with correct SessionConfig dict."""
        runner = _make_runner()
        mock_client = _make_mock_client()

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

        mock_client.create_session.assert_called_once()
        call_args = mock_client.create_session.call_args[0][0]
        assert call_args["model"] == "claude-sonnet-4-20250514"
        assert call_args["system_message"] == {
            "mode": "replace",
            "content": "You are a test agent.",
        }
        assert call_args["streaming"] is True
        assert call_args["on_permission_request"] is PermissionHandler.approve_all

    @pytest.mark.asyncio
    async def test_create_session_with_skill_directories(self) -> None:
        """Verify skill_directories are included in session config."""
        runner = _make_runner(skill_directories=["/path/to/skill.md"])
        mock_client = _make_mock_client()

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

        call_args = mock_client.create_session.call_args[0][0]
        assert call_args["skill_directories"] == ["/path/to/skill.md"]

    @pytest.mark.asyncio
    async def test_create_session_without_skill_directories(self) -> None:
        """Verify skill_directories is not in config when None."""
        runner = _make_runner()
        mock_client = _make_mock_client()

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

        call_args = mock_client.create_session.call_args[0][0]
        assert "skill_directories" not in call_args

    @pytest.mark.asyncio
    async def test_create_session_with_custom_agents(self) -> None:
        """Verify custom_agents are included in session config when provided."""
        runner = _make_runner(
            custom_agents=[
                {
                    "name": "security-reviewer",
                    "display_name": "Security Reviewer",
                    "description": "Reviews security issues.",
                    "prompt": "Review for security risks.",
                    "tools": ["grep", "view"],
                    "infer": True,
                }
            ]
        )
        mock_client = _make_mock_client()

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

        call_args = mock_client.create_session.call_args[0][0]
        assert "custom_agents" in call_args
        assert call_args["custom_agents"][0]["name"] == "security-reviewer"
        assert call_args["custom_agents"][0]["tools"] == ["grep", "view"]
        assert call_args["custom_agents"][0]["infer"] is True

    @pytest.mark.asyncio
    async def test_create_session_omits_custom_agents_when_none(self) -> None:
        """Verify custom_agents is omitted when no custom agents are provided."""
        runner = _make_runner(custom_agents=None)
        mock_client = _make_mock_client()

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

        call_args = mock_client.create_session.call_args[0][0]
        assert "custom_agents" not in call_args

    @pytest.mark.asyncio
    async def test_create_session_auth_failure(self) -> None:
        """Verify auth error raises AgentExecutionError(SESSION_CREATE_ERROR)."""
        runner = _make_runner()
        mock_client = _make_mock_client(
            create_session_side_effect=PermissionError("Invalid token")
        )

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()

            with pytest.raises(AgentExecutionError) as exc_info:
                await runner.create_session()

        assert exc_info.value.error_code == "SESSION_CREATE_ERROR"
        assert exc_info.value.error_details is not None
        assert exc_info.value.error_details["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_create_session_without_client_raises(self) -> None:
        """Verify create_session raises when client has not been started."""
        runner = _make_runner()

        with pytest.raises(AgentExecutionError) as exc_info:
            await runner.create_session()

        assert exc_info.value.error_code == "SDK_INIT_ERROR"

    @pytest.mark.asyncio
    async def test_create_session_subscribes_to_events(self) -> None:
        """Verify session.on() is called with the unified event handler."""
        runner = _make_runner()
        mock_client = _make_mock_client()

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

        mock_session = mock_client.create_session.return_value
        mock_session.on.assert_called_once()

        # The handler should be the runner's _handle_session_event method.
        handler = mock_session.on.call_args[0][0]
        assert callable(handler)


# ===========================================================================
# AgentRunner event handler tests
# ===========================================================================


class TestEventHandlers:
    """Tests for session event handler dispatch and individual handlers."""

    def test_on_assistant_message(self) -> None:
        """Verify assistant messages are logged and recorded."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.ASSISTANT_MESSAGE,
            content="Hello, I will implement this.",
        )

        runner._handle_session_event(event)

        assert len(runner.session_log.messages) == 1
        assert (
            runner.session_log.messages[0]["content"] == "Hello, I will implement this."
        )
        assert runner.session_log.final_message == "Hello, I will implement this."

    def test_on_assistant_message_updates_final(self) -> None:
        """Verify final_message is updated with the latest message."""
        runner = _make_runner()

        event1 = _make_session_event(
            SessionEventType.ASSISTANT_MESSAGE, content="First"
        )
        event2 = _make_session_event(
            SessionEventType.ASSISTANT_MESSAGE, content="Second"
        )

        runner._handle_session_event(event1)
        runner._handle_session_event(event2)

        assert runner.session_log.final_message == "Second"
        assert len(runner.session_log.messages) == 2

    def test_on_tool_call(self) -> None:
        """Verify tool calls are logged with name, args, and timestamp."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.TOOL_EXECUTION_START,
            tool_name="read_file",
            arguments='{"path": "/src/main.py"}',
        )

        runner._handle_session_event(event)

        assert len(runner.session_log.tool_calls) == 1
        tc = runner.session_log.tool_calls[0]
        assert tc["name"] == "read_file"
        assert "/src/main.py" in tc["arguments"]
        assert tc["result"] is None
        assert "timestamp" in tc

    def test_on_tool_call_stores_full_args(self) -> None:
        """Verify full tool arguments are stored without truncation."""
        runner = _make_runner()
        long_args = "x" * 500
        event = _make_session_event(
            SessionEventType.TOOL_EXECUTION_START,
            tool_name="write_file",
            arguments=long_args,
        )

        runner._handle_session_event(event)

        tc = runner.session_log.tool_calls[0]
        assert tc["arguments"] == long_args
        assert len(tc["arguments"]) == 500

    def test_on_tool_result_updates_matching_call(self) -> None:
        """Verify tool result updates the most recent matching tool call."""
        runner = _make_runner()

        call_event = _make_session_event(
            SessionEventType.TOOL_EXECUTION_START,
            tool_name="read_file",
            arguments="{}",
        )
        result_event = _make_session_event(
            SessionEventType.TOOL_EXECUTION_COMPLETE,
            tool_name="read_file",
            result="File contents here",
        )

        runner._handle_session_event(call_event)
        runner._handle_session_event(result_event)

        tc = runner.session_log.tool_calls[0]
        assert tc["result"] == "File contents here"

    def test_on_tool_result_stores_full_result(self) -> None:
        """Verify full tool results are stored without truncation."""
        runner = _make_runner()

        call_event = _make_session_event(
            SessionEventType.TOOL_EXECUTION_START,
            tool_name="read_file",
            arguments="{}",
        )
        long_result = "y" * 500
        result_event = _make_session_event(
            SessionEventType.TOOL_EXECUTION_COMPLETE,
            tool_name="read_file",
            result=long_result,
        )

        runner._handle_session_event(call_event)
        runner._handle_session_event(result_event)

        tc = runner.session_log.tool_calls[0]
        assert tc["result"] == long_result
        assert len(tc["result"]) == 500

    def test_on_session_idle(self) -> None:
        """Verify session idle event is handled without error."""
        runner = _make_runner()
        event = _make_session_event(SessionEventType.SESSION_IDLE)

        # Should not raise.
        runner._handle_session_event(event)

    def test_on_session_error_appends_to_log(self) -> None:
        """Verify session errors are appended to the session log."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.SESSION_ERROR,
            message="Tool execution failed",
            error_type="tool_error",
        )

        runner._handle_session_event(event)

        assert len(runner.session_log.errors) == 1
        assert runner.session_log.errors[0]["message"] == "Tool execution failed"
        assert runner.session_log.errors[0]["details"] == {"error_type": "tool_error"}

    def test_on_session_error_without_error_type(self) -> None:
        """Verify session error without error_type has None details."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.SESSION_ERROR,
            message="Generic error",
        )

        runner._handle_session_event(event)

        assert len(runner.session_log.errors) == 1
        assert runner.session_log.errors[0]["details"] is None

    def test_on_session_task_complete(self) -> None:
        """Verify session task complete event is handled without error."""
        runner = _make_runner()
        event = _make_session_event(SessionEventType.SESSION_TASK_COMPLETE)

        # Should not raise.
        runner._handle_session_event(event)

    def test_unhandled_event_type_ignored(self) -> None:
        """Verify unhandled event types do not raise."""
        runner = _make_runner()
        event = _make_session_event(SessionEventType.SESSION_START)

        # Should not raise.
        runner._handle_session_event(event)
        assert len(runner.session_log.messages) == 0

    def test_on_streaming_delta(self) -> None:
        """Verify streaming delta events are handled without error."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.ASSISTANT_STREAMING_DELTA,
            delta_content="chunk",
        )

        # Should not raise.
        runner._handle_session_event(event)
        # Streaming deltas are not persisted to session log.
        assert len(runner.session_log.messages) == 0

    def test_on_message_delta(self) -> None:
        """Verify message delta events are handled without error."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.ASSISTANT_MESSAGE_DELTA,
            delta_content="Hello world",
        )

        runner._handle_session_event(event)
        # Message deltas are not persisted — final message arrives via ASSISTANT_MESSAGE.
        assert len(runner.session_log.messages) == 0

    def test_on_assistant_usage_records_tokens(self) -> None:
        """Verify usage events are captured in session log."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.ASSISTANT_USAGE,
            input_tokens=500,
            output_tokens=200,
            cost=0.003,
            model="claude-sonnet-4-20250514",
            duration=1.5,
            cache_read_tokens=100,
            cache_write_tokens=50,
        )

        runner._handle_session_event(event)

        assert len(runner.session_log.usage) == 1
        entry = runner.session_log.usage[0]
        assert entry["input_tokens"] == 500
        assert entry["output_tokens"] == 200
        assert entry["cost"] == 0.003
        assert entry["model"] == "claude-sonnet-4-20250514"
        assert entry["duration"] == 1.5
        assert entry["cache_read_tokens"] == 100
        assert entry["cache_write_tokens"] == 50
        assert "timestamp" in entry
        assert runner.session_log.total_input_tokens == 500
        assert runner.session_log.total_output_tokens == 200

    def test_on_assistant_usage_accumulates_totals(self) -> None:
        """Verify multiple usage events accumulate token totals."""
        runner = _make_runner()
        event1 = _make_session_event(
            SessionEventType.ASSISTANT_USAGE,
            input_tokens=100,
            output_tokens=50,
        )
        event2 = _make_session_event(
            SessionEventType.ASSISTANT_USAGE,
            input_tokens=200,
            output_tokens=80,
        )

        runner._handle_session_event(event1)
        runner._handle_session_event(event2)

        assert len(runner.session_log.usage) == 2
        assert runner.session_log.total_input_tokens == 300
        assert runner.session_log.total_output_tokens == 130

    def test_on_assistant_usage_handles_none_values(self) -> None:
        """Verify usage handler tolerates None token values gracefully."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.ASSISTANT_USAGE,
            # Simulate missing fields by not providing them.
        )

        runner._handle_session_event(event)

        assert len(runner.session_log.usage) == 1
        entry = runner.session_log.usage[0]
        assert entry["input_tokens"] == 0
        assert entry["output_tokens"] == 0
        assert entry["cache_read_tokens"] == 0
        assert entry["cache_write_tokens"] == 0
        assert runner.session_log.total_input_tokens == 0
        assert runner.session_log.total_output_tokens == 0

    def test_on_turn_start_increments_count(self) -> None:
        """Verify turn start events increment the turn counter."""
        runner = _make_runner()
        event1 = _make_session_event(
            SessionEventType.ASSISTANT_TURN_START,
            turn_id="turn-1",
        )
        event2 = _make_session_event(
            SessionEventType.ASSISTANT_TURN_START,
            turn_id="turn-2",
        )

        runner._handle_session_event(event1)
        assert runner.session_log.turn_count == 1

        runner._handle_session_event(event2)
        assert runner.session_log.turn_count == 2

    def test_on_turn_end(self) -> None:
        """Verify turn end events are handled without error."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.ASSISTANT_TURN_END,
            turn_id="turn-1",
        )

        # Should not raise.
        runner._handle_session_event(event)

    def test_on_session_usage_info(self) -> None:
        """Verify session usage info events are handled without error."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.SESSION_USAGE_INFO,
            total_api_duration_ms=12500.0,
            total_premium_requests=3.0,
        )

        # Should not raise.
        runner._handle_session_event(event)

    def test_on_tool_partial_result(self) -> None:
        """Verify tool partial result events are handled without error."""
        runner = _make_runner()
        event = _make_session_event(
            SessionEventType.TOOL_EXECUTION_PARTIAL_RESULT,
            tool_name="run_in_terminal",
            partial_output="Building project...",
        )

        # Should not raise.
        runner._handle_session_event(event)


# ===========================================================================
# AgentRunner.send_instructions() tests
# ===========================================================================


class TestAgentRunnerSendInstructions:
    """Tests for :meth:`AgentRunner.send_instructions`."""

    @pytest.mark.asyncio
    async def test_send_instructions_returns_session_log(self) -> None:
        """Verify a successful session returns a populated session log."""
        runner = _make_runner()
        mock_session = _make_mock_session()
        mock_client = _make_mock_client(session=mock_session)

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

            log = await runner.send_instructions("Implement feature X")

        assert isinstance(log, AgentSessionLog)
        assert log.end_time is not None
        assert log.timed_out is False

        # Verify send_and_wait was called with correct args.
        mock_session.send_and_wait.assert_called_once()
        call_args = mock_session.send_and_wait.call_args
        assert call_args[0][0] == {"prompt": "Implement feature X"}
        assert call_args[0][1] == 60.0  # 1 minute * 60

    @pytest.mark.asyncio
    async def test_send_instructions_without_session_raises(self) -> None:
        """Verify sending without an active session raises."""
        runner = _make_runner()

        with pytest.raises(AgentExecutionError) as exc_info:
            await runner.send_instructions("Do something")

        assert exc_info.value.error_code == "AGENT_RUNTIME_ERROR"

    @pytest.mark.asyncio
    async def test_timeout_enforcement(self) -> None:
        """Verify timeout is detected when send_and_wait returns None."""
        runner = _make_runner(timeout_minutes=1)
        mock_session = _make_mock_session()
        mock_session.send_and_wait = AsyncMock(return_value=None)  # Timeout.
        mock_client = _make_mock_client(session=mock_session)

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

            log = await runner.send_instructions("Do something slow")

        assert log.timed_out is True
        assert log.end_time is not None
        # Verify abort was called after timeout.
        mock_session.abort.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_error_sets_timed_out(self) -> None:
        """Verify TimeoutError from SDK sets timed_out flag."""
        runner = _make_runner(timeout_minutes=1)
        mock_session = _make_mock_session()
        mock_session.send_and_wait = AsyncMock(
            side_effect=TimeoutError("Timeout after 60.0s waiting for session.idle")
        )
        mock_client = _make_mock_client(session=mock_session)

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

            log = await runner.send_instructions("Do something slow")

        assert log.timed_out is True
        assert log.end_time is not None
        assert len(log.errors) == 1
        assert "timed out" in log.errors[0]["message"].lower()
        mock_session.abort.assert_called_once()

    @pytest.mark.asyncio
    async def test_runtime_error_raises_agent_execution_error(self) -> None:
        """Verify runtime exceptions raise AgentExecutionError for orchestration handling."""
        runner = _make_runner()
        mock_session = _make_mock_session()
        mock_session.send_and_wait = AsyncMock(
            side_effect=RuntimeError("Agent crashed")
        )
        mock_client = _make_mock_client(session=mock_session)

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()

            with pytest.raises(AgentExecutionError) as exc_info:
                await runner.send_instructions("Do something")

        assert exc_info.value.error_code == "AGENT_RUNTIME_ERROR"
        assert "Agent crashed" in exc_info.value.error_message
        assert len(runner.session_log.errors) == 1
        assert "Agent crashed" in runner.session_log.errors[0]["message"]
        assert runner.session_log.end_time is not None


# ===========================================================================
# AgentRunner.shutdown() tests
# ===========================================================================


class TestAgentRunnerShutdown:
    """Tests for :meth:`AgentRunner.shutdown`."""

    @pytest.mark.asyncio
    async def test_shutdown_stops_client(self) -> None:
        """Verify shutdown calls session.destroy() and client.stop()."""
        runner = _make_runner()
        mock_session = _make_mock_session()
        mock_client = _make_mock_client(session=mock_session)

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()
            await runner.shutdown()

        mock_session.destroy.assert_called_once()
        mock_client.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_errors_silently(self) -> None:
        """Verify shutdown does not raise even if destroy/stop fail."""
        runner = _make_runner()
        mock_session = _make_mock_session()
        mock_session.destroy = AsyncMock(side_effect=RuntimeError("destroy failed"))
        mock_client = _make_mock_client(session=mock_session)
        mock_client.stop = AsyncMock(side_effect=RuntimeError("stop failed"))

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()
            # Should not raise.
            await runner.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_without_session(self) -> None:
        """Verify shutdown works when no session was created."""
        runner = _make_runner()
        mock_client = _make_mock_client()

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.shutdown()

        mock_client.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_unsubscribes_events(self) -> None:
        """Verify shutdown calls the unsubscribe function from session.on()."""
        runner = _make_runner()
        mock_session = _make_mock_session()
        unsubscribe_fn = MagicMock()
        mock_session.on = MagicMock(return_value=unsubscribe_fn)
        mock_client = _make_mock_client(session=mock_session)

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            await runner.start()
            await runner.create_session()
            await runner.shutdown()

        unsubscribe_fn.assert_called_once()


# ===========================================================================
# AgentRunner.run() tests
# ===========================================================================


class TestAgentRunnerRun:
    """Tests for :meth:`AgentRunner.run` (full lifecycle)."""

    @pytest.mark.asyncio
    async def test_shutdown_always_called(self) -> None:
        """Verify shutdown is called even when create_session raises."""
        runner = _make_runner()
        mock_client = _make_mock_client(
            create_session_side_effect=RuntimeError("Session creation boom")
        )

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            with pytest.raises(AgentExecutionError):
                await runner.run("Do something")

        # shutdown() should still have been called (stop the client).
        mock_client.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_full_lifecycle(self) -> None:
        """Verify run() calls start, create_session, send, shutdown in order."""
        runner = _make_runner()
        mock_session = _make_mock_session()
        mock_client = _make_mock_client(session=mock_session)

        call_order: list[str] = []

        orig_start = mock_client.start
        orig_create = mock_client.create_session
        orig_send = mock_session.send_and_wait
        orig_stop = mock_client.stop

        async def track_start() -> None:
            call_order.append("start")
            return await orig_start()

        async def track_create(config: Any) -> Any:
            call_order.append("create_session")
            return await orig_create(config)

        async def track_send(options: Any, timeout: Any) -> Any:
            call_order.append("send_and_wait")
            return await orig_send(options, timeout)

        async def track_stop() -> None:
            call_order.append("stop")
            return await orig_stop()

        mock_client.start = AsyncMock(side_effect=track_start)
        mock_client.create_session = AsyncMock(side_effect=track_create)
        mock_session.send_and_wait = AsyncMock(side_effect=track_send)
        mock_client.stop = AsyncMock(side_effect=track_stop)

        with (patch("app.src.agent.runner.CopilotClient", return_value=mock_client),):
            log = await runner.run("Implement feature")

        assert isinstance(log, AgentSessionLog)
        assert call_order == ["start", "create_session", "send_and_wait", "stop"]

    @pytest.mark.skip(reason="No longer retrying without explicit token")
    @pytest.mark.skip(reason="No longer retrying without explicit token")
    @pytest.mark.asyncio
    async def test_run_retries_without_explicit_token_after_model_list_error(
        self,
    ) -> None:
        """Verify run retries once without explicit token after list-models 400."""
        runner = _make_runner()

        failing_session = _make_mock_session()
        failing_session.send_and_wait = AsyncMock(
            side_effect=Exception(
                "Session error: Execution failed: Error: Failed to list models: 400 "
            )
        )
        succeeding_session = _make_mock_session()

        first_client = _make_mock_client(session=failing_session)
        second_client = _make_mock_client(session=succeeding_session)

        with (
            patch.dict(
                "os.environ",
                {"GITHUB_PAT": "test-token", "OPENAI_API_KEY": ""},
                clear=False,
            ),
            patch(
                "app.src.agent.runner.CopilotClient",
                side_effect=[first_client, second_client],
            ) as mock_cls,
        ):
            log = await runner.run("Implement feature")

        assert isinstance(log, AgentSessionLog)
        assert mock_cls.call_count == 2
        assert mock_cls.call_args_list[0].args == ({"github_token": "test-token"},)
        assert mock_cls.call_args_list[1].args == ()


# ===========================================================================
# CLI argument parser tests
# ===========================================================================


class TestCLIArgumentParser:
    """Tests for the CLI argument parser and instruction resolver."""

    def test_parses_required_args(self) -> None:
        """Verify all required arguments are parsed correctly."""
        parser = _build_argument_parser()
        args = parser.parse_args(
            [
                "--run-id",
                "run-001",
                "--role",
                "implement",
                "--model",
                "claude-sonnet-4-20250514",
                "--instructions",
                "Implement feature X",
                "--repo-path",
                "/tmp/target-repo",
            ]
        )

        assert args.run_id == "run-001"
        assert args.role == "implement"
        assert args.model == "claude-sonnet-4-20250514"
        assert args.instructions == "Implement feature X"
        assert args.repo_path == "/tmp/target-repo"

    def test_parses_optional_args(self) -> None:
        """Verify optional arguments have correct defaults and parse."""
        parser = _build_argument_parser()
        args = parser.parse_args(
            [
                "--run-id",
                "run-002",
                "--role",
                "review",
                "--model",
                "gpt-4.1",
                "--instructions",
                "Review PR #42",
                "--repo-path",
                "/tmp/repo",
                "--system-instructions",
                "Be thorough",
                "--timeout",
                "45",
                "--skill-paths",
                "skills/a.md,skills/b.md",
                "--agent-paths",
                ".github/agents/a.agent.md,.github/agents/b.agent.md",
                "--api-result-url",
                "https://api.example.com/result",
                "--webhook-secret",
                "s3cret",
                "--pr-number",
                "42",
                "--repository",
                "owner/repo",
                "--branch",
                "develop",
            ]
        )

        assert args.system_instructions == "Be thorough"
        assert args.timeout == 45
        assert args.skill_paths == "skills/a.md,skills/b.md"
        assert args.agent_paths == ".github/agents/a.agent.md,.github/agents/b.agent.md"
        assert args.api_result_url == "https://api.example.com/result"
        assert args.webhook_secret == "s3cret"
        assert args.pr_number == "42"
        assert args.repository == "owner/repo"
        assert args.branch == "develop"

    def test_defaults_for_optional_args(self) -> None:
        """Verify default values for optional arguments."""
        parser = _build_argument_parser()
        args = parser.parse_args(
            [
                "--run-id",
                "run-003",
                "--role",
                "merge",
                "--model",
                "gpt-5",
                "--instructions",
                "Merge PR #10",
                "--repo-path",
                "/tmp/repo",
            ]
        )

        assert args.system_instructions is None
        assert args.timeout == 30
        assert args.skill_paths is None
        assert args.agent_paths is None
        assert args.api_result_url is None
        assert args.webhook_secret is None
        assert args.pr_number is None
        assert args.repository is None
        assert args.branch == "main"

    def test_role_choices_enforced(self) -> None:
        """Verify invalid role choice is rejected."""
        parser = _build_argument_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "--run-id",
                    "run-004",
                    "--role",
                    "deploy",
                    "--model",
                    "gpt-5",
                    "--instructions",
                    "Deploy",
                    "--repo-path",
                    "/tmp/repo",
                ]
            )

    def test_resolve_instructions_plain_text(self) -> None:
        """Verify plain text instructions are returned as-is."""
        result = _resolve_instructions("Implement feature X")
        assert result == "Implement feature X"

    def test_resolve_instructions_from_file(self, tmp_path: Path) -> None:
        """Verify @filepath reads file contents."""
        instr_file = tmp_path / "instructions.txt"
        instr_file.write_text("Build the login page", encoding="utf-8")

        result = _resolve_instructions(f"@{instr_file}")
        assert result == "Build the login page"

    def test_resolve_instructions_missing_file(self, tmp_path: Path) -> None:
        """Verify missing @filepath exits with error."""
        with pytest.raises(SystemExit):
            _resolve_instructions(f"@{tmp_path / 'nonexistent.txt'}")


# ===========================================================================
# _parse_list_arg tests
# ===========================================================================


class TestParseListArg:
    """Tests for the _parse_list_arg helper that normalises list CLI args."""

    def test_none_returns_none(self) -> None:
        """Verify None input yields None."""
        assert _parse_list_arg(None) is None

    def test_empty_string_returns_none(self) -> None:
        """Verify empty string yields None."""
        assert _parse_list_arg("") is None

    def test_plain_comma_separated(self) -> None:
        """Verify standard comma-separated input is parsed correctly."""
        result = _parse_list_arg("a.md,b.md,c.md")
        assert result == ["a.md", "b.md", "c.md"]

    def test_single_value_no_brackets(self) -> None:
        """Verify a single value without brackets is parsed correctly."""
        result = _parse_list_arg(".github/agents/PhaseDocs.agent.md")
        assert result == [".github/agents/PhaseDocs.agent.md"]

    def test_json_encoded_array(self) -> None:
        """Verify proper JSON array string is parsed via json.loads."""
        result = _parse_list_arg('["a.md", "b.md"]')
        assert result == ["a.md", "b.md"]

    def test_bracket_wrapped_single_path(self) -> None:
        """Verify shell-unquoted JSON array with single path is normalised.

        This is the exact failure mode from the production bug: the workflow
        writes AGENT_PATHS=json.dumps(["path"]) and the shell strips inner
        quotes but leaves the brackets.
        """
        result = _parse_list_arg("[.github/agents/PhaseDocs.agent.md]")
        assert result == [".github/agents/PhaseDocs.agent.md"]

    def test_bracket_wrapped_multiple_paths(self) -> None:
        """Verify shell-unquoted JSON array with multiple paths is normalised."""
        result = _parse_list_arg("[.github/agents/A.md, .github/agents/B.md]")
        assert result == [".github/agents/A.md", ".github/agents/B.md"]

    def test_whitespace_only_returns_none(self) -> None:
        """Verify whitespace-only input yields None."""
        assert _parse_list_arg("   ") is None

    def test_brackets_with_only_whitespace_returns_none(self) -> None:
        """Verify bracket-wrapped whitespace yields None."""
        assert _parse_list_arg("[  ]") is None

    def test_json_empty_array_returns_none(self) -> None:
        """Verify JSON empty array yields None."""
        assert _parse_list_arg("[]") is None

    def test_strips_surrounding_whitespace(self) -> None:
        """Verify leading/trailing whitespace is ignored."""
        result = _parse_list_arg("  a.md , b.md  ")
        assert result == ["a.md", "b.md"]


# ===========================================================================
# AgentRunner property tests
# ===========================================================================


class TestAgentRunnerProperties:
    """Tests for AgentRunner properties and accessors."""

    def test_session_log_property(self) -> None:
        """Verify session_log property returns the internal log."""
        runner = _make_runner()
        assert isinstance(runner.session_log, AgentSessionLog)
        assert runner.session_log is runner._session_log
