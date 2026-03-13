"""Implement role logic — branch creation, agent execution, PR creation.

Orchestrates the complete implement role workflow:

1. **Pre-processing** — create a feature branch from the specified base branch,
   configure git identity.
2. **Execution** — run the Copilot SDK agent with the implement system prompt
   and caller's instructions.
3. **Post-processing** — push the feature branch, create a PR (or draft PR on
   test failures), collect commits, changed files, and test results.

Example::

    role = ImplementRole(
        run_id="abc-123",
        repo_path=Path("/workspace/target-repo"),
        branch="main",
        agent_instructions="Add a health-check endpoint.",
        model="claude-sonnet-4-20250514",
    )
    await role.pre_process()
    session_log = await role.execute(runner)
    result = await role.post_process(session_log)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.src.agent.prompts import PromptLoader
from app.src.agent.runner import AgentExecutionError, AgentRunner, AgentSessionLog

logger = logging.getLogger("dispatch.agent.roles.implement")

# ---------------------------------------------------------------------------
# Git identity defaults
# ---------------------------------------------------------------------------
_BOT_NAME = "Copilot Dispatch Bot"
_BOT_EMAIL = "copilot-dispatch@noreply.github.com"

# Maximum PR title length.
_MAX_TITLE_LENGTH = 72

# Separator used in ``git log --format`` to split SHA from message.
_GIT_LOG_SEPARATOR = "|||"


class ImplementRole:
    """Implements the full implement role lifecycle.

    Creates a feature branch, invokes the Copilot SDK agent to make code
    changes, then pushes the branch and creates a pull request. If the
    agent's test results indicate failures, the PR is created as a draft
    with failure details in the description.

    Attributes:
        run_id: Unique identifier for the agent run.
        repo_path: Absolute path to the checked-out target repository.
        branch: Base branch to create the feature branch from.
        agent_instructions: Natural-language instructions for the agent.
        model: LLM model identifier.
        system_instructions: Optional caller-supplied system instructions.
        skill_paths: Optional list of relative skill file paths.
        timeout_minutes: Maximum session duration in minutes.
    """

    def __init__(
        self,
        run_id: str,
        repo_path: Path,
        branch: str,
        agent_instructions: str,
        model: str,
        system_instructions: str | None = None,
        skill_paths: list[str] | None = None,
        agent_paths: list[str] | None = None,
        timeout_minutes: int = 30,
    ) -> None:
        """Initialise the ImplementRole.

        Args:
            run_id: Unique identifier for the agent run.
            repo_path: Absolute path to the checked-out target repository.
            branch: Base branch to create the feature branch from.
            agent_instructions: Natural-language instructions for the agent.
            model: LLM model identifier (e.g., ``"claude-sonnet-4-20250514"``).
            system_instructions: Optional caller-supplied system-level
                instructions to merge into the prompt.
            skill_paths: Optional list of skill file paths relative to the
                repo root, resolved to absolute paths for the SDK.
            agent_paths: Optional list of custom agent definition file paths
                relative to the repo root.
            timeout_minutes: Maximum session duration in minutes before
                forced termination. Defaults to ``30``.
        """
        self._run_id = run_id
        self._repo_path = repo_path
        self._branch = branch
        self._agent_instructions = agent_instructions
        self._model = model
        self._system_instructions = system_instructions
        self._skill_paths = skill_paths
        self._agent_paths = agent_paths
        self._timeout_minutes = timeout_minutes
        self._feature_branch = f"feature/{run_id}"

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    async def pre_process(self) -> None:
        """Prepare the repository for the implement agent run.

        Configures git identity, checks out the base branch, pulls the
        latest changes, and creates a new feature branch.

        Raises:
            AgentExecutionError: If any git command fails, with
                ``error_code="GIT_PRE_PROCESS_ERROR"`` and the command's
                stderr in ``error_details``.
        """
        logger.info(
            "Starting pre-processing for run %s: branch=%s, feature=%s",
            self._run_id,
            self._branch,
            self._feature_branch,
            extra={"run_id": self._run_id},
        )

        # 1. Configure git identity.
        await self._run_git("config", "user.name", _BOT_NAME)
        await self._run_git("config", "user.email", _BOT_EMAIL)

        # 2. Checkout and pull the base branch.
        await self._run_git("checkout", self._branch)
        await self._run_git("pull", "origin", self._branch)

        # 3. Create and checkout the feature branch.
        await self._run_git("checkout", "-b", self._feature_branch)

        logger.info(
            "Pre-processing complete for run %s — on branch %s",
            self._run_id,
            self._feature_branch,
            extra={"run_id": self._run_id},
        )

    async def execute(self, runner: AgentRunner) -> AgentSessionLog:
        """Run the Copilot SDK agent with the implement system prompt.

        Builds the system message from the implement template, injects
        ``system_instructions`` and repo-level instructions, resolves skill
        paths, and delegates to the :class:`AgentRunner`.

        Args:
            runner: A configured :class:`AgentRunner` instance. Note: the
                runner is provided externally so that creation parameters
                (system_message, skill_directories, etc.) can be configured
                by the caller or orchestrator.

        Returns:
            The :class:`AgentSessionLog` produced by the agent session.
        """
        logger.info(
            "Executing implement role agent for run %s",
            self._run_id,
            extra={"run_id": self._run_id},
        )
        return await runner.run(self._agent_instructions)

    async def post_process(self, session_log: AgentSessionLog) -> dict[str, Any]:
        """Push the branch, create a PR, and collect result data.

        After agent execution:
        1. Collects commits and changed files from git.
        2. Parses test results from the session log.
        3. Determines whether the PR should be created as a draft.
        4. Pushes the feature branch to the remote.
        5. Creates the pull request.
        6. Returns structured data for ``ImplementResult`` construction.

        Args:
            session_log: The session log from the completed agent run.

        Returns:
            A dictionary with all fields needed for
            :class:`~app.src.api.models.ImplementResult`:
            ``pr_url``, ``pr_number``, ``branch``, ``commits``,
            ``files_changed``, ``test_results``, ``security_findings``,
            ``session_summary``, ``agent_log_url``.
        """
        logger.info(
            "Starting post-processing for run %s",
            self._run_id,
            extra={"run_id": self._run_id},
        )

        # 0. Commit pending changes if the agent forgot.
        self._commit_pending_changes()

        # 1. Collect changes.
        changes = self._collect_changes()

        # Guard: If no changes were made, fail gracefully to avoid gh pr create crash.
        if not changes["commits"]:
            raise AgentExecutionError(
                error_message="Agent session completed but no commits were created.",
                error_code="NO_COMMITS_CREATED",
                error_details={"run_id": self._run_id},
            )

        # 2. Parse test results.
        test_results = self._parse_test_results(session_log)

        # 3. Determine if PR should be draft.
        is_draft = test_results["failed"] > 0 or session_log.timed_out

        # 4. Push branch to remote.
        await self._push_branch()

        # 5. Create the PR.
        pr_info = await self._create_pr(
            session_log=session_log,
            changes=changes,
            test_results=test_results,
            is_draft=is_draft,
        )

        # 6. Parse security findings.
        security_findings = self._parse_security_findings(session_log)

        result: dict[str, Any] = {
            "pr_url": pr_info["pr_url"],
            "pr_number": pr_info["pr_number"],
            "branch": self._feature_branch,
            "commits": changes["commits"],
            "files_changed": changes["files_changed"],
            "test_results": test_results,
            "security_findings": security_findings,
            "session_summary": (
                session_log.final_message or "Agent session completed."
            ),
            "agent_log_url": None,
        }

        logger.info(
            "Post-processing complete for run %s — PR #%s created (%s)",
            self._run_id,
            pr_info["pr_number"],
            "draft" if is_draft else "ready",
            extra={"run_id": self._run_id, "pr_number": pr_info["pr_number"]},
        )
        return result

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    def _commit_pending_changes(self) -> None:
        """Commit any pending file modifications and reconcile branches.

        After the agent session, the workspace may be on a different branch
        than the expected feature branch (e.g., if a custom agent created
        its own branch).  This method:

        1. Commits any uncommitted changes on the current branch.
        2. Detects whether the current branch differs from
           ``self._feature_branch``.
        3. If it does, merges the agent's branch into the feature branch so
           that ``_collect_changes`` finds the commits.
        """
        # 1. Commit pending changes on whatever branch the agent is on.
        try:
            status_output = self._run_cmd_sync("git", "status", "--porcelain")
            if status_output.strip():
                logger.info(
                    "Committing uncommitted changes for run %s",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
                self._run_cmd_sync("git", "add", "-A")
                self._run_cmd_sync(
                    "git", "commit", "-m", "Agent implementation changes"
                )
        except Exception as exc:
            logger.warning(
                "Failed to commit pending changes for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )

        # 2. Detect the current branch and reconcile if needed.
        self._reconcile_branch()

    def _reconcile_branch(self) -> None:
        """Ensure commits end up on the expected feature branch.

        If the agent checked out or created a different branch during the
        session, this method merges that branch's commits into the feature
        branch so that ``_collect_changes`` and the subsequent push/PR
        creation operate on the correct ref.

        The merge is expected to be a fast-forward in most cases because
        the feature branch was the starting point for whatever branch the
        agent created.
        """
        try:
            current_branch = self._run_cmd_sync(
                "git", "rev-parse", "--abbrev-ref", "HEAD"
            ).strip()
        except Exception as exc:
            logger.warning(
                "Failed to detect current branch for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )
            return

        if current_branch == self._feature_branch:
            return  # Already on the expected branch — nothing to reconcile.

        logger.warning(
            "Agent switched to branch '%s' (expected '%s') for run %s — "
            "reconciling commits onto feature branch",
            current_branch,
            self._feature_branch,
            self._run_id,
            extra={"run_id": self._run_id},
        )

        try:
            # Check whether the agent's branch has commits relative to base.
            log_output = self._run_cmd_sync(
                "git",
                "log",
                f"{self._branch}..{current_branch}",
                "--format=%H",
                "--no-merges",
            )
            if not log_output.strip():
                # No commits on the agent's branch either — just switch back.
                self._run_cmd_sync("git", "checkout", self._feature_branch)
                return

            # Switch to feature branch and merge the agent's branch.
            self._run_cmd_sync("git", "checkout", self._feature_branch)
            self._run_cmd_sync(
                "git", "merge", current_branch, "--no-edit", "--no-verify"
            )
            logger.info(
                "Merged agent branch '%s' into '%s' for run %s",
                current_branch,
                self._feature_branch,
                self._run_id,
                extra={"run_id": self._run_id},
            )
        except Exception as exc:
            logger.warning(
                "Failed to reconcile branch '%s' into '%s' for run %s: %s",
                current_branch,
                self._feature_branch,
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )

    async def _push_branch(self) -> None:
        """Push the feature branch to the remote.

        Raises:
            AgentExecutionError: On push failure, with
                ``error_code="GIT_PUSH_ERROR"``.
        """
        try:
            await self._run_git("push", "origin", self._feature_branch)
            logger.info(
                "Pushed branch %s for run %s",
                self._feature_branch,
                self._run_id,
                extra={"run_id": self._run_id},
            )
        except AgentExecutionError:
            raise
        except Exception as exc:
            raise AgentExecutionError(
                error_message=f"Failed to push branch {self._feature_branch}: {exc}",
                error_code="GIT_PUSH_ERROR",
                error_details={
                    "branch": self._feature_branch,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc

    def _collect_changes(self) -> dict[str, Any]:
        """Collect commits and changed files from git.

        Runs ``git log`` to get commits between the base and feature branch,
        and ``git diff --name-only`` to list changed files.

        Returns:
            A dictionary with keys:
            - ``commits``: list of dicts with ``sha`` and ``message`` keys.
            - ``files_changed``: list of changed file path strings.
        """
        # Get commits.
        try:
            log_result = self._run_cmd_sync(
                "git",
                "log",
                f"{self._branch}..{self._feature_branch}",
                f"--format=%H{_GIT_LOG_SEPARATOR}%s",
                "--no-merges",
            )
            commits: list[dict[str, str]] = []
            if log_result.strip():
                for line in log_result.strip().split("\n"):
                    if _GIT_LOG_SEPARATOR in line:
                        sha, message = line.split(_GIT_LOG_SEPARATOR, 1)
                        commits.append({"sha": sha.strip(), "message": message.strip()})
        except Exception as exc:
            logger.warning(
                "Failed to collect commits for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )
            commits = []

        # Get changed files.
        try:
            diff_result = self._run_cmd_sync(
                "git",
                "diff",
                "--name-only",
                f"{self._branch}...{self._feature_branch}",
            )
            files_changed: list[str] = [
                f.strip() for f in diff_result.strip().split("\n") if f.strip()
            ]
        except Exception as exc:
            logger.warning(
                "Failed to collect changed files for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )
            files_changed = []

        logger.info(
            "Collected changes for run %s: %d commits, %d files changed",
            self._run_id,
            len(commits),
            len(files_changed),
            extra={"run_id": self._run_id},
        )
        return {"commits": commits, "files_changed": files_changed}

    # ------------------------------------------------------------------
    # PR creation
    # ------------------------------------------------------------------

    async def _create_pr(
        self,
        session_log: AgentSessionLog,
        changes: dict[str, Any],
        test_results: dict[str, int],
        is_draft: bool,
    ) -> dict[str, Any]:
        """Create a pull request via ``gh pr create``.

        Generates a title from the agent instructions and builds a Markdown
        description with change summary, test results, and original
        instructions.

        Args:
            session_log: The session log from the agent run.
            changes: Collected commits and changed files.
            test_results: Parsed test result counts.
            is_draft: Whether to create the PR as a draft.

        Returns:
            A dictionary with ``pr_url`` and ``pr_number`` keys.

        Raises:
            AgentExecutionError: On PR creation failure, with
                ``error_code="PR_CREATE_ERROR"``.
        """
        title = self._generate_pr_title()
        body = self._build_pr_body(session_log, changes, test_results)

        # Build gh pr create command args.
        cmd_args = [
            "gh",
            "pr",
            "create",
            "--base",
            self._branch,
            "--head",
            self._feature_branch,
            "--title",
            title,
            "--body",
            body,
        ]
        if is_draft:
            cmd_args.append("--draft")

        try:
            create_output = await self._run_cmd_async(*cmd_args)
            logger.info(
                "PR created for run %s: %s",
                self._run_id,
                create_output.strip(),
                extra={"run_id": self._run_id},
            )
        except AgentExecutionError:
            raise
        except Exception as exc:
            raise AgentExecutionError(
                error_message=f"Failed to create PR: {exc}",
                error_code="PR_CREATE_ERROR",
                error_details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc

        # Retrieve PR number and URL via gh pr view.
        try:
            view_output = await self._run_cmd_async(
                "gh",
                "pr",
                "view",
                self._feature_branch,
                "--json",
                "number,url",
            )
            pr_data = json.loads(view_output.strip())
            return {
                "pr_url": pr_data.get("url", create_output.strip()),
                "pr_number": pr_data.get("number", 0),
            }
        except Exception as exc:
            logger.warning(
                "Failed to retrieve PR details for run %s, using create output: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )
            # Fallback: extract URL from create output and default number to 0.
            return {
                "pr_url": create_output.strip(),
                "pr_number": 0,
            }

    def _generate_pr_title(self) -> str:
        """Generate a concise PR title from the agent instructions.

        Extracts the first sentence (up to the first period, newline, or
        ``_MAX_TITLE_LENGTH`` chars), falling back to
        ``"Dispatch: {run_id}"`` if the instructions are empty or
        consist only of whitespace.

        Returns:
            A string no longer than ``_MAX_TITLE_LENGTH`` characters.
        """
        text = self._agent_instructions.strip()
        if not text:
            return f"Dispatch: {self._run_id}"

        # Take up to the first sentence-ending punctuation or newline.
        match = re.match(r"^(.+?)[.\n]", text)
        title = match.group(1).strip() if match else text

        if len(title) > _MAX_TITLE_LENGTH:
            # Truncate at the last word boundary before the limit.
            title = title[: _MAX_TITLE_LENGTH - 3].rsplit(" ", 1)[0] + "..."

        return title or f"Dispatch: {self._run_id}"

    def _build_pr_body(
        self,
        session_log: AgentSessionLog,
        changes: dict[str, Any],
        test_results: dict[str, int],
    ) -> str:
        """Build the PR description in Markdown format.

        Args:
            session_log: The session log from the agent run.
            changes: Collected commits and changed files.
            test_results: Parsed test result counts.

        Returns:
            A Markdown-formatted PR body string.
        """
        summary = session_log.final_message or "Agent session completed."
        files_list = (
            "\n".join(f"- `{f}`" for f in changes.get("files_changed", []))
            or "- No files changed"
        )

        body = f"""## Summary
{summary}

## Changes
- **Files changed**: {len(changes.get("files_changed", []))}
- **Commits**: {len(changes.get("commits", []))}

### Files Modified
{files_list}

## Test Results
- Passed: {test_results["passed"]}
- Failed: {test_results["failed"]}
- Skipped: {test_results["skipped"]}

## Agent Instructions
> {self._agent_instructions}

---
*Created by Copilot Dispatch (run: {self._run_id})*"""
        return body

    # ------------------------------------------------------------------
    # Test result parsing
    # ------------------------------------------------------------------

    def _parse_test_results(self, session_log: AgentSessionLog) -> dict[str, int]:
        """Parse test execution results from the agent session log.

        Scans tool call results in the session log for output from common
        test runners (pytest, jest, npm test, go test). Uses regex patterns
        to extract pass/fail/skip counts.

        Args:
            session_log: The session log containing tool call events.

        Returns:
            A dictionary with ``passed``, ``failed``, and ``skipped`` counts.
            Defaults to all zeros if no test output is detected.
        """
        passed = 0
        failed = 0
        skipped = 0
        found_test_output = False

        for tool_call in session_log.tool_calls:
            result_text = str(tool_call.get("result", "") or "")
            arguments_text = str(tool_call.get("arguments", "") or "")

            # Only examine tool calls that look like test execution.
            if not self._is_test_command(tool_call.get("name", ""), arguments_text):
                continue

            # --- jest / vitest format (check first — more specific) ---
            # "Tests:       3 passed, 1 failed, 4 total"
            # "Tests: 5 passed, 5 total"
            jest_match = re.search(
                r"^Tests:\s+.*?(\d+)\s+passed",
                result_text,
                re.IGNORECASE | re.MULTILINE,
            )
            if jest_match:
                passed += int(jest_match.group(1))
                found_test_output = True

                jest_failed = re.search(
                    r"^Tests:\s+.*?(\d+)\s+failed",
                    result_text,
                    re.IGNORECASE | re.MULTILINE,
                )
                if jest_failed:
                    failed += int(jest_failed.group(1))

                jest_skipped = re.search(
                    r"^Tests:\s+.*?(\d+)\s+skipped",
                    result_text,
                    re.IGNORECASE | re.MULTILINE,
                )
                if jest_skipped:
                    skipped += int(jest_skipped.group(1))
            else:
                # --- pytest format (generic — only if jest not matched) ---
                # "5 passed, 2 failed, 1 skipped"
                # "3 passed"
                # "1 failed"
                pytest_match = re.search(r"(\d+)\s+passed", result_text, re.IGNORECASE)
                if pytest_match:
                    passed += int(pytest_match.group(1))
                    found_test_output = True

                pytest_failed = re.search(r"(\d+)\s+failed", result_text, re.IGNORECASE)
                if pytest_failed:
                    failed += int(pytest_failed.group(1))
                    found_test_output = True

                pytest_skipped = re.search(
                    r"(\d+)\s+skipped", result_text, re.IGNORECASE
                )
                if pytest_skipped:
                    skipped += int(pytest_skipped.group(1))
                    found_test_output = True

            # --- go test format ---
            # "ok   package 0.123s" (pass)
            # "FAIL package 0.456s" (fail)
            # "--- PASS: TestName (0.00s)"
            # "--- FAIL: TestName (0.00s)"
            go_pass_count = len(re.findall(r"---\s+PASS:", result_text))
            go_fail_count = len(re.findall(r"---\s+FAIL:", result_text))
            if go_pass_count or go_fail_count:
                passed += go_pass_count
                failed += go_fail_count
                found_test_output = True

            go_skip_count = len(re.findall(r"---\s+SKIP:", result_text))
            if go_skip_count:
                skipped += go_skip_count

        if not found_test_output:
            logger.warning(
                "No test results detected in session log for run %s",
                self._run_id,
                extra={"run_id": self._run_id},
            )

        return {"passed": passed, "failed": failed, "skipped": skipped}

    @staticmethod
    def _is_test_command(tool_name: str, arguments: str) -> bool:
        """Determine whether a tool call looks like a test execution command.

        Args:
            tool_name: Name of the tool (e.g., ``"shell"``, ``"run_command"``).
            arguments: Stringified arguments of the tool call.

        Returns:
            ``True`` if the tool call appears to invoke a test runner.
        """
        test_patterns = [
            "pytest",
            "npm test",
            "npm run test",
            "npx jest",
            "jest",
            "vitest",
            "go test",
            "cargo test",
            "make test",
        ]
        combined = f"{tool_name} {arguments}".lower()
        return any(pattern in combined for pattern in test_patterns)

    # ------------------------------------------------------------------
    # Security findings
    # ------------------------------------------------------------------

    def _parse_security_findings(self, session_log: AgentSessionLog) -> list[str]:
        """Scan the session log for security-related findings.

        Examines tool call results for security-related output from linters,
        dependency auditors, or the agent's own analysis.

        Args:
            session_log: The session log containing tool call events.

        Returns:
            A list of security finding strings. Empty if none detected.
        """
        findings: list[str] = []
        security_patterns = re.compile(
            r"(security\s+(?:warning|issue|concern|vulnerability|finding)|"
            r"CVE-\d{4}-\d+|"
            r"npm\s+audit|"
            r"bandit|"
            r"safety\s+check|"
            r"critical\s+vulnerability|"
            r"high\s+severity)",
            re.IGNORECASE,
        )

        for tool_call in session_log.tool_calls:
            result_text = str(tool_call.get("result", "") or "")
            if security_patterns.search(result_text):
                # Truncate the finding to a reasonable length.
                snippet = result_text[:500]
                findings.append(snippet)

        # Also scan assistant messages for security mentions.
        for message in session_log.messages:
            content = str(message.get("content", "") or "")
            if security_patterns.search(content):
                snippet = content[:500]
                findings.append(snippet)

        if findings:
            logger.info(
                "Found %d security finding(s) for run %s",
                len(findings),
                self._run_id,
                extra={"run_id": self._run_id},
            )

        return findings

    # ------------------------------------------------------------------
    # Command execution helpers
    # ------------------------------------------------------------------

    async def _run_git(self, *args: str) -> str:
        """Run a git command asynchronously in the repository directory.

        Args:
            *args: Git sub-command and arguments (e.g., ``"checkout"``,
                ``"main"``).

        Returns:
            The stdout output of the command.

        Raises:
            AgentExecutionError: On non-zero exit code, with
                ``error_code="GIT_PRE_PROCESS_ERROR"``.
        """
        return await self._run_cmd_async("git", *args)

    async def _run_cmd_async(self, *args: str) -> str:
        """Run a command asynchronously and return stdout.

        Args:
            *args: The command and its arguments.

        Returns:
            The stdout output of the command as a string.

        Raises:
            AgentExecutionError: On non-zero exit code, with the command's
                stderr included in ``error_details``.
        """
        cmd_str = " ".join(args)
        logger.debug(
            "Running command for run %s: %s",
            self._run_id,
            cmd_str,
            extra={"run_id": self._run_id},
        )

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_path),
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if process.returncode != 0:
            error_msg = f"Command failed with exit code {process.returncode}: {cmd_str}"
            logger.error(
                "%s\nstderr: %s",
                error_msg,
                stderr,
                extra={"run_id": self._run_id},
            )
            # Determine error code based on the command.
            error_code = "GIT_PRE_PROCESS_ERROR"
            if args[0] == "gh":
                error_code = "PR_CREATE_ERROR"
            elif len(args) >= 2 and args[1] == "push":
                error_code = "GIT_PUSH_ERROR"

            raise AgentExecutionError(
                error_message=error_msg,
                error_code=error_code,
                error_details={
                    "command": cmd_str,
                    "exit_code": process.returncode,
                    "stderr": stderr.strip(),
                    "stdout": stdout.strip(),
                },
            )

        return stdout

    def _run_cmd_sync(self, *args: str) -> str:
        """Run a command synchronously and return stdout.

        Used for non-async contexts (e.g., ``_collect_changes`` which is
        called during post-processing and does not require true async).

        Args:
            *args: The command and its arguments.

        Returns:
            The stdout output of the command as a string.

        Raises:
            AgentExecutionError: On non-zero exit code.
        """
        import subprocess

        cmd_str = " ".join(args)
        logger.debug(
            "Running sync command for run %s: %s",
            self._run_id,
            cmd_str,
            extra={"run_id": self._run_id},
        )

        result = subprocess.run(
            args,
            cwd=str(self._repo_path),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            error_msg = f"Command failed with exit code {result.returncode}: {cmd_str}"
            logger.error(
                "%s\nstderr: %s",
                error_msg,
                result.stderr,
                extra={"run_id": self._run_id},
            )
            raise AgentExecutionError(
                error_message=error_msg,
                error_code="GIT_PRE_PROCESS_ERROR",
                error_details={
                    "command": cmd_str,
                    "exit_code": result.returncode,
                    "stderr": result.stderr.strip(),
                    "stdout": result.stdout.strip(),
                },
            )

        return result.stdout

    # ------------------------------------------------------------------
    # System message / skill path helpers
    # ------------------------------------------------------------------

    def build_system_message(self) -> str:
        """Build the fully-rendered system message for the implement role.

        Uses :class:`~app.src.agent.prompts.PromptLoader` to load the
        implement template and substitute placeholders with
        ``system_instructions`` and repo-level instructions.

        Returns:
            The rendered system message string.
        """
        loader = PromptLoader()
        return loader.build_system_message(
            role="implement",
            system_instructions=self._system_instructions,
            repo_path=self._repo_path,
        )

    def resolve_skill_directories(self) -> list[str]:
        """Resolve skill paths to absolute filesystem paths.

        Uses :meth:`PromptLoader.resolve_skill_paths` to resolve relative
        paths against the repo checkout directory.

        Returns:
            List of absolute path strings for valid skill files.
        """
        return PromptLoader.resolve_skill_paths(self._skill_paths, self._repo_path)
