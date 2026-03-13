"""Unit tests for the implement role logic (Component 4.3).

Tests cover:
- :meth:`ImplementRole.pre_process` — git identity configuration, branch
  creation, failure handling.
- :meth:`ImplementRole._collect_changes` — parsing ``git log`` and
  ``git diff`` output.
- :meth:`ImplementRole._parse_test_results` — pytest, jest, and go test
  output formats; no-test-output fallback.
- :meth:`ImplementRole._create_pr` — ``gh pr create`` command building,
  draft flag, output parsing.
- :meth:`ImplementRole.post_process` — complete result dictionary structure.
- :meth:`ImplementRole._parse_security_findings` — security pattern scanning.
- :meth:`ImplementRole._generate_pr_title` — title extraction and truncation.
- :meth:`ImplementRole.build_system_message` — prompt loader integration.
- :meth:`ImplementRole.resolve_skill_directories` — skill path resolution.

Note:
    Subprocess calls are mocked via ``unittest.mock.patch``. The
    ``AgentRunner`` is mocked to avoid SDK dependency in unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.src.agent.roles.implement import ImplementRole
from app.src.agent.runner import AgentExecutionError, AgentSessionLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_role(**overrides: Any) -> ImplementRole:
    """Create an ImplementRole with default test parameters.

    Args:
        **overrides: Keyword arguments to override defaults.

    Returns:
        A configured :class:`ImplementRole` instance.
    """
    defaults: dict[str, Any] = {
        "run_id": "test-run-001",
        "repo_path": Path("/tmp/test-repo"),
        "branch": "main",
        "agent_instructions": "Add a health-check endpoint to the API.",
        "model": "claude-sonnet-4-20250514",
    }
    defaults.update(overrides)
    return ImplementRole(**defaults)


def _make_session_log(**overrides: Any) -> AgentSessionLog:
    """Create an AgentSessionLog with default test values.

    Args:
        **overrides: Keyword arguments to override defaults.

    Returns:
        A configured :class:`AgentSessionLog` instance.
    """
    defaults: dict[str, Any] = {
        "messages": [
            {"content": "I implemented the health check endpoint.", "timestamp": "t1"},
        ],
        "tool_calls": [],
        "errors": [],
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:30:00+00:00",
        "final_message": "I implemented the health check endpoint.",
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


# ===========================================================================
# Pre-process tests
# ===========================================================================


class TestPreProcess:
    """Tests for :meth:`ImplementRole.pre_process`."""

    @pytest.mark.asyncio
    async def test_pre_process_creates_branch(self) -> None:
        """Verify git commands for identity config, checkout, and branch creation."""
        role = _make_role()
        calls: list[tuple[str, ...]] = []

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            calls.append(args)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            await role.pre_process()

        # Verify the correct git commands in the correct order.
        assert len(calls) == 5
        assert calls[0] == ("git", "config", "user.name", "Copilot Dispatch Bot")
        assert calls[1] == (
            "git",
            "config",
            "user.email",
            "copilot-dispatch@noreply.github.com",
        )
        assert calls[2] == ("git", "checkout", "main")
        assert calls[3] == ("git", "pull", "origin", "main")
        assert calls[4] == ("git", "checkout", "-b", "feature/test-run-001")

    @pytest.mark.asyncio
    async def test_pre_process_git_failure(self) -> None:
        """Verify AgentExecutionError is raised on git command failure."""
        role = _make_role()

        async def mock_failing_subprocess(*args: str, **kwargs: Any) -> AsyncMock:
            return _make_process_mock(
                returncode=1,
                stderr="fatal: not a git repository",
            )

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_failing_subprocess,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role.pre_process()

            assert exc_info.value.error_code == "GIT_PRE_PROCESS_ERROR"
            assert "exit code 1" in exc_info.value.error_message

    @pytest.mark.asyncio
    async def test_pre_process_uses_repo_path_as_cwd(self) -> None:
        """Verify git commands run in the repo directory."""
        role = _make_role(repo_path=Path("/workspace/my-repo"))
        captured_kwargs: list[dict[str, Any]] = []

        async def mock_subprocess(*args: str, **kwargs: Any) -> AsyncMock:
            captured_kwargs.append(kwargs)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_subprocess,
        ):
            await role.pre_process()

        for kw in captured_kwargs:
            assert kw.get("cwd") == "/workspace/my-repo"


# ===========================================================================
# Collect changes tests
# ===========================================================================


class TestCollectChanges:
    """Tests for :meth:`ImplementRole._collect_changes`."""

    def test_collect_changes_parses_git_log(self) -> None:
        """Verify commits and files are parsed correctly from git output."""
        role = _make_role()

        log_output = (
            "abc123|||Add health check endpoint\n" "def456|||Fix test assertion\n"
        )
        diff_output = "src/api/health.py\ntests/test_health.py\n"

        with patch.object(
            role,
            "_run_cmd_sync",
            side_effect=[log_output, diff_output],
        ):
            changes = role._collect_changes()

        assert len(changes["commits"]) == 2
        assert changes["commits"][0] == {
            "sha": "abc123",
            "message": "Add health check endpoint",
        }
        assert changes["commits"][1] == {
            "sha": "def456",
            "message": "Fix test assertion",
        }
        assert changes["files_changed"] == [
            "src/api/health.py",
            "tests/test_health.py",
        ]

    def test_collect_changes_empty_output(self) -> None:
        """Verify empty lists when no commits or files changed."""
        role = _make_role()

        with patch.object(
            role,
            "_run_cmd_sync",
            side_effect=["", ""],
        ):
            changes = role._collect_changes()

        assert changes["commits"] == []
        assert changes["files_changed"] == []

    def test_collect_changes_handles_git_errors(self) -> None:
        """Verify graceful handling when git commands fail."""
        role = _make_role()

        with patch.object(
            role,
            "_run_cmd_sync",
            side_effect=AgentExecutionError(
                error_message="git log failed",
                error_code="GIT_PRE_PROCESS_ERROR",
            ),
        ):
            changes = role._collect_changes()

        assert changes["commits"] == []
        assert changes["files_changed"] == []


# ===========================================================================
# Test result parsing tests
# ===========================================================================


class TestParseTestResults:
    """Tests for :meth:`ImplementRole._parse_test_results`."""

    def test_parse_test_results_pytest_format(self) -> None:
        """Parse pytest-style output: 'X passed, Y failed, Z skipped'."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest -q",
                    "result": "5 passed, 1 failed, 2 skipped in 3.45s",
                    "timestamp": "t1",
                },
            ],
        )

        result = role._parse_test_results(session_log)
        assert result == {"passed": 5, "failed": 1, "skipped": 2}

    def test_parse_test_results_jest_format(self) -> None:
        """Parse jest-style output: 'Tests: X passed, Y failed, Z total'."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "run_command",
                    "arguments": "npm test",
                    "result": (
                        "Test Suites: 2 passed, 2 total\n"
                        "Tests:       3 passed, 1 failed, 2 skipped, 6 total"
                    ),
                    "timestamp": "t1",
                },
            ],
        )

        result = role._parse_test_results(session_log)
        assert result["passed"] == 3
        assert result["failed"] == 1
        assert result["skipped"] == 2

    def test_parse_test_results_go_test_format(self) -> None:
        """Parse go test output with --- PASS/FAIL lines."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "go test ./...",
                    "result": (
                        "--- PASS: TestHealthCheck (0.00s)\n"
                        "--- PASS: TestEndpoint (0.01s)\n"
                        "--- FAIL: TestBroken (0.00s)\n"
                        "--- SKIP: TestSkipped (0.00s)\n"
                    ),
                    "timestamp": "t1",
                },
            ],
        )

        result = role._parse_test_results(session_log)
        assert result["passed"] == 2
        assert result["failed"] == 1
        assert result["skipped"] == 1

    def test_parse_test_results_no_tests(self) -> None:
        """Verify default values when no test output is detected."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "ls -la",
                    "result": "total 8\ndrwxr-xr-x ...",
                    "timestamp": "t1",
                },
            ],
        )

        result = role._parse_test_results(session_log)
        assert result == {"passed": 0, "failed": 0, "skipped": 0}

    def test_parse_test_results_empty_log(self) -> None:
        """Verify defaults when session log has no tool calls."""
        role = _make_role()
        session_log = _make_session_log(tool_calls=[])

        result = role._parse_test_results(session_log)
        assert result == {"passed": 0, "failed": 0, "skipped": 0}

    def test_parse_test_results_multiple_test_runs(self) -> None:
        """Verify counts accumulate across multiple test tool calls."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest tests/unit/",
                    "result": "3 passed in 1.0s",
                    "timestamp": "t1",
                },
                {
                    "name": "shell",
                    "arguments": "pytest tests/integration/",
                    "result": "2 passed, 1 failed in 2.0s",
                    "timestamp": "t2",
                },
            ],
        )

        result = role._parse_test_results(session_log)
        assert result["passed"] == 5
        assert result["failed"] == 1
        assert result["skipped"] == 0


# ===========================================================================
# PR creation tests
# ===========================================================================


class TestCreatePR:
    """Tests for :meth:`ImplementRole._create_pr`."""

    @pytest.mark.asyncio
    async def test_create_pr_builds_correct_command(self) -> None:
        """Verify ``gh pr create`` is called with correct title, body, base, head."""
        role = _make_role()
        session_log = _make_session_log()
        changes = {
            "commits": [{"sha": "abc", "message": "test"}],
            "files_changed": ["f.py"],
        }
        test_results = {"passed": 5, "failed": 0, "skipped": 0}

        captured_calls: list[tuple[str, ...]] = []

        async def mock_subprocess(*args: str, **kwargs: Any) -> AsyncMock:
            captured_calls.append(args)
            if args[0] == "gh" and args[1] == "pr" and args[2] == "create":
                return _make_process_mock(stdout="https://github.com/o/r/pull/42\n")
            if args[0] == "gh" and args[1] == "pr" and args[2] == "view":
                return _make_process_mock(
                    stdout=json.dumps(
                        {"number": 42, "url": "https://github.com/o/r/pull/42"}
                    )
                )
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_subprocess,
        ):
            pr_info = await role._create_pr(
                session_log, changes, test_results, is_draft=False
            )

        # Verify the create command.
        create_call = captured_calls[0]
        assert create_call[0] == "gh"
        assert create_call[1] == "pr"
        assert create_call[2] == "create"
        assert "--base" in create_call
        assert "main" in create_call
        assert "--head" in create_call
        assert "feature/test-run-001" in create_call
        assert "--draft" not in create_call
        assert pr_info["pr_number"] == 42
        assert "pull/42" in pr_info["pr_url"]

    @pytest.mark.asyncio
    async def test_create_pr_draft_on_test_failure(self) -> None:
        """Verify ``--draft`` flag is included when tests fail."""
        role = _make_role()
        session_log = _make_session_log()
        changes = {"commits": [], "files_changed": []}
        test_results = {"passed": 3, "failed": 2, "skipped": 0}

        captured_calls: list[tuple[str, ...]] = []

        async def mock_subprocess(*args: str, **kwargs: Any) -> AsyncMock:
            captured_calls.append(args)
            if args[0] == "gh" and args[1] == "pr" and args[2] == "create":
                return _make_process_mock(stdout="https://github.com/o/r/pull/1\n")
            if args[0] == "gh" and args[1] == "pr" and args[2] == "view":
                return _make_process_mock(
                    stdout=json.dumps(
                        {"number": 1, "url": "https://github.com/o/r/pull/1"}
                    )
                )
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_subprocess,
        ):
            await role._create_pr(session_log, changes, test_results, is_draft=True)

        create_call = captured_calls[0]
        assert "--draft" in create_call

    @pytest.mark.asyncio
    async def test_create_pr_failure_raises_error(self) -> None:
        """Verify AgentExecutionError when ``gh pr create`` fails."""
        role = _make_role()
        session_log = _make_session_log()
        changes = {"commits": [], "files_changed": []}
        test_results = {"passed": 0, "failed": 0, "skipped": 0}

        async def mock_failing_subprocess(*args: str, **kwargs: Any) -> AsyncMock:
            return _make_process_mock(
                returncode=1,
                stderr="gh: No remote found",
            )

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_failing_subprocess,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role._create_pr(
                    session_log, changes, test_results, is_draft=False
                )

            assert exc_info.value.error_code == "PR_CREATE_ERROR"

    @pytest.mark.asyncio
    async def test_create_pr_fallback_on_view_failure(self) -> None:
        """Verify fallback when ``gh pr view`` fails after successful create."""
        role = _make_role()
        session_log = _make_session_log()
        changes = {"commits": [], "files_changed": []}
        test_results = {"passed": 0, "failed": 0, "skipped": 0}
        call_count = 0

        async def mock_subprocess(*args: str, **kwargs: Any) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            if args[0] == "gh" and args[1] == "pr" and args[2] == "create":
                return _make_process_mock(stdout="https://github.com/o/r/pull/99\n")
            # Simulate gh pr view failure.
            return _make_process_mock(
                returncode=1,
                stderr="no PR found",
            )

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_subprocess,
        ):
            # The view step will fail but we handle it gracefully by catching
            # the AgentExecutionError from _run_cmd_async.
            # We need to test that the fallback works by handling the exception
            # in _create_pr's except block.
            pr_info = await role._create_pr(
                session_log, changes, test_results, is_draft=False
            )

        assert pr_info["pr_url"] == "https://github.com/o/r/pull/99"
        assert pr_info["pr_number"] == 0


# ===========================================================================
# PR title generation tests
# ===========================================================================


class TestGeneratePrTitle:
    """Tests for :meth:`ImplementRole._generate_pr_title`."""

    def test_short_instructions(self) -> None:
        """Verify first sentence is used for short instructions."""
        role = _make_role(
            agent_instructions="Add a health check endpoint. Also update docs."
        )
        title = role._generate_pr_title()
        assert title == "Add a health check endpoint"

    def test_long_instructions_truncated(self) -> None:
        """Verify long titles are truncated to _MAX_TITLE_LENGTH chars."""
        long_text = "A" * 100
        role = _make_role(agent_instructions=long_text)
        title = role._generate_pr_title()
        assert len(title) <= 72
        assert title.endswith("...")

    def test_empty_instructions_fallback(self) -> None:
        """Verify fallback title when instructions are empty."""
        role = _make_role(agent_instructions="")
        title = role._generate_pr_title()
        assert title == "Dispatch: test-run-001"

    def test_whitespace_only_fallback(self) -> None:
        """Verify fallback title when instructions are whitespace."""
        role = _make_role(agent_instructions="   \n  ")
        title = role._generate_pr_title()
        assert title == "Dispatch: test-run-001"

    def test_newline_terminated(self) -> None:
        """Verify first line is used when instructions have newlines."""
        role = _make_role(agent_instructions="Fix the bug\nMore details here")
        title = role._generate_pr_title()
        assert title == "Fix the bug"


# ===========================================================================
# Push branch tests
# ===========================================================================


class TestPushBranch:
    """Tests for :meth:`ImplementRole._push_branch`."""

    @pytest.mark.asyncio
    async def test_push_branch_success(self) -> None:
        """Verify git push is called correctly."""
        role = _make_role()
        captured_calls: list[tuple[str, ...]] = []

        async def mock_subprocess(*args: str, **kwargs: Any) -> AsyncMock:
            captured_calls.append(args)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_subprocess,
        ):
            await role._push_branch()

        assert captured_calls[0] == ("git", "push", "origin", "feature/test-run-001")

    @pytest.mark.asyncio
    async def test_push_branch_failure(self) -> None:
        """Verify error on push failure."""
        role = _make_role()

        async def mock_failing(*args: str, **kwargs: Any) -> AsyncMock:
            return _make_process_mock(
                returncode=128,
                stderr="fatal: remote rejected",
            )

        with patch(
            "app.src.agent.roles.implement.asyncio.create_subprocess_exec",
            side_effect=mock_failing,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role._push_branch()

            assert exc_info.value.error_code == "GIT_PUSH_ERROR"


# ===========================================================================
# Post-process tests
# ===========================================================================


class TestPostProcess:
    """Tests for :meth:`ImplementRole.post_process`."""

    @pytest.mark.asyncio
    async def test_post_process_returns_implement_result_fields(self) -> None:
        """Verify the returned dict has all fields needed for ImplementResult."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest -q",
                    "result": "5 passed in 0.5s",
                    "timestamp": "t1",
                },
            ],
        )

        # Mock _collect_changes.
        with (
            patch.object(role, "_commit_pending_changes", return_value=None),
            patch.object(
                role,
                "_collect_changes",
                return_value={
                    "commits": [{"sha": "abc", "message": "Add endpoint"}],
                    "files_changed": ["src/health.py"],
                },
            ),
            patch.object(
                role,
                "_push_branch",
                new_callable=AsyncMock,
            ),
            patch.object(
                role,
                "_create_pr",
                new_callable=AsyncMock,
                return_value={
                    "pr_url": "https://github.com/o/r/pull/42",
                    "pr_number": 42,
                },
            ),
        ):
            result = await role.post_process(session_log)

        # Verify all ImplementResult fields are present.
        assert result["pr_url"] == "https://github.com/o/r/pull/42"
        assert result["pr_number"] == 42
        assert result["branch"] == "feature/test-run-001"
        assert result["commits"] == [{"sha": "abc", "message": "Add endpoint"}]
        assert result["files_changed"] == ["src/health.py"]
        assert result["test_results"]["passed"] == 5
        assert result["test_results"]["failed"] == 0
        assert result["test_results"]["skipped"] == 0
        assert result["security_findings"] == []
        assert result["session_summary"] == "I implemented the health check endpoint."
        assert "agent_log_url" in result

    @pytest.mark.asyncio
    async def test_post_process_draft_on_test_failure(self) -> None:
        """Verify PR is created as draft when tests fail."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest -q",
                    "result": "3 passed, 2 failed in 1.0s",
                    "timestamp": "t1",
                },
            ],
        )

        create_pr_mock = AsyncMock(
            return_value={"pr_url": "https://example.com/pull/1", "pr_number": 1}
        )

        with (
            patch.object(role, "_commit_pending_changes", return_value=None),
            patch.object(
                role,
                "_collect_changes",
                return_value={
                    "commits": [{"sha": "abc", "message": "Add endpoint"}],
                    "files_changed": ["src/health.py"],
                },
            ),
            patch.object(
                role,
                "_push_branch",
                new_callable=AsyncMock,
            ),
            patch.object(
                role,
                "_create_pr",
                create_pr_mock,
            ),
        ):
            await role.post_process(session_log)

        # Verify _create_pr was called with is_draft=True.
        create_pr_mock.assert_called_once()
        call_kwargs = create_pr_mock.call_args
        assert call_kwargs.kwargs.get("is_draft") is True or (
            len(call_kwargs.args) >= 4 and call_kwargs.args[3] is True
        )

    @pytest.mark.asyncio
    async def test_post_process_draft_on_timeout(self) -> None:
        """Verify PR is created as draft when session timed out."""
        role = _make_role()
        session_log = _make_session_log(timed_out=True)

        create_pr_mock = AsyncMock(
            return_value={"pr_url": "https://example.com/pull/1", "pr_number": 1}
        )

        with (
            patch.object(role, "_commit_pending_changes", return_value=None),
            patch.object(
                role,
                "_collect_changes",
                return_value={
                    "commits": [{"sha": "abc", "message": "Add endpoint"}],
                    "files_changed": ["src/health.py"],
                },
            ),
            patch.object(
                role,
                "_push_branch",
                new_callable=AsyncMock,
            ),
            patch.object(
                role,
                "_create_pr",
                create_pr_mock,
            ),
        ):
            await role.post_process(session_log)

        call_kwargs = create_pr_mock.call_args
        assert call_kwargs.kwargs.get("is_draft") is True or (
            len(call_kwargs.args) >= 4 and call_kwargs.args[3] is True
        )

    @pytest.mark.asyncio
    async def test_post_process_empty_commits_raises_error(self) -> None:
        """Verify AgentExecutionError is raised when no commits were created."""
        role = _make_role()
        session_log = _make_session_log()

        with (
            patch.object(role, "_commit_pending_changes", return_value=None),
            patch.object(
                role,
                "_collect_changes",
                return_value={"commits": [], "files_changed": []},
            ),
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role.post_process(session_log)

            assert exc_info.value.error_code == "NO_COMMITS_CREATED"


# ===========================================================================
# Security findings tests
# ===========================================================================


class TestParseSecurityFindings:
    """Tests for :meth:`ImplementRole._parse_security_findings`."""

    def test_no_security_findings(self) -> None:
        """Verify empty list when no security output detected."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "ls",
                    "result": "file.txt",
                    "timestamp": "t1",
                },
            ],
        )
        findings = role._parse_security_findings(session_log)
        assert findings == []

    def test_detects_cve(self) -> None:
        """Verify CVE pattern is detected in tool results."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "npm audit",
                    "result": "Found CVE-2025-12345 in lodash@4.17.20",
                    "timestamp": "t1",
                },
            ],
        )
        findings = role._parse_security_findings(session_log)
        assert len(findings) == 1
        assert "CVE-2025-12345" in findings[0]

    def test_detects_security_warning_in_messages(self) -> None:
        """Verify security patterns are detected in assistant messages."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": "I found a security vulnerability in the auth module.",
                    "timestamp": "t1",
                },
            ],
        )
        findings = role._parse_security_findings(session_log)
        assert len(findings) == 1
        assert "security vulnerability" in findings[0]


# ===========================================================================
# Build system message / resolve skill directories tests
# ===========================================================================


class TestSystemMessageAndSkills:
    """Tests for system message building and skill path resolution."""

    def test_build_system_message(self, tmp_path: Path) -> None:
        """Verify system message is built via PromptLoader."""
        # Create a minimal implement.md template.
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "implement.md").write_text(
            "You are an implement agent.\n"
            "## Additional Instructions\n{system_instructions}\n"
            "## Repository-Specific Guidance\n{repo_instructions}"
        )

        with patch("app.src.agent.roles.implement.PromptLoader") as mock_loader_cls:
            mock_loader = MagicMock()
            mock_loader.build_system_message.return_value = "rendered message"
            mock_loader_cls.return_value = mock_loader

            role = _make_role(
                repo_path=tmp_path,
                system_instructions="Focus on tests.",
            )
            result = role.build_system_message()

        mock_loader.build_system_message.assert_called_once_with(
            role="implement",
            system_instructions="Focus on tests.",
            repo_path=tmp_path,
        )
        assert result == "rendered message"

    def test_resolve_skill_directories(self, tmp_path: Path) -> None:
        """Verify skill paths are resolved via PromptLoader."""
        with patch(
            "app.src.agent.roles.implement.PromptLoader.resolve_skill_paths",
            return_value=["/abs/path/skill.md"],
        ):
            role = _make_role(
                repo_path=tmp_path,
                skill_paths=[".github/skills/skill.md"],
            )
            result = role.resolve_skill_directories()

        assert result == ["/abs/path/skill.md"]


# ===========================================================================
# Execute tests
# ===========================================================================


class TestExecute:
    """Tests for :meth:`ImplementRole.execute`."""

    @pytest.mark.asyncio
    async def test_execute_delegates_to_runner(self) -> None:
        """Verify execute calls runner.run with agent instructions."""
        role = _make_role()
        expected_log = _make_session_log()

        mock_runner = AsyncMock(spec=["run"])
        mock_runner.run = AsyncMock(return_value=expected_log)

        result = await role.execute(mock_runner)

        mock_runner.run.assert_called_once_with(
            "Add a health-check endpoint to the API."
        )
        assert result is expected_log


# ===========================================================================
# PR body building tests
# ===========================================================================


class TestBuildPrBody:
    """Tests for :meth:`ImplementRole._build_pr_body`."""

    def test_pr_body_contains_required_sections(self) -> None:
        """Verify the PR body has all required sections."""
        role = _make_role()
        session_log = _make_session_log()
        changes = {
            "commits": [{"sha": "abc", "message": "test"}],
            "files_changed": ["src/health.py"],
        }
        test_results = {"passed": 5, "failed": 0, "skipped": 1}

        body = role._build_pr_body(session_log, changes, test_results)

        assert "## Summary" in body
        assert "## Changes" in body
        assert "### Files Modified" in body
        assert "## Test Results" in body
        assert "## Agent Instructions" in body
        assert "Copilot Dispatch (run: test-run-001)" in body
        assert "`src/health.py`" in body
        assert "Passed: 5" in body
        assert "Failed: 0" in body
        assert "Skipped: 1" in body

    def test_pr_body_no_files_changed(self) -> None:
        """Verify PR body handles empty files list."""
        role = _make_role()
        session_log = _make_session_log()
        changes = {"commits": [], "files_changed": []}
        test_results = {"passed": 0, "failed": 0, "skipped": 0}

        body = role._build_pr_body(session_log, changes, test_results)

        assert "No files changed" in body


# ===========================================================================
# is_test_command tests
# ===========================================================================


class TestIsTestCommand:
    """Tests for :meth:`ImplementRole._is_test_command`."""

    def test_pytest_detected(self) -> None:
        """Verify pytest command is detected."""
        assert ImplementRole._is_test_command("shell", "pytest -q tests/")

    def test_npm_test_detected(self) -> None:
        """Verify npm test command is detected."""
        assert ImplementRole._is_test_command("run_command", "npm test")

    def test_jest_detected(self) -> None:
        """Verify jest command is detected."""
        assert ImplementRole._is_test_command("shell", "npx jest --verbose")

    def test_go_test_detected(self) -> None:
        """Verify go test command is detected."""
        assert ImplementRole._is_test_command("shell", "go test ./...")

    def test_non_test_command(self) -> None:
        """Verify non-test commands are not detected."""
        assert not ImplementRole._is_test_command("shell", "ls -la")

    def test_make_test_detected(self) -> None:
        """Verify make test command is detected."""
        assert ImplementRole._is_test_command("shell", "make test")


# ===========================================================================
# RoleHandler protocol compliance
# ===========================================================================


class TestRoleHandlerProtocol:
    """Verify ImplementRole satisfies the RoleHandler protocol."""

    def test_implements_role_handler(self) -> None:
        """Verify ImplementRole has the required protocol methods."""
        from app.src.agent.roles import RoleHandler

        role = _make_role()
        assert isinstance(role, RoleHandler)


# ===========================================================================
# Branch reconciliation tests
# ===========================================================================


class TestReconcileBranch:
    """Tests for :meth:`ImplementRole._reconcile_branch`."""

    def test_no_reconciliation_when_on_correct_branch(self) -> None:
        """Verify no action when already on the expected feature branch."""
        role = _make_role()
        calls: list[tuple[str, ...]] = []

        def mock_run_cmd_sync(*args: str) -> str:
            calls.append(args)
            if args[1:3] == ("rev-parse", "--abbrev-ref"):
                return "feature/test-run-001\n"
            return ""

        with patch.object(role, "_run_cmd_sync", side_effect=mock_run_cmd_sync):
            role._reconcile_branch()

        # Should only call rev-parse to check the current branch.
        assert len(calls) == 1
        assert calls[0][1] == "rev-parse"

    def test_reconcile_merges_agent_branch(self) -> None:
        """Verify agent branch is merged into feature branch when diverged."""
        role = _make_role()
        calls: list[tuple[str, ...]] = []

        def mock_run_cmd_sync(*args: str) -> str:
            calls.append(args)
            if args[1:3] == ("rev-parse", "--abbrev-ref"):
                return "feature/agent-created-branch\n"
            if args[1] == "log":
                return "abc123\ndef456\n"
            return ""

        with patch.object(role, "_run_cmd_sync", side_effect=mock_run_cmd_sync):
            role._reconcile_branch()

        # Should checkout feature branch and merge.
        cmd_strs = [" ".join(c) for c in calls]
        assert any("checkout" in c and "feature/test-run-001" in c for c in cmd_strs)
        assert any(
            "merge" in c and "feature/agent-created-branch" in c for c in cmd_strs
        )

    def test_reconcile_no_commits_on_agent_branch_just_switches(self) -> None:
        """Verify we just checkout feature branch when agent branch has no commits."""
        role = _make_role()
        calls: list[tuple[str, ...]] = []

        def mock_run_cmd_sync(*args: str) -> str:
            calls.append(args)
            if args[1:3] == ("rev-parse", "--abbrev-ref"):
                return "some-other-branch\n"
            if args[1] == "log":
                return "\n"  # No commits.
            return ""

        with patch.object(role, "_run_cmd_sync", side_effect=mock_run_cmd_sync):
            role._reconcile_branch()

        cmd_strs = [" ".join(c) for c in calls]
        assert any("checkout" in c and "feature/test-run-001" in c for c in cmd_strs)
        # Should NOT have a `git merge` call (note: "merge" also appears in
        # --no-merges, so match the exact git subcommand).
        assert not any(c[1] == "merge" for c in calls)

    def test_reconcile_handles_detection_failure(self) -> None:
        """Verify graceful handling when branch detection fails."""
        role = _make_role()

        def mock_failing(*args: str) -> str:
            raise AgentExecutionError(
                error_message="git failed",
                error_code="GIT_PRE_PROCESS_ERROR",
            )

        with patch.object(role, "_run_cmd_sync", side_effect=mock_failing):
            # Should not raise — just logs a warning.
            role._reconcile_branch()


class TestCommitPendingChangesWithReconciliation:
    """Tests for branch reconciliation within _commit_pending_changes."""

    def test_commit_pending_changes_calls_reconcile(self) -> None:
        """Verify _commit_pending_changes calls _reconcile_branch."""
        role = _make_role()

        with (
            patch.object(
                role,
                "_run_cmd_sync",
                return_value="",  # No pending changes.
            ),
            patch.object(role, "_reconcile_branch") as mock_reconcile,
        ):
            role._commit_pending_changes()

        mock_reconcile.assert_called_once()
