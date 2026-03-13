"""Unit tests for the review role logic (Component 4.4).

Tests cover:
- :meth:`ReviewRole.pre_process` — PR metadata fetch, branch checkout, diff fetch.
- :meth:`ReviewRole._fetch_pr_diff` — diff retrieval, large diff truncation.
- :meth:`ReviewRole._enrich_instructions` — PR context injection.
- :meth:`ReviewRole._parse_review_output` — JSON format, text fallback, ambiguous input.
- :meth:`ReviewRole._extract_json_review` — JSON block extraction and normalisation.
- :meth:`ReviewRole._normalise_review_data` — field normalisation and validation.
- :meth:`ReviewRole._parse_review_text_fallback` — heuristic text parsing.
- :meth:`ReviewRole._submit_review` — assessment-to-event mapping, API call,
  response parsing, non-fatal error handling.
- :meth:`ReviewRole._approve_pr` — called/not called based on assessment.
- :meth:`ReviewRole.post_process` — complete result dict structure.
- :meth:`ReviewRole.build_system_message` — prompt loader integration.
- :meth:`ReviewRole.resolve_skill_directories` — skill path resolution.
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
from app.src.agent.roles.review import _MAX_DIFF_SIZE, ReviewRole
from app.src.agent.runner import AgentExecutionError, AgentRunner, AgentSessionLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_role(**overrides: Any) -> ReviewRole:
    """Create a ReviewRole with default test parameters.

    Args:
        **overrides: Keyword arguments to override defaults.

    Returns:
        A configured :class:`ReviewRole` instance.
    """
    defaults: dict[str, Any] = {
        "run_id": "test-review-001",
        "repo_path": Path("/tmp/test-repo"),
        "branch": "main",
        "pr_number": 42,
        "agent_instructions": "Review this PR for security and correctness.",
        "model": "claude-sonnet-4-20250514",
        "repository": "owner/test-repo",
    }
    defaults.update(overrides)
    return ReviewRole(**defaults)


def _make_session_log(**overrides: Any) -> AgentSessionLog:
    """Create an AgentSessionLog with default test values.

    Args:
        **overrides: Keyword arguments to override defaults.

    Returns:
        A configured :class:`AgentSessionLog` instance.
    """
    defaults: dict[str, Any] = {
        "messages": [
            {"content": "I reviewed the pull request.", "timestamp": "t1"},
        ],
        "tool_calls": [],
        "errors": [],
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:30:00+00:00",
        "final_message": "I reviewed the pull request.",
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


_PR_METADATA = {
    "headRefName": "feature/add-endpoint",
    "baseRefName": "main",
    "title": "Add health-check endpoint",
    "body": "This PR adds a /health endpoint.",
    "additions": 45,
    "deletions": 3,
    "changedFiles": 2,
}

_PR_METADATA_JSON = json.dumps(_PR_METADATA)

_SAMPLE_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,10 @@\n"
    " from flask import Flask\n"
    "+\n"
    '+@app.route("/health")\n'
    "+def health():\n"
    '+    return {"status": "ok"}\n'
)

_STRUCTURED_REVIEW_JSON = json.dumps(
    {
        "assessment": "approve",
        "summary": "Code is clean and well-tested.",
        "review_comments": [
            {
                "file_path": "app.py",
                "line": 5,
                "body": "Consider adding a docstring.",
            }
        ],
        "suggested_changes": [
            {
                "file_path": "app.py",
                "start_line": 5,
                "end_line": 7,
                "suggested_code": 'def health():\n    """Return health status."""\n    return {"status": "ok"}',
            }
        ],
        "security_concerns": [],
    }
)

_REVIEW_URL_RESPONSE = json.dumps(
    {
        "html_url": "https://github.com/owner/test-repo/pull/42#pullrequestreview-12345",
        "id": 12345,
    }
)


# ===========================================================================
# Protocol compliance
# ===========================================================================


class TestRoleHandlerProtocol:
    """Tests for :class:`RoleHandler` protocol compliance."""

    def test_review_role_satisfies_role_handler_protocol(self) -> None:
        """Verify ReviewRole is a structural subtype of RoleHandler."""
        role = _make_role()
        assert isinstance(role, RoleHandler)


# ===========================================================================
# Pre-process tests
# ===========================================================================


class TestPreProcess:
    """Tests for :meth:`ReviewRole.pre_process`."""

    @pytest.mark.asyncio
    async def test_pre_process_fetches_pr_metadata(self) -> None:
        """Verify ``gh pr view`` is called with correct args and metadata is stored."""
        role = _make_role()
        calls: list[tuple[str, ...]] = []

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            calls.append(args)
            # Return PR metadata JSON for gh pr view.
            if args[0] == "gh" and "view" in args:
                return _make_process_mock(stdout=_PR_METADATA_JSON)
            # Return diff for gh pr diff.
            if args[0] == "gh" and "diff" in args:
                return _make_process_mock(stdout=_SAMPLE_DIFF)
            # Default for git commands.
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            await role.pre_process()

        # Verify gh pr view was called.
        gh_view_calls = [c for c in calls if c[0] == "gh" and "view" in c]
        assert len(gh_view_calls) == 1
        assert "42" in gh_view_calls[0]
        assert (
            "headRefName,baseRefName,title,body,additions,deletions,changedFiles"
            in gh_view_calls[0]
        )

    @pytest.mark.asyncio
    async def test_pre_process_checks_out_pr_branch(self) -> None:
        """Verify ``git checkout`` is called with the PR head branch."""
        role = _make_role()
        calls: list[tuple[str, ...]] = []

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            calls.append(args)
            if args[0] == "gh" and "view" in args:
                return _make_process_mock(stdout=_PR_METADATA_JSON)
            if args[0] == "gh" and "diff" in args:
                return _make_process_mock(stdout=_SAMPLE_DIFF)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            await role.pre_process()

        # Verify git checkout of the PR head branch.
        git_checkout_calls = [c for c in calls if c[0] == "git" and "checkout" in c]
        assert any(
            "feature/add-endpoint" in c for c in git_checkout_calls
        ), f"Expected checkout of PR head branch; got: {git_checkout_calls}"

    @pytest.mark.asyncio
    async def test_pre_process_fetches_base_branch(self) -> None:
        """Verify ``git fetch`` is called for the base branch."""
        role = _make_role()
        calls: list[tuple[str, ...]] = []

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            calls.append(args)
            if args[0] == "gh" and "view" in args:
                return _make_process_mock(stdout=_PR_METADATA_JSON)
            if args[0] == "gh" and "diff" in args:
                return _make_process_mock(stdout=_SAMPLE_DIFF)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            await role.pre_process()

        git_fetch_calls = [c for c in calls if c[0] == "git" and "fetch" in c]
        # Should fetch head and base branches.
        assert len(git_fetch_calls) == 2
        fetched_refs = [c[-1] for c in git_fetch_calls]
        assert "feature/add-endpoint" in fetched_refs
        assert "main" in fetched_refs

    @pytest.mark.asyncio
    async def test_pre_process_missing_head_ref_raises(self) -> None:
        """Verify error when PR metadata has no headRefName."""
        role = _make_role()
        bad_metadata = json.dumps({"baseRefName": "main", "title": "Test"})

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            if args[0] == "gh" and "view" in args:
                return _make_process_mock(stdout=bad_metadata)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role.pre_process()
            assert exc_info.value.error_code == "PR_PRE_PROCESS_ERROR"
            assert "headRefName" in exc_info.value.error_message

    @pytest.mark.asyncio
    async def test_pre_process_gh_failure_raises(self) -> None:
        """Verify AgentExecutionError when ``gh pr view`` fails."""
        role = _make_role()

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            if args[0] == "gh":
                return _make_process_mock(
                    returncode=1,
                    stderr="Could not resolve to a PullRequest",
                )
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            with pytest.raises(AgentExecutionError) as exc_info:
                await role.pre_process()
            assert exc_info.value.error_code == "PR_PRE_PROCESS_ERROR"

    @pytest.mark.asyncio
    async def test_pre_process_uses_repo_path_as_cwd(self) -> None:
        """Verify all commands run in the repo directory."""
        role = _make_role(repo_path=Path("/workspace/my-repo"))
        captured_kwargs: list[dict[str, Any]] = []

        async def mock_subprocess(*args: str, **kwargs: Any) -> AsyncMock:
            captured_kwargs.append(kwargs)
            if args[0] == "gh" and "view" in args:
                return _make_process_mock(stdout=_PR_METADATA_JSON)
            if args[0] == "gh" and "diff" in args:
                return _make_process_mock(stdout=_SAMPLE_DIFF)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_subprocess,
        ):
            await role.pre_process()

        for kw in captured_kwargs:
            assert kw.get("cwd") == "/workspace/my-repo"


# ===========================================================================
# PR diff tests
# ===========================================================================


class TestFetchPrDiff:
    """Tests for :meth:`ReviewRole._fetch_pr_diff`."""

    @pytest.mark.asyncio
    async def test_fetch_pr_diff_returns_diff(self) -> None:
        """Verify ``gh pr diff`` output is returned."""
        role = _make_role()

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            if args[0] == "gh" and "diff" in args:
                return _make_process_mock(stdout=_SAMPLE_DIFF)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            diff = await role._fetch_pr_diff()

        assert "health" in diff
        assert "diff --git" in diff

    @pytest.mark.asyncio
    async def test_fetch_pr_diff_truncates_large_diff(self) -> None:
        """Verify diffs > _MAX_DIFF_SIZE chars are truncated."""
        role = _make_role()
        large_diff = "a" * (_MAX_DIFF_SIZE + 5000)

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            if args[0] == "gh" and "diff" in args:
                return _make_process_mock(stdout=large_diff)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            diff = await role._fetch_pr_diff()

        assert "[diff truncated" in diff
        # The truncated diff (excluding the note) should be within _MAX_DIFF_SIZE.
        assert diff.startswith("a" * 100)

    @pytest.mark.asyncio
    async def test_fetch_pr_diff_no_truncation_for_small_diff(self) -> None:
        """Verify small diffs are returned without modification."""
        role = _make_role()
        small_diff = "small diff content"

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            if args[0] == "gh" and "diff" in args:
                return _make_process_mock(stdout=small_diff)
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            diff = await role._fetch_pr_diff()

        assert diff == small_diff
        assert "[diff truncated" not in diff


# ===========================================================================
# Instruction enrichment tests
# ===========================================================================


class TestEnrichInstructions:
    """Tests for :meth:`ReviewRole._enrich_instructions`."""

    def test_enrich_instructions_includes_pr_context(self) -> None:
        """Verify enriched instructions include PR title, branch, and stats."""
        role = _make_role()
        role._pr_metadata = _PR_METADATA
        role._head_ref = "feature/add-endpoint"
        role._base_ref = "main"
        role._pr_diff = _SAMPLE_DIFF

        enriched = role._enrich_instructions()

        assert "PR #42" in enriched
        assert "Add health-check endpoint" in enriched
        assert "feature/add-endpoint" in enriched
        assert "+45/-3" in enriched
        assert "2 files" in enriched

    def test_enrich_instructions_includes_pr_body(self) -> None:
        """Verify the PR description is included."""
        role = _make_role()
        role._pr_metadata = _PR_METADATA
        role._head_ref = "feature/add-endpoint"
        role._base_ref = "main"
        role._pr_diff = _SAMPLE_DIFF

        enriched = role._enrich_instructions()

        assert "This PR adds a /health endpoint." in enriched

    def test_enrich_instructions_includes_diff(self) -> None:
        """Verify the PR diff is included in a code block."""
        role = _make_role()
        role._pr_metadata = _PR_METADATA
        role._head_ref = "feature/add-endpoint"
        role._base_ref = "main"
        role._pr_diff = _SAMPLE_DIFF

        enriched = role._enrich_instructions()

        assert "```diff" in enriched
        assert "health" in enriched

    def test_enrich_instructions_includes_review_instructions(self) -> None:
        """Verify original agent instructions are included."""
        role = _make_role(agent_instructions="Focus on authentication logic.")
        role._pr_metadata = _PR_METADATA
        role._head_ref = "feature/add-endpoint"
        role._base_ref = "main"
        role._pr_diff = _SAMPLE_DIFF

        enriched = role._enrich_instructions()

        assert "Focus on authentication logic." in enriched

    def test_enrich_instructions_handles_empty_body(self) -> None:
        """Verify enrichment works when PR body is empty."""
        role = _make_role()
        metadata = dict(_PR_METADATA)
        metadata["body"] = ""
        role._pr_metadata = metadata
        role._head_ref = "feature/add-endpoint"
        role._base_ref = "main"
        role._pr_diff = _SAMPLE_DIFF

        enriched = role._enrich_instructions()

        assert "PR #42" in enriched
        assert "PR Description" not in enriched


# ===========================================================================
# Review output parsing tests
# ===========================================================================


class TestParseReviewOutput:
    """Tests for :meth:`ReviewRole._parse_review_output`."""

    def test_parse_review_output_json_format(self) -> None:
        """Verify correctly parsed JSON review block."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": (
                        "Here is my review:\n\n"
                        f"```json\n{_STRUCTURED_REVIEW_JSON}\n```"
                    ),
                    "timestamp": "t1",
                }
            ],
            final_message="Review complete.",
        )

        result = role._parse_review_output(session_log)

        assert result["assessment"] == "approve"
        assert len(result["review_comments"]) == 1
        assert result["review_comments"][0]["file_path"] == "app.py"
        assert result["review_comments"][0]["line"] == 5
        assert "docstring" in result["review_comments"][0]["body"]
        assert len(result["suggested_changes"]) == 1
        assert result["suggested_changes"][0]["start_line"] == 5
        assert result["security_concerns"] == []

    def test_parse_review_output_text_fallback(self) -> None:
        """Verify heuristic parsing with WARNING log when no JSON found."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": (
                        "I approve this PR. The code is clean.\n"
                        "**app.py** (line 5): Add a docstring to the function.\n"
                    ),
                    "timestamp": "t1",
                }
            ],
            final_message="Review complete.",
        )

        with patch("app.src.agent.roles.review.logger") as mock_logger:
            result = role._parse_review_output(session_log)
            mock_logger.warning.assert_called()

        assert result["assessment"] == "approve"
        assert len(result["review_comments"]) >= 1

    def test_parse_review_output_defaults_to_comment(self) -> None:
        """Verify ambiguous output defaults assessment to ``"comment"``."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": "The code looks reasonable. Some minor notes.",
                    "timestamp": "t1",
                }
            ],
            final_message="Review done.",
        )

        result = role._parse_review_output(session_log)

        assert result["assessment"] == "comment"

    def test_parse_review_output_request_changes(self) -> None:
        """Verify request_changes is detected from text."""
        role = _make_role()
        review_json = json.dumps(
            {
                "assessment": "request_changes",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": ["SQL injection risk in query builder."],
            }
        )
        session_log = _make_session_log(
            messages=[
                {
                    "content": f"```json\n{review_json}\n```",
                    "timestamp": "t1",
                }
            ],
        )

        result = role._parse_review_output(session_log)

        assert result["assessment"] == "request_changes"
        assert len(result["security_concerns"]) == 1

    def test_parse_review_output_prefers_last_message(self) -> None:
        """Verify the parser checks messages in reverse order."""
        role = _make_role()
        old_review = json.dumps(
            {
                "assessment": "comment",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
        )
        new_review = json.dumps(
            {
                "assessment": "approve",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
        )
        session_log = _make_session_log(
            messages=[
                {"content": f"```json\n{old_review}\n```", "timestamp": "t1"},
                {"content": f"```json\n{new_review}\n```", "timestamp": "t2"},
            ],
        )

        result = role._parse_review_output(session_log)

        assert result["assessment"] == "approve"


# ===========================================================================
# JSON extraction tests
# ===========================================================================


class TestExtractJsonReview:
    """Tests for :meth:`ReviewRole._extract_json_review`."""

    def test_extracts_valid_json_block(self) -> None:
        """Verify a valid JSON block is extracted and normalised."""
        role = _make_role()
        text = f"Some preamble.\n\n```json\n{_STRUCTURED_REVIEW_JSON}\n```\n\nDone."

        result = role._extract_json_review(text)

        assert result is not None
        assert result["assessment"] == "approve"
        assert len(result["review_comments"]) == 1

    def test_returns_none_for_no_json(self) -> None:
        """Verify None when no JSON block is present."""
        role = _make_role()
        text = "This is just plain text with no JSON."

        result = role._extract_json_review(text)

        assert result is None

    def test_returns_none_for_invalid_json(self) -> None:
        """Verify None when JSON block is invalid."""
        role = _make_role()
        text = '```json\n{"assessment": "approve", invalid}\n```'

        result = role._extract_json_review(text)

        assert result is None

    def test_returns_none_for_json_without_assessment(self) -> None:
        """Verify None when JSON has no assessment field."""
        role = _make_role()
        non_review = json.dumps({"foo": "bar", "baz": 123})
        text = f"```json\n{non_review}\n```"

        result = role._extract_json_review(text)

        assert result is None

    def test_handles_multiple_json_blocks(self) -> None:
        """Verify the first valid review JSON is returned."""
        role = _make_role()
        non_review = json.dumps({"config": True})
        review = json.dumps(
            {
                "assessment": "comment",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
        )
        text = f"```json\n{non_review}\n```\n\n```json\n{review}\n```"

        result = role._extract_json_review(text)

        assert result is not None
        assert result["assessment"] == "comment"


# ===========================================================================
# Normalisation tests
# ===========================================================================


class TestNormaliseReviewData:
    """Tests for :meth:`ReviewRole._normalise_review_data`."""

    def test_normalises_assessment_variants(self) -> None:
        """Verify common assessment string variants are normalised."""
        role = _make_role()
        variants: dict[str, str] = {
            "approve": "approve",
            "approved": "approve",
            "APPROVE": "approve",
            "request_changes": "request_changes",
            "request changes": "request_changes",
            "changes_requested": "request_changes",
            "comment": "comment",
            "unknown": "comment",
        }
        for raw, expected in variants.items():
            data: dict[str, Any] = {
                "assessment": raw,
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
            result = role._normalise_review_data(data)
            assert result["assessment"] == expected, f"Failed for {raw!r}"

    def test_normalises_review_comments(self) -> None:
        """Verify review comment fields are properly extracted."""
        role = _make_role()
        data: dict[str, Any] = {
            "assessment": "comment",
            "review_comments": [
                {"file_path": "a.py", "line": 10, "body": "Fix this."},
                {"body": "General comment."},
            ],
        }

        result = role._normalise_review_data(data)

        assert len(result["review_comments"]) == 2
        assert result["review_comments"][0]["file_path"] == "a.py"
        assert result["review_comments"][0]["line"] == 10
        assert result["review_comments"][1]["file_path"] == ""

    def test_skips_comments_without_body(self) -> None:
        """Verify comments missing the body field are excluded."""
        role = _make_role()
        data: dict[str, Any] = {
            "assessment": "comment",
            "review_comments": [
                {"file_path": "a.py", "line": 10},
                {"file_path": "b.py", "body": "OK"},
            ],
        }

        result = role._normalise_review_data(data)

        assert len(result["review_comments"]) == 1
        assert result["review_comments"][0]["file_path"] == "b.py"

    def test_normalises_suggested_changes(self) -> None:
        """Verify suggested change fields are properly extracted."""
        role = _make_role()
        data: dict[str, Any] = {
            "assessment": "comment",
            "suggested_changes": [
                {
                    "file_path": "a.py",
                    "start_line": 1,
                    "end_line": 5,
                    "suggested_code": "fixed code",
                },
            ],
        }

        result = role._normalise_review_data(data)

        assert len(result["suggested_changes"]) == 1
        assert result["suggested_changes"][0]["suggested_code"] == "fixed code"

    def test_handles_missing_optional_fields(self) -> None:
        """Verify missing list fields default to empty lists."""
        role = _make_role()
        data: dict[str, Any] = {"assessment": "approve"}

        result = role._normalise_review_data(data)

        assert result["review_comments"] == []
        assert result["suggested_changes"] == []
        assert result["security_concerns"] == []


# ===========================================================================
# Text fallback parsing tests
# ===========================================================================


class TestParseReviewTextFallback:
    """Tests for :meth:`ReviewRole._parse_review_text_fallback`."""

    def test_detects_approve(self) -> None:
        """Verify approval is detected from text keywords."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {"content": "I approve this PR, it looks great.", "timestamp": "t1"}
            ]
        )

        result = role._parse_review_text_fallback(session_log)

        assert result["assessment"] == "approve"

    def test_detects_request_changes(self) -> None:
        """Verify request_changes is detected from text keywords."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": "I request changes on this PR due to bugs.",
                    "timestamp": "t1",
                }
            ]
        )

        result = role._parse_review_text_fallback(session_log)

        assert result["assessment"] == "request_changes"

    def test_defaults_to_comment_for_ambiguous_text(self) -> None:
        """Verify assessment defaults to ``"comment"`` for ambiguous text."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {"content": "Some observations about the code.", "timestamp": "t1"}
            ]
        )

        result = role._parse_review_text_fallback(session_log)

        assert result["assessment"] == "comment"

    def test_extracts_file_level_comments(self) -> None:
        """Verify file-level comments are extracted from text patterns."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": (
                        "**utils.py** (line 10): Missing error handling.\n"
                        "**config.py**: Should use environment variables.\n"
                    ),
                    "timestamp": "t1",
                }
            ]
        )

        result = role._parse_review_text_fallback(session_log)

        assert len(result["review_comments"]) == 2
        assert result["review_comments"][0]["file_path"] == "utils.py"
        assert result["review_comments"][0]["line"] == 10
        assert result["review_comments"][1]["file_path"] == "config.py"
        assert result["review_comments"][1]["line"] is None

    def test_extracts_security_concerns(self) -> None:
        """Verify security concerns are extracted from text patterns."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": (
                        "Security concern: User input is not sanitised.\n"
                        "CVE-2024-12345 vulnerability detected.\n"
                    ),
                    "timestamp": "t1",
                }
            ]
        )

        result = role._parse_review_text_fallback(session_log)

        assert len(result["security_concerns"]) >= 1

    def test_approve_blocked_by_critical_keyword(self) -> None:
        """Verify approve is not set if blocking keywords are present."""
        role = _make_role()
        session_log = _make_session_log(
            messages=[
                {
                    "content": (
                        "I would approve but there is a critical bug "
                        "that must fix before merging."
                    ),
                    "timestamp": "t1",
                }
            ]
        )

        result = role._parse_review_text_fallback(session_log)

        # "critical" and "must fix" should block approve.
        assert result["assessment"] != "approve"


# ===========================================================================
# Review submission tests
# ===========================================================================


class TestSubmitReview:
    """Tests for :meth:`ReviewRole._submit_review`."""

    @pytest.mark.asyncio
    async def test_submit_review_maps_assessment_to_event(self) -> None:
        """Verify assessment → event mapping: approve → APPROVE."""
        role = _make_role()
        captured_inputs: list[str] = []

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            process = _make_process_mock(stdout=_REVIEW_URL_RESPONSE)
            # Capture stdin input.
            original_communicate = process.communicate

            async def capture_communicate(
                input: bytes | None = None,
            ) -> tuple[bytes, bytes]:
                if input:
                    captured_inputs.append(input.decode("utf-8"))
                return await original_communicate()

            process.communicate = capture_communicate
            return process

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            review_data: dict[str, Any] = {
                "assessment": "approve",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
            url = await role._submit_review(review_data)

        assert url is not None
        # Verify the API payload contains the APPROVE event.
        assert len(captured_inputs) == 1
        payload = json.loads(captured_inputs[0])
        assert payload["event"] == "APPROVE"

    @pytest.mark.asyncio
    async def test_submit_review_request_changes_event(self) -> None:
        """Verify request_changes → REQUEST_CHANGES mapping."""
        role = _make_role()

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            process = _make_process_mock(stdout=_REVIEW_URL_RESPONSE)

            async def communicate_stub(
                input: bytes | None = None,
            ) -> tuple[bytes, bytes]:
                return (
                    _REVIEW_URL_RESPONSE.encode("utf-8"),
                    b"",
                )

            process.communicate = communicate_stub
            return process

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            review_data: dict[str, Any] = {
                "assessment": "request_changes",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
            url = await role._submit_review(review_data)

        assert url is not None

    @pytest.mark.asyncio
    async def test_submit_review_returns_url(self) -> None:
        """Verify review URL is returned from gh api response."""
        role = _make_role()

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            process = _make_process_mock(stdout=_REVIEW_URL_RESPONSE)

            async def communicate_stub(
                input: bytes | None = None,
            ) -> tuple[bytes, bytes]:
                return (
                    _REVIEW_URL_RESPONSE.encode("utf-8"),
                    b"",
                )

            process.communicate = communicate_stub
            return process

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            review_data: dict[str, Any] = {
                "assessment": "comment",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
            url = await role._submit_review(review_data)

        assert (
            url == "https://github.com/owner/test-repo/pull/42#pullrequestreview-12345"
        )

    @pytest.mark.asyncio
    async def test_submit_review_no_repository_returns_none(self) -> None:
        """Verify None is returned when no repository is set."""
        role = _make_role(repository="")

        review_data: dict[str, Any] = {
            "assessment": "approve",
            "review_comments": [],
            "suggested_changes": [],
            "security_concerns": [],
        }
        url = await role._submit_review(review_data)

        assert url is None

    @pytest.mark.asyncio
    async def test_submit_review_failure_returns_none(self) -> None:
        """Verify non-fatal failure returns None instead of raising."""
        role = _make_role()

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            return _make_process_mock(
                returncode=1,
                stderr="API error: Not found",
            )

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            review_data: dict[str, Any] = {
                "assessment": "approve",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
            url = await role._submit_review(review_data)

        assert url is None

    @pytest.mark.asyncio
    async def test_submit_review_approve_fallback_to_comment(self) -> None:
        """When APPROVE is rejected (e.g. self-review), retry as COMMENT.

        Regression test for the scenario where the authenticated GitHub
        user is the PR author and GitHub rejects APPROVE with HTTP 422.
        The review content must still be posted as a COMMENT.
        """
        role = _make_role()
        call_events: list[str] = []

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            # Intercept stdin to determine which event was submitted.
            process = AsyncMock()

            async def communicate_fn(
                input: bytes | None = None,
            ) -> tuple[bytes, bytes]:
                if input:
                    payload = json.loads(input.decode("utf-8"))
                    call_events.append(payload.get("event", ""))
                    if payload.get("event") == "APPROVE":
                        # First call with APPROVE fails (422).
                        process.returncode = 1
                        return (
                            b"",
                            b"gh: Unprocessable Entity (HTTP 422)",
                        )
                # Second call with COMMENT succeeds.
                process.returncode = 0
                return (
                    _REVIEW_URL_RESPONSE.encode("utf-8"),
                    b"",
                )

            process.communicate = communicate_fn
            process.returncode = 0
            return process

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            review_data: dict[str, Any] = {
                "assessment": "approve",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
            url = await role._submit_review(review_data)

        # Should have attempted APPROVE first, then fallen back to COMMENT.
        assert call_events == ["APPROVE", "COMMENT"]
        assert url is not None

    @pytest.mark.asyncio
    async def test_submit_review_comment_event_no_fallback(self) -> None:
        """When event is already COMMENT and fails, no retry occurs."""
        role = _make_role()
        call_count = 0

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            return _make_process_mock(
                returncode=1,
                stderr="API error: unexpected failure",
            )

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            review_data: dict[str, Any] = {
                "assessment": "comment",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
            url = await role._submit_review(review_data)

        # Only one attempt — no fallback when already COMMENT.
        assert call_count == 1
        assert url is None

    @pytest.mark.asyncio
    async def test_submit_review_request_changes_fallback_to_comment(self) -> None:
        """When REQUEST_CHANGES is rejected, retry as COMMENT."""
        role = _make_role()
        call_events: list[str] = []

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            process = AsyncMock()

            async def communicate_fn(
                input: bytes | None = None,
            ) -> tuple[bytes, bytes]:
                if input:
                    payload = json.loads(input.decode("utf-8"))
                    call_events.append(payload.get("event", ""))
                    if payload.get("event") == "REQUEST_CHANGES":
                        process.returncode = 1
                        return (b"", b"gh: Unprocessable Entity (HTTP 422)")
                process.returncode = 0
                return (
                    _REVIEW_URL_RESPONSE.encode("utf-8"),
                    b"",
                )

            process.communicate = communicate_fn
            process.returncode = 0
            return process

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            review_data: dict[str, Any] = {
                "assessment": "request_changes",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
            url = await role._submit_review(review_data)

        assert call_events == ["REQUEST_CHANGES", "COMMENT"]
        assert url is not None


# ===========================================================================
# Assessment mapping tests
# ===========================================================================


class TestMapAssessmentToEvent:
    """Tests for :meth:`ReviewRole._map_assessment_to_event`."""

    def test_approve_maps_to_approve(self) -> None:
        """Verify ``"approve"`` → ``"APPROVE"``."""
        assert ReviewRole._map_assessment_to_event("approve") == "APPROVE"

    def test_request_changes_maps_to_request_changes(self) -> None:
        """Verify ``"request_changes"`` → ``"REQUEST_CHANGES"``."""
        assert (
            ReviewRole._map_assessment_to_event("request_changes") == "REQUEST_CHANGES"
        )

    def test_comment_maps_to_comment(self) -> None:
        """Verify ``"comment"`` → ``"COMMENT"``."""
        assert ReviewRole._map_assessment_to_event("comment") == "COMMENT"

    def test_unknown_defaults_to_comment(self) -> None:
        """Verify unknown values default to ``"COMMENT"``."""
        assert ReviewRole._map_assessment_to_event("unknown") == "COMMENT"


# ===========================================================================
# PR approval tests
# ===========================================================================


class TestApprovePr:
    """Tests for :meth:`ReviewRole._approve_pr`."""

    @pytest.mark.asyncio
    async def test_approve_pr_success(self) -> None:
        """Verify ``gh pr review --approve`` returns True on success."""
        role = _make_role()

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            return _make_process_mock(stdout="Approved")

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await role._approve_pr()

        assert result is True

    @pytest.mark.asyncio
    async def test_approve_pr_failure_returns_false(self) -> None:
        """Verify False is returned on approval failure."""
        role = _make_role()

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            return _make_process_mock(
                returncode=1,
                stderr="GraphQL error: cannot approve your own PR",
            )

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await role._approve_pr()

        assert result is False

    @pytest.mark.asyncio
    async def test_approve_pr_called_with_correct_args(self) -> None:
        """Verify ``gh pr review`` is called with the correct PR number."""
        role = _make_role(pr_number=99)
        calls: list[tuple[str, ...]] = []

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            calls.append(args)
            return _make_process_mock(stdout="Approved")

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            await role._approve_pr()

        assert len(calls) == 1
        assert calls[0] == ("gh", "pr", "review", "99", "--approve")


# ===========================================================================
# Post-process tests
# ===========================================================================


class TestPostProcess:
    """Tests for :meth:`ReviewRole.post_process`."""

    @pytest.mark.asyncio
    async def test_post_process_returns_review_result_fields(self) -> None:
        """Verify all ReviewResult fields are present in the returned dict."""
        role = _make_role()
        review_json = json.dumps(
            {
                "assessment": "approve",
                "review_comments": [{"file_path": "a.py", "line": 1, "body": "LGTM"}],
                "suggested_changes": [],
                "security_concerns": [],
            }
        )
        session_log = _make_session_log(
            messages=[{"content": f"```json\n{review_json}\n```", "timestamp": "t1"}],
            final_message="Review completed successfully.",
        )

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            # Approval command.
            if args[0] == "gh" and "approve" in args:
                return _make_process_mock(stdout="Approved")
            # Review submission.
            if args[0] == "gh" and "api" in args:
                process = _make_process_mock(stdout=_REVIEW_URL_RESPONSE)

                async def communicate_stub(
                    input: bytes | None = None,
                ) -> tuple[bytes, bytes]:
                    return (
                        _REVIEW_URL_RESPONSE.encode("utf-8"),
                        b"",
                    )

                process.communicate = communicate_stub
                return process
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await role.post_process(session_log)

        # Verify all expected fields.
        assert "review_url" in result
        assert "assessment" in result
        assert "review_comments" in result
        assert "suggested_changes" in result
        assert "security_concerns" in result
        assert "pr_approved" in result
        assert "session_summary" in result
        assert "agent_log_url" in result

        assert result["assessment"] == "approve"
        assert result["pr_approved"] is True
        assert result["session_summary"] == "Review completed successfully."

    @pytest.mark.asyncio
    async def test_post_process_approve_not_called_on_request_changes(self) -> None:
        """Verify approval is skipped when assessment is ``"request_changes"``."""
        role = _make_role()
        review_json = json.dumps(
            {
                "assessment": "request_changes",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
        )
        session_log = _make_session_log(
            messages=[{"content": f"```json\n{review_json}\n```", "timestamp": "t1"}],
        )
        approve_called = False

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            nonlocal approve_called
            if args[0] == "gh" and "--approve" in args:
                approve_called = True
                return _make_process_mock(stdout="Approved")
            if args[0] == "gh" and "api" in args:
                process = _make_process_mock(stdout=_REVIEW_URL_RESPONSE)

                async def communicate_stub(
                    input: bytes | None = None,
                ) -> tuple[bytes, bytes]:
                    return (
                        _REVIEW_URL_RESPONSE.encode("utf-8"),
                        b"",
                    )

                process.communicate = communicate_stub
                return process
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await role.post_process(session_log)

        assert approve_called is False
        assert result["pr_approved"] is False
        assert result["assessment"] == "request_changes"

    @pytest.mark.asyncio
    async def test_post_process_approve_not_called_on_comment(self) -> None:
        """Verify approval is skipped when assessment is ``"comment"``."""
        role = _make_role()
        review_json = json.dumps(
            {
                "assessment": "comment",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
        )
        session_log = _make_session_log(
            messages=[{"content": f"```json\n{review_json}\n```", "timestamp": "t1"}],
        )

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            if args[0] == "gh" and "api" in args:
                process = _make_process_mock(stdout=_REVIEW_URL_RESPONSE)

                async def communicate_stub(
                    input: bytes | None = None,
                ) -> tuple[bytes, bytes]:
                    return (
                        _REVIEW_URL_RESPONSE.encode("utf-8"),
                        b"",
                    )

                process.communicate = communicate_stub
                return process
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await role.post_process(session_log)

        assert result["pr_approved"] is False

    @pytest.mark.asyncio
    async def test_post_process_session_summary_fallback(self) -> None:
        """Verify session_summary uses fallback when final_message is None."""
        role = _make_role()
        review_json = json.dumps(
            {
                "assessment": "comment",
                "review_comments": [],
                "suggested_changes": [],
                "security_concerns": [],
            }
        )
        session_log = _make_session_log(
            messages=[{"content": f"```json\n{review_json}\n```", "timestamp": "t1"}],
            final_message=None,
        )

        async def mock_create_subprocess_exec(*args: str, **kwargs: Any) -> AsyncMock:
            if args[0] == "gh" and "api" in args:
                process = _make_process_mock(stdout=_REVIEW_URL_RESPONSE)

                async def communicate_stub(
                    input: bytes | None = None,
                ) -> tuple[bytes, bytes]:
                    return (
                        _REVIEW_URL_RESPONSE.encode("utf-8"),
                        b"",
                    )

                process.communicate = communicate_stub
                return process
            return _make_process_mock()

        with patch(
            "app.src.agent.roles.review.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await role.post_process(session_log)

        assert result["session_summary"] == "Agent review session completed."


# ===========================================================================
# Execute tests
# ===========================================================================


class TestExecute:
    """Tests for :meth:`ReviewRole.execute`."""

    @pytest.mark.asyncio
    async def test_execute_calls_runner_with_enriched_instructions(self) -> None:
        """Verify the runner receives enriched instructions with PR context."""
        role = _make_role()
        role._pr_metadata = _PR_METADATA
        role._head_ref = "feature/add-endpoint"
        role._base_ref = "main"
        role._pr_diff = _SAMPLE_DIFF

        mock_runner = AsyncMock(spec=AgentRunner)
        mock_session_log = _make_session_log()
        mock_runner.run = AsyncMock(return_value=mock_session_log)

        result = await role.execute(mock_runner)

        mock_runner.run.assert_called_once()
        instructions = mock_runner.run.call_args[0][0]
        assert "PR #42" in instructions
        assert "Add health-check endpoint" in instructions
        assert result is mock_session_log


# ===========================================================================
# System message / skill path helpers tests
# ===========================================================================


class TestBuildSystemMessage:
    """Tests for :meth:`ReviewRole.build_system_message`."""

    def test_build_system_message_uses_review_template(self) -> None:
        """Verify the review template is loaded."""
        role = _make_role(system_instructions="Focus on security.")

        with patch.object(
            PromptLoader,
            "build_system_message",
            return_value="review system message",
        ) as mock_build:
            result = role.build_system_message()

        mock_build.assert_called_once_with(
            role="review",
            system_instructions="Focus on security.",
            repo_path=Path("/tmp/test-repo"),
        )
        assert result == "review system message"


class TestResolveSkillDirectories:
    """Tests for :meth:`ReviewRole.resolve_skill_directories`."""

    def test_resolve_skill_directories_delegates_to_prompt_loader(self) -> None:
        """Verify delegation to PromptLoader.resolve_skill_paths."""
        role = _make_role(skill_paths=[".github/skills/backend.md"])

        with patch.object(
            PromptLoader,
            "resolve_skill_paths",
            return_value=["/tmp/test-repo/.github/skills/backend.md"],
        ) as mock_resolve:
            result = role.resolve_skill_directories()

        mock_resolve.assert_called_once_with(
            [".github/skills/backend.md"], Path("/tmp/test-repo")
        )
        assert result == ["/tmp/test-repo/.github/skills/backend.md"]


# ===========================================================================
# Review body building tests
# ===========================================================================


class TestBuildReviewBody:
    """Tests for :meth:`ReviewRole._build_review_body`."""

    def test_build_review_body_includes_assessment(self) -> None:
        """Verify assessment is included in the review body."""
        role = _make_role()
        review_data: dict[str, Any] = {
            "assessment": "approve",
            "review_comments": [],
            "suggested_changes": [],
            "security_concerns": [],
        }

        body = role._build_review_body(review_data)

        assert "approve" in body
        assert "Copilot Dispatch Code Review" in body

    def test_build_review_body_includes_comments(self) -> None:
        """Verify review comments are formatted in the body."""
        role = _make_role()
        review_data: dict[str, Any] = {
            "assessment": "comment",
            "review_comments": [
                {"file_path": "app.py", "line": 5, "body": "Add docstring."},
                {"file_path": "config.py", "line": None, "body": "Consider env vars."},
            ],
            "suggested_changes": [],
            "security_concerns": [],
        }

        body = role._build_review_body(review_data)

        assert "**app.py** (line 5): Add docstring." in body
        assert "**config.py**: Consider env vars." in body

    def test_build_review_body_includes_security_concerns(self) -> None:
        """Verify security concerns are listed."""
        role = _make_role()
        review_data: dict[str, Any] = {
            "assessment": "request_changes",
            "review_comments": [],
            "suggested_changes": [],
            "security_concerns": ["SQL injection risk."],
        }

        body = role._build_review_body(review_data)

        assert "Security Concerns" in body
        assert "SQL injection risk." in body

    def test_build_review_body_includes_suggested_changes(self) -> None:
        """Verify suggested changes are formatted in the body."""
        role = _make_role()
        review_data: dict[str, Any] = {
            "assessment": "comment",
            "review_comments": [],
            "suggested_changes": [
                {
                    "file_path": "app.py",
                    "start_line": 5,
                    "end_line": 7,
                    "suggested_code": "def health():\n    pass",
                }
            ],
            "security_concerns": [],
        }

        body = role._build_review_body(review_data)

        assert "Suggested Changes" in body
        assert "app.py" in body
        assert "def health():" in body


# Need to import PromptLoader for mocking.
from app.src.agent.prompts import PromptLoader
