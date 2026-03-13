"""Merge role logic — merge attempt, conflict resolution, merge execution.

Orchestrates the complete merge role workflow:

1. **Pre-processing** — fetch PR metadata, fetch both branches, attempt a
   local ``git merge`` to detect conflicts.
2. **Execution** — on a clean merge, instruct the agent to run tests and
   validate; on a conflicted merge, instruct the agent to resolve conflicts,
   apply resolutions, and run the test suite.
3. **Post-processing** — determine the merge outcome, execute the merge via
   the GitHub API when safe, or compile a conflict report for human review.

The merge role supports four distinct outcome paths:

- **Clean merge + tests pass** → merge executed via GitHub API.
- **Clean merge + tests fail** → merge NOT executed; test failures reported.
- **Conflicts + agent resolved + tests pass** → resolution pushed, merge executed.
- **Conflicts + agent could not resolve (or tests fail)** → conflict report produced.

Example::

    role = MergeRole(
        run_id="abc-123",
        repo_path=Path("/workspace/target-repo"),
        branch="main",
        pr_number=42,
        agent_instructions="Merge this PR.",
        model="claude-sonnet-4-20250514",
        repository="owner/repo",
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
import subprocess
from pathlib import Path
from typing import Any

from app.src.agent.prompts import PromptLoader
from app.src.agent.runner import AgentExecutionError, AgentRunner, AgentSessionLog

logger = logging.getLogger("dispatch.agent.roles.merge")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum characters per conflicted file context in agent instructions.
_MAX_FILE_CONTEXT_SIZE = 5_000

# Number of recent git log entries to fetch per conflicted file.
_CONFLICT_LOG_DEPTH = 3

# Git identity for merge commits.
_BOT_NAME = "Copilot Dispatch Bot"
_BOT_EMAIL = "copilot-dispatch@noreply.github.com"


class MergeRole:
    """Implements the full merge role lifecycle.

    Attempts to merge a pull request by first performing a local
    ``git merge`` to detect conflicts. On a clean merge, the test suite
    is executed via the Copilot SDK agent and, if tests pass, the merge
    is executed via the GitHub API. On a conflicted merge, the agent is
    invoked to analyse and resolve conflicts; if resolution succeeds and
    tests pass the resolution is pushed and merged; otherwise a structured
    conflict report is produced for human review.

    Attributes:
        run_id: Unique identifier for the agent run.
        repo_path: Absolute path to the checked-out target repository.
        branch: Target (base) branch for the merge.
        pr_number: The pull request number to merge.
        agent_instructions: Natural-language instructions for the agent.
        model: LLM model identifier.
        system_instructions: Optional caller-supplied system instructions.
        skill_paths: Optional list of relative skill file paths.
        timeout_minutes: Maximum session duration in minutes.
        repository: Target repository in ``owner/repo`` format.
    """

    def __init__(
        self,
        run_id: str,
        repo_path: Path,
        branch: str,
        pr_number: int,
        agent_instructions: str,
        model: str,
        system_instructions: str | None = None,
        skill_paths: list[str] | None = None,
        agent_paths: list[str] | None = None,
        timeout_minutes: int = 30,
        repository: str = "",
    ) -> None:
        """Initialise the MergeRole.

        Args:
            run_id: Unique identifier for the agent run.
            repo_path: Absolute path to the checked-out target repository.
            branch: Target (base) branch for the merge.
            pr_number: The pull request number to merge.
            agent_instructions: Natural-language instructions for the agent.
            model: LLM model identifier (e.g., ``"claude-sonnet-4-20250514"``).
            system_instructions: Optional caller-supplied system-level
                instructions to merge into the prompt.  Also scanned for
                merge method directives (``"squash"``, ``"rebase"``).
            skill_paths: Optional list of skill file paths relative to the
                repo root, resolved to absolute paths for the SDK.
            agent_paths: Optional list of custom agent definition file paths
                relative to the repo root.
            timeout_minutes: Maximum session duration in minutes before
                forced termination. Defaults to ``30``.
            repository: Target repository in ``owner/repo`` format, used for
                GitHub API calls. Required for merge execution.
        """
        self._run_id = run_id
        self._repo_path = repo_path
        self._branch = branch
        self._pr_number = pr_number
        self._agent_instructions = agent_instructions
        self._model = model
        self._system_instructions = system_instructions
        self._skill_paths = skill_paths
        self._agent_paths = agent_paths
        self._timeout_minutes = timeout_minutes
        self._repository = repository

        # Merge method from system_instructions (default: "merge").
        self._merge_method = self._parse_merge_method()

        # Populated during pre_process.
        self._head_ref: str = ""
        self._base_ref: str = ""
        self._merge_attempt: dict[str, Any] = {"clean": True, "conflict_files": []}

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    async def pre_process(self) -> None:
        """Prepare the repository and attempt a local merge.

        Fetches PR metadata to discover branch names, fetches both
        branches, checks out the target (base) branch, configures git
        identity, and attempts a local ``git merge`` to detect conflicts.

        Raises:
            AgentExecutionError: If any git or ``gh`` command fails, with
                ``error_code="MERGE_PRE_PROCESS_ERROR"`` and the command's
                stderr in ``error_details``.
        """
        logger.info(
            "Starting merge pre-processing for run %s: PR #%d",
            self._run_id,
            self._pr_number,
            extra={"run_id": self._run_id, "pr_number": self._pr_number},
        )

        # 1. Fetch PR metadata.
        pr_metadata = await self._fetch_pr_metadata()
        self._head_ref = pr_metadata.get("headRefName", "")
        self._base_ref = pr_metadata.get("baseRefName", self._branch)

        if not self._head_ref:
            raise AgentExecutionError(
                error_message=(
                    f"PR #{self._pr_number} metadata missing headRefName — "
                    "cannot determine PR branch."
                ),
                error_code="MERGE_PRE_PROCESS_ERROR",
                error_details={"pr_metadata": pr_metadata},
            )

        # 2. Configure git identity.
        await self._run_git("config", "user.name", _BOT_NAME)
        await self._run_git("config", "user.email", _BOT_EMAIL)

        # 3. Fetch both branches.
        await self._run_git("fetch", "origin", self._head_ref)
        await self._run_git("fetch", "origin", self._base_ref)

        # 4. Checkout the target (base) branch and pull latest.
        await self._run_git("checkout", self._base_ref)
        await self._run_git("pull", "origin", self._base_ref)

        # 5. Attempt local merge.
        self._merge_attempt = self._attempt_local_merge()

        logger.info(
            "Merge pre-processing complete for run %s — PR #%d, "
            "head=%s, base=%s, clean=%s, conflict_files=%d",
            self._run_id,
            self._pr_number,
            self._head_ref,
            self._base_ref,
            self._merge_attempt["clean"],
            len(self._merge_attempt["conflict_files"]),
            extra={
                "run_id": self._run_id,
                "pr_number": self._pr_number,
                "clean": self._merge_attempt["clean"],
            },
        )

    async def execute(self, runner: AgentRunner) -> AgentSessionLog:
        """Run the Copilot SDK agent for the merge role.

        Behaviour depends on the merge attempt result from
        :meth:`pre_process`:

        - **Clean merge**: agent is instructed to run the test suite on the
          merged result and report whether tests pass, without modifying
          any files.
        - **Conflict merge**: the merge is re-applied (leaving conflict
          markers in place), and the agent is instructed to resolve
          conflicts, run the test suite, and report results.

        Args:
            runner: A configured :class:`AgentRunner` instance.

        Returns:
            The :class:`AgentSessionLog` produced by the agent session.
        """
        logger.info(
            "Executing merge role agent for run %s, PR #%d (clean=%s)",
            self._run_id,
            self._pr_number,
            self._merge_attempt["clean"],
            extra={
                "run_id": self._run_id,
                "pr_number": self._pr_number,
                "clean": self._merge_attempt["clean"],
            },
        )

        if self._merge_attempt["clean"]:
            enriched = self._build_clean_merge_instructions()
        else:
            # Re-apply merge to leave conflict markers in place for the agent.
            await self._reapply_merge_with_conflicts()
            enriched = self._build_conflict_merge_instructions()

        return await runner.run(enriched)

    async def post_process(self, session_log: AgentSessionLog) -> dict[str, Any]:
        """Determine merge outcome and execute or report.

        Evaluates the agent session results to determine the merge outcome
        and takes the appropriate action:

        - Clean merge + tests pass → execute merge via GitHub API.
        - Clean merge + tests fail → report test failures, no merge.
        - Conflicts + agent resolved + tests pass → push and merge.
        - Conflicts + agent could not resolve → compile conflict report.

        Args:
            session_log: The session log from the completed agent run.

        Returns:
            A dictionary with all fields needed for
            :class:`~app.src.api.models.MergeResult`:
            ``merge_status``, ``merge_sha``, ``conflict_files``,
            ``conflict_resolutions``, ``test_results``,
            ``session_summary``, ``agent_log_url``.
        """
        logger.info(
            "Starting merge post-processing for run %s, PR #%d",
            self._run_id,
            self._pr_number,
            extra={"run_id": self._run_id, "pr_number": self._pr_number},
        )

        test_results = self._parse_test_results(session_log)
        tests_passed = test_results["failed"] == 0 and test_results["passed"] > 0

        if self._merge_attempt["clean"]:
            result = await self._handle_clean_merge(
                session_log, test_results, tests_passed
            )
        else:
            result = await self._handle_conflict_merge(
                session_log, test_results, tests_passed
            )

        logger.info(
            "Merge post-processing complete for run %s, PR #%d — status=%s",
            self._run_id,
            self._pr_number,
            result["merge_status"],
            extra={
                "run_id": self._run_id,
                "pr_number": self._pr_number,
                "merge_status": result["merge_status"],
            },
        )
        return result

    # ------------------------------------------------------------------
    # Outcome handlers
    # ------------------------------------------------------------------

    async def _handle_clean_merge(
        self,
        session_log: AgentSessionLog,
        test_results: dict[str, int],
        tests_passed: bool,
    ) -> dict[str, Any]:
        """Handle the clean merge outcome path.

        If tests pass, executes the merge via the GitHub API. If tests
        fail, aborts the local merge and reports the failures.

        Args:
            session_log: The session log from the agent run.
            test_results: Parsed test result counts.
            tests_passed: Whether all tests passed.

        Returns:
            A dictionary with :class:`~app.src.api.models.MergeResult` fields.
        """
        if tests_passed:
            # Abort local merge state before API merge.
            self._abort_local_merge()
            merge_sha = await self._execute_github_merge()
            return {
                "merge_status": "merged",
                "merge_sha": merge_sha,
                "conflict_files": [],
                "conflict_resolutions": [],
                "test_results": test_results,
                "session_summary": (
                    session_log.final_message or "Clean merge — tests passed."
                ),
                "agent_log_url": None,
            }

        # Tests failed — abort and report.
        self._abort_local_merge()
        return {
            "merge_status": "conflicts_unresolved",
            "merge_sha": None,
            "conflict_files": [],
            "conflict_resolutions": [],
            "test_results": test_results,
            "session_summary": (
                session_log.final_message
                or "Merge was clean but tests failed. Merge aborted."
            ),
            "agent_log_url": None,
        }

    async def _handle_conflict_merge(
        self,
        session_log: AgentSessionLog,
        test_results: dict[str, int],
        tests_passed: bool,
    ) -> dict[str, Any]:
        """Handle the conflict merge outcome path.

        If the agent resolved conflicts and tests pass, pushes the
        resolution and executes the merge via the GitHub API. Otherwise,
        compiles a conflict report.

        Args:
            session_log: The session log from the agent run.
            test_results: Parsed test result counts.
            tests_passed: Whether all tests passed.

        Returns:
            A dictionary with :class:`~app.src.api.models.MergeResult` fields.
        """
        conflict_files = self._merge_attempt["conflict_files"]

        # Check if conflicts are actually resolved (no remaining markers).
        conflicts_resolved = self._check_conflicts_resolved()

        if conflicts_resolved and tests_passed:
            # Commit resolution, push, and merge.
            await self._commit_conflict_resolution()
            await self._push_resolution()
            merge_sha = await self._execute_github_merge()
            return {
                "merge_status": "conflicts_resolved_and_merged",
                "merge_sha": merge_sha,
                "conflict_files": conflict_files,
                "conflict_resolutions": [],
                "test_results": test_results,
                "session_summary": (
                    session_log.final_message
                    or "Conflicts resolved, tests passed, merge executed."
                ),
                "agent_log_url": None,
            }

        # Either conflicts remain or tests failed — produce report.
        conflict_resolutions = self._compile_conflict_report(
            session_log, conflict_files
        )
        return {
            "merge_status": "conflicts_unresolved",
            "merge_sha": None,
            "conflict_files": conflict_files,
            "conflict_resolutions": conflict_resolutions,
            "test_results": test_results,
            "session_summary": (
                session_log.final_message
                or "Conflicts could not be confidently resolved."
            ),
            "agent_log_url": None,
        }

    # ------------------------------------------------------------------
    # PR metadata
    # ------------------------------------------------------------------

    async def _fetch_pr_metadata(self) -> dict[str, Any]:
        """Fetch PR metadata via ``gh pr view``.

        Retrieves the PR's head and base branch names.

        Returns:
            A dictionary with PR metadata fields.

        Raises:
            AgentExecutionError: If the ``gh`` command fails, with
                ``error_code="MERGE_PRE_PROCESS_ERROR"``.
        """
        output = await self._run_cmd_async(
            "gh",
            "pr",
            "view",
            str(self._pr_number),
            "--json",
            "headRefName,baseRefName",
        )
        try:
            metadata: dict[str, Any] = json.loads(output.strip())
        except json.JSONDecodeError as exc:
            raise AgentExecutionError(
                error_message=(
                    f"Failed to parse PR #{self._pr_number} metadata JSON: {exc}"
                ),
                error_code="MERGE_PRE_PROCESS_ERROR",
                error_details={
                    "raw_output": output[:500],
                    "exception_type": type(exc).__name__,
                },
            ) from exc

        logger.info(
            "Fetched PR #%d metadata: head=%s, base=%s",
            self._pr_number,
            metadata.get("headRefName", ""),
            metadata.get("baseRefName", ""),
            extra={"run_id": self._run_id, "pr_number": self._pr_number},
        )
        return metadata

    # ------------------------------------------------------------------
    # Local merge attempt
    # ------------------------------------------------------------------

    def _attempt_local_merge(self) -> dict[str, Any]:
        """Attempt a local merge of the PR branch into the base branch.

        Runs ``git merge origin/{head_ref} --no-commit --no-ff`` to detect
        conflicts without committing.  If conflicts are detected, the
        conflicted files are collected and the merge state is aborted so
        the agent can work with the repository in a clean state.

        Returns:
            A dictionary with:
            - ``clean`` (``bool``): ``True`` if no conflicts.
            - ``conflict_files`` (``list[str]``): Paths of conflicted files.
        """
        cmd_args = [
            "git",
            "merge",
            f"origin/{self._head_ref}",
            "--no-commit",
            "--no-ff",
        ]
        try:
            self._run_cmd_sync(*cmd_args)
            logger.info(
                "Local merge clean for run %s: origin/%s into %s",
                self._run_id,
                self._head_ref,
                self._base_ref,
                extra={"run_id": self._run_id},
            )
            return {"clean": True, "conflict_files": []}
        except AgentExecutionError:
            # Merge failed — likely conflicts.
            conflict_files = self._get_conflict_files()
            if conflict_files:
                logger.info(
                    "Merge conflicts detected for run %s: %d file(s) — %s",
                    self._run_id,
                    len(conflict_files),
                    ", ".join(conflict_files),
                    extra={"run_id": self._run_id},
                )
                # Abort the merge so the agent starts from a clean state.
                self._abort_local_merge()
                return {"clean": False, "conflict_files": conflict_files}

            # Non-conflict merge failure — re-raise.
            logger.error(
                "Local merge failed for run %s but no conflict files detected",
                self._run_id,
                extra={"run_id": self._run_id},
            )
            raise

    def _get_conflict_files(self) -> list[str]:
        """Get the list of files with unresolved merge conflicts.

        Runs ``git diff --name-only --diff-filter=U`` to identify files
        with conflict markers.

        Returns:
            A list of relative file paths with unresolved conflicts.
        """
        try:
            output = self._run_cmd_sync("git", "diff", "--name-only", "--diff-filter=U")
            files = [f.strip() for f in output.strip().split("\n") if f.strip()]
            return files
        except AgentExecutionError:
            logger.warning(
                "Failed to list conflict files for run %s",
                self._run_id,
                extra={"run_id": self._run_id},
            )
            return []

    def _abort_local_merge(self) -> None:
        """Abort the current local merge state.

        Runs ``git merge --abort`` to restore the repository to its
        pre-merge state. Failures are logged but not raised — aborting
        is best-effort cleanup.
        """
        try:
            self._run_cmd_sync("git", "merge", "--abort")
            logger.debug(
                "Local merge aborted for run %s",
                self._run_id,
                extra={"run_id": self._run_id},
            )
        except AgentExecutionError:
            logger.warning(
                "Failed to abort local merge for run %s — may already be clean",
                self._run_id,
                extra={"run_id": self._run_id},
            )

    def _check_conflicts_resolved(self) -> bool:
        """Check whether conflict markers remain in tracked files.

        Searches for ``<<<<<<<``, ``=======``, and ``>>>>>>>`` markers in
        all tracked files within the repository.

        Returns:
            ``True`` if no conflict markers are found; ``False`` otherwise.
        """
        try:
            result = subprocess.run(
                ["git", "diff", "--check"],
                cwd=str(self._repo_path),
                capture_output=True,
                text=True,
            )
            # git diff --check exits 0 if no issues, non-zero if markers found.
            has_markers = result.returncode != 0
            if has_markers:
                logger.info(
                    "Conflict markers still present for run %s",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
                return False
            return True
        except Exception as exc:
            logger.warning(
                "Failed to check conflict markers for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )
            # Default to assuming conflicts remain.
            return False

    # ------------------------------------------------------------------
    # Conflict context building
    # ------------------------------------------------------------------

    def _build_conflict_context(self, conflict_files: list[str]) -> str:
        """Build context about merge conflicts for the agent.

        For each conflicted file, includes the file contents with conflict
        markers and recent commit history from both branches to help the
        agent understand the intent of each side.

        Args:
            conflict_files: List of file paths with merge conflicts.

        Returns:
            A formatted context string describing all conflicts.
        """
        sections: list[str] = [
            "## Merge Conflicts",
            "",
            f"The following {len(conflict_files)} file(s) have merge conflicts "
            f"that need to be resolved:",
            "",
        ]

        for file_path in conflict_files:
            sections.append(f"### {file_path}")
            sections.append("")

            # Read file contents (with conflict markers).
            full_path = self._repo_path / file_path
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
                if len(content) > _MAX_FILE_CONTEXT_SIZE:
                    content = (
                        content[:_MAX_FILE_CONTEXT_SIZE]
                        + f"\n... [truncated — {len(content)} total chars]"
                    )
                sections.append("**File contents with conflict markers:**")
                sections.append("```")
                sections.append(content)
                sections.append("```")
            except OSError as exc:
                sections.append(f"*Could not read file: {exc}*")

            # Recent changes from each branch.
            sections.append("")
            sections.append(f"**Recent changes on base ({self._base_ref}):**")
            base_log = self._get_recent_log(self._base_ref, file_path)
            sections.append(base_log or "*No recent changes*")

            sections.append("")
            sections.append(
                f"**Recent changes on PR branch (origin/{self._head_ref}):**"
            )
            head_log = self._get_recent_log(f"origin/{self._head_ref}", file_path)
            sections.append(head_log or "*No recent changes*")

            sections.append("")

        return "\n".join(sections)

    def _get_recent_log(self, ref: str, file_path: str) -> str:
        """Get recent git log entries for a file on a given ref.

        Args:
            ref: The git ref to inspect (branch or remote ref).
            file_path: Path to the file within the repository.

        Returns:
            Formatted string of recent commits, or empty string on failure.
        """
        try:
            output = self._run_cmd_sync(
                "git",
                "log",
                f"--oneline",
                f"-{_CONFLICT_LOG_DEPTH}",
                ref,
                "--",
                file_path,
            )
            return output.strip() if output.strip() else ""
        except AgentExecutionError:
            return ""

    # ------------------------------------------------------------------
    # Instruction building
    # ------------------------------------------------------------------

    def _build_clean_merge_instructions(self) -> str:
        """Build agent instructions for the clean merge path.

        Instructs the agent to run the test suite on the merged result
        and report whether tests pass. The agent is explicitly told not
        to modify any files.

        Returns:
            The enriched instruction string for the agent.
        """
        sections: list[str] = [
            "## Merge Context",
            "",
            f"**PR #{self._pr_number}**: merging `{self._head_ref}` into "
            f"`{self._base_ref}`",
            f"**Merge method**: {self._merge_method}",
            f"**Status**: The merge is clean (no conflicts).",
            "",
            "## Instructions",
            "",
            "The merge of the PR branch into the target branch produced no "
            "conflicts. Your task is to validate the merged result:",
            "",
            "1. Run the full test suite against the merged codebase.",
            "2. Report the test results (pass/fail/skip counts).",
            "3. **Do NOT modify any files.** The merge is already applied.",
            "4. If tests fail, report the failing tests and error messages.",
            "",
        ]

        if self._agent_instructions.strip():
            sections.extend(
                [
                    "## Additional Context",
                    "",
                    self._agent_instructions,
                    "",
                ]
            )

        return "\n".join(sections)

    def _build_conflict_merge_instructions(self) -> str:
        """Build agent instructions for the conflict merge path.

        Instructs the agent to resolve conflicts by editing the conflicted
        files, removing conflict markers, and running the test suite.

        Returns:
            The enriched instruction string for the agent.
        """
        conflict_context = self._build_conflict_context(
            self._merge_attempt["conflict_files"]
        )

        sections: list[str] = [
            "## Merge Context",
            "",
            f"**PR #{self._pr_number}**: merging `{self._head_ref}` into "
            f"`{self._base_ref}`",
            f"**Merge method**: {self._merge_method}",
            f"**Status**: Merge conflicts detected in "
            f"{len(self._merge_attempt['conflict_files'])} file(s).",
            "",
            conflict_context,
            "",
            "## Instructions",
            "",
            "Resolve the merge conflicts following these steps:",
            "",
            "1. Read and understand the conflict markers in each affected file.",
            "2. For each conflict, understand the intent of both the base branch "
            '("ours") and the PR branch ("theirs") changes.',
            "3. Edit each conflicted file to resolve the conflict — remove all "
            "conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) and produce "
            "the correct resolved code.",
            "4. Run the full test suite to validate the resolution.",
            "5. If tests pass, your resolution is validated.",
            "6. **If you are NOT confident in a resolution, do NOT force it.** "
            "Leave a comment in your session output describing the conflict "
            "and your suggested resolution so a human can review.",
            "",
        ]

        if self._agent_instructions.strip():
            sections.extend(
                [
                    "## Additional Context",
                    "",
                    self._agent_instructions,
                    "",
                ]
            )

        return "\n".join(sections)

    async def _reapply_merge_with_conflicts(self) -> None:
        """Re-apply the merge to leave conflict markers in place.

        After the initial merge attempt (which was aborted to detect
        conflicts), this re-applies the merge so the agent can see and
        edit the conflict markers in the actual files.
        """
        try:
            # The merge was previously aborted. Re-run it to produce markers.
            process = await asyncio.create_subprocess_exec(
                "git",
                "merge",
                f"origin/{self._head_ref}",
                "--no-commit",
                "--no-ff",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._repo_path),
            )
            await process.communicate()
            # We expect this to fail with conflicts — that's intentional.
            logger.debug(
                "Re-applied merge with conflicts for run %s (exit=%s)",
                self._run_id,
                process.returncode,
                extra={"run_id": self._run_id},
            )
        except Exception as exc:
            logger.warning(
                "Failed to re-apply merge for run %s: %s",
                self._run_id,
                exc,
                extra={"run_id": self._run_id},
            )

    # ------------------------------------------------------------------
    # Merge execution
    # ------------------------------------------------------------------

    async def _execute_github_merge(self) -> str:
        """Execute the PR merge via the GitHub API.

        Uses ``gh pr merge`` with the configured merge method
        (``merge``, ``squash``, or ``rebase``).

        Returns:
            The merge commit SHA.

        Raises:
            AgentExecutionError: On merge failure, with
                ``error_code="MERGE_EXECUTION_ERROR"``.
        """
        try:
            output = await self._run_cmd_async(
                "gh",
                "pr",
                "merge",
                str(self._pr_number),
                f"--{self._merge_method}",
            )
            logger.info(
                "PR #%d merged via GitHub API for run %s (method=%s)",
                self._pr_number,
                self._run_id,
                self._merge_method,
                extra={
                    "run_id": self._run_id,
                    "pr_number": self._pr_number,
                    "merge_method": self._merge_method,
                },
            )
        except AgentExecutionError:
            raise
        except Exception as exc:
            raise AgentExecutionError(
                error_message=f"Failed to merge PR #{self._pr_number}: {exc}",
                error_code="MERGE_EXECUTION_ERROR",
                error_details={
                    "pr_number": self._pr_number,
                    "merge_method": self._merge_method,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc

        # Retrieve the merge commit SHA.
        merge_sha = await self._get_merge_sha()
        return merge_sha

    async def _get_merge_sha(self) -> str:
        """Retrieve the merge commit SHA after a successful merge.

        Queries the GitHub API for the PR's merge commit SHA.

        Returns:
            The merge commit SHA string, or an empty string on failure.
        """
        try:
            output = await self._run_cmd_async(
                "gh",
                "pr",
                "view",
                str(self._pr_number),
                "--json",
                "mergeCommit",
            )
            data = json.loads(output.strip())
            merge_commit = data.get("mergeCommit", {}) or {}
            sha = merge_commit.get("oid", "")
            if sha:
                logger.debug(
                    "Merge SHA for PR #%d: %s",
                    self._pr_number,
                    sha,
                    extra={"run_id": self._run_id},
                )
            return sha
        except Exception as exc:
            logger.warning(
                "Failed to retrieve merge SHA for PR #%d: %s",
                self._pr_number,
                exc,
                extra={"run_id": self._run_id},
            )
            return ""

    # ------------------------------------------------------------------
    # Conflict resolution push
    # ------------------------------------------------------------------

    async def _commit_conflict_resolution(self) -> None:
        """Commit the agent's conflict resolution.

        Stages all changes and commits with a descriptive message.

        Raises:
            AgentExecutionError: If no changes exist to commit, or if the
                commit fails.
        """
        status_output = await self._run_cmd_async("git", "status", "--porcelain")
        if not status_output.strip():
            raise AgentExecutionError(
                error_message=(
                    "Conflict resolution produced no file changes. "
                    "The agent may have discussed the conflicts without "
                    "editing the files."
                ),
                error_code="NO_CHANGES_TO_COMMIT",
                error_details={
                    "run_id": self._run_id,
                    "pr_number": self._pr_number,
                    "conflict_files": self._merge_attempt.get("conflict_files", []),
                },
            )

        await self._run_git("add", "--all")
        await self._run_git(
            "commit",
            "-m",
            f"Resolve merge conflicts for PR #{self._pr_number} (run: {self._run_id})",
        )
        logger.info(
            "Committed conflict resolution for run %s",
            self._run_id,
            extra={"run_id": self._run_id},
        )

    async def _push_resolution(self) -> None:
        """Push the conflict resolution to the PR branch.

        Pushes to the head ref so the resolution is included in the PR.

        Raises:
            AgentExecutionError: On push failure, with
                ``error_code="GIT_PUSH_ERROR"``.
        """
        try:
            await self._run_git("push", "origin", f"HEAD:{self._head_ref}")
            logger.info(
                "Pushed conflict resolution to %s for run %s",
                self._head_ref,
                self._run_id,
                extra={"run_id": self._run_id},
            )
        except AgentExecutionError:
            raise
        except Exception as exc:
            raise AgentExecutionError(
                error_message=(
                    f"Failed to push conflict resolution to {self._head_ref}: {exc}"
                ),
                error_code="GIT_PUSH_ERROR",
                error_details={
                    "head_ref": self._head_ref,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc

    # ------------------------------------------------------------------
    # Merge method parsing
    # ------------------------------------------------------------------

    def _parse_merge_method(self) -> str:
        """Determine the merge method from system instructions.

        Scans ``system_instructions`` for merge method keywords. If
        ``"squash"`` is found, returns ``"squash"``. If ``"rebase"`` is
        found, returns ``"rebase"``. Defaults to ``"merge"``.

        Returns:
            One of ``"merge"``, ``"squash"``, or ``"rebase"``.
        """
        if not self._system_instructions:
            return "merge"

        lower = self._system_instructions.lower()
        if "squash" in lower:
            logger.info(
                "Merge method 'squash' detected from system_instructions for run %s",
                self._run_id,
                extra={"run_id": self._run_id},
            )
            return "squash"
        if "rebase" in lower:
            logger.info(
                "Merge method 'rebase' detected from system_instructions for run %s",
                self._run_id,
                extra={"run_id": self._run_id},
            )
            return "rebase"

        return "merge"

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
                # --- pytest format ---
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
            # Fallback: scan assistant messages for test result summaries.
            # The agent often reports test results in its messages even when
            # tool output is unavailable or cannot be matched.
            msg_results = self._parse_test_results_from_messages(session_log)
            if msg_results["passed"] > 0 or msg_results["failed"] > 0:
                passed = msg_results["passed"]
                failed = msg_results["failed"]
                skipped = msg_results["skipped"]
                found_test_output = True
                logger.info(
                    "Test results recovered from assistant messages for run %s: "
                    "passed=%d, failed=%d, skipped=%d",
                    self._run_id,
                    passed,
                    failed,
                    skipped,
                    extra={"run_id": self._run_id},
                )

        if not found_test_output:
            logger.warning(
                "No test results detected in session log for run %s",
                self._run_id,
                extra={"run_id": self._run_id},
            )

        return {"passed": passed, "failed": failed, "skipped": skipped}

    def _parse_test_results_from_messages(
        self,
        session_log: AgentSessionLog,
    ) -> dict[str, int]:
        """Extract test result counts from assistant messages.

        Provides a fallback when tool call results do not contain parseable
        test runner output. Scans all assistant messages for common test
        result summary patterns (pytest, jest, go test).

        Args:
            session_log: The session log containing assistant messages.

        Returns:
            A dictionary with ``passed``, ``failed``, and ``skipped`` counts.
            Defaults to all zeros if no results are found in messages.
        """
        passed = 0
        failed = 0
        skipped = 0

        all_text = "\n".join(
            str(m.get("content", "") or "") for m in session_log.messages
        )

        if not all_text.strip():
            return {"passed": passed, "failed": failed, "skipped": skipped}

        # Accumulate counts from all pytest-style matches (e.g. "26 passed").
        for match in re.finditer(r"(\d+)\s+passed", all_text, re.IGNORECASE):
            passed += int(match.group(1))

        for match in re.finditer(r"(\d+)\s+failed", all_text, re.IGNORECASE):
            failed += int(match.group(1))

        for match in re.finditer(r"(\d+)\s+skipped", all_text, re.IGNORECASE):
            skipped += int(match.group(1))

        # jest / vitest format: "Tests: 17 passed"
        for match in re.finditer(
            r"Tests:\s+.*?(\d+)\s+passed", all_text, re.IGNORECASE
        ):
            passed += int(match.group(1))

        for match in re.finditer(
            r"Tests:\s+.*?(\d+)\s+failed", all_text, re.IGNORECASE
        ):
            failed += int(match.group(1))

        # go test format
        go_pass_count = len(re.findall(r"---\s+PASS:", all_text))
        go_fail_count = len(re.findall(r"---\s+FAIL:", all_text))
        passed += go_pass_count
        failed += go_fail_count

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
    # Conflict report compilation
    # ------------------------------------------------------------------

    def _compile_conflict_report(
        self,
        session_log: AgentSessionLog,
        conflict_files: list[str],
    ) -> list[dict[str, str]]:
        """Compile a conflict report from the agent's session output.

        Scans the agent's messages for descriptions of conflicts and
        proposed resolutions for each conflicted file. Returns a list of
        :class:`~app.src.api.models.ConflictResolution`-compatible dicts.

        Args:
            session_log: The session log with the agent's analysis.
            conflict_files: List of files with merge conflicts.

        Returns:
            A list of dicts with ``file_path``, ``description``, and
            ``suggested_resolution`` keys.
        """
        resolutions: list[dict[str, str]] = []

        # Combine all agent messages.
        all_text = "\n".join(
            str(m.get("content", "") or "") for m in session_log.messages
        )

        for file_path in conflict_files:
            # Try to find the agent's discussion about this file.
            description = self._extract_file_discussion(all_text, file_path)
            suggestion = self._extract_file_suggestion(all_text, file_path)

            resolutions.append(
                {
                    "file_path": file_path,
                    "description": description or f"Conflict in {file_path}",
                    "suggested_resolution": suggestion or "No resolution suggested.",
                }
            )

        return resolutions

    def _extract_file_discussion(self, text: str, file_path: str) -> str:
        """Extract the agent's discussion about a specific conflicted file.

        Looks for text mentioning the file path followed by a description
        of the conflict.

        Args:
            text: Combined agent message text.
            file_path: The file to search for.

        Returns:
            The extracted description, or empty string if not found.
        """
        # Escape the file path for regex.
        escaped = re.escape(file_path)
        # Look for the filename followed by descriptive text (up to 500 chars).
        pattern = re.compile(
            rf"(?:^|\n)\s*(?:\*\*|`)?{escaped}(?:\*\*|`)?\s*[:—\-]\s*(.{{1,500}}?)(?:\n\n|\n(?=\*\*|`|###|##)|$)",
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(text)
        return match.group(1).strip() if match else ""

    def _extract_file_suggestion(self, text: str, file_path: str) -> str:
        """Extract the agent's suggested resolution for a conflicted file.

        Looks for code blocks or resolution descriptions near mentions of
        the file.

        Args:
            text: Combined agent message text.
            file_path: The file to search for.

        Returns:
            The extracted suggestion, or empty string if not found.
        """
        escaped = re.escape(file_path)
        # Look for the filename followed by a code block.
        pattern = re.compile(
            rf"{escaped}.*?```[\w]*\n(.*?)```",
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(text)
        if match:
            return match.group(1).strip()[:2000]

        # Fallback: look for "resolution" or "resolve" near the filename.
        resolution_pattern = re.compile(
            rf"{escaped}.*?(?:resolution|resolve|fix|solution)\s*[:—\-]?\s*(.{{1,500}}?)(?:\n\n|$)",
            re.IGNORECASE | re.DOTALL,
        )
        match = resolution_pattern.search(text)
        return match.group(1).strip() if match else ""

    # ------------------------------------------------------------------
    # System message / skill path helpers
    # ------------------------------------------------------------------

    def build_system_message(self) -> str:
        """Build the fully-rendered system message for the merge role.

        Uses :class:`~app.src.agent.prompts.PromptLoader` to load the
        merge template and substitute placeholders with
        ``system_instructions`` and repo-level instructions.

        Returns:
            The rendered system message string.
        """
        loader = PromptLoader()
        return loader.build_system_message(
            role="merge",
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
                ``error_code="MERGE_PRE_PROCESS_ERROR"``.
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
            error_code = "MERGE_PRE_PROCESS_ERROR"
            if args[0] == "gh" and "merge" in args:
                error_code = "MERGE_EXECUTION_ERROR"
            elif args[0] == "gh":
                error_code = "MERGE_PRE_PROCESS_ERROR"
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

        Used for non-async contexts (e.g., ``_attempt_local_merge``,
        ``_get_conflict_files``, ``_abort_local_merge``).

        Args:
            *args: The command and its arguments.

        Returns:
            The stdout output of the command as a string.

        Raises:
            AgentExecutionError: On non-zero exit code.
        """
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
                error_code="MERGE_PRE_PROCESS_ERROR",
                error_details={
                    "command": cmd_str,
                    "exit_code": result.returncode,
                    "stderr": result.stderr.strip(),
                    "stdout": result.stdout.strip(),
                },
            )

        return result.stdout
