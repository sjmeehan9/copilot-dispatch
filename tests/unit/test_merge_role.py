"""Unit tests for the merge role logic (Component 4.5).

Tests cover:
- :meth:`MergeRole.pre_process` — PR metadata fetch, branch fetch, local merge attempt.
- :meth:`MergeRole._attempt_local_merge` — clean merge, conflict detection.
- :meth:`MergeRole._get_conflict_files` — conflict file listing.
- :meth:`MergeRole._check_conflicts_resolved` — marker detection.
- :meth:`MergeRole._build_conflict_context` — context string with markers and logs.
- :meth:`MergeRole._build_clean_merge_instructions` — clean merge instruction enrichment.
- :meth:`MergeRole._build_conflict_merge_instructions` — conflict merge instruction enrichment.
- :meth:`MergeRole.execute` — clean and conflict execution paths.
- :meth:`MergeRole._execute_github_merge` — ``gh pr merge`` command and SHA retrieval.
- :meth:`MergeRole._parse_merge_method` — squash, rebase, default.
- :meth:`MergeRole._parse_test_results` — pytest, jest, go test formats.
- :meth:`MergeRole._compile_conflict_report` — session log scanning.
- :meth:`MergeRole.post_process` — all four outcome variations.
- :meth:`MergeRole.build_system_message` — prompt loader integration.
- :meth:`MergeRole.resolve_skill_directories` — skill path resolution.
- :class:`RoleHandler` protocol compliance.

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

from app.src.agent.roles import RoleHandler
from app.src.agent.roles.merge import (
    _MAX_FILE_CONTEXT_SIZE,
    MergeRole,
)
from app.src.agent.runner import AgentExecutionError, AgentRunner, AgentSessionLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_role(**overrides: Any) -> MergeRole:
    """Create a MergeRole with default test parameters.

    Args:
        **overrides: Keyword arguments to override defaults.

    Returns:
        A configured :class:`MergeRole` instance.
    """
    defaults: dict[str, Any] = {
        "run_id": "test-merge-001",
        "repo_path": Path("/tmp/test-repo"),
        "branch": "main",
        "pr_number": 42,
        "agent_instructions": "Merge this PR safely.",
        "model": "claude-sonnet-4-20250514",
        "repository": "owner/test-repo",
    }
    defaults.update(overrides)
    return MergeRole(**defaults)


def _make_session_log(**overrides: Any) -> AgentSessionLog:
    """Create an AgentSessionLog with default test values.

    Args:
        **overrides: Keyword arguments to override defaults.

    Returns:
        A configured :class:`AgentSessionLog` instance.
    """
    defaults: dict[str, Any] = {
        "messages": [
            {"content": "Merge analysis complete.", "timestamp": "t1"},
        ],
        "tool_calls": [],
        "errors": [],
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:30:00+00:00",
        "final_message": "Merge analysis complete.",
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


# ===================================================================
# Protocol compliance
# ===================================================================


class TestRoleHandlerProtocol:
    """Verify MergeRole satisfies the RoleHandler protocol."""

    def test_merge_role_implements_role_handler(self) -> None:
        """MergeRole is an instance of the RoleHandler protocol."""
        role = _make_role()
        assert isinstance(role, RoleHandler)

    def test_merge_role_has_pre_process(self) -> None:
        """MergeRole has a pre_process method."""
        role = _make_role()
        assert hasattr(role, "pre_process")
        assert callable(role.pre_process)

    def test_merge_role_has_execute(self) -> None:
        """MergeRole has an execute method."""
        role = _make_role()
        assert hasattr(role, "execute")
        assert callable(role.execute)

    def test_merge_role_has_post_process(self) -> None:
        """MergeRole has a post_process method."""
        role = _make_role()
        assert hasattr(role, "post_process")
        assert callable(role.post_process)


# ===================================================================
# Initialisation
# ===================================================================


class TestMergeRoleInit:
    """Test MergeRole constructor and attribute storage."""

    def test_default_merge_method(self) -> None:
        """Default merge method is 'merge'."""
        role = _make_role()
        assert role._merge_method == "merge"

    def test_squash_merge_method(self) -> None:
        """Merge method is 'squash' when system_instructions contains 'squash'."""
        role = _make_role(system_instructions="Use squash merge for this PR.")
        assert role._merge_method == "squash"

    def test_rebase_merge_method(self) -> None:
        """Merge method is 'rebase' when system_instructions contains 'rebase'."""
        role = _make_role(system_instructions="Please use rebase merge strategy.")
        assert role._merge_method == "rebase"

    def test_squash_takes_precedence_over_rebase(self) -> None:
        """If both squash and rebase appear, squash wins (checked first)."""
        role = _make_role(system_instructions="Use squash or rebase merge strategy.")
        assert role._merge_method == "squash"

    def test_attributes_stored(self) -> None:
        """Constructor stores all provided attributes."""
        role = _make_role(
            run_id="run-x",
            repo_path=Path("/work/repo"),
            branch="develop",
            pr_number=99,
            agent_instructions="Do something.",
            model="gpt-5",
            timeout_minutes=15,
            repository="org/repo",
        )
        assert role._run_id == "run-x"
        assert role._repo_path == Path("/work/repo")
        assert role._branch == "develop"
        assert role._pr_number == 99
        assert role._agent_instructions == "Do something."
        assert role._model == "gpt-5"
        assert role._timeout_minutes == 15
        assert role._repository == "org/repo"


# ===================================================================
# Parse merge method
# ===================================================================


class TestParseMergeMethod:
    """Test _parse_merge_method for various input scenarios."""

    def test_none_system_instructions(self) -> None:
        """Returns 'merge' when system_instructions is None."""
        role = _make_role(system_instructions=None)
        assert role._parse_merge_method() == "merge"

    def test_empty_system_instructions(self) -> None:
        """Returns 'merge' when system_instructions is empty."""
        role = _make_role(system_instructions="")
        assert role._parse_merge_method() == "merge"

    def test_squash_keyword(self) -> None:
        """Returns 'squash' when 'squash' is in system_instructions."""
        role = _make_role(system_instructions="Always squash commits.")
        assert role._parse_merge_method() == "squash"

    def test_rebase_keyword(self) -> None:
        """Returns 'rebase' when 'rebase' is in system_instructions."""
        role = _make_role(system_instructions="Use rebase merge.")
        assert role._parse_merge_method() == "rebase"

    def test_no_merge_keywords(self) -> None:
        """Returns 'merge' when no merge keywords present."""
        role = _make_role(system_instructions="Follow coding standards.")
        assert role._parse_merge_method() == "merge"


# ===================================================================
# PR metadata fetch
# ===================================================================


class TestFetchPRMetadata:
    """Test _fetch_pr_metadata for PR metadata retrieval."""

    @pytest.mark.asyncio
    async def test_fetches_pr_metadata(self) -> None:
        """Fetches and parses PR metadata from gh CLI."""
        role = _make_role()
        metadata_json = json.dumps(
            {"headRefName": "feature/test", "baseRefName": "main"}
        )
        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            return_value=metadata_json,
        ) as mock_cmd:
            result = await role._fetch_pr_metadata()

        assert result["headRefName"] == "feature/test"
        assert result["baseRefName"] == "main"
        mock_cmd.assert_called_once_with(
            "gh", "pr", "view", "42", "--json", "headRefName,baseRefName"
        )

    @pytest.mark.asyncio
    async def test_raises_on_invalid_json(self) -> None:
        """Raises AgentExecutionError on invalid JSON."""
        role = _make_role()
        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            return_value="not json",
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role._fetch_pr_metadata()

        assert exc_info.value.error_code == "MERGE_PRE_PROCESS_ERROR"


# ===================================================================
# Local merge attempt
# ===================================================================


class TestAttemptLocalMerge:
    """Test _attempt_local_merge for clean and conflict scenarios."""

    def test_clean_merge(self) -> None:
        """Returns clean=True on successful merge."""
        role = _make_role()
        role._head_ref = "feature/test"
        role._base_ref = "main"

        with patch.object(role, "_run_cmd_sync", return_value=""):
            result = role._attempt_local_merge()

        assert result["clean"] is True
        assert result["conflict_files"] == []

    def test_conflict_merge(self) -> None:
        """Returns clean=False with conflict files on merge failure."""
        role = _make_role()
        role._head_ref = "feature/test"
        role._base_ref = "main"

        call_count = 0

        def mock_run_cmd_sync(*args: str) -> str:
            """Simulate merge failure then conflict file listing then abort."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # git merge — fails with conflicts.
                raise AgentExecutionError(
                    error_message="Merge failed",
                    error_code="MERGE_PRE_PROCESS_ERROR",
                )
            if call_count == 2:
                # git diff --name-only --diff-filter=U — list conflicts.
                return "src/main.py\nREADME.md\n"
            if call_count == 3:
                # git merge --abort.
                return ""
            return ""

        with patch.object(role, "_run_cmd_sync", side_effect=mock_run_cmd_sync):
            result = role._attempt_local_merge()

        assert result["clean"] is False
        assert result["conflict_files"] == ["src/main.py", "README.md"]

    def test_non_conflict_failure_reraises(self) -> None:
        """Re-raises when merge fails but no conflict files are found."""
        role = _make_role()
        role._head_ref = "feature/test"
        role._base_ref = "main"

        call_count = 0

        def mock_run_cmd_sync(*args: str) -> str:
            """Simulate merge failure with no conflict files."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AgentExecutionError(
                    error_message="Merge failed",
                    error_code="MERGE_PRE_PROCESS_ERROR",
                )
            if call_count == 2:
                # No conflict files.
                return ""
            return ""

        with patch.object(role, "_run_cmd_sync", side_effect=mock_run_cmd_sync):
            with pytest.raises(AgentExecutionError):
                role._attempt_local_merge()


# ===================================================================
# Get conflict files
# ===================================================================


class TestGetConflictFiles:
    """Test _get_conflict_files for conflict file listing."""

    def test_returns_conflict_files(self) -> None:
        """Parses git diff output for conflicted files."""
        role = _make_role()
        with patch.object(role, "_run_cmd_sync", return_value="a.py\nb.py\nc.py\n"):
            files = role._get_conflict_files()

        assert files == ["a.py", "b.py", "c.py"]

    def test_returns_empty_on_failure(self) -> None:
        """Returns empty list and logs warning on command failure."""
        role = _make_role()
        with patch.object(
            role,
            "_run_cmd_sync",
            side_effect=AgentExecutionError(
                error_message="fail", error_code="MERGE_PRE_PROCESS_ERROR"
            ),
        ):
            files = role._get_conflict_files()

        assert files == []

    def test_returns_empty_for_empty_output(self) -> None:
        """Returns empty list for empty output."""
        role = _make_role()
        with patch.object(role, "_run_cmd_sync", return_value=""):
            files = role._get_conflict_files()

        assert files == []


# ===================================================================
# Abort local merge
# ===================================================================


class TestAbortLocalMerge:
    """Test _abort_local_merge for cleanup behaviour."""

    def test_abort_succeeds(self) -> None:
        """Abort runs git merge --abort."""
        role = _make_role()
        with patch.object(role, "_run_cmd_sync", return_value="") as mock_cmd:
            role._abort_local_merge()

        mock_cmd.assert_called_once_with("git", "merge", "--abort")

    def test_abort_failure_does_not_raise(self) -> None:
        """Abort failure is logged but not raised."""
        role = _make_role()
        with patch.object(
            role,
            "_run_cmd_sync",
            side_effect=AgentExecutionError(
                error_message="fail", error_code="MERGE_PRE_PROCESS_ERROR"
            ),
        ):
            # Should not raise.
            role._abort_local_merge()


# ===================================================================
# Check conflicts resolved
# ===================================================================


class TestCheckConflictsResolved:
    """Test _check_conflicts_resolved for marker detection."""

    def test_no_markers(self) -> None:
        """Returns True when git diff --check exits 0."""
        role = _make_role()
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            assert role._check_conflicts_resolved() is True

    def test_markers_present(self) -> None:
        """Returns False when git diff --check exits non-zero."""
        role = _make_role()
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=2, stdout="", stderr=""),
        ):
            assert role._check_conflicts_resolved() is False

    def test_exception_defaults_to_false(self) -> None:
        """Returns False when subprocess raises."""
        role = _make_role()
        with patch("subprocess.run", side_effect=OSError("fail")):
            assert role._check_conflicts_resolved() is False


# ===================================================================
# Build conflict context
# ===================================================================


class TestBuildConflictContext:
    """Test _build_conflict_context for context string building."""

    def test_includes_file_and_markers(self, tmp_path: Path) -> None:
        """Context includes file path and file contents with markers."""
        role = _make_role(repo_path=tmp_path)
        role._base_ref = "main"
        role._head_ref = "feature/test"

        conflict_content = (
            "line 1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\nline 2\n"
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text(conflict_content)

        with patch.object(role, "_get_recent_log", return_value="abc1234 Fix bug"):
            context = role._build_conflict_context(["src/app.py"])

        assert "src/app.py" in context
        assert "<<<<<<< HEAD" in context
        assert "ours" in context
        assert "theirs" in context
        assert "abc1234 Fix bug" in context

    def test_truncates_large_files(self, tmp_path: Path) -> None:
        """Large file contents are truncated."""
        role = _make_role(repo_path=tmp_path)
        role._base_ref = "main"
        role._head_ref = "feature/test"

        large_content = "x" * (_MAX_FILE_CONTEXT_SIZE + 1000)
        (tmp_path / "big.py").write_text(large_content)

        with patch.object(role, "_get_recent_log", return_value=""):
            context = role._build_conflict_context(["big.py"])

        assert "truncated" in context

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        """Gracefully handles files that cannot be read."""
        role = _make_role(repo_path=tmp_path)
        role._base_ref = "main"
        role._head_ref = "feature/test"

        with patch.object(role, "_get_recent_log", return_value=""):
            context = role._build_conflict_context(["nonexistent.py"])

        assert "Could not read file" in context


# ===================================================================
# Instruction building
# ===================================================================


class TestBuildInstructions:
    """Test clean and conflict instruction building."""

    def test_clean_merge_instructions(self) -> None:
        """Clean merge instructions include test-only directive."""
        role = _make_role()
        role._head_ref = "feature/test"
        role._base_ref = "main"

        instructions = role._build_clean_merge_instructions()

        assert "clean" in instructions.lower()
        assert "Do NOT modify any files" in instructions
        assert "Run the full test suite" in instructions
        assert "PR #42" in instructions

    def test_clean_merge_includes_additional_context(self) -> None:
        """Clean merge instructions include agent_instructions."""
        role = _make_role(agent_instructions="Check performance too.")
        role._head_ref = "feature/test"
        role._base_ref = "main"

        instructions = role._build_clean_merge_instructions()

        assert "Check performance too." in instructions
        assert "Additional Context" in instructions

    def test_conflict_merge_instructions(self) -> None:
        """Conflict merge instructions include conflict resolution steps."""
        role = _make_role()
        role._head_ref = "feature/test"
        role._base_ref = "main"
        role._merge_attempt = {"clean": False, "conflict_files": ["a.py"]}

        with patch.object(
            role, "_build_conflict_context", return_value="## Conflicts\nSome context"
        ):
            instructions = role._build_conflict_merge_instructions()

        assert "conflicts" in instructions.lower()
        assert "Resolve the merge conflicts" in instructions
        assert "NOT confident" in instructions

    def test_conflict_instructions_include_additional_context(self) -> None:
        """Conflict instructions include agent_instructions."""
        role = _make_role(agent_instructions="Be careful with config.")
        role._head_ref = "feature/test"
        role._base_ref = "main"
        role._merge_attempt = {"clean": False, "conflict_files": ["a.py"]}

        with patch.object(role, "_build_conflict_context", return_value="context"):
            instructions = role._build_conflict_merge_instructions()

        assert "Be careful with config." in instructions

    def test_clean_merge_empty_instructions(self) -> None:
        """Clean merge with empty instructions omits Additional Context."""
        role = _make_role(agent_instructions="")
        role._head_ref = "feature/test"
        role._base_ref = "main"

        instructions = role._build_clean_merge_instructions()

        assert "Additional Context" not in instructions


# ===================================================================
# Execute
# ===================================================================


class TestExecute:
    """Test execute method for clean and conflict paths."""

    @pytest.mark.asyncio
    async def test_execute_clean_merge(self) -> None:
        """Clean merge delegates to runner with clean instructions."""
        role = _make_role()
        role._merge_attempt = {"clean": True, "conflict_files": []}
        role._head_ref = "feature/test"
        role._base_ref = "main"

        runner = AsyncMock(spec=AgentRunner)
        mock_log = _make_session_log()
        runner.run = AsyncMock(return_value=mock_log)

        result = await role.execute(runner)

        assert result is mock_log
        runner.run.assert_called_once()
        call_args = runner.run.call_args[0][0]
        assert "clean" in call_args.lower()

    @pytest.mark.asyncio
    async def test_execute_conflict_merge(self) -> None:
        """Conflict merge re-applies merge and delegates with conflict instructions."""
        role = _make_role()
        role._merge_attempt = {"clean": False, "conflict_files": ["a.py"]}
        role._head_ref = "feature/test"
        role._base_ref = "main"

        runner = AsyncMock(spec=AgentRunner)
        mock_log = _make_session_log()
        runner.run = AsyncMock(return_value=mock_log)

        with patch.object(
            role, "_reapply_merge_with_conflicts", new_callable=AsyncMock
        ) as mock_reapply:
            with patch.object(
                role,
                "_build_conflict_context",
                return_value="conflict context",
            ):
                result = await role.execute(runner)

        assert result is mock_log
        mock_reapply.assert_called_once()
        runner.run.assert_called_once()
        call_args = runner.run.call_args[0][0]
        assert "conflicts" in call_args.lower()


# ===================================================================
# Execute GitHub merge
# ===================================================================


class TestExecuteGitHubMerge:
    """Test _execute_github_merge for gh CLI merge command."""

    @pytest.mark.asyncio
    async def test_merge_calls_gh(self) -> None:
        """Calls gh pr merge with correct method."""
        role = _make_role()
        role._merge_method = "squash"

        call_count = 0

        async def mock_run(*args: str) -> str:
            """Handle both merge and view calls."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # gh pr merge.
                return "Merged!"
            # gh pr view for SHA.
            return json.dumps({"mergeCommit": {"oid": "abc123def456"}})

        with patch.object(role, "_run_cmd_async", side_effect=mock_run):
            sha = await role._execute_github_merge()

        assert sha == "abc123def456"

    @pytest.mark.asyncio
    async def test_merge_default_method(self) -> None:
        """Default merge method is 'merge'."""
        role = _make_role()
        assert role._merge_method == "merge"

        calls: list[tuple[str, ...]] = []

        async def mock_run(*args: str) -> str:
            """Capture calls."""
            calls.append(args)
            if "merge" in args and "view" not in args:
                return "Merged!"
            return json.dumps({"mergeCommit": {"oid": "sha1"}})

        with patch.object(role, "_run_cmd_async", side_effect=mock_run):
            await role._execute_github_merge()

        # First call is the merge command.
        assert "--merge" in calls[0]

    @pytest.mark.asyncio
    async def test_merge_failure_raises(self) -> None:
        """Raises AgentExecutionError on merge failure."""
        role = _make_role()

        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            side_effect=AgentExecutionError(
                error_message="Merge failed",
                error_code="MERGE_EXECUTION_ERROR",
            ),
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role._execute_github_merge()

        assert exc_info.value.error_code == "MERGE_EXECUTION_ERROR"

    @pytest.mark.asyncio
    async def test_merge_sha_fallback_on_view_failure(self) -> None:
        """Returns empty string if SHA retrieval fails."""
        role = _make_role()

        call_count = 0

        async def mock_run(*args: str) -> str:
            """Merge succeeds, view fails."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "Merged!"
            raise AgentExecutionError(
                error_message="View failed",
                error_code="MERGE_PRE_PROCESS_ERROR",
            )

        with patch.object(role, "_run_cmd_async", side_effect=mock_run):
            sha = await role._execute_github_merge()

        assert sha == ""


# ===================================================================
# Test result parsing
# ===================================================================


class TestParseTestResults:
    """Test _parse_test_results for various test runner formats."""

    def test_pytest_format(self) -> None:
        """Parses pytest output: '5 passed, 2 failed, 1 skipped'."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest",
                    "result": "5 passed, 2 failed, 1 skipped",
                }
            ]
        )
        result = role._parse_test_results(session_log)
        assert result == {"passed": 5, "failed": 2, "skipped": 1}

    def test_jest_format(self) -> None:
        """Parses jest output: 'Tests: 3 passed, 1 failed, 4 total'."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "npx jest",
                    "result": "Tests:       3 passed, 1 failed, 4 total",
                }
            ]
        )
        result = role._parse_test_results(session_log)
        assert result["passed"] == 3
        assert result["failed"] == 1

    def test_go_test_format(self) -> None:
        """Parses go test output."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "go test ./...",
                    "result": "--- PASS: TestA (0.00s)\n--- PASS: TestB (0.01s)\n--- FAIL: TestC (0.00s)",
                }
            ]
        )
        result = role._parse_test_results(session_log)
        assert result["passed"] == 2
        assert result["failed"] == 1

    def test_no_test_output(self) -> None:
        """Returns zeros and logs warning when no test output detected."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "ls -la",
                    "result": "total 48",
                }
            ]
        )
        result = role._parse_test_results(session_log)
        assert result == {"passed": 0, "failed": 0, "skipped": 0}

    def test_empty_tool_calls(self) -> None:
        """Returns zeros for empty tool calls."""
        role = _make_role()
        session_log = _make_session_log(tool_calls=[])
        result = role._parse_test_results(session_log)
        assert result == {"passed": 0, "failed": 0, "skipped": 0}

    def test_long_pytest_output_not_truncated(self) -> None:
        """Parses test counts from full (non-truncated) pytest output.

        Regression test: previously, tool results were truncated to 200 chars
        before storage, causing the summary line to be lost.
        """
        role = _make_role()
        long_output = (
            "collecting ...\n"
            "tests/test_calculator.py::TestAdd::test_add_positive PASSED [  4%]\n"
            "tests/test_calculator.py::TestAdd::test_add_negative PASSED [  8%]\n"
            "tests/test_calculator.py::TestAdd::test_add_zero PASSED [ 12%]\n"
            "tests/test_calculator.py::TestSub::test_sub_positive PASSED [ 16%]\n"
            "tests/test_calculator.py::TestSub::test_sub_negative PASSED [ 20%]\n"
            "\n"
            "========================= 26 passed in 2.34s =========================\n"
        )
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest",
                    "result": long_output,
                }
            ]
        )
        result = role._parse_test_results(session_log)
        assert result["passed"] == 26
        assert result["failed"] == 0

    def test_fallback_to_assistant_messages(self) -> None:
        """Falls back to assistant messages when tool calls have no test output.

        Simulates the scenario where the agent ran tests but the tool call
        results don't contain parseable output. Test results should be
        extracted from the assistant's messages instead.
        """
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "ls -la",
                    "result": "total 48",
                }
            ],
            messages=[
                {"content": "All 26 Python tests pass.", "timestamp": "t1"},
                {
                    "content": (
                        "## Test Results\n"
                        "| Suite | Tests | Result |\n"
                        "| Python (pytest) | **26 passed** | pass |\n"
                        "| JavaScript (jest) | **17 passed** | pass |\n"
                        "| **Total** | **43 passed, 0 failed, 0 skipped** | pass |"
                    ),
                    "timestamp": "t2",
                },
            ],
            final_message="Merge complete.",
        )
        result = role._parse_test_results(session_log)
        assert result["passed"] > 0
        assert result["failed"] == 0

    def test_fallback_not_used_when_tool_output_found(self) -> None:
        """Fallback is NOT invoked when tool call output is parseable."""
        role = _make_role()
        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest",
                    "result": "5 passed, 1 failed",
                }
            ],
            messages=[
                {
                    "content": "100 passed, 0 failed",
                    "timestamp": "t1",
                }
            ],
        )
        result = role._parse_test_results(session_log)
        # Should use tool call values, not message values.
        assert result["passed"] == 5
        assert result["failed"] == 1


# ===================================================================
# Parse test results from messages (fallback)
# ===================================================================


class TestParseTestResultsFromMessages:
    """Test _parse_test_results_from_messages fallback parser."""

    def test_pytest_counts_in_messages(self) -> None:
        """Extracts pytest-style counts from assistant messages."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {"content": "26 passed, 0 failed, 0 skipped in 2.3s", "timestamp": "t1"}
            ]
        )
        result = role._parse_test_results_from_messages(session_log)
        assert result["passed"] == 26
        assert result["failed"] == 0
        assert result["skipped"] == 0

    def test_jest_counts_in_messages(self) -> None:
        """Extracts jest-style counts from assistant messages."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[{"content": "Tests: 17 passed, 0 failed", "timestamp": "t1"}]
        )
        result = role._parse_test_results_from_messages(session_log)
        assert result["passed"] >= 17
        assert result["failed"] == 0

    def test_no_results_in_messages(self) -> None:
        """Returns zeros when messages contain no test results."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[{"content": "I'm working on the merge.", "timestamp": "t1"}]
        )
        result = role._parse_test_results_from_messages(session_log)
        assert result == {"passed": 0, "failed": 0, "skipped": 0}

    def test_empty_messages(self) -> None:
        """Returns zeros for empty messages."""
        role = _make_role()
        session_log = _make_session_log(messages=[])
        result = role._parse_test_results_from_messages(session_log)
        assert result == {"passed": 0, "failed": 0, "skipped": 0}


# ===================================================================
# Is test command
# ===================================================================


class TestIsTestCommand:
    """Test _is_test_command static method."""

    def test_pytest_detected(self) -> None:
        """Detects pytest commands."""
        assert MergeRole._is_test_command("shell", "pytest -v")

    def test_npm_test_detected(self) -> None:
        """Detects npm test commands."""
        assert MergeRole._is_test_command("run_command", "npm test")

    def test_go_test_detected(self) -> None:
        """Detects go test commands."""
        assert MergeRole._is_test_command("shell", "go test ./...")

    def test_non_test_command(self) -> None:
        """Returns False for non-test commands."""
        assert not MergeRole._is_test_command("shell", "ls -la")


# ===================================================================
# Compile conflict report
# ===================================================================


class TestCompileConflictReport:
    """Test _compile_conflict_report for session log scanning."""

    def test_basic_report(self) -> None:
        """Produces report entries for each conflict file."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": (
                        "Analysis:\n"
                        "**src/app.py**: Both sides modified the import section. "
                        "The base added logging, the PR added requests.\n\n"
                        "**README.md**: Documentation conflict in the setup section.\n"
                    ),
                    "timestamp": "t1",
                }
            ]
        )

        report = role._compile_conflict_report(session_log, ["src/app.py", "README.md"])

        assert len(report) == 2
        assert report[0]["file_path"] == "src/app.py"
        assert report[1]["file_path"] == "README.md"
        # All entries have required keys.
        for entry in report:
            assert "description" in entry
            assert "suggested_resolution" in entry

    def test_empty_session_log(self) -> None:
        """Still produces entries (with defaults) for empty session log."""
        role = _make_role()
        session_log = _make_session_log(messages=[])

        report = role._compile_conflict_report(session_log, ["a.py"])

        assert len(report) == 1
        assert report[0]["file_path"] == "a.py"
        assert "Conflict in a.py" in report[0]["description"]
        assert "No resolution suggested" in report[0]["suggested_resolution"]

    def test_report_with_code_suggestion(self) -> None:
        """Extracts code block as suggested resolution."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": (
                        "For src/app.py resolution:\n"
                        "```python\nimport logging\nimport requests\n```\n"
                    ),
                    "timestamp": "t1",
                }
            ]
        )

        report = role._compile_conflict_report(session_log, ["src/app.py"])

        assert len(report) == 1
        assert "import logging" in report[0]["suggested_resolution"]


# ===================================================================
# Pre-process
# ===================================================================


class TestPreProcess:
    """Test pre_process end-to-end flow."""

    @pytest.mark.asyncio
    async def test_pre_process_success(self) -> None:
        """Pre-process fetches metadata, branches, and attempts merge."""
        role = _make_role()

        metadata = {"headRefName": "feature/xyz", "baseRefName": "main"}

        with patch.object(
            role,
            "_fetch_pr_metadata",
            new_callable=AsyncMock,
            return_value=metadata,
        ):
            with patch.object(
                role, "_run_git", new_callable=AsyncMock, return_value=""
            ) as mock_git:
                with patch.object(
                    role,
                    "_attempt_local_merge",
                    return_value={"clean": True, "conflict_files": []},
                ):
                    await role.pre_process()

        assert role._head_ref == "feature/xyz"
        assert role._base_ref == "main"
        assert role._merge_attempt["clean"] is True

        # Verify git commands: config name, config email, fetch head,
        # fetch base, checkout, pull.
        assert mock_git.call_count == 6

    @pytest.mark.asyncio
    async def test_pre_process_missing_head_ref(self) -> None:
        """Raises AgentExecutionError when headRefName is missing."""
        role = _make_role()

        metadata = {"baseRefName": "main"}  # No headRefName.

        with patch.object(
            role,
            "_fetch_pr_metadata",
            new_callable=AsyncMock,
            return_value=metadata,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role.pre_process()

        assert exc_info.value.error_code == "MERGE_PRE_PROCESS_ERROR"


# ===================================================================
# Post-process — all four outcome paths
# ===================================================================


class TestPostProcessCleanMergeTestsPass:
    """Test post_process: clean merge + tests pass → merge executed."""

    @pytest.mark.asyncio
    async def test_clean_merge_tests_pass(self) -> None:
        """Executes merge and returns 'merged' status."""
        role = _make_role()
        role._merge_attempt = {"clean": True, "conflict_files": []}
        role._head_ref = "feature/test"
        role._base_ref = "main"

        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest",
                    "result": "5 passed, 0 failed",
                }
            ],
            final_message="All tests passed.",
        )

        with patch.object(role, "_abort_local_merge"):
            with patch.object(
                role,
                "_execute_github_merge",
                new_callable=AsyncMock,
                return_value="abc123",
            ):
                result = await role.post_process(session_log)

        assert result["merge_status"] == "merged"
        assert result["merge_sha"] == "abc123"
        assert result["conflict_files"] == []
        assert result["test_results"]["passed"] == 5
        assert result["test_results"]["failed"] == 0


class TestPostProcessCleanMergeTestsFail:
    """Test post_process: clean merge + tests fail → no merge."""

    @pytest.mark.asyncio
    async def test_clean_merge_tests_fail(self) -> None:
        """Does NOT merge and returns 'conflicts_unresolved' status."""
        role = _make_role()
        role._merge_attempt = {"clean": True, "conflict_files": []}
        role._head_ref = "feature/test"
        role._base_ref = "main"

        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest",
                    "result": "3 passed, 2 failed",
                }
            ],
            final_message="Tests failed.",
        )

        with patch.object(role, "_abort_local_merge"):
            result = await role.post_process(session_log)

        assert result["merge_status"] == "conflicts_unresolved"
        assert result["merge_sha"] is None
        assert result["test_results"]["failed"] == 2


class TestPostProcessConflictResolved:
    """Test post_process: conflicts + resolved + tests pass → merge."""

    @pytest.mark.asyncio
    async def test_conflict_resolved_tests_pass(self) -> None:
        """Commits, pushes, and merges when conflicts resolved and tests pass."""
        role = _make_role()
        role._merge_attempt = {
            "clean": False,
            "conflict_files": ["src/app.py"],
        }
        role._head_ref = "feature/test"
        role._base_ref = "main"

        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest",
                    "result": "10 passed, 0 failed",
                }
            ],
            final_message="Conflicts resolved successfully.",
        )

        with patch.object(role, "_check_conflicts_resolved", return_value=True):
            with patch.object(
                role, "_commit_conflict_resolution", new_callable=AsyncMock
            ):
                with patch.object(role, "_push_resolution", new_callable=AsyncMock):
                    with patch.object(
                        role,
                        "_execute_github_merge",
                        new_callable=AsyncMock,
                        return_value="def456",
                    ):
                        result = await role.post_process(session_log)

        assert result["merge_status"] == "conflicts_resolved_and_merged"
        assert result["merge_sha"] == "def456"
        assert result["conflict_files"] == ["src/app.py"]


class TestPostProcessConflictUnresolved:
    """Test post_process: conflicts + unresolved → conflict report."""

    @pytest.mark.asyncio
    async def test_conflict_unresolved(self) -> None:
        """Returns conflict report when conflicts remain."""
        role = _make_role()
        role._merge_attempt = {
            "clean": False,
            "conflict_files": ["src/app.py", "README.md"],
        }
        role._head_ref = "feature/test"
        role._base_ref = "main"

        session_log = _make_session_log(
            messages=[
                {
                    "content": "Could not resolve all conflicts confidently.",
                    "timestamp": "t1",
                }
            ],
            final_message="Could not resolve all conflicts confidently.",
        )

        with patch.object(role, "_check_conflicts_resolved", return_value=False):
            result = await role.post_process(session_log)

        assert result["merge_status"] == "conflicts_unresolved"
        assert result["merge_sha"] is None
        assert result["conflict_files"] == ["src/app.py", "README.md"]
        assert isinstance(result["conflict_resolutions"], list)
        assert len(result["conflict_resolutions"]) == 2

    @pytest.mark.asyncio
    async def test_conflict_resolved_but_tests_fail(self) -> None:
        """Returns conflict report when resolution passes but tests fail."""
        role = _make_role()
        role._merge_attempt = {
            "clean": False,
            "conflict_files": ["src/app.py"],
        }
        role._head_ref = "feature/test"
        role._base_ref = "main"

        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest",
                    "result": "3 passed, 1 failed",
                }
            ],
        )

        # Conflicts resolved but tests fail.
        with patch.object(role, "_check_conflicts_resolved", return_value=True):
            result = await role.post_process(session_log)

        assert result["merge_status"] == "conflicts_unresolved"
        assert result["merge_sha"] is None
        assert result["test_results"]["failed"] == 1


# ===================================================================
# Post-process result structure
# ===================================================================


class TestPostProcessResultStructure:
    """Verify post_process returns all MergeResult fields."""

    @pytest.mark.asyncio
    async def test_merged_result_has_all_fields(self) -> None:
        """Merged outcome includes all expected fields."""
        role = _make_role()
        role._merge_attempt = {"clean": True, "conflict_files": []}
        role._head_ref = "feature/test"
        role._base_ref = "main"

        session_log = _make_session_log(
            tool_calls=[
                {
                    "name": "shell",
                    "arguments": "pytest",
                    "result": "1 passed",
                }
            ],
        )

        with patch.object(role, "_abort_local_merge"):
            with patch.object(
                role,
                "_execute_github_merge",
                new_callable=AsyncMock,
                return_value="sha1",
            ):
                result = await role.post_process(session_log)

        required_keys = {
            "merge_status",
            "merge_sha",
            "conflict_files",
            "conflict_resolutions",
            "test_results",
            "session_summary",
            "agent_log_url",
        }
        assert required_keys.issubset(result.keys())

    @pytest.mark.asyncio
    async def test_unresolved_result_has_all_fields(self) -> None:
        """Unresolved outcome includes all expected fields."""
        role = _make_role()
        role._merge_attempt = {"clean": True, "conflict_files": []}
        role._head_ref = "feature/test"
        role._base_ref = "main"

        session_log = _make_session_log()

        with patch.object(role, "_abort_local_merge"):
            result = await role.post_process(session_log)

        required_keys = {
            "merge_status",
            "merge_sha",
            "conflict_files",
            "conflict_resolutions",
            "test_results",
            "session_summary",
            "agent_log_url",
        }
        assert required_keys.issubset(result.keys())


# ===================================================================
# System message and skill path helpers
# ===================================================================


class TestSystemMessageAndSkills:
    """Test build_system_message and resolve_skill_directories."""

    def test_build_system_message(self) -> None:
        """Delegates to PromptLoader for merge role."""
        role = _make_role()
        with patch("app.src.agent.roles.merge.PromptLoader") as mock_loader_cls:
            mock_loader = MagicMock()
            mock_loader.build_system_message.return_value = "merged prompt"
            mock_loader_cls.return_value = mock_loader

            result = role.build_system_message()

        assert result == "merged prompt"
        mock_loader.build_system_message.assert_called_once_with(
            role="merge",
            system_instructions=None,
            repo_path=Path("/tmp/test-repo"),
        )

    def test_resolve_skill_directories(self) -> None:
        """Delegates to PromptLoader.resolve_skill_paths."""
        role = _make_role(skill_paths=[".github/skills/backend.md"])
        with patch("app.src.agent.roles.merge.PromptLoader") as mock_loader_cls:
            mock_loader_cls.resolve_skill_paths.return_value = [
                "/tmp/test-repo/.github/skills/backend.md"
            ]

            result = role.resolve_skill_directories()

        assert result == ["/tmp/test-repo/.github/skills/backend.md"]


# ===================================================================
# Command execution helpers
# ===================================================================


class TestCommandHelpers:
    """Test _run_git, _run_cmd_async, and _run_cmd_sync."""

    @pytest.mark.asyncio
    async def test_run_git_delegates_to_run_cmd_async(self) -> None:
        """_run_git prepends 'git' and delegates."""
        role = _make_role()
        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            return_value="output",
        ) as mock_cmd:
            result = await role._run_git("status")

        assert result == "output"
        mock_cmd.assert_called_once_with("git", "status")

    @pytest.mark.asyncio
    async def test_run_cmd_async_success(self) -> None:
        """_run_cmd_async returns stdout on success."""
        role = _make_role()
        process = _make_process_mock(stdout="hello world")

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ):
            result = await role._run_cmd_async("echo", "hello")

        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_run_cmd_async_failure(self) -> None:
        """_run_cmd_async raises AgentExecutionError on non-zero exit."""
        role = _make_role()
        process = _make_process_mock(returncode=1, stderr="error occurred")

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role._run_cmd_async("git", "checkout", "bad-branch")

        assert "error occurred" in str(exc_info.value.error_details)

    @pytest.mark.asyncio
    async def test_run_cmd_async_merge_error_code(self) -> None:
        """gh pr merge failure uses MERGE_EXECUTION_ERROR code."""
        role = _make_role()
        process = _make_process_mock(returncode=1, stderr="merge failed")

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role._run_cmd_async("gh", "pr", "merge", "42", "--merge")

        assert exc_info.value.error_code == "MERGE_EXECUTION_ERROR"

    @pytest.mark.asyncio
    async def test_run_cmd_async_push_error_code(self) -> None:
        """git push failure uses GIT_PUSH_ERROR code."""
        role = _make_role()
        process = _make_process_mock(returncode=1, stderr="push rejected")

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role._run_cmd_async("git", "push", "origin", "HEAD:main")

        assert exc_info.value.error_code == "GIT_PUSH_ERROR"

    def test_run_cmd_sync_success(self) -> None:
        """_run_cmd_sync returns stdout on success."""
        role = _make_role()
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="ok", stderr=""),
        ):
            result = role._run_cmd_sync("git", "status")

        assert result == "ok"

    def test_run_cmd_sync_failure(self) -> None:
        """_run_cmd_sync raises AgentExecutionError on failure."""
        role = _make_role()
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=128, stdout="", stderr="fatal error"),
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                role._run_cmd_sync("git", "merge", "--no-commit")

        assert exc_info.value.error_code == "MERGE_PRE_PROCESS_ERROR"


# ===================================================================
# Reapply merge with conflicts
# ===================================================================


class TestReapplyMerge:
    """Test _reapply_merge_with_conflicts."""

    @pytest.mark.asyncio
    async def test_reapply_merge_runs_git_merge(self) -> None:
        """Re-applies merge without raising on conflict exit code."""
        role = _make_role()
        role._head_ref = "feature/test"

        process = _make_process_mock(returncode=1, stderr="CONFLICT")

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ):
            # Should not raise even though returncode=1.
            await role._reapply_merge_with_conflicts()

    @pytest.mark.asyncio
    async def test_reapply_merge_handles_exception(self) -> None:
        """Exception during reapply is logged but not raised."""
        role = _make_role()
        role._head_ref = "feature/test"

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=OSError("fail"),
        ):
            await role._reapply_merge_with_conflicts()


# ===================================================================
# Commit and push resolution
# ===================================================================


class TestCommitAndPush:
    """Test _commit_conflict_resolution and _push_resolution."""

    @pytest.mark.asyncio
    async def test_commit_resolution(self) -> None:
        """Stages and commits with descriptive message."""
        role = _make_role()

        calls: list[tuple[str, ...]] = []

        async def mock_run_git(*args: str) -> str:
            """Capture git calls."""
            calls.append(args)
            return ""

        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            return_value="M src/app.py\n",
        ) as mock_cmd:
            with patch.object(role, "_run_git", side_effect=mock_run_git):
                await role._commit_conflict_resolution()

        mock_cmd.assert_awaited_once_with("git", "status", "--porcelain")
        assert len(calls) == 2
        assert calls[0] == ("add", "--all")
        assert "commit" in calls[1][0]
        assert "PR #42" in calls[1][2]

    @pytest.mark.asyncio
    async def test_commit_conflict_resolution_no_changes_raises_error(self) -> None:
        """Raises a descriptive error when the working tree is clean."""
        role = _make_role()
        role._merge_attempt = {
            "clean": False,
            "conflict_files": ["src/app.py", "README.md"],
        }

        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            return_value="",
        ) as mock_cmd:
            with patch.object(role, "_run_git", new_callable=AsyncMock) as mock_git:
                with pytest.raises(AgentExecutionError) as exc_info:
                    await role._commit_conflict_resolution()

        mock_cmd.assert_awaited_once_with("git", "status", "--porcelain")
        mock_git.assert_not_called()
        assert exc_info.value.error_code == "NO_CHANGES_TO_COMMIT"
        assert exc_info.value.error_details == {
            "run_id": "test-merge-001",
            "pr_number": 42,
            "conflict_files": ["src/app.py", "README.md"],
        }

    @pytest.mark.asyncio
    async def test_push_resolution(self) -> None:
        """Pushes to head ref."""
        role = _make_role()
        role._head_ref = "feature/test"

        with patch.object(
            role, "_run_git", new_callable=AsyncMock, return_value=""
        ) as mock_git:
            await role._push_resolution()

        mock_git.assert_called_once_with("push", "origin", "HEAD:feature/test")

    @pytest.mark.asyncio
    async def test_push_resolution_failure(self) -> None:
        """Raises AgentExecutionError on push failure."""
        role = _make_role()
        role._head_ref = "feature/test"

        with patch.object(
            role,
            "_run_git",
            new_callable=AsyncMock,
            side_effect=AgentExecutionError(
                error_message="Push failed", error_code="GIT_PUSH_ERROR"
            ),
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role._push_resolution()

        assert exc_info.value.error_code == "GIT_PUSH_ERROR"


# ===================================================================
# Get merge SHA
# ===================================================================


class TestGetMergeSHA:
    """Test _get_merge_sha for SHA retrieval."""

    @pytest.mark.asyncio
    async def test_returns_sha(self) -> None:
        """Returns merge commit SHA from gh pr view."""
        role = _make_role()
        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            return_value=json.dumps({"mergeCommit": {"oid": "sha123"}}),
        ):
            sha = await role._get_merge_sha()

        assert sha == "sha123"

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self) -> None:
        """Returns empty string when SHA retrieval fails."""
        role = _make_role()
        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            side_effect=Exception("fail"),
        ):
            sha = await role._get_merge_sha()

        assert sha == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_missing_merge_commit(self) -> None:
        """Returns empty string when mergeCommit is null."""
        role = _make_role()
        with patch.object(
            role,
            "_run_cmd_async",
            new_callable=AsyncMock,
            return_value=json.dumps({"mergeCommit": None}),
        ):
            sha = await role._get_merge_sha()

        assert sha == ""
