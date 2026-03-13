"""Agent runner core — Copilot SDK session lifecycle management.

Manages the complete Copilot SDK session lifecycle: client initialisation,
CLI server startup, session creation with configurable model and system
message, event-driven structured logging, instruction sending, agent loop
management, timeout enforcement, and graceful shutdown.

The :class:`AgentRunner` is intentionally role-agnostic. Role-specific logic
(branch creation, PR creation, review submission, merge execution) is handled
by the role modules (``app.src.agent.roles``) that wrap the runner.

Example::

    runner = AgentRunner(
        run_id="abc-123",
        model="claude-sonnet-4-20250514",
        system_message="You are an implement agent.",
        timeout_minutes=30,
    )
    session_log = asyncio.run(runner.run("Implement feature X..."))

Note:
    The Copilot SDK Python API (``github-copilot-sdk``) exposes native async
    methods. The runner awaits these directly to maintain an async interface
    compatible with the role modules and CLI entry point.
    The ``session.send_and_wait()`` method is used for blocking instruction
    execution with built-in timeout support.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from copilot import CopilotClient, PermissionHandler
from copilot.generated.session_events import SessionEvent, SessionEventType

from app.src.agent.prompts import PromptLoader

logger = logging.getLogger("dispatch.agent.runner")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AgentExecutionError(Exception):
    """Exception raised when the agent runner encounters a lifecycle error.

    This is an internal exception used within the agent runner and role
    modules. It is converted to a result error payload by the result compiler
    (Component 4.6).

    Attributes:
        error_code: Machine-readable error code identifying the failure class.
        error_message: Human-readable description of the error.
        error_details: Optional dictionary with additional diagnostic context.
    """

    def __init__(
        self,
        error_message: str,
        error_code: str = "AGENT_RUNTIME_ERROR",
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the AgentExecutionError.

        Args:
            error_message: A human-readable description of the error.
            error_code: Machine-readable code (e.g., ``SDK_INIT_ERROR``,
                ``SESSION_CREATE_ERROR``, ``AGENT_RUNTIME_ERROR``).
            error_details: Additional context such as exception type, stderr
                output, or related identifiers.
        """
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message
        self.error_details = error_details


# ---------------------------------------------------------------------------
# Session log dataclass
# ---------------------------------------------------------------------------


@dataclass
class AgentSessionLog:
    """Collects session events produced during an agent run.

    Acts as a structured record of everything that happened during the
    Copilot SDK session — assistant messages, tool calls and results,
    errors, and timing information. Used by the result compiler to construct
    the final result payload.

    Attributes:
        messages: List of assistant message dicts, each with ``content`` and
            ``timestamp`` keys.
        tool_calls: List of tool invocation dicts, each with ``name``,
            ``arguments``, ``result``, and ``timestamp`` keys.
        errors: List of error event dicts, each with ``message``,
            ``details``, and ``timestamp`` keys.
        usage: List of per-turn token usage dicts, each with
            ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
            ``cache_write_tokens``, ``cost``, ``model``, ``duration``, and
            ``timestamp`` keys.
        start_time: ISO 8601 timestamp when the session started.
        end_time: ISO 8601 timestamp when the session ended, or ``None`` if
            the session has not yet completed.
        final_message: The last assistant message content, serving as the
            session summary.
        timed_out: Whether the session was terminated due to timeout.
        turn_count: Number of assistant turns observed during the session.
        total_input_tokens: Cumulative input tokens across all turns.
        total_output_tokens: Cumulative output tokens across all turns.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    start_time: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    end_time: str | None = None
    final_message: str | None = None
    timed_out: bool = False
    turn_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------

# Maximum character length for tool argument / result summaries in logs.
_MAX_SUMMARY_LENGTH: int = 200


class AgentRunner:
    """Orchestrates the full Copilot SDK session lifecycle.

    Creates a :class:`CopilotClient`, starts the Copilot CLI in server mode,
    creates a session with the specified model and system message, subscribes
    to session events for structured logging, sends agent instructions, manages
    the agent loop, and handles timeouts and errors gracefully.

    The runner is designed to be executed both within a GitHub Actions workflow
    step and locally for development/testing. It exposes a clean interface that
    role-specific logic hooks into for pre- and post-processing.

    Note:
        The Copilot SDK exposes native async methods. This runner awaits
        them directly. The SDK's ``session.send_and_wait()`` is used for
        instruction execution, which blocks until the session reaches idle
        or the timeout expires.

    Args:
        run_id: Unique identifier for the agent run.
        model: LLM model identifier (e.g., ``"claude-sonnet-4-20250514"``).
        system_message: Fully-rendered system prompt for the agent session.
        timeout_minutes: Maximum session duration in minutes before forced
            termination. Defaults to ``30``.
        skill_directories: Optional list of absolute paths to Copilot SDK
            skill directories passed to ``create_session``.
        custom_agents: Optional list of SDK-compatible custom agent
            definitions passed to ``create_session``.
        cli_path: Optional path to the Copilot CLI binary. When ``None``,
            the SDK uses the default PATH lookup.
        repo_path: Optional absolute path to the target repository. When
            provided, the Copilot SDK CLI engine uses this as the working
            directory via ``CopilotClientOptions.cwd``.
        log_level: Python logging level string applied to this runner's logger.
            Defaults to ``"INFO"``.
    """

    def __init__(
        self,
        run_id: str,
        model: str,
        system_message: str,
        timeout_minutes: int = 30,
        skill_directories: list[str] | None = None,
        custom_agents: list[dict[str, object]] | None = None,
        cli_path: str | None = None,
        repo_path: Path | None = None,
        log_level: str = "INFO",
    ) -> None:
        """Initialise the AgentRunner with session configuration."""
        self._run_id = run_id
        self._model = model
        self._system_message = system_message
        self._timeout_minutes = timeout_minutes
        self._skill_directories = skill_directories
        self._custom_agents = custom_agents
        self._cli_path = cli_path
        self._repo_path = repo_path

        # Set logger level based on configuration.
        logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

        # Internal state — populated during the lifecycle.
        self._client: CopilotClient | None = None
        self._session: Any = None
        self._session_log = AgentSessionLog()
        self._unsubscribe: Any = None
        self._used_explicit_token = False
        self._disable_explicit_token = self._is_truthy_env(
            "DISPATCH_DISABLE_EXPLICIT_GITHUB_TOKEN"
        )

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Copilot CLI server process.

        Creates a :class:`CopilotClient` instance and calls ``start()`` to
        spawn the Copilot CLI in server mode. The ``CopilotClientOptions``
        TypedDict is used for configuration when a custom CLI path is
        specified.

        Raises:
            AgentExecutionError: If the CLI binary is not found, permission
                is denied, or the SDK raises an initialisation error. Uses
                ``error_code="SDK_INIT_ERROR"``.
        """
        try:
            options: dict[str, Any] = {}
            if self._cli_path:
                options["cli_path"] = self._cli_path
            if self._repo_path is not None:
                options["cwd"] = str(self._repo_path)

            if "cwd" in options:
                logger.info(
                    "Agent workspace CWD set to %s for run %s",
                    options["cwd"],
                    self._run_id,
                    extra={"run_id": self._run_id, "cwd": options["cwd"]},
                )

            self._used_explicit_token = False
            github_token = self._resolve_github_token()

            if github_token and not self._disable_explicit_token:
                options["github_token"] = github_token
                self._used_explicit_token = True
                logger.info(
                    "Using explicit GitHub token auth for run %s",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
            elif github_token and self._disable_explicit_token:
                options["use_logged_in_user"] = False
                logger.info(
                    "Explicit GitHub token auth disabled for run %s; skipping auth",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
            else:
                byok = bool(
                    os.environ.get("OPENAI_API_KEY")
                    or os.environ.get("ANTHROPIC_API_KEY")
                )
                if byok:
                    options["use_logged_in_user"] = False
                    logger.info(
                        "Falling back to BYOK auth for run %s since no GitHub token was provided",
                        self._run_id,
                        extra={"run_id": self._run_id},
                    )
                else:
                    options["use_logged_in_user"] = False
                    logger.warning(
                        "No explicit GitHub token found for run %s; ignoring auth (expecting BYOK or proxy)",
                        self._run_id,
                        extra={"run_id": self._run_id},
                    )

            if options:
                self._client = CopilotClient(options)
            else:
                self._client = CopilotClient()

            await self._client.start()
            logger.info(
                "Copilot CLI server started for run %s",
                self._run_id,
                extra={"run_id": self._run_id},
            )
        except FileNotFoundError as exc:
            raise AgentExecutionError(
                error_message=(
                    f"Copilot CLI binary not found: {exc}. "
                    "Ensure the Copilot CLI is installed and available in PATH."
                ),
                error_code="SDK_INIT_ERROR",
                error_details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc
        except PermissionError as exc:
            raise AgentExecutionError(
                error_message=(
                    f"Permission denied when starting Copilot CLI: {exc}. "
                    "Ensure the CLI binary has execute permissions."
                ),
                error_code="SDK_INIT_ERROR",
                error_details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc
        except Exception as exc:
            raise AgentExecutionError(
                error_message=f"Failed to start Copilot CLI server: {exc}",
                error_code="SDK_INIT_ERROR",
                error_details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc

    async def create_session(self) -> None:
        """Create an agent session with the configured model and system message.

        Calls ``create_session`` on the Copilot client with a
        ``SessionConfig`` TypedDict containing the model, system message
        (in ``replace`` mode), streaming flag, permission handler, and
        optional skill directories.
        Subscribes to session events via a single callback for structured
        logging.

        Raises:
            AgentExecutionError: If session creation fails (invalid model,
                authentication failure, SDK error). Uses
                ``error_code="SESSION_CREATE_ERROR"``.
        """
        if self._client is None:
            raise AgentExecutionError(
                error_message="Cannot create session: Copilot client not started.",
                error_code="SDK_INIT_ERROR",
                error_details={"hint": "Call start() before create_session()."},
            )

        try:
            session_config: dict[str, Any] = {
                "model": self._model,
                "system_message": {
                    "mode": "replace",
                    "content": self._system_message,
                },
                "streaming": True,
                "on_permission_request": PermissionHandler.approve_all,
            }
            if self._skill_directories:
                session_config["skill_directories"] = self._skill_directories
            if self._custom_agents:
                session_config["custom_agents"] = self._custom_agents

            byok_enabled = not self._used_explicit_token
            if byok_enabled and os.environ.get("OPENAI_API_KEY"):
                session_config["provider"] = {
                    "type": "openai",
                    "api_key": os.environ["OPENAI_API_KEY"],
                    "base_url": "https://api.openai.com/v1",
                }
            elif byok_enabled and os.environ.get("ANTHROPIC_API_KEY"):
                session_config["provider"] = {
                    "type": "anthropic",
                    "api_key": os.environ["ANTHROPIC_API_KEY"],
                }

            self._session = await self._client.create_session(session_config)
            self._subscribe_to_events()

            logger.info(
                "Session created with model %s for run %s",
                self._model,
                self._run_id,
                extra={"run_id": self._run_id, "model": self._model},
            )
        except Exception as exc:
            raise AgentExecutionError(
                error_message=f"Failed to create agent session: {exc}",
                error_code="SESSION_CREATE_ERROR",
                error_details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "model": self._model,
                },
            ) from exc

    async def send_instructions(self, instructions: str) -> AgentSessionLog:
        """Send instructions to the agent and wait for completion or timeout.

        Uses the SDK's ``session.send_and_wait()`` method which blocks until
        the session reaches idle state or the timeout expires.

        A ``None`` return from ``send_and_wait`` indicates the session timed
        out before completing.

        Args:
            instructions: Natural-language instructions to send to the agent.

        Returns:
            The :class:`AgentSessionLog` containing all session events,
            messages, tool calls, and error information.

        Raises:
            AgentExecutionError: If the session is not active or a fatal
                runtime error occurs. Uses
                ``error_code="AGENT_RUNTIME_ERROR"``.
        """
        if self._session is None:
            raise AgentExecutionError(
                error_message=(
                    "Cannot send instructions: no active session. "
                    "Call create_session() first."
                ),
                error_code="AGENT_RUNTIME_ERROR",
                error_details={
                    "hint": "Call create_session() before send_instructions()."
                },
            )

        try:
            timeout_seconds = float(self._timeout_minutes * 60)
            logger.info(
                "Sending instructions to agent for run %s (%d chars, timeout=%dm)",
                self._run_id,
                len(instructions),
                self._timeout_minutes,
                extra={
                    "run_id": self._run_id,
                    "instructions_length": len(instructions),
                    "timeout_minutes": self._timeout_minutes,
                },
            )

            # send_and_wait blocks until idle or timeout in the SDK.
            result_event = await self._session.send_and_wait(
                {"prompt": instructions},
                timeout_seconds,
            )

            self._session_log.end_time = datetime.now(timezone.utc).isoformat()

            # A None result from send_and_wait indicates timeout.
            if result_event is None:
                self._session_log.timed_out = True
                logger.warning(
                    "Agent session timed out after %d minutes for run %s",
                    self._timeout_minutes,
                    self._run_id,
                    extra={
                        "run_id": self._run_id,
                        "timeout_minutes": self._timeout_minutes,
                    },
                )
                # Attempt graceful session abort.
                await self._abort_session()
            else:
                logger.info(
                    "Agent session completed for run %s",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )

        except TimeoutError as exc:
            # The SDK raises TimeoutError when send_and_wait exceeds the
            # timeout. Treat this identically to the None-return path.
            self._session_log.timed_out = True
            self._session_log.end_time = datetime.now(timezone.utc).isoformat()
            self._session_log.errors.append(
                {
                    "message": f"Agent timed out: {exc}",
                    "details": {
                        "exception_type": "TimeoutError",
                        "exception_message": str(exc),
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            logger.warning(
                "Agent session timed out (TimeoutError) after %d minutes "
                "for run %s: %s",
                self._timeout_minutes,
                self._run_id,
                exc,
                extra={
                    "run_id": self._run_id,
                    "timeout_minutes": self._timeout_minutes,
                },
            )
            await self._abort_session()

        except Exception as exc:
            self._session_log.end_time = datetime.now(timezone.utc).isoformat()
            self._session_log.errors.append(
                {
                    "message": f"Runtime error during agent execution: {exc}",
                    "details": {
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            logger.error(
                "Runtime error during agent execution for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
                exc_info=True,
            )
            raise AgentExecutionError(
                error_message=f"Runtime error during agent execution: {exc}",
                error_code="AGENT_RUNTIME_ERROR",
                error_details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc

        return self._session_log

    async def shutdown(self) -> None:
        """Gracefully stop the Copilot CLI server.

        Unsubscribes from session events, destroys the active session (if
        any), and stops the client. Errors during shutdown are logged but
        never raised — shutdown must be idempotent and safe to call from
        ``finally`` blocks.
        """
        # Unsubscribe from events first.
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception as exc:
                logger.warning(
                    "Error unsubscribing from events for run %s: %s",
                    self._run_id,
                    exc,
                    extra={"run_id": self._run_id},
                )
            self._unsubscribe = None

        # Destroy the session.
        try:
            if self._session is not None:
                await self._session.destroy()
                logger.info(
                    "Session destroyed for run %s",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
        except Exception as exc:
            logger.warning(
                "Error destroying session for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )

        # Stop the client.
        try:
            if self._client is not None:
                await self._client.stop()
                logger.info(
                    "Copilot CLI server stopped for run %s",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
        except Exception as exc:
            logger.warning(
                "Error stopping Copilot CLI server for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )

    async def run(self, instructions: str) -> AgentSessionLog:
        """Execute the full agent run lifecycle.

        High-level orchestration method that calls :meth:`start`,
        :meth:`create_session`, :meth:`send_instructions`, and
        :meth:`shutdown` in sequence. Wraps the entire flow in
        ``try``/``except``/``finally`` to ensure :meth:`shutdown` is always
        called.

        Args:
            instructions: Natural-language instructions to send to the agent.

        Returns:
            The :class:`AgentSessionLog` containing all session events.

        Raises:
            AgentExecutionError: Propagated from :meth:`start` or
                :meth:`create_session` for lifecycle failures. Errors during
                :meth:`send_instructions` are captured in the session log
                rather than raised.
        """
        try:
            await self.start()
            await self.create_session()
            return await self.send_instructions(instructions)
        except AgentExecutionError as exc:
            if self._should_retry_without_explicit_token(exc):
                logger.warning(
                    "Retrying run %s without explicit GitHub token after model-list error",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
                self._disable_explicit_token = True
                await self.shutdown()
                self._client = None
                self._session = None
                self._unsubscribe = None
                await self.start()
                await self.create_session()
                return await self.send_instructions(instructions)
            raise
        finally:
            await self.shutdown()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _subscribe_to_events(self) -> None:
        """Subscribe to all session events via a single callback.

        The Copilot SDK's ``session.on()`` accepts a single callback that
        receives all :class:`SessionEvent` instances. The callback dispatches
        to type-specific handlers based on ``event.type``.
        """
        if self._session is None:
            return

        self._unsubscribe = self._session.on(self._handle_session_event)

    def _handle_session_event(self, event: SessionEvent) -> None:
        """Dispatch a session event to the appropriate handler.

        Routes events by their :attr:`SessionEventType` to specialised
        handler methods for structured logging and session log population.

        Args:
            event: The SDK session event to handle.
        """
        event_type = event.type

        logger.debug(
            "Session event for run %s: %s",
            self._run_id,
            getattr(event_type, "value", str(event_type)),
            extra={"run_id": self._run_id},
        )

        if event_type == SessionEventType.ASSISTANT_MESSAGE:
            self._on_assistant_message(event)
        elif event_type == SessionEventType.TOOL_EXECUTION_START:
            self._on_tool_call(event)
        elif event_type == SessionEventType.TOOL_EXECUTION_COMPLETE:
            self._on_tool_result(event)
        elif event_type == SessionEventType.SESSION_IDLE:
            self._on_session_idle(event)
        elif event_type == SessionEventType.SESSION_ERROR:
            self._on_session_error(event)
        elif event_type == SessionEventType.SESSION_WARNING:
            self._on_session_warning(event)
        elif event_type == SessionEventType.SESSION_INFO:
            self._on_session_info(event)
        elif event_type == SessionEventType.SESSION_TASK_COMPLETE:
            self._on_session_task_complete(event)
        elif event_type == SessionEventType.ASSISTANT_STREAMING_DELTA:
            self._on_streaming_delta(event)
        elif event_type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            self._on_message_delta(event)
        elif event_type == SessionEventType.ASSISTANT_USAGE:
            self._on_assistant_usage(event)
        elif event_type == SessionEventType.ASSISTANT_TURN_START:
            self._on_turn_start(event)
        elif event_type == SessionEventType.ASSISTANT_TURN_END:
            self._on_turn_end(event)
        elif event_type == SessionEventType.SESSION_USAGE_INFO:
            self._on_session_usage_info(event)
        elif event_type == SessionEventType.TOOL_EXECUTION_PARTIAL_RESULT:
            self._on_tool_partial_result(event)
        else:
            logger.debug(
                "Unhandled session event for run %s: %s",
                self._run_id,
                getattr(event_type, "value", str(event_type)),
                extra={"run_id": self._run_id},
            )

    def _on_assistant_message(self, event: SessionEvent) -> None:
        """Handle an assistant message event.

        Logs the message at INFO level and appends it to the session log.
        Updates ``final_message`` with the latest message content.

        Args:
            event: The SDK session event containing the assistant message.
        """
        content = getattr(event.data, "content", None) or str(event.data)
        timestamp = datetime.now(timezone.utc).isoformat()

        self._session_log.messages.append({"content": content, "timestamp": timestamp})
        self._session_log.final_message = content

        display = (
            content[:_MAX_SUMMARY_LENGTH] + "..."
            if len(content) > _MAX_SUMMARY_LENGTH
            else content
        )
        logger.info(
            "Assistant message for run %s: %s",
            self._run_id,
            display,
            extra={
                "run_id": self._run_id,
                "event_type": "assistant.message",
                "content_length": len(content),
            },
        )

    def _on_tool_call(self, event: SessionEvent) -> None:
        """Handle a tool execution start event.

        Logs the tool invocation at DEBUG level and appends it to the session
        log with the tool name, full arguments, and a timestamp.  Arguments
        are only truncated for the log line — the full text is stored in the
        session log so that downstream consumers (e.g. test-result parsers)
        can inspect the complete content.

        Args:
            event: The SDK session event containing tool call details.
        """
        data = event.data
        name = getattr(data, "tool_name", None) or getattr(data, "name", "unknown")
        arguments = str(getattr(data, "arguments", ""))
        display_args = (
            arguments[:_MAX_SUMMARY_LENGTH] + "..."
            if len(arguments) > _MAX_SUMMARY_LENGTH
            else arguments
        )
        timestamp = datetime.now(timezone.utc).isoformat()

        self._session_log.tool_calls.append(
            {
                "name": name,
                "arguments": arguments,
                "result": None,
                "timestamp": timestamp,
            }
        )

        logger.debug(
            "Tool call for run %s: %s(%s)",
            self._run_id,
            name,
            display_args,
            extra={
                "run_id": self._run_id,
                "event_type": "tool.execution_start",
                "tool_name": name,
            },
        )

    def _on_tool_result(self, event: SessionEvent) -> None:
        """Handle a tool execution complete event.

        Logs the result at DEBUG level and updates the most recent tool call
        entry with the full result text.  Only the log line is truncated —
        the complete result is stored in the session log so that downstream
        consumers (e.g. test-result parsers) can inspect the full output.

        Args:
            event: The SDK session event containing the tool result.
        """
        data = event.data
        name = getattr(data, "tool_name", None) or getattr(data, "name", "unknown")
        result_obj = getattr(data, "result", None)
        result = str(result_obj) if result_obj is not None else ""
        display_result = (
            result[:_MAX_SUMMARY_LENGTH] + "..."
            if len(result) > _MAX_SUMMARY_LENGTH
            else result
        )

        # Update the most recent matching tool call entry with the full result.
        for tool_call in reversed(self._session_log.tool_calls):
            if tool_call["name"] == name and tool_call["result"] is None:
                tool_call["result"] = result
                break

        logger.debug(
            "Tool result for run %s: %s -> %s",
            self._run_id,
            name,
            display_result,
            extra={
                "run_id": self._run_id,
                "event_type": "tool.execution_complete",
                "tool_name": name,
            },
        )

    def _on_session_idle(self, event: SessionEvent) -> None:
        """Handle a session idle event.

        Indicates that the agent has completed its current task.

        Args:
            event: The SDK session event for the idle signal.
        """
        logger.info(
            "Agent session idle — task may be complete for run %s",
            self._run_id,
            extra={"run_id": self._run_id, "event_type": "session.idle"},
        )

    def _on_session_task_complete(self, event: SessionEvent) -> None:
        """Handle a session task complete event.

        Logged at INFO level to mark the agent's task as done.

        Args:
            event: The SDK session event for task completion.
        """
        logger.info(
            "Agent task complete for run %s",
            self._run_id,
            extra={"run_id": self._run_id, "event_type": "session.task_complete"},
        )

    def _on_session_error(self, event: SessionEvent) -> None:
        """Handle a session error event.

        Logs the error at ERROR level and appends it to the session log.

        Args:
            event: The SDK session event containing error details.
        """
        data = event.data
        error_message = (
            getattr(data, "message", None) or getattr(data, "error", None) or str(data)
        )
        error_details: dict[str, Any] | None = None
        error_type = getattr(data, "error_type", None)
        if error_type:
            error_details = {"error_type": str(error_type)}

        timestamp = datetime.now(timezone.utc).isoformat()

        self._session_log.errors.append(
            {
                "message": str(error_message),
                "details": error_details,
                "timestamp": timestamp,
            }
        )

        logger.error(
            "Session error for run %s: %s",
            self._run_id,
            error_message,
            extra={
                "run_id": self._run_id,
                "event_type": "session.error",
                "error_type": str(error_type) if error_type else None,
            },
        )

    def _on_session_warning(self, event: SessionEvent) -> None:
        """Handle a session warning event.

        Args:
            event: The SDK session event containing warning details.
        """
        warning_message = getattr(event.data, "message", None) or str(event.data)
        logger.warning(
            "Session warning for run %s: %s",
            self._run_id,
            warning_message,
            extra={"run_id": self._run_id, "event_type": "session.warning"},
        )

    def _on_session_info(self, event: SessionEvent) -> None:
        """Handle a session info event.

        Args:
            event: The SDK session event containing informational details.
        """
        info_message = getattr(event.data, "message", None) or str(event.data)
        logger.info(
            "Session info for run %s: %s",
            self._run_id,
            info_message,
            extra={"run_id": self._run_id, "event_type": "session.info"},
        )

    # ------------------------------------------------------------------
    # Streaming and usage event handlers
    # ------------------------------------------------------------------

    def _on_streaming_delta(self, event: SessionEvent) -> None:
        """Handle a raw streaming delta (token-level chunk).

        Logged at DEBUG only — these fire at very high frequency during
        streaming sessions. The complete content is delivered via the
        final ``assistant.message`` event.

        Args:
            event: The SDK session event containing the streaming chunk.
        """
        logger.debug(
            "Streaming delta for run %s",
            self._run_id,
            extra={"run_id": self._run_id, "event_type": "assistant.streaming_delta"},
        )

    def _on_message_delta(self, event: SessionEvent) -> None:
        """Handle an incremental message delta.

        Contains ``delta_content`` — a text chunk of the assistant
        response being built incrementally. Logged at DEBUG. The final
        ``assistant.message`` event delivers the complete content.

        Args:
            event: The SDK session event containing the message delta.
        """
        delta = getattr(event.data, "delta_content", None) or ""
        logger.debug(
            "Message delta for run %s (%d chars)",
            self._run_id,
            len(delta),
            extra={
                "run_id": self._run_id,
                "event_type": "assistant.message_delta",
                "delta_length": len(delta),
            },
        )

    def _on_assistant_usage(self, event: SessionEvent) -> None:
        """Handle per-turn token usage from the assistant.

        Captures input/output token counts, cache token metrics, cost,
        model identifier, and duration for observability and cost
        tracking. Appended to :attr:`AgentSessionLog.usage`.

        Args:
            event: The SDK session event containing usage metrics.
        """
        data = event.data
        input_tokens = int(getattr(data, "input_tokens", 0) or 0)
        output_tokens = int(getattr(data, "output_tokens", 0) or 0)
        cost = getattr(data, "cost", None)
        model = getattr(data, "model", None)
        duration = getattr(data, "duration", None)
        cache_read = int(getattr(data, "cache_read_tokens", 0) or 0)
        cache_write = int(getattr(data, "cache_write_tokens", 0) or 0)

        usage_entry: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cost": cost,
            "model": model,
            "duration": duration,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._session_log.usage.append(usage_entry)
        self._session_log.total_input_tokens += input_tokens
        self._session_log.total_output_tokens += output_tokens

        logger.debug(
            "Usage for run %s: in=%d out=%d cost=%s model=%s",
            self._run_id,
            input_tokens,
            output_tokens,
            cost,
            model,
            extra={
                "run_id": self._run_id,
                "event_type": "assistant.usage",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

    def _on_turn_start(self, event: SessionEvent) -> None:
        """Handle an assistant turn start event.

        Increments the session turn counter and logs the turn boundary.

        Args:
            event: The SDK session event for the turn start signal.
        """
        self._session_log.turn_count += 1
        turn_id = getattr(event.data, "turn_id", None)
        logger.debug(
            "Turn %d started for run %s (turn_id=%s)",
            self._session_log.turn_count,
            self._run_id,
            turn_id,
            extra={
                "run_id": self._run_id,
                "event_type": "assistant.turn_start",
                "turn_count": self._session_log.turn_count,
            },
        )

    def _on_turn_end(self, event: SessionEvent) -> None:
        """Handle an assistant turn end event.

        Logs the turn boundary at DEBUG level.

        Args:
            event: The SDK session event for the turn end signal.
        """
        turn_id = getattr(event.data, "turn_id", None)
        logger.debug(
            "Turn ended for run %s (turn_id=%s)",
            self._run_id,
            turn_id,
            extra={
                "run_id": self._run_id,
                "event_type": "assistant.turn_end",
            },
        )

    def _on_session_usage_info(self, event: SessionEvent) -> None:
        """Handle aggregate session usage information.

        Contains model-level metrics and total API duration. Logged at
        INFO since it fires infrequently (once per turn) and carries
        high-value observability data.

        Args:
            event: The SDK session event containing session-level usage.
        """
        data = event.data
        total_duration = getattr(data, "total_api_duration_ms", None)
        premium_requests = getattr(data, "total_premium_requests", None)
        logger.info(
            "Session usage for run %s: api_duration=%sms premium_requests=%s",
            self._run_id,
            total_duration,
            premium_requests,
            extra={
                "run_id": self._run_id,
                "event_type": "session.usage_info",
                "total_api_duration_ms": total_duration,
                "total_premium_requests": premium_requests,
            },
        )

    def _on_tool_partial_result(self, event: SessionEvent) -> None:
        """Handle a partial result from a running tool execution.

        Contains intermediate output from long-running tools (e.g.
        terminal commands that stream output). Logged at DEBUG.

        Args:
            event: The SDK session event containing the partial output.
        """
        data = event.data
        name = getattr(data, "tool_name", None) or getattr(data, "name", "unknown")
        partial = getattr(data, "partial_output", None) or ""
        logger.debug(
            "Tool partial result for run %s: %s (%d chars)",
            self._run_id,
            name,
            len(partial),
            extra={
                "run_id": self._run_id,
                "event_type": "tool.execution_partial_result",
                "tool_name": name,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _abort_session(self) -> None:
        """Attempt to gracefully abort the active session.

        Called when the session times out. Uses the SDK's ``session.abort()``
        method. Errors are logged but not raised.
        """
        try:
            if self._session is not None:
                await self._session.abort()
                logger.info(
                    "Session aborted after timeout for run %s",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
        except Exception as exc:
            logger.warning(
                "Error aborting session for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )

    def _resolve_github_token(self) -> str | None:
        """Resolve a GitHub auth token for SDK CLI startup.

        Returns:
            The first non-empty token from supported environment variables,
            otherwise ``None``.
        """
        token = (
            os.environ.get("GITHUB_PAT")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
        )
        if token and token.strip():
            return token.strip()
        return None

    def _should_retry_without_explicit_token(self, error: AgentExecutionError) -> bool:
        """Determine whether explicit-token auth should be disabled and retried.

        Args:
            error: The structured agent execution error.

        Returns:
            ``True`` when the run used explicit token auth and failed with the
            model listing 400 runtime error.
        """
        return False

    @staticmethod
    def _is_truthy_env(name: str) -> bool:
        """Parse an environment variable as a truthy boolean.

        Args:
            name: Environment variable name.

        Returns:
            ``True`` when set to a truthy value (1, true, yes, on).
        """
        raw = os.environ.get(name, "")
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @property
    def session_log(self) -> AgentSessionLog:
        """Return the current session log.

        Returns:
            The :class:`AgentSessionLog` instance collecting session events.
        """
        return self._session_log


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the agent runner.

    Returns:
        A configured :class:`argparse.ArgumentParser` instance.
    """
    parser = argparse.ArgumentParser(
        description="Copilot Dispatch Agent Runner — execute a Copilot SDK agent session.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Unique identifier for this agent run.",
    )
    parser.add_argument(
        "--role",
        required=True,
        choices=["implement", "review", "merge"],
        help="Agent role determining the system prompt and pre/post processing.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help='LLM model identifier (e.g., "claude-sonnet-4-20250514").',
    )
    parser.add_argument(
        "--instructions",
        required=True,
        help=(
            "Agent instructions. Prefix with '@' to read from a file "
            "(e.g., @instructions.txt)."
        ),
    )
    parser.add_argument(
        "--system-instructions",
        default=None,
        help=(
            "Additional system-level instructions to merge into the prompt. "
            "Prefix with '@' to read from a file (e.g., @system.txt)."
        ),
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Path to the checked-out target repository.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Maximum session duration in minutes (default: 30).",
    )
    parser.add_argument(
        "--skill-paths",
        default=None,
        help="Comma-separated list of skill file paths relative to the repo.",
    )
    parser.add_argument(
        "--agent-paths",
        default=None,
        help="Comma-separated list of Markdown agent definition file paths relative to the repo.",
    )
    parser.add_argument(
        "--api-result-url",
        default=None,
        help="URL to POST the agent result payload to.",
    )
    parser.add_argument(
        "--webhook-secret",
        default=None,
        help="HMAC secret for signing result POST requests.",
    )
    parser.add_argument(
        "--pr-number",
        default=None,
        help="Pull request number (required for review and merge roles).",
    )
    parser.add_argument(
        "--repository",
        default=None,
        help="Target repository in owner/repo format (required for review and merge roles).",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help='Base branch name (default: "main").',
    )
    return parser


def _parse_list_arg(value: str | None) -> list[str] | None:
    """Parse a CLI list argument that may be JSON-encoded or comma-separated.

    The GitHub Actions workflow encodes list arguments with ``json.dumps``,
    producing JSON arrays like ``["a.md", "b.md"]``.  When this value is
    interpolated into a shell command via ``format('--flag "{0}"', ...)``,
    the shell consumes the inner double-quotes but leaves the ``[`` ``]``
    brackets intact.  This function handles:

    - Proper JSON arrays (direct invocation with correct quoting).
    - Bracket-wrapped comma-separated values (shell-unquoted JSON arrays).
    - Plain comma-separated values (backward-compatible).

    Args:
        value: Raw string from argparse, or ``None``.

    Returns:
        A list of trimmed, non-empty path strings, or ``None`` when the
        input is ``None`` or empty.
    """
    if not value:
        return None

    raw = value.strip()

    # Attempt proper JSON parsing first (works when outer quoting is preserved).
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            items = [str(s).strip() for s in parsed if str(s).strip()]
            return items or None
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip residual brackets left by shell-unquoted JSON arrays.
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]

    items = [s.strip() for s in raw.split(",") if s.strip()]
    return items or None


def _resolve_instructions(raw_value: str) -> str:
    """Resolve the instructions argument value.

    If the value starts with ``@``, reads the rest as a filepath and returns
    the file contents. Otherwise returns the value directly.

    Args:
        raw_value: The raw ``--instructions`` CLI argument value.

    Returns:
        The resolved instruction text.

    Raises:
        SystemExit: If the ``@filepath`` does not exist or cannot be read.
    """
    if raw_value.startswith("@"):
        filepath = Path(raw_value[1:])
        if not filepath.is_file():
            logger.error("Instructions file not found: %s", filepath)
            sys.exit(1)
        return filepath.read_text(encoding="utf-8")
    return raw_value


def main() -> None:
    """CLI entry point for the agent runner.

    Parses command-line arguments, instantiates the appropriate role module,
    executes the full role lifecycle (pre-process → execute → post-process),
    compiles the result via :class:`ResultCompiler`, and POSTs it to the API
    via :class:`ApiPoster`.

    If ``--api-result-url`` and ``--webhook-secret`` are provided, the result
    is always posted — even on failure or timeout. If they are not provided,
    the runner executes locally and logs the summary without posting.
    """
    parser = _build_argument_parser()
    args = parser.parse_args()

    # Configure logging.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    asyncio.run(_run_with_role(args))


async def _run_with_role(args: argparse.Namespace) -> None:
    """Orchestrate the full role lifecycle with result compilation and posting.

    Creates the role module, runs the lifecycle (pre-process → execute →
    post-process), compiles the result, and posts it to the API.

    Args:
        args: Parsed CLI arguments from :func:`_build_argument_parser`.
    """
    # Local imports to avoid circular dependencies — result.py and role
    # modules import AgentRunner, AgentSessionLog, and AgentExecutionError
    # from this module.
    from app.src.agent.result import ApiPoster, ResultCompiler
    from app.src.agent.roles.implement import ImplementRole
    from app.src.agent.roles.merge import MergeRole
    from app.src.agent.roles.review import ReviewRole

    # Resolve instructions (supports @filepath).
    instructions = _resolve_instructions(args.instructions)

    # Resolve system instructions (supports @filepath).
    if args.system_instructions is not None:
        args.system_instructions = _resolve_instructions(args.system_instructions)

    # Resolve skill paths.
    repo_path = Path(args.repo_path)
    skill_paths_raw = _parse_list_arg(args.skill_paths)
    agent_paths_raw = _parse_list_arg(args.agent_paths)

    # Build system message.
    prompt_loader = PromptLoader()
    skill_directories = PromptLoader.resolve_skill_paths(skill_paths_raw, repo_path)
    custom_agents = PromptLoader.resolve_agent_paths(agent_paths_raw, repo_path)

    # Build role-specific template context for placeholder substitution.
    feature_branch = f"feature/{args.run_id}"
    prompt_context: dict[str, str] = {
        "feature_branch": feature_branch,
        "run_id": args.run_id,
    }

    system_message = prompt_loader.build_system_message(
        role=args.role,
        system_instructions=args.system_instructions,
        repo_path=repo_path,
        context=prompt_context,
    )

    # Create the AgentRunner.
    runner = AgentRunner(
        run_id=args.run_id,
        model=args.model,
        system_message=system_message,
        timeout_minutes=args.timeout,
        skill_directories=skill_directories or None,
        custom_agents=custom_agents or None,
        repo_path=repo_path,
    )

    # Create the role instance.
    role: ImplementRole | ReviewRole | MergeRole
    if args.role == "implement":
        role = ImplementRole(
            run_id=args.run_id,
            repo_path=repo_path,
            branch=args.branch,
            agent_instructions=instructions,
            model=args.model,
            system_instructions=args.system_instructions,
            skill_paths=skill_paths_raw,
            agent_paths=agent_paths_raw,
            timeout_minutes=args.timeout,
        )
    elif args.role == "review":
        pr_number = int(args.pr_number) if args.pr_number else 0
        role = ReviewRole(
            run_id=args.run_id,
            repo_path=repo_path,
            branch=args.branch,
            pr_number=pr_number,
            agent_instructions=instructions,
            model=args.model,
            system_instructions=args.system_instructions,
            skill_paths=skill_paths_raw,
            agent_paths=agent_paths_raw,
            timeout_minutes=args.timeout,
            repository=args.repository or "",
        )
    else:
        pr_number = int(args.pr_number) if args.pr_number else 0
        role = MergeRole(
            run_id=args.run_id,
            repo_path=repo_path,
            branch=args.branch,
            pr_number=pr_number,
            agent_instructions=instructions,
            model=args.model,
            system_instructions=args.system_instructions,
            skill_paths=skill_paths_raw,
            agent_paths=agent_paths_raw,
            timeout_minutes=args.timeout,
            repository=args.repository or "",
        )

    # Create result compiler.
    compiler = ResultCompiler(run_id=args.run_id, role=args.role, model=args.model)

    # Create API poster (only when credentials are provided).
    poster: ApiPoster | None = None
    if args.api_result_url and args.webhook_secret:
        poster = ApiPoster(args.api_result_url, args.webhook_secret)

    session_log: AgentSessionLog | None = None
    payload: dict[str, Any] = {}

    try:
        # --- Pre-process ---
        logger.info(
            "Running pre-process for %s role, run %s",
            args.role,
            args.run_id,
            extra={"run_id": args.run_id},
        )
        await role.pre_process()

        # --- Execute ---
        logger.info(
            "Running agent execution for %s role, run %s",
            args.role,
            args.run_id,
            extra={"run_id": args.run_id},
        )
        session_log = await role.execute(runner)

        # --- Compile result ---
        if session_log.timed_out:
            logger.warning(
                "Agent timed out for run %s",
                args.run_id,
                extra={"run_id": args.run_id},
            )
            payload = compiler.compile_timeout(session_log)
        else:
            # --- Post-process ---
            logger.info(
                "Running post-process for %s role, run %s",
                args.role,
                args.run_id,
                extra={"run_id": args.run_id},
            )
            role_result = await role.post_process(session_log)
            payload = compiler.compile_success(role_result, session_log)

    except AgentExecutionError as exc:
        if session_log is None:
            session_log = runner.session_log
        logger.error(
            "Agent execution error for run %s: [%s] %s",
            args.run_id,
            exc.error_code,
            exc.error_message,
            extra={"run_id": args.run_id},
        )
        payload = compiler.compile_error(exc, session_log)

    except Exception as exc:
        if session_log is None:
            session_log = runner.session_log
        logger.error(
            "Unexpected error for run %s: %s",
            args.run_id,
            exc,
            exc_info=True,
            extra={"run_id": args.run_id},
        )
        payload = compiler.compile_error(exc, session_log)

    # --- Post result to API ---
    if poster:
        try:
            success = await poster.post_result(payload)
            if success:
                logger.info(
                    "Result posted successfully for run %s",
                    args.run_id,
                    extra={"run_id": args.run_id},
                )
            else:
                logger.error(
                    "Failed to post result for run %s after all retries",
                    args.run_id,
                    extra={"run_id": args.run_id},
                )
        finally:
            await poster.close()
    else:
        logger.info(
            "No API result URL configured — skipping result posting for run %s",
            args.run_id,
            extra={"run_id": args.run_id},
        )

    # Output summary for workflow consumption.
    summary = {
        "run_id": args.run_id,
        "role": args.role,
        "status": payload.get("status", "unknown"),
        "timed_out": session_log.timed_out if session_log else False,
        "messages_count": len(session_log.messages) if session_log else 0,
        "tool_calls_count": len(session_log.tool_calls) if session_log else 0,
        "errors_count": len(session_log.errors) if session_log else 0,
        "final_message": session_log.final_message if session_log else None,
        "start_time": session_log.start_time if session_log else None,
        "end_time": session_log.end_time if session_log else None,
    }
    logger.info(
        "Agent run completed: %s",
        json.dumps(summary, indent=2),
        extra={"run_id": args.run_id},
    )


if __name__ == "__main__":
    main()
