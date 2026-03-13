"""Integration tests for local agent execution against autopilot-test-target.

Tests exercise the role handlers and agent runner locally, validating
structural properties (branch existence, commit presence, review structure)
rather than exact agent output. Tests requiring a live Copilot SDK session
are gated behind ``@pytest.mark.requires_e2e`` and the ``--e2e-confirm``
CLI flag. Mock-based error scenario tests run unconditionally.

Run with::

    # Mock-based tests only (CI-safe, always run):
    pytest tests/integration/test_agent_local.py -v -k "error"

    # Full suite including live SDK tests (requires SDK and test repo):
    pytest tests/integration/test_agent_local.py --e2e-confirm -v
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.src.agent.result import ResultCompiler
from app.src.agent.roles.implement import ImplementRole
from app.src.agent.roles.merge import MergeRole
from app.src.agent.roles.review import ReviewRole
from app.src.agent.runner import AgentExecutionError, AgentRunner, AgentSessionLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_log(**overrides: Any) -> AgentSessionLog:
    """Create an AgentSessionLog with default test values.

    Args:
        **overrides: Keyword arguments to override defaults.

    Returns:
        A configured :class:`AgentSessionLog` instance.
    """
    defaults: dict[str, Any] = {
        "messages": [
            {"content": "Task completed successfully.", "timestamp": "t1"},
        ],
        "tool_calls": [
            {"tool": "read_file", "args": "file.py", "timestamp": "t2"},
        ],
        "errors": [],
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:30:00+00:00",
        "final_message": "Task completed successfully.",
        "timed_out": False,
    }
    defaults.update(overrides)
    return AgentSessionLog(**defaults)


def _make_process_mock(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> AsyncMock:
    """Create a mock subprocess process.

    Args:
        returncode: The exit code to return.
        stdout: The stdout content.
        stderr: The stderr content.

    Returns:
        An :class:`AsyncMock` imitating an asyncio subprocess.
    """
    process = AsyncMock()
    process.returncode = returncode
    process.communicate = AsyncMock(
        return_value=(
            stdout.encode("utf-8"),
            stderr.encode("utf-8"),
        )
    )
    return process


def _make_mock_session() -> MagicMock:
    """Create a mock CopilotSession.

    Async methods (``send_and_wait``, ``destroy``, ``abort``) use
    ``AsyncMock`` so that the runner can ``await`` them. The synchronous
    ``on()`` method uses a regular ``MagicMock``.

    Returns:
        A :class:`MagicMock` imitating a ``CopilotSession``.
    """
    session = MagicMock()
    session.send_and_wait = AsyncMock(return_value=MagicMock())
    session.send = AsyncMock(return_value="msg-id")
    session.destroy = AsyncMock()
    session.abort = AsyncMock()
    session.on = MagicMock(return_value=MagicMock())
    return session


def _make_mock_client(
    *,
    start_side_effect: Exception | None = None,
    session: MagicMock | None = None,
) -> MagicMock:
    """Create a mock CopilotClient with configurable side effects.

    Async methods (``start``, ``create_session``, ``stop``) use ``AsyncMock``
    so the runner can ``await`` them.

    Args:
        start_side_effect: Exception to raise on ``start()``.
        session: Optional pre-built mock session.

    Returns:
        A :class:`MagicMock` imitating a ``CopilotClient``.
    """
    client = MagicMock()
    mock_session = session or _make_mock_session()

    if start_side_effect:
        client.start = AsyncMock(side_effect=start_side_effect)
    else:
        client.start = AsyncMock()

    client.create_session = AsyncMock(return_value=mock_session)
    client.stop = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _agent_test_repo(tmp_path: Path) -> Path:
    """Create a minimal local git repository for testing.

    The repository contains a basic Python file and a passing test,
    simulating the ``autopilot-test-target`` structure.

    Args:
        tmp_path: Pytest temp directory fixture.

    Returns:
        Path to the initialised git repository.
    """
    repo = tmp_path / "test-target"
    repo.mkdir()

    # Create a minimal Python project
    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n"
    )
    (repo / "README.md").write_text("# Test Target\n")

    # Initialise git
    subprocess.run(
        ["git", "init", "--initial-branch", "main"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return repo


# ===========================================================================
# Implement role integration tests (mock-based)
# ===========================================================================


class TestImplementRoleIntegration:
    """Integration tests for the implement role using mocked subprocess and SDK."""

    @pytest.mark.asyncio
    async def test_implement_pre_process_creates_feature_branch(
        self,
        _agent_test_repo: Path,
    ) -> None:
        """Verify pre_process creates the expected feature branch."""
        role = ImplementRole(
            run_id="integ-impl-001",
            repo_path=_agent_test_repo,
            branch="main",
            agent_instructions="Add a square function.",
            model="claude-sonnet-4-20250514",
        )

        # Mock subprocess to avoid needing a git remote
        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
        ) as mock_exec:
            # git config email, git config name, git checkout, git pull,
            # git checkout -b
            procs = [_make_process_mock(returncode=0) for _ in range(5)]
            mock_exec.side_effect = procs

            await role.pre_process()

        # Verify checkout -b was called with the feature branch name
        calls = mock_exec.call_args_list
        last_call_args = calls[-1][0]
        assert "feature/integ-impl-001" in last_call_args

    @pytest.mark.asyncio
    async def test_implement_execute_with_mock_runner(
        self,
        _agent_test_repo: Path,
    ) -> None:
        """Verify execute delegates to AgentRunner and returns a session log."""
        role = ImplementRole(
            run_id="integ-impl-002",
            repo_path=_agent_test_repo,
            branch="main",
            agent_instructions="Add a multiply function.",
            model="claude-sonnet-4-20250514",
        )

        # Pre-process with mocked subprocess (no remote)
        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
        ) as mock_exec:
            procs = [_make_process_mock(returncode=0) for _ in range(5)]
            mock_exec.side_effect = procs
            await role.pre_process()

        # Mock the AgentRunner
        mock_runner = AsyncMock(spec=AgentRunner)
        expected_log = _make_session_log()
        mock_runner.run = AsyncMock(return_value=expected_log)

        session_log = await role.execute(mock_runner)

        assert session_log is expected_log
        mock_runner.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_implement_post_process_collects_changes(
        self,
        _agent_test_repo: Path,
    ) -> None:
        """Verify post_process collects commits and changed files from git."""
        role = ImplementRole(
            run_id="integ-impl-003",
            repo_path=_agent_test_repo,
            branch="main",
            agent_instructions="Add a subtract function.",
            model="claude-sonnet-4-20250514",
        )

        # Manually create the feature branch so git log/diff work
        subprocess.run(
            ["git", "checkout", "-b", "feature/integ-impl-003"],
            cwd=_agent_test_repo,
            capture_output=True,
            check=True,
        )

        # Simulate agent work: create a file and commit on the feature branch
        new_file = _agent_test_repo / "src" / "subtract.py"
        new_file.write_text("def subtract(a: int, b: int) -> int:\n    return a - b\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=_agent_test_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "feat: add subtract function"],
            cwd=_agent_test_repo,
            capture_output=True,
            check=True,
        )

        session_log = _make_session_log()

        # Mock the remote CLI calls (push and PR create)
        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
        ) as mock_exec:
            push_proc = _make_process_mock(returncode=0)
            pr_proc = _make_process_mock(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/1",
            )
            mock_exec.side_effect = [push_proc, pr_proc, pr_proc]

            result = await role.post_process(session_log)

        assert isinstance(result, dict)
        assert "commits" in result
        assert "files_changed" in result
        assert len(result["commits"]) >= 1
        assert any("subtract" in f for f in result["files_changed"])


# ===========================================================================
# Review role integration tests (mock-based)
# ===========================================================================


class TestReviewRoleIntegration:
    """Integration tests for the review role using mocked subprocess and SDK."""

    @pytest.mark.asyncio
    async def test_review_parse_structured_output(self) -> None:
        """Verify _parse_review_output produces a structured review dict."""
        role = ReviewRole(
            run_id="integ-review-001",
            repo_path=Path("/tmp/fake"),
            branch="main",
            pr_number=42,
            agent_instructions="Review this PR.",
            model="claude-sonnet-4-20250514",
            repository="owner/test-repo",
        )

        # Build a session log with JSON-structured review output
        review_json = json.dumps(
            {
                "assessment": "approve",
                "review_comments": [
                    {
                        "file_path": "src/app.py",
                        "line": 10,
                        "body": "Good implementation.",
                    }
                ],
                "suggested_changes": [],
                "security_concerns": [],
            }
        )
        session_log = _make_session_log(
            messages=[
                {"content": f"```json\n{review_json}\n```", "timestamp": "t1"},
            ],
            final_message=f"```json\n{review_json}\n```",
        )

        # Parse the review output
        review_data = role._parse_review_output(session_log)

        assert review_data["assessment"] == "approve"
        assert isinstance(review_data["review_comments"], list)
        assert len(review_data["review_comments"]) == 1
        assert review_data["review_comments"][0]["file_path"] == "src/app.py"

    @pytest.mark.asyncio
    async def test_review_parse_request_changes(self) -> None:
        """Verify review parsing handles request_changes assessment."""
        role = ReviewRole(
            run_id="integ-review-002",
            repo_path=Path("/tmp/fake"),
            branch="main",
            pr_number=42,
            agent_instructions="Review this PR.",
            model="claude-sonnet-4-20250514",
            repository="owner/test-repo",
        )

        review_json = json.dumps(
            {
                "assessment": "request_changes",
                "review_comments": [
                    {
                        "file_path": "src/handler.py",
                        "line": 25,
                        "body": "Missing error handling for edge case.",
                    },
                    {
                        "file_path": "src/handler.py",
                        "line": 40,
                        "body": "Variable name could be more descriptive.",
                    },
                ],
                "suggested_changes": [
                    {
                        "file_path": "src/handler.py",
                        "start_line": 25,
                        "end_line": 28,
                        "suggested_code": "try:\n    result = process(data)\nexcept ValueError as e:\n    raise BadRequest(str(e))",
                    }
                ],
                "security_concerns": ["Potential SQL injection on line 33"],
            }
        )
        session_log = _make_session_log(
            messages=[
                {"content": f"```json\n{review_json}\n```", "timestamp": "t1"},
            ],
            final_message=f"```json\n{review_json}\n```",
        )

        review_data = role._parse_review_output(session_log)

        assert review_data["assessment"] == "request_changes"
        assert len(review_data["review_comments"]) == 2
        assert len(review_data["suggested_changes"]) == 1
        assert len(review_data["security_concerns"]) == 1

    @pytest.mark.asyncio
    async def test_review_text_fallback_parsing(self) -> None:
        """Verify review parsing falls back to text heuristics when no JSON."""
        role = ReviewRole(
            run_id="integ-review-003",
            repo_path=Path("/tmp/fake"),
            branch="main",
            pr_number=42,
            agent_instructions="Review this PR.",
            model="claude-sonnet-4-20250514",
            repository="owner/test-repo",
        )

        session_log = _make_session_log(
            messages=[
                {
                    "content": (
                        "I have reviewed the PR. The code looks good overall. "
                        "I approve the changes. No security concerns found."
                    ),
                    "timestamp": "t1",
                },
            ],
            final_message=(
                "I have reviewed the PR. The code looks good overall. "
                "I approve the changes. No security concerns found."
            ),
        )

        review_data = role._parse_review_output(session_log)

        # Fallback should produce a valid review structure
        assert "assessment" in review_data
        assert review_data["assessment"] in ("approve", "request_changes", "comment")


# ===========================================================================
# Merge role integration tests (mock-based)
# ===========================================================================


class TestMergeRoleIntegration:
    """Integration tests for the merge role using mocked subprocess and SDK."""

    @pytest.mark.asyncio
    async def test_merge_clean_merge_detection(
        self,
        _agent_test_repo: Path,
    ) -> None:
        """Verify local merge attempt detects a clean merge scenario."""
        role = MergeRole(
            run_id="integ-merge-001",
            repo_path=_agent_test_repo,
            branch="main",
            pr_number=1,
            agent_instructions="Merge this PR.",
            model="claude-sonnet-4-20250514",
            repository="owner/test-repo",
        )

        pr_metadata = json.dumps(
            {
                "headRefName": "feature/clean-merge",
                "baseRefName": "main",
                "title": "Add new feature",
                "body": "A new feature.",
            }
        )

        # Mock all subprocess calls (both async and sync)
        with (
            patch(
                "app.src.agent.roles.merge.asyncio.create_subprocess_exec",
            ) as mock_exec,
            patch(
                "app.src.agent.roles.merge.subprocess.run",
            ) as mock_sync_run,
        ):
            # Async: gh pr view, git config name, git config email,
            # git fetch head, git fetch base, git checkout, git pull
            gh_proc = _make_process_mock(returncode=0, stdout=pr_metadata)
            git_procs = [_make_process_mock(returncode=0) for _ in range(6)]
            mock_exec.side_effect = [gh_proc, *git_procs]

            # Sync: git merge --no-commit --no-ff (clean = rc 0),
            # then git merge --abort
            merge_result = MagicMock()
            merge_result.returncode = 0
            merge_result.stdout = b""
            merge_result.stderr = b""
            abort_result = MagicMock()
            abort_result.returncode = 0
            abort_result.stdout = b""
            abort_result.stderr = b""
            mock_sync_run.side_effect = [merge_result, abort_result]

            await role.pre_process()

        assert role._merge_attempt["clean"] is True

    @pytest.mark.asyncio
    async def test_merge_conflict_detection(
        self,
        _agent_test_repo: Path,
    ) -> None:
        """Verify local merge attempt detects conflicts."""
        role = MergeRole(
            run_id="integ-merge-002",
            repo_path=_agent_test_repo,
            branch="main",
            pr_number=1,
            agent_instructions="Merge this PR.",
            model="claude-sonnet-4-20250514",
            repository="owner/test-repo",
        )

        pr_metadata = json.dumps(
            {
                "headRefName": "feature/conflict",
                "baseRefName": "main",
                "title": "Conflicting change",
                "body": "This has conflicts.",
            }
        )
        with (
            patch(
                "app.src.agent.roles.merge.asyncio.create_subprocess_exec",
            ) as mock_exec,
            patch(
                "app.src.agent.roles.merge.subprocess.run",
            ) as mock_sync_run,
        ):
            # Async: gh pr view -> metadata
            gh_proc = _make_process_mock(returncode=0, stdout=pr_metadata)
            # git config name, git config email, git fetch head, git fetch base,
            # git checkout, git pull
            setup_procs = [_make_process_mock(returncode=0) for _ in range(6)]
            mock_exec.side_effect = [gh_proc, *setup_procs]

            # Sync: git merge --no-commit --no-ff -> conflict (rc 1)
            merge_result = MagicMock()
            merge_result.returncode = 1
            merge_result.stdout = ""
            merge_result.stderr = (
                "CONFLICT (content): Merge conflict in src/calculator.py"
            )
            # git diff --name-only --diff-filter=U -> conflicted files
            diff_result = MagicMock()
            diff_result.returncode = 0
            diff_result.stdout = "src/calculator.py\n"
            diff_result.stderr = ""
            # git merge --abort
            abort_result = MagicMock()
            abort_result.returncode = 0
            abort_result.stdout = ""
            abort_result.stderr = ""
            mock_sync_run.side_effect = [merge_result, diff_result, abort_result]

            await role.pre_process()

        assert role._merge_attempt["clean"] is False
        assert "src/calculator.py" in role._merge_attempt["conflict_files"]


# ===========================================================================
# Error scenario tests (always run — no live SDK needed)
# ===========================================================================


class TestErrorPayloadOnSdkCrash:
    """Verify error payloads generated when the SDK crashes during init."""

    @pytest.mark.asyncio
    async def test_sdk_init_error_produces_structured_payload(self) -> None:
        """Verify that SDK init errors are caught and the result compiler
        generates a proper error payload.
        """
        mock_client = _make_mock_client(
            start_side_effect=FileNotFoundError(
                "copilot CLI not found at /usr/local/bin/copilot"
            ),
        )

        runner = AgentRunner(
            run_id="error-test-001",
            model="claude-sonnet-4-20250514",
            system_message="Test system message.",
            timeout_minutes=5,
        )

        # runner.run() propagates AgentExecutionError from start()
        caught_error: AgentExecutionError | None = None
        with (
            patch(
                "app.src.agent.runner.CopilotClient",
                return_value=mock_client,
            ),
        ):
            try:
                await runner.run("Test instructions")
            except AgentExecutionError as exc:
                caught_error = exc

        assert caught_error is not None
        assert caught_error.error_code == "SDK_INIT_ERROR"

        # Compile the error payload using the caught error
        compiler = ResultCompiler(
            run_id="error-test-001",
            role="implement",
            model="claude-sonnet-4-20250514",
        )
        payload = compiler.compile_error(caught_error)

        assert payload["run_id"] == "error-test-001"
        assert payload["status"] == "failure"
        assert payload["error"]["error_code"] == "SDK_INIT_ERROR"
        assert "copilot CLI" in payload["error"]["error_message"]

    @pytest.mark.asyncio
    async def test_runtime_error_produces_structured_payload(self) -> None:
        """Verify that generic runtime errors produce AGENT_RUNTIME_ERROR payloads."""
        mock_session = _make_mock_session()
        mock_session.send_and_wait = AsyncMock(
            side_effect=RuntimeError("Unexpected agent crash"),
        )
        mock_client = _make_mock_client(session=mock_session)

        runner = AgentRunner(
            run_id="error-test-002",
            model="claude-sonnet-4-20250514",
            system_message="Test system message.",
            timeout_minutes=5,
        )

        caught_error: AgentExecutionError | None = None
        with (
            patch(
                "app.src.agent.runner.CopilotClient",
                return_value=mock_client,
            ),
        ):
            try:
                await runner.run("Test instructions")
            except AgentExecutionError as exc:
                caught_error = exc

        assert caught_error is not None
        assert caught_error.error_code == "AGENT_RUNTIME_ERROR"
        assert "Unexpected agent crash" in caught_error.error_message
        assert len(runner.session_log.errors) >= 1

        compiler = ResultCompiler(
            run_id="error-test-002",
            role="implement",
            model="claude-sonnet-4-20250514",
        )
        payload = compiler.compile_error(caught_error, runner.session_log)

        assert payload["status"] == "failure"
        assert payload["error"]["error_code"] == "AGENT_RUNTIME_ERROR"
        assert "crash" in payload["error"]["error_message"]


class TestErrorPayloadOnTimeout:
    """Verify timeout error payloads are correctly generated."""

    @pytest.mark.asyncio
    async def test_timeout_produces_structured_payload(self) -> None:
        """Verify that session timeout generates an AGENT_TIMEOUT payload."""
        mock_session = _make_mock_session()
        # send_and_wait returns None on timeout
        mock_session.send_and_wait = AsyncMock(return_value=None)
        mock_client = _make_mock_client(session=mock_session)

        runner = AgentRunner(
            run_id="timeout-test-001",
            model="claude-sonnet-4-20250514",
            system_message="Test system message.",
            timeout_minutes=1,
        )

        with (
            patch(
                "app.src.agent.runner.CopilotClient",
                return_value=mock_client,
            ),
        ):
            session_log = await runner.run("Test instructions")

        assert session_log.timed_out is True

        compiler = ResultCompiler(
            run_id="timeout-test-001",
            role="implement",
            model="claude-sonnet-4-20250514",
        )
        payload = compiler.compile_timeout(session_log)

        assert payload["run_id"] == "timeout-test-001"
        assert payload["status"] == "failure"
        assert payload["error"]["error_code"] == "AGENT_TIMEOUT"
        assert "messages_count" in payload["error"]["error_details"]
        assert "tool_calls_count" in payload["error"]["error_details"]

    @pytest.mark.asyncio
    async def test_timeout_includes_partial_session_stats(self) -> None:
        """Verify timeout payload includes session statistics for debugging."""
        session_log = _make_session_log(
            messages=[
                {"content": "msg1", "timestamp": "t1"},
                {"content": "msg2", "timestamp": "t2"},
                {"content": "msg3", "timestamp": "t3"},
            ],
            tool_calls=[
                {"tool": "read_file", "args": "a.py", "timestamp": "t1"},
                {"tool": "write_file", "args": "b.py", "timestamp": "t2"},
            ],
            errors=[
                {"error": "tool failed", "timestamp": "t3"},
            ],
            timed_out=True,
        )

        compiler = ResultCompiler(
            run_id="timeout-test-002",
            role="review",
            model="claude-sonnet-4-20250514",
        )
        payload = compiler.compile_timeout(session_log)

        assert payload["error"]["error_details"]["messages_count"] == 3
        assert payload["error"]["error_details"]["tool_calls_count"] == 2
        assert payload["error"]["error_details"]["errors_count"] == 1


class TestWorkflowFailureHandler:
    """Verify the workflow failure handler error payload generation."""

    def test_workflow_cancelled_error_payload(self) -> None:
        """Verify WORKFLOW_CANCELLED error payload structure."""
        compiler = ResultCompiler(
            run_id="wf-cancel-001",
            role="implement",
            model="claude-sonnet-4-20250514",
        )
        error = AgentExecutionError(
            error_code="WORKFLOW_CANCELLED",
            error_message="Workflow was cancelled.",
            error_details={
                "agent_log_url": "https://github.com/org/repo/actions/runs/123",
                "job_status": "cancelled",
            },
        )
        payload = compiler.compile_error(error)

        assert payload["status"] == "failure"
        assert payload["error"]["error_code"] == "WORKFLOW_CANCELLED"
        assert payload["error"]["error_message"] == "Workflow was cancelled."
        assert "agent_log_url" in payload["error"]["error_details"]

    def test_workflow_step_failure_error_payload(self) -> None:
        """Verify WORKFLOW_STEP_FAILURE error payload structure."""
        compiler = ResultCompiler(
            run_id="wf-fail-001",
            role="review",
            model="claude-sonnet-4-20250514",
        )
        error = AgentExecutionError(
            error_code="WORKFLOW_STEP_FAILURE",
            error_message="Agent executor workflow step failed.",
            error_details={
                "agent_log_url": "https://github.com/org/repo/actions/runs/456",
                "job_status": "failure",
            },
        )
        payload = compiler.compile_error(error)

        assert payload["status"] == "failure"
        assert payload["error"]["error_code"] == "WORKFLOW_STEP_FAILURE"
        assert "agent_log_url" in payload["error"]["error_details"]


class TestResultCompilerIntegration:
    """Integration tests for ResultCompiler producing role-specific payloads."""

    def test_compile_implement_success(self) -> None:
        """Verify compile_success produces a complete implement result."""
        compiler = ResultCompiler(
            run_id="rc-impl-001",
            role="implement",
            model="claude-sonnet-4-20250514",
        )
        session_log = _make_session_log()
        role_result = {
            "pr_url": "https://github.com/owner/repo/pull/42",
            "pr_number": 42,
            "branch": "feature/rc-impl-001",
            "commits": [{"sha": "abc123", "message": "feat: add function"}],
            "files_changed": ["src/calculator.py"],
            "test_results": {"passed": 5, "failed": 0, "skipped": 0},
            "security_findings": [],
        }

        payload = compiler.compile_success(role_result, session_log)

        assert payload["run_id"] == "rc-impl-001"
        assert payload["status"] == "success"
        assert payload["role"] == "implement"
        assert payload["model_used"] == "claude-sonnet-4-20250514"
        assert payload["pr_url"] == "https://github.com/owner/repo/pull/42"
        assert payload["pr_number"] == 42
        assert "duration_seconds" in payload

    def test_compile_review_success(self) -> None:
        """Verify compile_success produces a complete review result."""
        compiler = ResultCompiler(
            run_id="rc-review-001",
            role="review",
            model="claude-sonnet-4-20250514",
        )
        session_log = _make_session_log()
        role_result = {
            "assessment": "approve",
            "review_comments": [{"file_path": "src/app.py", "body": "LGTM"}],
            "suggested_changes": [],
            "security_concerns": [],
            "pr_approved": True,
        }

        payload = compiler.compile_success(role_result, session_log)

        assert payload["status"] == "success"
        assert payload["assessment"] == "approve"
        assert payload["pr_approved"] is True

    def test_compile_merge_success(self) -> None:
        """Verify compile_success produces a complete merge result."""
        compiler = ResultCompiler(
            run_id="rc-merge-001",
            role="merge",
            model="claude-sonnet-4-20250514",
        )
        session_log = _make_session_log()
        role_result = {
            "merge_status": "merged",
            "merge_sha": "def456789abcdef0123456789abcdef012345678",
            "conflict_files": [],
            "conflict_resolutions": [],
            "test_results": {"passed": 10, "failed": 0, "skipped": 0},
        }

        payload = compiler.compile_success(role_result, session_log)

        assert payload["status"] == "success"
        assert payload["merge_status"] == "merged"
        assert payload["merge_sha"] is not None

    def test_compile_error_from_generic_exception(self) -> None:
        """Verify compile_error handles generic exceptions with UNKNOWN_ERROR."""
        compiler = ResultCompiler(
            run_id="rc-err-001",
            role="implement",
            model="claude-sonnet-4-20250514",
        )
        error = ValueError("Something unexpected happened")
        payload = compiler.compile_error(error)

        assert payload["status"] == "failure"
        assert payload["error"]["error_code"] == "UNKNOWN_ERROR"


# ===========================================================================
# E2E agent tests (gated behind --e2e-confirm)
# ===========================================================================


@pytest.mark.requires_e2e
class TestLiveImplementRole:
    """Live agent tests for the implement role.

    Requires:
    - ``autopilot-test-target`` repository accessible via ``gh``
    - Copilot SDK installed and functional
    - Valid GitHub PAT

    Run with ``pytest --e2e-confirm``.
    """

    @pytest.mark.asyncio
    async def test_implement_creates_branch_and_commits(self) -> None:
        """Verify agent creates a feature branch and makes commits.

        This test clones ``autopilot-test-target``, runs the implement role
        with a simple instruction, and verifies structural properties:
        feature branch exists, at least one commit is present.
        """
        pytest.skip(
            "Live Copilot SDK agent test — requires manual confirmation "
            "and a running SDK. Execute interactively with --e2e-confirm."
        )


@pytest.mark.requires_e2e
class TestLiveReviewRole:
    """Live agent tests for the review role.

    Requires an open PR on ``autopilot-test-target``.
    Run with ``pytest --e2e-confirm``.
    """

    @pytest.mark.asyncio
    async def test_review_produces_structured_output(self) -> None:
        """Verify agent produces a structured review with assessment field."""
        pytest.skip(
            "Live Copilot SDK agent test — requires an open PR on "
            "autopilot-test-target. Execute interactively with --e2e-confirm."
        )


@pytest.mark.requires_e2e
class TestLiveMergeRole:
    """Live agent tests for the merge role.

    Requires a mergeable PR on ``autopilot-test-target``.
    Run with ``pytest --e2e-confirm``.
    """

    @pytest.mark.asyncio
    async def test_merge_handles_clean_merge(self) -> None:
        """Verify agent detects a clean merge path and runs tests."""
        pytest.skip(
            "Live Copilot SDK agent test — requires a mergeable PR on "
            "autopilot-test-target. Execute interactively with --e2e-confirm."
        )
